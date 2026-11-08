"""Bounded event feed shared by the watcher, desktop notifications and Decky."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from save_sync import read_json, write_json


def emit(base, kind, body):
    title = 'Dusklight — сейви'
    with (base/'.save-watcher-events.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        events = read_json(base/'.save-watcher-events.json',{'sequence':0,'events':[]})
        sequence = events['sequence']+1
        event = {'sequence':sequence,'kind':kind,'title':title,'body':body,'timestamp':time.time()}
        write_json(base/'.save-watcher-events.json', {'sequence':sequence,'events':(events['events']+[event])[-100:]})
    notify = shutil.which('notify-send')
    if notify:
        environment = os.environ.copy()
        environment.setdefault('DBUS_SESSION_BUS_ADDRESS',f'unix:path=/run/user/{os.getuid()}/bus')
        # The Steam stub advertises no capabilities; Decky renders the same event in Game Mode.
        try:
            probe = subprocess.run(['gdbus','call','--session','--dest','org.freedesktop.Notifications',
                                    '--object-path','/org/freedesktop/Notifications','--method',
                                    'org.freedesktop.Notifications.GetServerInformation'],
                                    env=environment,capture_output=True,text=True,timeout=3)
            if probe.returncode==0 and 'steam' in probe.stdout.lower():
                return event
            subprocess.run([notify,'-a','Dusklight Save Sync','-i',str(base/'dusklight.png'),
                            '-t','6500',title,body],env=environment,capture_output=True,timeout=5)
        except (OSError,subprocess.TimeoutExpired):
            pass
    return event
