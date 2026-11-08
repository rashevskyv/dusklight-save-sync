#!/usr/bin/env python3
"""Bidirectional, durable synchronization of stable Dusklight GCI saves."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path, PurePosixPath
import signal
import socket
import time
import urllib.parse
import base64

from save_sync import GitHub, SyncError, backup, fingerprint, read_json, saved_at, validate, write_json

LOG = logging.getLogger('save-watcher')
BASE = Path(__file__).resolve().parent


def signature(path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass
class Save:
    data: bytes
    stamp: tuple
    @property
    def modified_ns(self):
        return self.stamp[3]


def read_stable(path):
    before = signature(path)
    if path.is_symlink():
        raise SyncError(f'Сейв є символічним посиланням: {path.name}')
    if before[2] > 16 * 1024 * 1024:
        raise SyncError('GCI перевищує розмір карти пам’яті.')
    data = path.read_bytes()
    if signature(path) != before:
        return None
    return Save(data, before)


def data_root(base, config):
    root = Path(config.get('dusklight_user_dir', 'DusklightData')).expanduser()
    return (root if root.is_absolute() else base / root).resolve()


def game_running(base, include_launcher=True, proc=Path('/proc')):
    if include_launcher:
        with (base / '.launcher.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    # Works across both SteamOS sessions and direct AppImage launches.
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / 'comm').read_text().strip().lower()
            if name.startswith('dusklight'):
                return True
            if name in ('apprun', 'apprun.wrapped'):
                exe = str((entry / 'exe').resolve()).lower()
                if 'dusk' in exe:
                    return True
        except (OSError, PermissionError):
            continue
    return False


@contextmanager
def sync_lock(base, blocking=True):
    with (base / '.save-sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


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
        entries = {}
        for item in tree['tree']:
            path = item['path']
            if item['type'] != 'blob' or not path.startswith(self.prefix + '/') or not path.endswith('.gci'):
                continue
            if item.get('mode') not in ('100644','100755'):
                raise SyncError('Посилання замість сейву на GitHub.')
            key = path[len(self.prefix)+1:]
            # A malicious path is rejected before any local destination is built.
            parts = PurePosixPath(key).parts
            if len(parts) != 3 or '..' in parts:
                raise SyncError(f'Некоректний шлях сейву на GitHub: {key}')
            old = self.cached.get(key) if self.cached else None
            if old and old['sha'] == item['sha']:
                data = old['data']
            else:
                blob = self.api('git/blobs/' + item['sha'])
                if blob['encoding'] != 'base64':
                    raise SyncError('Невідоме кодування GitHub.')
                data = base64.b64decode(blob['content'])
            validate(key, data)
            commits = self.api('commits?' + urllib.parse.urlencode({'path':path, 'sha':head, 'per_page':1}))
            stamp = None
            if commits:
                for line in commits[0]['commit']['message'].splitlines():
                    if line.startswith('Save-Sync: '):
                        try:
                            meta = json.loads(line[len('Save-Sync: '):])
                            if meta['sha256'] == fingerprint(data):
                                value = int(meta['modified_ns'])
                                if 0 < value < 2**63:
                                    stamp = value
                        except (ValueError, KeyError, TypeError):
                            pass
            # Existing manually uploaded files retain the date inside GCI.
            # Future watcher commits carry the actual original save-write time.
            if stamp is None and saved_at(data) is not None:
                stamp = (946684800 + saved_at(data)) * 1_000_000_000
            if stamp is None and commits:
                value = commits[0]['commit']['committer']['date'].replace('Z','+00:00')
                stamp = int(datetime.fromisoformat(value).timestamp()*1e9)
            entries[key] = {'data':data, 'sha':item['sha'], 'modified_ns':stamp}
        self.head, self.cached = head, entries
        return entries

    def upload(self, key, data, sha, modified_ns):
        if not self.token:
            raise SyncError('Немає входу в GitHub. Запусти Connect-GitHub.sh; локальний сейв збережено.')
        meta = {'sha256':fingerprint(data), 'modified_ns':modified_ns}
        payload = {'message':'Sync Twilight Princess save from Steam Deck\n\nSave-Sync: '+json.dumps(meta,separators=(',',':')),
                   'content':base64.b64encode(data).decode('ascii'), 'branch':self.branch}
        if sha:
            payload['sha'] = sha
        self.api('contents/' + urllib.parse.quote(self.prefix + '/' + key, safe='/'), payload)
        self.invalidate()


def choose(local, remote, baseline):
    lh = fingerprint(local.data) if local else None
    rh = fingerprint(remote['data']) if remote else None
    if lh == rh:
        return 'equal'
    if local is None:
        return 'pull'
    if remote is None:
        # Deletions are never propagated, but do not silently resurrect a remote deletion.
        return 'push' if baseline is None else 'conflict'
    lt, rt = local.modified_ns, remote.get('modified_ns')
    if rt is not None and lt != rt:
        return 'push' if lt > rt else 'pull'
    if baseline is not None:
        if rh == baseline:
            return 'push'
        if lh == baseline:
            return 'pull'
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
            os.utime(temp, ns=(stamp,stamp))
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def sync_once(base, config, cloud, local, unsettled=(), allow_pull=True, ignore_launcher=False):
    """Caller holds sync_lock. local contains immutable, quiet-period snapshots."""
    root = data_root(base, config)
    state_file = base / '.save-sync-state.json'
    state = read_json(state_file, read_json(base / '.save-state.json', {}))
    entries = cloud.snapshot()
    result = {'uploaded':[], 'downloaded':[], 'deferred':[], 'conflicts':[], 'unstable':[], 'errors':[]}
    identifier = config.get('watcher',{}).get('disc_id','GZ2E01')
    for key in sorted(set(local) | set(entries)):
        if key in unsettled:
            result['unstable'].append(key)
            continue
        current, remote = local.get(key), entries.get(key)
        if current and not validate(key,current.data,identifier):
            continue
        if remote and not validate(key,remote['data'],identifier):
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
                    backup(base,'github',key,remote['data'])
                cloud.upload(key, current.data, remote['sha'] if remote else None, current.modified_ns)
                state[key] = fingerprint(current.data)
                result['uploaded'].append(key)
                LOG.info('Сейв відправлено на GitHub: %s',key)
            elif action == 'pull':
                # Recheck process state immediately before touching the game's card.
                if not allow_pull or game_running(base, include_launcher=not ignore_launcher):
                    result['deferred'].append(key)
                    continue
                if current:
                    backup(base,'local',key,current.data)
                restore(root,key,remote)
                state[key] = fingerprint(remote['data'])
                result['downloaded'].append(key)
                LOG.info('Новіший сейв із GitHub установлено: %s',key)
            else:
                result['conflicts'].append(key)
                # Keep both versions, once per unique conflict, without backup spam.
                conflicts = read_json(base / '.save-watcher-conflicts.json',{})
                pair = [fingerprint(current.data) if current else None, fingerprint(remote['data']) if remote else None]
                if conflicts.get(key) != pair:
                    backup(base,'local-conflict',key,current.data if current else None)
                    backup(base,'github-conflict',key,remote['data'] if remote else None)
                    conflicts[key] = pair
                    write_json(base / '.save-watcher-conflicts.json',conflicts)
                    LOG.error('Конфлікт сейву; обидві копії збережено: %s',key)
            write_json(state_file,state)
        except (OSError, SyncError) as error:
            cloud.invalidate()
            result['errors'].append(f'{key}: {error}')
    write_json(base / '.save-sync-pending.json', {'files':result['unstable'], 'conflicts':result['conflicts'],
               'errors':result['errors'], 'downloads_after_game_exit':result['deferred']})
    return result


class Watcher:
    def __init__(self, base, config, cloud=None, clock=time.monotonic):
        self.base, self.config, self.clock = base, config, clock
        self.root = data_root(base, config)
        settings = config.get('watcher',{})
        self.quiet = max(2, float(settings.get('quiet_seconds',4)))
        self.cloud_interval = max(15,float(settings.get('cloud_poll_seconds',30)))
        self.cloud = cloud or Cloud(base,config)
        self.seen, self.invalid = {}, {}
        self.last_attempt = None
        self.next_cloud = self.clock() + self.quiet
        self.retry_at, self.failures = 0,0
        self.was_running = True
        self.last_issue = None
        self.error_since = None
        self.notified_error = False
        self.last_deferred = None
        self.last_conflicts = None

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
                    self.seen[key] = (stamp,now)
                if now-self.seen[key][1] < self.quiet:
                    unsettled.add(key)
                    continue
                value = read_stable(path)
                if value is None:
                    unsettled.add(key)
                    continue
                if validate(key,value.data,self.config.get('watcher',{}).get('disc_id','GZ2E01')):
                    stable[key] = value
            except (OSError,SyncError) as error:
                unsettled.add(key)
                if self.invalid.get(key) != str(error):
                    LOG.warning('Очікуємо завершення/виправлення запису %s: %s',key,error)
                    self.invalid[key] = str(error)
        self.seen = {key:value for key,value in self.seen.items() if key in present}
        return stable, unsettled

    def tick(self, force=False):
        stable,unsettled = self.scan()
        now = self.clock()
        stamp = tuple((key, value.stamp, fingerprint(value.data)) for key,value in sorted(stable.items()))
        running = game_running(self.base)
        ended = self.was_running and not running
        self.was_running = running
        if not force and (now < self.retry_at or (stamp==self.last_attempt and now<self.next_cloud and not ended)):
            return None
        # Do not make an initial empty scan race a write that is still settling.
        if unsettled and not stable and not force:
            return None
        try:
            with sync_lock(self.base,blocking=False):
                result = sync_once(self.base,self.config,self.cloud,stable,unsettled,allow_pull=not running)
            if result['errors']:
                raise SyncError('; '.join(result['errors']))
            self.last_attempt = stamp
            self.next_cloud = now + self.cloud_interval
            self.retry_at, self.failures = 0,0
            self.last_issue = None
            from watcher_notifications import emit
            if self.notified_error:
                text = 'Зв’язок із GitHub відновлено.'
                if not result['deferred'] and not result['conflicts'] and not result['unstable']:
                    text += ' Сейви синхронізовано.'
                emit(self.base,'recovered',text)
            self.error_since = None
            self.notified_error = False
            if result['uploaded']:
                emit(self.base,'uploaded','Сейв відправлено на GitHub.')
            if result['downloaded']:
                emit(self.base,'downloaded','Новіший сейв із GitHub завантажено на Steam Deck.')
            deferred = tuple(result['deferred'])
            if deferred and deferred != self.last_deferred:
                emit(self.base,'deferred','На GitHub є новіший сейв. Завантажимо після закриття Dusklight.')
            self.last_deferred = deferred
            conflicts = tuple(result['conflicts'])
            if conflicts and conflicts != self.last_conflicts:
                emit(self.base,'conflict','Конфлікт сейвів: обидві версії збережено в резервних копіях.')
            self.last_conflicts = conflicts
            write_json(self.base/'.save-watcher-status.json', {'checked_at':datetime.now(timezone.utc).isoformat(),
                       'game_running':running, 'result':result})
            return result
        except BlockingIOError:
            return None
        except Exception as error:
            self.failures += 1
            delay = min(300,15*2**min(self.failures-1,5))
            self.retry_at = now+delay
            self.cloud.invalidate()
            issue = str(error)
            if issue != self.last_issue:
                LOG.warning('Синхронізація очікує повтору; сейви залишено цілими: %s',issue)
                self.last_issue = issue
            if self.error_since is None:
                self.error_since = now
            transient = isinstance(error, OSError) or any(word in issue.lower() for word in
                         ('urlopen', 'timed out', 'name resolution', 'network', 'connection', 'offline'))
            if not self.notified_error and (not transient or now-self.error_since >= 30):
                from watcher_notifications import emit
                text = ('Тимчасово немає зв’язку з GitHub. Сейв збережено локально; спроби продовжуються.'
                        if transient else 'GitHub не прийняв синхронізацію. Сейв збережено локально; подробиці є в журналі watcher.')
                emit(self.base,'error',text)
                self.notified_error = True
            write_json(self.base/'.save-watcher-status.json', {'checked_at':datetime.now(timezone.utc).isoformat(),
                       'error':issue,'retry_seconds':delay})
            return None


def notify_systemd(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0'+address[1:]
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
                sock.connect(address);sock.sendall(message.encode())
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--test-notification',action='store_true')
    args = parser.parse_args()
    if args.test_notification:
        from watcher_notifications import emit
        emit(BASE,'test','Тест сповіщення: watcher сейвів готовий. Сейви не змінено.')
        print('Тестове сповіщення надіслано.')
        return
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config = read_json(BASE/'config.json',{})
    if config.get('save_format','gci') != 'gci':
        raise SyncError('Watcher підтримує сейви GCI.')
    with (BASE/'.save-watcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('Watcher уже працює.') from None
        worker = Watcher(BASE,config)
        write_json(BASE/'.save-watcher-runtime.json', {'pid':os.getpid(),'started_at':datetime.now(timezone.utc).isoformat()})
        stop = False
        requested = False
        def stopping(signum, frame):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM,stopping)
        signal.signal(signal.SIGINT,stopping)
        def request_sync(signum, frame):
            nonlocal requested
            requested = True
        signal.signal(signal.SIGUSR1,request_sync)
        LOG.info('Watcher активний: %s; GitHub кожні %s с; стабілізація %s с',worker.root,worker.cloud_interval,worker.quiet)
        notify_systemd('READY=1')
        if args.once:
            worker.scan();time.sleep(worker.quiet);worker.tick(force=True)
            return
        while not stop:
            force, requested = requested, False
            worker.tick(force=force)
            time.sleep(2)
        (BASE/'.save-watcher-runtime.json').unlink(missing_ok=True)


if __name__=='__main__':
    main()
