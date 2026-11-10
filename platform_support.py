"""OS specifics shared by the watcher and the launcher: file locks, game detection, popups."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import time

WINDOWS = os.name == 'nt'
# pythonw has no console: without this flag every child process flashes a window.
HIDDEN = {'creationflags': 0x08000000} if WINDOWS else {}
DEFAULT_PROCESSES = ('dusklight',)

if WINDOWS:
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(path, blocking=True):
    """Exclusive inter-process lock. Raises BlockingIOError when busy and blocking is False."""
    with open(path, 'a+b') as stream:
        if WINDOWS:
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not blocking:
                        raise BlockingIOError(f'{path} is locked') from None
                    time.sleep(0.05)
        else:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if WINDOWS:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def lock_busy(path):
    try:
        with file_lock(path, blocking=False):
            return False
    except BlockingIOError:
        return True


def windows_images():
    """Executable names of all processes. A Toolhelp snapshot takes milliseconds; tasklist can hang for minutes."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), 'CreateToolhelp32Snapshot')
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        images, more = [], kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            images.append(entry.szExeFile)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if not images:
            raise OSError('empty process list')
        return images
    finally:
        kernel.CloseHandle(wintypes.HANDLE(snapshot))


def process_running(names=DEFAULT_PROCESSES, proc=Path('/proc')):
    names = tuple(name.lower().removesuffix('.exe') for name in names)
    if WINDOWS:
        try:
            images = windows_images()
        except OSError:
            return True  # Unknown state: never write into a card the game may hold in memory.
        return any(image.lower().removesuffix('.exe').startswith(names) for image in images)
    # Works across SteamOS sessions and direct AppImage launches.
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / 'comm').read_text().strip().lower()
            if name.startswith(names):
                return True
            if name in ('apprun', 'apprun.wrapped'):
                exe = str((entry / 'exe').resolve()).lower()
                if any(item[:4] in exe for item in names):
                    return True
        except (OSError, PermissionError):
            continue
    return False


TOAST = r'''
$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$lines = $template.GetElementsByTagName('text')
$null = $lines.Item(0).AppendChild($template.CreateTextNode($env:SAVE_SYNC_TITLE))
$null = $lines.Item(1).AppendChild($template.CreateTextNode($env:SAVE_SYNC_BODY))
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
$toast.Tag = $env:SAVE_SYNC_TAG
$toast.Group = 'save-sync'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SAVE_SYNC_APP).Show($toast)
'''
# Registered by install-windows.ps1 (HKCU AppUserModelId); PowerShell's own id is the fallback.
APP_ID = 'DusklightSaveSync'
POWERSHELL_APP_ID = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'


def windows_app_id():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f'Software\\Classes\\AppUserModelId\\{APP_ID}'):
            return APP_ID
    except OSError:
        return POWERSHELL_APP_ID


def toasts_blocked():
    """Windows hides toasts without an error when the user turned them off; name the switch instead."""
    if not WINDOWS:
        return None
    import winreg
    switches = ((r'Software\Microsoft\Windows\CurrentVersion\PushNotifications', 'ToastEnabled',
                 'сповіщення Windows вимкнено (Параметри → Система → Сповіщення)'),
                (rf'Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{APP_ID}', 'Enabled',
                 'сповіщення «Dusklight Save Sync» вимкнено (Параметри → Система → Сповіщення)'))
    for key, name, reason in switches:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                if winreg.QueryValueEx(handle, name)[0] == 0:
                    return reason
        except OSError:
            pass
    return None


def notify(title, body, base, tag='save-sync'):
    """Show a native desktop popup. Returns True when the OS accepted it."""
    if WINDOWS:
        if toasts_blocked():
            return False
        environment = dict(os.environ, SAVE_SYNC_TITLE=title, SAVE_SYNC_BODY=body,
                           SAVE_SYNC_TAG=tag[:16], SAVE_SYNC_APP=windows_app_id())
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-Command', TOAST], env=environment, capture_output=True, timeout=20, **HIDDEN)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    notify_send = shutil.which('notify-send')
    if not notify_send:
        return False
    environment = os.environ.copy()
    environment.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{os.getuid()}/bus')
    try:
        # The Steam stub advertises no capabilities; Decky renders the same event in Game Mode.
        probe = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
                                '--object-path', '/org/freedesktop/Notifications', '--method',
                                'org.freedesktop.Notifications.GetServerInformation'],
                               env=environment, capture_output=True, text=True, timeout=3)
        if probe.returncode == 0 and 'steam' in probe.stdout.lower():
            return False
        result = subprocess.run([notify_send, '-a', 'Dusklight Save Sync', '-i', str(base / 'dusklight.png'),
                                 '-t', '6500', title, body], env=environment, capture_output=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
