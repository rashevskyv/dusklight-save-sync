"""Bounded event feed shared by the watcher, desktop popups and the Decky plugin."""
import time

from platform_support import file_lock, notify
from save_sync import read_json, write_json


def emit(base, kind, body, config=None):
    title = (config or {}).get('notification_title', 'Dusklight — сейви')
    with file_lock(base/'.save-watcher-events.lock'):
        events = read_json(base/'.save-watcher-events.json', {'sequence': 0, 'events': []})
        sequence = events['sequence']+1
        event = {'sequence': sequence, 'kind': kind, 'title': title, 'body': body, 'timestamp': time.time()}
        write_json(base/'.save-watcher-events.json', {'sequence': sequence, 'events': (events['events']+[event])[-100:]})
    if (config or {}).get('notifications', True):
        event['shown'] = notify(title, body, base, tag=kind)
    return event
