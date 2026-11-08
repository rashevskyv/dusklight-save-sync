import asyncio
import json
import os
from pathlib import Path
import signal
import time

BASE = Path(os.environ.get('DUSKLIGHT_GAME_DIR', str(Path.home() / 'Downloads' / 'Twilight Princess')))

def read(name, default):
    try:
        return json.loads((BASE/name).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return default

def watcher_pid():
    pid=read('.save-watcher-runtime.json',{}).get('pid')
    if isinstance(pid,int) and pid>1:
        try:
            command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(BASE/'save_watcher.py').encode() in command:
                return pid
        except OSError:
            pass
    return None

class Plugin:
    async def get_events(self, after: int=0):
        if isinstance(after,dict):
            after=after.get('after',0)
        after=int(after)
        heartbeat=BASE/'.save-watcher-decky-heartbeat.json'
        temporary=heartbeat.with_suffix('.tmp')
        temporary.write_text(json.dumps({'timestamp':time.time(),'last_seen_sequence':after,'version':'1.0.2'}))
        temporary.replace(heartbeat)
        value=read('.save-watcher-events.json',{'sequence':0,'events':[]})
        return {'latest_sequence':value['sequence'],'events':[event for event in value['events']
                if event['sequence']>after and time.time()-event['timestamp']<120]}

    async def get_status(self):
        return {'active':watcher_pid() is not None, 'status':read('.save-watcher-status.json',{})}

    async def test_notification(self):
        process=await asyncio.create_subprocess_exec('/usr/bin/python3',str(BASE/'save_watcher.py'),
                    '--test-notification',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await process.communicate()
        return {'success':process.returncode==0}

    async def sync_now(self):
        pid=watcher_pid()
        if pid is None:
            return {'success':False,'message':'Watcher не працює.'}
        os.kill(pid,signal.SIGUSR1)
        return {'success':True,'message':'Перевірку синхронізації запитано.'}

    async def _main(self):
        pass

    async def _unload(self):
        pass
