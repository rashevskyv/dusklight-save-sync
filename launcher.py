#!/usr/bin/env python3
"""Start Dusklight with the newest published translation and synchronized saves.

1. Holds .launcher.lock: the watcher never writes into the card while the launcher prepares the game.
2. Compares the translation folders on GitHub with the files inside the disc image (git blob SHA-1),
   and rebuilds the image only when the published translation changed. A GameCube builder (gcr.exe,
   GCRebuilder) makes the image; WIT is not used because its default build makes a Wii disc.
   The new image replaces the old one only after its header and files are verified.
3. Synchronizes saves once (newer cloud save is installed now, while the game is closed).
4. Starts the game from its own folder, so settings, mods and saves are the same as for a Steam launch.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse

from platform_support import HIDDEN, WINDOWS, file_lock, process_running
import save_watcher as sw
from save_sync import GitHub, SyncError, read_json, write_json

GC_MAGIC, WII_MAGIC = 0xC2339F3D, 0x5D1C9EA3
SYSTEM = '&&systemdata'  # GCRebuilder layout: ISO.hdr, AppLoader.ldr, Start.dol, Game.toc


def say(text):
    print(text, flush=True)


def blob_sha(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


class Disc:
    """Read-only view of a GameCube disc image: header, system files and the FST."""
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open('rb') as stream:
            self.head = stream.read(0x2440)
            if len(self.head) < 0x2440 or struct.unpack_from('>I', self.head, 0x1C)[0] != GC_MAGIC:
                raise SyncError(f'Не образ диска GameCube: {self.path}')
            if struct.unpack_from('>I', self.head, 0x18)[0] == WII_MAGIC:
                raise SyncError(f'Образ є диском Wii, а не GameCube: {self.path}')
            self.id = self.head[:6].decode('ascii', errors='replace')
            self.dol_offset, self.fst_offset, self.fst_size = struct.unpack_from('>III', self.head, 0x420)
            stream.seek(0x2440)
            loader = stream.read(0x20)
            self.loader_size = 0x20 + struct.unpack_from('>I', loader, 0x14)[0] + struct.unpack_from('>I', loader, 0x18)[0]
            stream.seek(self.dol_offset)
            dol = stream.read(0x100)
            sizes = struct.unpack_from('>18I', dol, 0x90)
            self.dol_size = max(o + s for o, s in zip(struct.unpack_from('>18I', dol, 0), sizes) if s)
            stream.seek(self.fst_offset)
            self.fst = stream.read(self.fst_size)
        self.size = self.path.stat().st_size
        self.files = self._entries()

    def _entries(self):
        fst = self.fst
        count = struct.unpack_from('>I', fst, 8)[0]
        names = fst[count * 12:]
        files, stack = {}, [(count, '')]
        for index in range(1, count):
            while index >= stack[-1][0]:
                stack.pop()
            word, first, second = struct.unpack_from('>III', fst, index * 12)
            start = word & 0xFFFFFF
            name = names[start:names.index(b'\0', start)].decode('latin-1')
            if word >> 24:
                stack.append((second, stack[-1][1] + name + '/'))
            else:
                if first + second > self.size:
                    raise SyncError(f'Файл {name} виходить за межі образу.')
                files[stack[-1][1] + name] = (first, second)
        return files

    def read(self, disc_path):
        offset, size = self.files[disc_path]
        with self.path.open('rb') as stream:
            stream.seek(offset)
            return stream.read(size)

    def read_dol(self):
        with self.path.open('rb') as stream:
            stream.seek(self.dol_offset)
            return stream.read(self.dol_size)

    def extract(self, root):
        """Write the image as a GCRebuilder folder."""
        system = root / SYSTEM
        system.mkdir(parents=True)
        with self.path.open('rb') as stream:
            def copy(offset, size, target):
                stream.seek(offset)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('wb') as out:
                    left = size
                    while left:
                        chunk = stream.read(min(left, 8 << 20))
                        if not chunk:
                            raise SyncError('Образ обрізаний.')
                        out.write(chunk)
                        left -= len(chunk)
            (system / 'ISO.hdr').write_bytes(self.head)
            copy(0x2440, self.loader_size, system / 'AppLoader.ldr')
            copy(self.dol_offset, self.dol_size, system / 'Start.dol')
            (system / 'Game.toc').write_bytes(self.fst)
            for disc_path, (offset, size) in self.files.items():
                copy(offset, size, root.joinpath(*PurePosixPath(disc_path).parts))


def remote_translation(config):
    """{disc path: blob SHA} of the published translation, plus a fingerprint of that set."""
    settings = config['launcher']
    github = GitHub(Path('.'), config)
    head = github.api('git/ref/heads/' + urllib.parse.quote(github.branch, safe=''))['object']['sha']
    tree = github.api(f'git/trees/{head}?recursive=1')
    if tree.get('truncated'):
        raise SyncError('GitHub повернув неповний список файлів перекладу.')
    files = {}
    for item in tree['tree']:
        folder, _, name = item['path'].partition('/')
        if item['type'] == 'blob' and folder in settings['translation_dirs'] and name and '/' not in name:
            files[settings['translation_dirs'][folder].strip('/') + '/' + name] = (item['sha'], item['path'])
    if not files:
        raise SyncError('На GitHub немає файлів перекладу.')
    stamp = hashlib.sha256(json.dumps(sorted((k, v[0]) for k, v in files.items())).encode()).hexdigest()
    return github, files, stamp


def differences(disc, files):
    return [path for path, (sha, _) in files.items() if path not in disc.files or blob_sha(disc.read(path)) != sha]


def verify(new, old, files, expected_id):
    """The rebuilt image must be the same GameCube game with exactly the published translation."""
    if new.id != expected_id:
        raise SyncError(f'Новий образ має ID {new.id}, очікувався {expected_id}.')
    missing = set(old.files) - set(new.files)
    if missing:
        raise SyncError(f'У новому образі бракує файлів: {sorted(missing)[:5]}')
    if differences(new, files):
        raise SyncError('Файли перекладу в новому образі не збігаються з GitHub.')
    changed = [p for p, (_, size) in old.files.items() if p not in files and new.files[p][1] != size]
    if changed:
        raise SyncError(f'Збирач змінив інші файли: {changed[:5]}')
    if new.head[:0x420] != old.head[:0x420] or new.read_dol() != old.read_dol():
        raise SyncError('Заголовок або main.dol нового образу відрізняються.')


def ensure_image(settings):
    """First launch on a new machine: convert the configured RVZ once."""
    iso = Path(settings['iso'])
    if iso.exists():
        return iso
    source, tool = settings.get('source_image'), settings.get('dolphin_tool')
    if not source or not tool:
        raise SyncError(f'Немає образу {iso}; вкажи launcher.source_image (RVZ) і launcher.dolphin_tool.')
    temp = iso.with_name(iso.stem + '.converting.iso')
    say(f'Перший запуск: конвертую {source} у ISO…')
    subprocess.run([tool, 'convert', '-i', source, '-o', str(temp), '-f', 'iso'], check=True, **HIDDEN)
    Disc(temp)
    os.replace(temp, iso)
    return iso


def update_translation(home, config, force=False):
    """Return 'current', 'rebuilt' or 'kept' (published translation unchanged since the last check)."""
    settings = config['launcher']
    iso = ensure_image(settings)
    github, files, stamp = remote_translation(config)
    state = read_json(home / '.translation-state.json', {})
    if state.get('tree') == stamp and not force:
        return 'kept'  # A local test build of unpublished work is never replaced by an unchanged publication.
    disc = Disc(iso)
    changed = differences(disc, files)
    if not changed:
        write_json(home / '.translation-state.json', {'tree': stamp, 'iso': str(iso)})
        return 'current'
    say(f'Переклад на GitHub оновився ({len(changed)} файлів). Перебудовую образ…')
    blobs = {}
    for path in changed:
        sha, repo_path = files[path]
        data = base64.b64decode(github.api('git/blobs/' + sha)['content'])
        if blob_sha(data) != sha:
            raise SyncError(f'Пошкоджене завантаження {repo_path}.')
        blobs[path] = data
    work = Path(settings.get('work_dir') or home / 'iso-work')
    if work.exists():
        shutil.rmtree(work)
    temp = iso.with_name(iso.stem + '.building.iso')
    temp.unlink(missing_ok=True)
    try:
        disc.extract(work / 'root')
        for path, data in blobs.items():
            target = (work / 'root').joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        builder = Path(settings['builder'])
        result = subprocess.run([str(builder), str(work / 'root'), str(temp)], cwd=builder.parent,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                                timeout=3600, **HIDDEN)
        if result.returncode != 0 or not temp.exists():
            raise SyncError(f'Збирач образу завершився з кодом {result.returncode}: {result.stdout[-500:]}')
        verify(Disc(temp), disc, files, disc.id)
        os.replace(temp, iso)  # Only a verified image replaces the old one.
    finally:
        temp.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    write_json(home / '.translation-state.json', {'tree': stamp, 'iso': str(iso)})
    return 'rebuilt'


def sync_saves(home, config):
    worker = sw.Watcher(home, config, ignore_launcher=True)
    worker.scan()
    time.sleep(worker.quiet)  # A save written just before the launch must settle first.
    with sw.sync_lock(home):  # Waits for a watcher pass in progress.
        stable, unsettled = worker.scan()
        return sw.sync_once(home, config, worker.cloud, stable, unsettled, allow_pull=True, ignore_launcher=True)


def start_game(config, names):
    exe = Path(config['launcher']['game_exe'])
    flags = 0x00000008 | 0x00000200 if WINDOWS else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], cwd=exe.parent, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if process_running(names):
            return True
        time.sleep(0.5)
    return False


def check_iso_path(config):
    exe = Path(config['launcher']['game_exe'])
    current = read_json(exe.parent / 'data' / 'config.json', {}).get('backend.isoPath')
    if current and Path(current) != Path(config['launcher']['iso']):
        say(f'Увага: Dusklight налаштовано на інший образ: {current}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', help='runtime folder with config.json')
    parser.add_argument('--no-launch', action='store_true', help='update and sync only')
    parser.add_argument('--force-rebuild-check', action='store_true', help='compare the image even if GitHub is unchanged')
    args = parser.parse_args()
    home = sw.home_dir(args.home)
    config = read_json(home / 'config.json', {})
    names = sw.process_names(config)
    from watcher_notifications import emit
    try:
        lock = file_lock(home / '.launcher.lock', blocking=False)
        lock.__enter__()
    except BlockingIOError:
        say('Лаунчер уже працює.')
        return 1
    try:
        if process_running(names):
            say('Dusklight уже запущено. Закрий гру, щоб оновити переклад і сейви.')
            return 1
        try:
            outcome = update_translation(home, config, args.force_rebuild_check)
            say({'kept': 'Переклад: без змін на GitHub.', 'current': 'Переклад: образ уже актуальний.',
                 'rebuilt': 'Переклад оновлено; образ перебудовано й перевірено.'}[outcome])
            if outcome == 'rebuilt':
                emit(home, 'translation', 'Український переклад оновлено.', config)
        except Exception as error:
            say(f'Оновлення перекладу пропущено, гра запуститься з наявним образом: {error}')
        try:
            result = sync_saves(home, config)
            say('Сейви: ' + ', '.join(f'{k}: {len(v)}' for k, v in result.items() if v) if any(result.values())
                else 'Сейви: синхронізовано.')
            if result['downloaded']:
                emit(home, 'downloaded', 'Новіший сейв із GitHub завантажено.', config)
            if result['conflicts']:
                emit(home, 'conflict', 'Конфлікт сейвів: обидві версії збережено в резервних копіях.', config)
        except Exception as error:
            say(f'Сейви не синхронізовано зараз (watcher повторить пізніше): {error}')
        if args.no_launch:
            return 0
        check_iso_path(config)
        if not start_game(config, names):
            say('Dusklight не запустився за 30 секунд.')
            return 1
        say('Dusklight запущено.')
        return 0
    finally:
        lock.__exit__(None, None, None)


if __name__ == '__main__':
    sys.exit(main())
