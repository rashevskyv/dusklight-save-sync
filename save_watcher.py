#!/usr/bin/env python3
"""Bidirectional, durable synchronization of stable Dusklight GCI saves (Steam Deck and Windows)."""
import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import logging.handlers
import os
from pathlib import Path, PurePosixPath
import signal
import socket
import sys
import time
import urllib.parse

from platform_support import DEFAULT_PROCESSES, WINDOWS, file_lock, lock_busy, process_running, toasts_blocked
from save_sync import (GC_EPOCH, GitHub, NetworkError, SyncError, backup, fingerprint, game_codes, read_json,
                       saved_at, validate, write_json)

LOG = logging.getLogger('save-watcher')
CODE = Path(__file__).resolve().parent
META = 'Save-Sync: '
TICK_NS = 100  # NTFS keeps file times in 100 ns units, ext4 in 1 ns: compare at the coarser one.
WAKE_GAP = 60  # A longer pause between 2-second ticks means sleep/hibernation or a frozen session.


def home_dir(value=None):
    """Runtime folder (config, state, backups). Default: next to the code, as on the Steam Deck."""
    return Path(value or os.environ.get('SAVE_SYNC_HOME') or CODE).expanduser().resolve()


def signature(path):
    st = path.stat()
    return (st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass
class Save:
    data: bytes
    stamp: tuple

    @property
    def modified_ns(self):
        return self.stamp[2]


def read_stable(path):
    if path.is_symlink():
        raise SyncError(f'Сейв є символічним посиланням: {path.name}')
    before = signature(path)
    if before[1] > 16 * 1024 * 1024:
        raise SyncError('GCI перевищує розмір карти пам’яті.')
    data = path.read_bytes()
    if signature(path) != before or len(data) != before[1]:
        return None
    return Save(data, before)


def data_root(base, config):
    root = Path(config.get('dusklight_user_dir', 'DusklightData')).expanduser()
    return (root if root.is_absolute() else base / root).resolve()


def process_names(config):
    return tuple(config.get('watcher', {}).get('process_names', DEFAULT_PROCESSES))


def game_running(base, include_launcher=True, names=DEFAULT_PROCESSES):
    """The launcher lock counts as running: it is about to start the game."""
    if include_launcher and lock_busy(base / '.launcher.lock'):
        return True
    return process_running(names)


def sync_lock(base, blocking=True):
    """One lock for every local synchronizer: watcher, launcher and --once."""
    return file_lock(base / '.save-sync.lock', blocking)


def newer(a, b):
    """True/False when a is later/earlier than b; None for the same moment at NTFS precision."""
    a, b = a // TICK_NS, b // TICK_NS
    return None if a == b else a > b


class Cloud(GitHub):
    """Cache by repository head; keep save mtime in the same commit as its data."""
    def __init__(self, base, config):
        super().__init__(base, config)
        self.head, self.cached = None, None

    def invalidate(self):
        self.head = None

    def snapshot(self):
        branch = urllib.parse.quote(self.branch, safe='')
        head = self.api('git/ref/heads/' + branch)['object']['sha']
        if head == self.head and self.cached is not None:
            return self.cached
        tree = self.api(f'git/trees/{head}?recursive=1')
        if tree.get('truncated'):
            raise SyncError('GitHub повернув неповний список файлів.')
        codes = game_codes(self.config)
        entries = {}
        for item in tree['tree']:
            path = item['path']
            if item['type'] != 'blob' or not path.startswith(self.prefix + '/') or not path.endswith('.gci'):
                continue
            if item.get('mode') not in ('100644', '100755'):
                raise SyncError('Посилання замість сейву на GitHub.')
            key = path[len(self.prefix)+1:]
            # A malicious path is rejected before any local destination is built.
            parts = PurePosixPath(key).parts
            if len(parts) != 3 or '..' in parts:
                raise SyncError(f'Некоректний шлях сейву на GitHub: {key}')
            old = self.cached.get(key) if self.cached else None
            if old and old['sha'] == item['sha']:
                entries[key] = old
                continue
            blob = self.api('git/blobs/' + item['sha'])
            if blob['encoding'] != 'base64':
                raise SyncError('Невідоме кодування GitHub.')
            data = base64.b64decode(blob['content'])
            validate(key, data, codes=codes)
            commits = self.api('commits?' + urllib.parse.urlencode({'path': path, 'sha': head, 'per_page': 1}))
            stamp, source = None, None
            if commits:
                for line in commits[0]['commit']['message'].splitlines():
                    if line.startswith(META):
                        try:
                            meta = json.loads(line[len(META):])
                            # Metadata is trusted only for the exact bytes it describes.
                            if meta['sha256'] == fingerprint(data):
                                value = int(meta['modified_ns'])
                                if 0 < value < 2**63:
                                    stamp, source = value, 'meta'
                        except (ValueError, KeyError, TypeError):
                            pass
            # Older commits without metadata keep the date inside GCI; it is compared only with another GCI date.
            if stamp is None and saved_at(data) is not None:
                stamp, source = (GC_EPOCH + saved_at(data)) * 1_000_000_000, 'gci'
            entries[key] = {'data': data, 'sha': item['sha'], 'modified_ns': stamp, 'source': source}
        self.head, self.cached = head, entries
        return entries

    def upload(self, key, data, sha, modified_ns):
        if not self.token:
            raise SyncError('Немає входу в GitHub на цьому пристрої (gh auth login). Локальний сейв збережено.')
        meta = {'sha256': fingerprint(data), 'modified_ns': modified_ns}
        title = self.config.get('game_title', 'Twilight Princess')
        device = self.config.get('device_name', 'Windows PC' if WINDOWS else 'Steam Deck')
        payload = {'message': f'Sync {title} save from {device}\n\n{META}' + json.dumps(meta, separators=(',', ':')),
                   'content': base64.b64encode(data).decode('ascii'), 'branch': self.branch}
        if sha:
            payload['sha'] = sha  # GitHub refuses the write if another device changed the file meanwhile.
        self.api('contents/' + urllib.parse.quote(self.prefix + '/' + key, safe='/'), payload)
        self.invalidate()


def choose(local, remote, baseline):
    """push / pull / equal / conflict for one save. baseline = SHA-256 of the last agreed version."""
    lh = fingerprint(local.data) if local else None
    rh = fingerprint(remote['data']) if remote else None
    if lh == rh:
        return 'equal'
    if local is None:
        return 'pull'
    if remote is None:
        # Deletions are never propagated, but do not silently resurrect a remote deletion.
        return 'push' if baseline is None else 'conflict'
    if remote.get('source') == 'meta':
        later = newer(local.modified_ns, remote['modified_ns'])
        if later is not None:
            return 'push' if later else 'pull'
    if baseline is not None:
        if rh == baseline:
            return 'push'
        if lh == baseline:
            return 'pull'
    lt, rt = saved_at(local.data), saved_at(remote['data'])
    if lt is not None and rt is not None and lt != rt:
        return 'push' if lt > rt else 'pull'
    return 'conflict'


def restore(root, key, remote):
    target = root.joinpath(*PurePosixPath(key).parts)
    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
        raise SyncError('Посилання у шляху локального сейву.')
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.watcher-tmp')
    try:
        with temp.open('wb') as stream:
            stream.write(remote['data'])
            stream.flush()
            os.fsync(stream.fileno())
        stamp = remote.get('modified_ns')
        if stamp is not None:
            # The original save time, never the download time.
            os.utime(temp, ns=(stamp, stamp))
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def sync_once(base, config, cloud, local, unsettled=(), allow_pull=True, ignore_launcher=False):
    """Caller holds sync_lock. local contains immutable, quiet-period snapshots."""
    root = data_root(base, config)
    state_file = base / '.save-sync-state.json'
    state = read_json(state_file, read_json(base / '.save-state.json', {}))
    entries = cloud.snapshot()
    result = {'uploaded': [], 'downloaded': [], 'deferred': [], 'conflicts': [], 'unstable': [], 'errors': []}
    identifier = config.get('watcher', {}).get('disc_id', 'GZ2E01')
    codes = game_codes(config)
    for key in sorted(set(local) | set(entries)):
        if key in unsettled:
            result['unstable'].append(key)
            continue
        current, remote = local.get(key), entries.get(key)
        if current and not validate(key, current.data, identifier, codes):
            continue
        if remote and not validate(key, remote['data'], identifier, codes):
            continue
        path = root.joinpath(*PurePosixPath(key).parts)
        # Network lookups can take time: skip any save that changed in the meantime.
        try:
            exists = path.exists()
            if (current and (not exists or signature(path) != current.stamp)) or (not current and exists):
                result['unstable'].append(key)
                continue
            action = choose(current, remote, state.get(key))
            if action == 'equal':
                if current:
                    state[key] = fingerprint(current.data)
            elif action == 'push':
                if remote:
                    backup(base, 'github', key, remote['data'])
                cloud.upload(key, current.data, remote['sha'] if remote else None, current.modified_ns)
                state[key] = fingerprint(current.data)
                result['uploaded'].append(key)
                LOG.info('Сейв відправлено на GitHub: %s', key)
            elif action == 'pull':
                # Dusklight keeps the card in memory and would overwrite the download: only with the game closed.
                if not allow_pull or game_running(base, not ignore_launcher, process_names(config)):
                    result['deferred'].append(key)
                    continue
                if current:
                    backup(base, 'local', key, current.data)
                restore(root, key, remote)
                state[key] = fingerprint(remote['data'])
                result['downloaded'].append(key)
                LOG.info('Новіший сейв із GitHub установлено: %s', key)
            else:
                result['conflicts'].append(key)
                # Keep both versions, once per unique conflict, without backup spam.
                conflicts = read_json(base / '.save-watcher-conflicts.json', {})
                pair = [fingerprint(current.data) if current else None, fingerprint(remote['data']) if remote else None]
                if conflicts.get(key) != pair:
                    mine = backup(base, 'local-conflict', key, current.data if current else None)
                    theirs = backup(base, 'github-conflict', key, remote['data'] if remote else None)
                    conflicts[key] = pair
                    write_json(base / '.save-watcher-conflicts.json', conflicts)
                    LOG.error('Конфлікт сейву %s; обидві копії збережено: %s | %s', key, mine, theirs)
            write_json(state_file, state)
        except (OSError, SyncError) as error:
            cloud.invalidate()
            if isinstance(error, (OSError, NetworkError)):
                raise
            result['errors'].append(f'{key}: {error}')
    write_json(base / '.save-sync-pending.json', {'files': result['unstable'], 'conflicts': result['conflicts'],
               'errors': result['errors'], 'downloads_after_game_exit': result['deferred']})
    return result


def transient(error):
    issue = str(error).lower()
    return isinstance(error, (OSError, NetworkError)) or any(word in issue for word in
           ('urlopen', 'timed out', 'name resolution', 'network', 'connection', 'offline'))


class Watcher:
    def __init__(self, base, config, cloud=None, clock=time.monotonic, wall=time.time, ignore_launcher=False):
        self.base, self.config, self.clock, self.wall = base, config, clock, wall
        self.root = data_root(base, config)
        settings = config.get('watcher', {})
        self.quiet = max(2, float(settings.get('quiet_seconds', 4)))
        self.cloud_interval = max(15, float(settings.get('cloud_poll_seconds', 30)))
        self.alert_after = float(settings.get('alert_after_seconds', 30))
        self.names = process_names(config)
        self.ignore_launcher = ignore_launcher
        self.cloud = cloud or Cloud(base, config)
        self.seen, self.invalid = {}, {}
        self.last_attempt = None
        self.next_cloud = self.clock() + self.quiet
        self.retry_at, self.failures = 0, 0
        self.was_running = True
        self.last_issue = None
        self.error_since = None
        self.notified_error = False
        self.last_deferred = None
        self.last_conflicts = None
        self.last_wall = None
        self.last_success = None

    def emit(self, kind, text):
        from watcher_notifications import emit
        emit(self.base, kind, text, self.config)

    def scan(self):
        now = self.clock()
        stable, unsettled, present = {}, set(), set()
        for path in sorted(self.root.glob('*/Card */*.gci')):
            key = path.relative_to(self.root).as_posix()
            present.add(key)
            try:
                stamp = signature(path)
                previous = self.seen.get(key)
                if previous is None or previous[0] != stamp:
                    self.seen[key] = (stamp, now)
                if now-self.seen[key][1] < self.quiet:
                    unsettled.add(key)
                    continue
                value = read_stable(path)
                if value is None:
                    unsettled.add(key)
                    continue
                if validate(key, value.data, self.config.get('watcher', {}).get('disc_id', 'GZ2E01'),
                            game_codes(self.config)):
                    stable[key] = value
                self.invalid.pop(key, None)
            except (OSError, SyncError) as error:
                unsettled.add(key)
                if self.invalid.get(key) != str(error):
                    LOG.warning('Очікуємо завершення/виправлення запису %s: %s', key, error)
                    self.invalid[key] = str(error)
        self.seen = {key: value for key, value in self.seen.items() if key in present}
        return stable, unsettled

    def woke(self):
        """After sleep or hibernation retry at once and give the network a fresh grace period."""
        wall = self.wall()
        gap = None if self.last_wall is None else wall - self.last_wall
        self.last_wall = wall
        if gap is None or gap < WAKE_GAP:
            return False
        LOG.info('Пауза %.0f с (сон або пробудження ПК): повторюємо синхронізацію одразу', gap)
        self.retry_at = 0
        self.next_cloud = 0
        self.cloud.invalidate()
        if not self.notified_error:
            self.error_since = None
        return True

    def tick(self, force=False):
        woke = self.woke()
        stable, unsettled = self.scan()
        now = self.clock()
        stamp = tuple((key, value.stamp, fingerprint(value.data)) for key, value in sorted(stable.items()))
        running = game_running(self.base, not self.ignore_launcher, self.names)
        ended = self.was_running and not running
        self.was_running = running
        if not force and not woke and (now < self.retry_at or (stamp == self.last_attempt and now < self.next_cloud
                                                               and not ended)):
            return None
        # Do not make an initial empty scan race a write that is still settling.
        if unsettled and not stable and not force:
            return None
        try:
            with sync_lock(self.base, blocking=False):
                result = sync_once(self.base, self.config, self.cloud, stable, unsettled, allow_pull=not running,
                                   ignore_launcher=self.ignore_launcher)
            if result['errors']:
                raise SyncError('; '.join(result['errors']))
            self.last_attempt = stamp
            self.next_cloud = now + self.cloud_interval
            self.retry_at, self.failures = 0, 0
            self.last_issue = None
            self.last_success = datetime.now(timezone.utc).isoformat()
            if self.notified_error:
                text = 'Зв’язок із GitHub відновлено.'
                if not result['deferred'] and not result['conflicts'] and not result['unstable']:
                    text += ' Сейви синхронізовано.'
                self.emit('recovered', text)
            self.error_since = None
            self.notified_error = False
            if result['uploaded']:
                self.emit('uploaded', 'Сейв відправлено на GitHub.')
            if result['downloaded']:
                self.emit('downloaded', 'Новіший сейв із GitHub завантажено.')
            deferred = tuple(result['deferred'])
            if deferred and deferred != self.last_deferred:
                self.emit('deferred', 'На GitHub є новіший сейв. Завантажимо після закриття Dusklight.')
            self.last_deferred = deferred
            conflicts = tuple(result['conflicts'])
            if conflicts and conflicts != self.last_conflicts:
                self.emit('conflict', 'Конфлікт сейвів: обидві версії збережено в резервних копіях. '
                                      'Вибери потрібну вручну; автоматично нічого не замінено.')
            self.last_conflicts = conflicts
            write_json(self.base/'.save-watcher-status.json', {'checked_at': self.last_success,
                       'last_success': self.last_success, 'game_running': running, 'result': result})
            return result
        except BlockingIOError:
            return None
        except Exception as error:
            self.failures += 1
            delay = min(60, 15*2**min(self.failures-1, 2))
            self.retry_at = now+delay
            self.cloud.invalidate()
            issue = str(error)
            if issue != self.last_issue:
                LOG.warning('Синхронізація очікує повтору; сейви залишено цілими: %s', issue)
                self.last_issue = issue
            if self.error_since is None:
                self.error_since = now
            short = transient(error)
            if not self.notified_error and (not short or now-self.error_since >= self.alert_after):
                self.emit('error', 'Тимчасово немає зв’язку з GitHub. Сейв збережено локально; спроби продовжуються.'
                          if short else 'GitHub не прийняв синхронізацію. Сейв збережено локально; '
                          'подробиці є в журналі watcher.')
                self.notified_error = True
            write_json(self.base/'.save-watcher-status.json', {'checked_at': datetime.now(timezone.utc).isoformat(),
                       'last_success': self.last_success, 'game_running': running, 'error': issue,
                       'retry_seconds': delay})
            return None


def notify_systemd(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address and hasattr(socket, 'AF_UNIX'):
        if address.startswith('@'):
            address = '\0'+address[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(address)
                sock.sendall(message.encode())
        except OSError:
            pass


def status_text(base):
    """Human-readable sync state for --status and the Start menu shortcut."""
    lines = []
    running = lock_busy(base/'.save-watcher.lock')
    lines.append('Watcher: ' + ('працює' if running else 'НЕ ПРАЦЮЄ'))
    status = read_json(base/'.save-watcher-status.json', {})
    pending = read_json(base/'.save-sync-pending.json', {})
    if status.get('checked_at'):
        lines.append(f"Остання перевірка: {status['checked_at']}")
    if status.get('last_success'):
        lines.append(f"Остання успішна синхронізація: {status['last_success']}")
    if status.get('error'):
        lines.append(f"Помилка (повтор через {status.get('retry_seconds')} с): {status['error']}")
    lines.append('Dusklight запущено' if status.get('game_running') else 'Dusklight закрито')
    if blocked := toasts_blocked():
        lines.append(f'Спливні вікна не з’являться: {blocked}. Події видно тут і в журналі.')
    for title, field in (('Чекають стабільного запису', 'files'), ('Завантажаться після закриття гри',
                         'downloads_after_game_exit'), ('Конфлікти', 'conflicts'), ('Помилки', 'errors')):
        if pending.get(field):
            lines.append(f"{title}: {', '.join(pending[field])}")
    events = read_json(base/'.save-watcher-events.json', {'events': []})['events'][-5:]
    if events:
        lines.append('Останні події:')
        lines += [f"  {datetime.fromtimestamp(e['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}  {e['body']}"
                  for e in events]
    lines.append(f'Журнал: {base / "save-watcher.log"}')
    lines.append(f'Резервні копії: {base / "save-backups"}')
    return '\n'.join(lines)


def setup_logging(base):
    handlers = [logging.handlers.RotatingFileHandler(base/'save-watcher.log', maxBytes=1_000_000, backupCount=3,
                                                     encoding='utf-8')]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='runtime folder with config.json (default: SAVE_SYNC_HOME or code folder)')
    parser.add_argument('--once', action='store_true', help='one synchronization pass, then exit')
    parser.add_argument('--status', action='store_true', help='print the synchronization state')
    parser.add_argument('--sync-now', action='store_true', help='ask the running watcher for an immediate pass')
    parser.add_argument('--test-notification', action='store_true')
    args = parser.parse_args()
    base = home_dir(args.home)
    base.mkdir(parents=True, exist_ok=True)
    config = read_json(base/'config.json', {})
    if args.status:
        print(status_text(base))
        return
    if args.sync_now:
        (base/'.save-watcher-request').write_text(str(time.time()))
        print('Перевірку синхронізації запитано.' if lock_busy(base/'.save-watcher.lock')
              else 'Watcher не працює; запит виконається після його запуску.')
        return
    if args.test_notification:
        from watcher_notifications import emit
        event = emit(base, 'test', 'Тест сповіщення: watcher сейвів готовий. Сейви не змінено.', config)
        blocked = toasts_blocked()
        print('Тестове сповіщення надіслано.' if event.get('shown', True) else
              f'Сповіщення не показано: {blocked}.' if blocked else 'Система не прийняла сповіщення.')
        return
    setup_logging(base)
    if config.get('save_format', 'gci') != 'gci':
        raise SyncError('Watcher підтримує сейви GCI.')
    try:
        instance = file_lock(base/'.save-watcher.lock', blocking=False)
        instance.__enter__()
    except BlockingIOError:
        print('Watcher уже працює.')
        return
    try:
        worker = Watcher(base, config)
        write_json(base/'.save-watcher-runtime.json', {'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat()})
        stop = False
        requested = False

        def stopping(signum, frame):
            nonlocal stop
            stop = True

        def request_sync(signum, frame):
            nonlocal requested
            requested = True
        signal.signal(signal.SIGTERM, stopping)
        signal.signal(signal.SIGINT, stopping)
        if hasattr(signal, 'SIGUSR1'):
            signal.signal(signal.SIGUSR1, request_sync)
        LOG.info('Watcher активний: %s; GitHub кожні %s с; стабілізація %s с', worker.root, worker.cloud_interval,
                 worker.quiet)
        if blocked := toasts_blocked():
            LOG.warning('Спливні вікна не з’являться: %s', blocked)
        notify_systemd('READY=1')
        if args.once:
            worker.scan()
            time.sleep(worker.quiet)
            result = worker.tick(force=True)
            print(json.dumps(result, ensure_ascii=False) if result else status_text(base))
            return
        request = base/'.save-watcher-request'
        while not stop:
            try:
                if request.exists():
                    request.unlink(missing_ok=True)
                    requested = True
                force, requested = requested, False
                worker.tick(force=force)
            except Exception:
                # Never die on an unexpected error: log it and keep watching.
                LOG.exception('Неочікувана помилка watcher; продовжуємо')
            time.sleep(2)
    finally:
        (base/'.save-watcher-runtime.json').unlink(missing_ok=True)
        instance.__exit__(None, None, None)


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        if isinstance(error, SystemExit) and not error.code:
            raise
        # pythonw has no console: keep the reason of a crash next to the log; the task restarts the watcher.
        import traceback
        crash = home_dir(next((sys.argv[i+1] for i, a in enumerate(sys.argv[:-1]) if a == '--home'), None))
        with (crash/'save-watcher-crash.log').open('a', encoding='utf-8') as stream:
            stream.write(f'{datetime.now().isoformat()} argv={sys.argv} home={crash}\n{traceback.format_exc()}\n')
        raise
