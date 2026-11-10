"""GCI validation, GitHub access and backups shared by the watcher, the launcher and the legacy one-shot sync."""
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from platform_support import HIDDEN


class SyncError(RuntimeError):
    pass


class NetworkError(SyncError):
    """Temporary failure: retried quietly before any popup."""


# Twilight Princess GameCube and Wii codes; another GameCube game sets watcher.game_codes in config.json.
DEFAULT_GAME_CODES = ('GZ2E', 'GZ2P', 'GZ2J', 'RZDE', 'RZDP', 'RZDJ')
REGIONS = {'E': 'USA', 'P': 'EUR', 'J': 'JAP'}
GC_EPOCH = 946684800  # 2000-01-01 UTC


def game_codes(config):
    return tuple(config.get('watcher', {}).get('game_codes', DEFAULT_GAME_CODES))


def fingerprint(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def validate(key, data, identifier=None, codes=DEFAULT_GAME_CODES):
    """Raise SyncError for a damaged or misplaced GCI; return False for a valid save of another game."""
    parts = PurePosixPath(key).parts
    if len(parts) != 3 or parts[0] not in REGIONS.values() or parts[1] not in ('Card A', 'Card B'):
        raise SyncError(f'Некоректний шлях GCI-сейву: {key}')
    if len(data) <= 64 or len(data) != 64 + struct.unpack_from('>H', data, 0x38)[0] * 8192:
        raise SyncError(f'Некоректний розмір GCI: {key}')
    game = data[:4].decode('ascii', errors='replace')
    if game not in codes or not data[4:6].isalnum():
        raise SyncError(f'GCI не належить налаштованій грі: {key}')
    if parts[0] != REGIONS.get(game[3]):
        raise SyncError(f'Регіон GCI не відповідає теці: {key}')
    name = data[8:40].split(b'\0', 1)[0].decode('ascii', errors='replace')
    maker = data[4:6].decode('ascii')
    if not name or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name) or parts[2] != f'{maker}-{game}-{name}.gci':
        raise SyncError(f'Назва GCI не відповідає заголовку: {key}')
    return identifier is None or game == identifier[:4]


def saved_at(data):
    """GCI header time: seconds since 2000-01-01. Dusklight writes it when the file is created only."""
    value = int.from_bytes(data[40:44], 'big')
    if value in (0, 0xffffffff):
        return None
    return value


def decision(local, remote, baseline):
    """Legacy launch-time rule used by synchronize()."""
    lh, rh = fingerprint(local), fingerprint(remote)
    if lh == rh:
        return 'equal'
    if local is None:
        return 'remote'
    if remote is None:
        return 'local' if baseline is None else 'conflict'
    if baseline is not None:
        if rh == baseline:
            return 'local'
        if lh == baseline:
            return 'remote'
    lt, rt = saved_at(local), saved_at(remote)
    if lt is not None and rt is not None and lt != rt:
        return 'local' if lt > rt else 'remote'
    return 'conflict'


class GitHub:
    def __init__(self, base, config):
        self.base, self.config = base, config
        parsed = urllib.parse.urlparse(config['github_repo'].removesuffix('.git').rstrip('/'))
        if parsed.scheme != 'https' or parsed.netloc != 'github.com' or len(parsed.path.strip('/').split('/')) != 2:
            raise SyncError('Некоректний github_repo.')
        self.repo = parsed.path.strip('/')
        self.branch = config.get('branch', 'main')
        self.prefix = config.get('saves_repo_path', 'Saves').strip('/')
        path = PurePosixPath(self.prefix)
        if not self.prefix or path.is_absolute() or '..' in path.parts:
            raise SyncError('Некоректний saves_repo_path.')
        self.token = self.read_token()

    def read_token(self):
        """Token from the environment or this device's own GitHub CLI login; never logged or stored."""
        token = next((os.environ[name] for name in ('TWILIGHT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN')
                      if os.environ.get(name)), None)
        if token:
            return token
        gh = self.base / 'soft/linux/gh'
        if not gh.exists():
            gh = shutil.which('gh')
        if not gh:
            return None
        environment = os.environ.copy()
        private_config = self.base / '.github-auth'
        if private_config.exists():
            environment['GH_CONFIG_DIR'] = str(private_config)
        try:
            result = subprocess.run([str(gh), 'auth', 'token', '--hostname', 'github.com'], capture_output=True,
                                    text=True, env=environment, timeout=30, **HIDDEN)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (result.stdout.strip() or None) if result.returncode == 0 else None

    def api(self, path, payload=None):
        headers = {'User-Agent': 'TwilightPrincess-SaveSync/3', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if not self.token:
            self.token = self.read_token()  # gh may finish its login after the watcher started.
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(f'https://api.github.com/repos/{self.repo}/{path}'.rstrip('/'), data=data,
                                         headers=headers, method='GET' if payload is None else 'PUT')
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (409, 422) and payload is not None:
                raise SyncError('GitHub змінився під час синхронізації. Локальний сейв збережено; повтори синхронізацію.') from None
            if error.code >= 500 or error.code == 429:
                raise NetworkError(f'GitHub тимчасово недоступний (HTTP {error.code}); локальні сейви збережено.') from None
            if error.code == 401:
                self.token = None
            raise SyncError(f'GitHub повернув HTTP {error.code}; локальні сейви збережено.') from None

    def snapshot(self):
        branch = urllib.parse.quote(self.branch, safe='')
        head = self.api('git/ref/heads/' + branch)['object']['sha']
        tree = self.api(f'git/trees/{head}?recursive=1')
        if tree.get('truncated'):
            raise SyncError('GitHub повернув неповне дерево файлів.')
        result = {}
        for entry in tree['tree']:
            path = entry['path']
            if entry['type'] != 'blob' or not path.startswith(self.prefix + '/') or not path.endswith('.gci'):
                continue
            if entry.get('mode') != '100644' and entry.get('mode') != '100755':
                raise SyncError('Посилання замість сейву в репозиторії.')
            key = path[len(self.prefix)+1:]
            blob = self.api('git/blobs/' + entry['sha'])
            if blob['encoding'] != 'base64':
                raise SyncError('Невідомий формат відповіді GitHub.')
            result[key] = {'data': base64.b64decode(blob['content']), 'sha': entry['sha']}
        return result

    def upload(self, key, data, sha):
        if not self.token:
            raise SyncError('Немає входу в GitHub на цьому пристрої (gh auth login). Сейв залишився локально.')
        payload = {'message': 'Update Twilight Princess save after game session',
                   'content': base64.b64encode(data).decode('ascii'), 'branch': self.branch}
        if sha:
            payload['sha'] = sha
        self.api('contents/' + urllib.parse.quote(self.prefix + '/' + key, safe='/'), payload)


def backup(base, side, key, data):
    if data is None:
        return None
    root = base / 'save-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f') / side
    path = root.joinpath(*PurePosixPath(key).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_save(root, key, data):
    target = root.joinpath(*PurePosixPath(key).parts)
    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
        # The canonical data directory itself may be selected through its alias.
        raise SyncError(f'Посилання у шляху сейву: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.sync')
    temp.write_bytes(data)
    temp.replace(target)


def synchronize(base, config, phase, identifier='GZ2E01', remote=None):
    """Legacy one-shot sync around a game session (kept for older Steam Deck scripts)."""
    if config.get('save_format', 'gci') != 'gci':
        raise SyncError('Автоматична двостороння синхронізація наразі потребує формату GCI.')
    user_dir = Path(config.get('dusklight_user_dir', 'DusklightData')).expanduser()
    user_dir = (user_dir if user_dir.is_absolute() else base / user_dir).resolve()
    remote = remote or GitHub(base, config)
    codes = game_codes(config)
    entries = remote.snapshot()
    state_path = base / '.save-sync-state.json'
    state = read_json(state_path, read_json(base / '.save-state.json', {}))
    pending_path = base / '.save-sync-pending.json'
    local_files = {p.relative_to(user_dir).as_posix(): p.read_bytes() for p in user_dir.glob('*/Card */*.gci')}
    keys = sorted(set(entries) | set(local_files))
    pending, conflicts, errors = [], [], []
    for key in keys:
        local = local_files.get(key)
        item = entries.get(key)
        other = item['data'] if item else None
        if other is not None and not validate(key, other, identifier, codes):
            continue
        if local is not None and not validate(key, local, identifier, codes):
            continue
        action = decision(local, other, state.get(key))
        if action == 'equal':
            if local is not None:
                state[key] = fingerprint(local)
        elif action == 'remote':
            backup(base, 'local', key, local)
            write_save(user_dir, key, other)
            state[key] = fingerprint(other)
            print(f'Сейв із GitHub установлено: {key}', flush=True)
        elif action == 'local':
            pending.append(key)
            if phase == 'after':
                try:
                    # Never send a half-written file if a separate process is still writing.
                    current = user_dir.joinpath(*PurePosixPath(key).parts).read_bytes()
                    if current != local:
                        raise SyncError('Сейв змінюється під час синхронізації; закрий гру повністю.')
                    backup(base, 'github', key, other)
                    remote.upload(key, local, item['sha'] if item else None)
                    state[key] = fingerprint(local)
                    pending.remove(key)
                    print(f'Сейв відправлено на GitHub: {key}', flush=True)
                except Exception as error:
                    errors.append(str(error))
            else:
                print(f'Локальний сейв свіжіший; буде відправлений після закриття гри: {key}', flush=True)
        else:
            backup(base, 'local-conflict', key, local)
            backup(base, 'github-conflict', key, other)
            conflicts.append(key)
        write_json(state_path, state)
    write_json(pending_path, {'files': pending, 'conflicts': conflicts, 'errors': errors})
    if conflicts:
        raise SyncError('Різні версії сейву мають однакову або невідому дату. Обидві збережено в save-backups; потрібен вибір: ' + ', '.join(conflicts))
    if errors:
        raise SyncError('; '.join(errors))
    if not pending:
        print('Сейви синхронізовано.', flush=True)
    return pending
