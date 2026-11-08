import unittest,tempfile,struct,sys,os,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import save_watcher as w
KEY='USA/Card A/01-GZ2E-gczelda2.gci'
CONFIG={'dusklight_user_dir':'DusklightData','watcher':{'quiet_seconds':4,'cloud_poll_seconds':30}}
def data(tag,t=100):
 b=bytearray(64+8192);b[:6]=b'GZ2E01';b[8:16]=b'gczelda2';struct.pack_into('>H',b,0x38,1);struct.pack_into('>I',b,40,t);b[-1]=tag;return bytes(b)
class Fake:
 def __init__(self,remote=None,when=1_000_000_000):
  self.entries={KEY:{'data':remote,'sha':'old','modified_ns':when}} if remote else {}
  self.uploads=[];self.calls=0;self.fail=False;self.during=None
 def snapshot(self):
  self.calls+=1
  if self.fail:raise OSError('offline')
  if self.during:self.during()
  return self.entries
 def upload(self,key,value,sha,when):
  self.uploads.append((key,value,sha,when));self.entries[key]={'data':value,'sha':'new','modified_ns':when}
 def invalidate(self):pass
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve()
  self.root=self.base/'DusklightData';self.path=self.root/KEY;self.path.parent.mkdir(parents=True)
  self.game=patch.object(w,'game_running',return_value=False);self.game.start()
  self.notifications=patch('watcher_notifications.emit');self.notice=self.notifications.start()
 def tearDown(self):self.notifications.stop();self.game.stop();self.temp.cleanup()
 def put(self,value,when):
  self.path.write_bytes(value);os.utime(self.path,ns=(when,when));return w.read_stable(self.path)
 def sync(self,cloud,local,**kw):return w.sync_once(self.base,CONFIG,cloud,{KEY:local} if local else {},**kw)
 def test_new_local_upload_while_game_open(self):
  c=Fake(data(1));local=self.put(data(2),2_000_000_000)
  with patch.object(w,'game_running',return_value=True):r=self.sync(c,local,allow_pull=False)
  self.assertEqual(r['uploaded'],[KEY]);self.assertEqual(c.uploads[0][1],data(2))
 def test_new_cloud_pull_restores_source_date(self):
  c=Fake(data(2),3_000_000_000);local=self.put(data(1),2_000_000_000)
  r=self.sync(c,local);self.assertEqual(r['downloaded'],[KEY]);self.assertEqual(self.path.read_bytes(),data(2))
  self.assertEqual(self.path.stat().st_mtime_ns,3_000_000_000)
  self.assertTrue(any(p.read_bytes()==data(1) for p in (self.base/'save-backups').rglob('*.gci')))
 def test_cloud_pull_defers_until_closed(self):
  c=Fake(data(2),3_000_000_000);local=self.put(data(1),2_000_000_000)
  r=self.sync(c,local,allow_pull=False);self.assertEqual(r['deferred'],[KEY]);self.assertEqual(self.path.read_bytes(),data(1))
  self.assertEqual(self.sync(c,local)['downloaded'],[KEY])
 def test_remote_older_never_replaces_new_local_even_with_old_baseline(self):
  c=Fake(data(1),1_000_000_000);local=self.put(data(2),2_000_000_000)
  w.write_json(self.base/'.save-sync-state.json',{KEY:w.fingerprint(data(2))})
  self.assertEqual(self.sync(c,local)['uploaded'],[KEY])
 def test_file_changed_during_network_never_uploaded_or_overwritten(self):
  c=Fake(data(1));local=self.put(data(2),2_000_000_000)
  c.during=lambda:self.put(data(3),3_000_000_000)
  r=self.sync(c,local);self.assertEqual(r['unstable'],[KEY]);self.assertFalse(c.uploads);self.assertEqual(self.path.read_bytes(),data(3))
 def test_equal_content_no_duplicate_upload_or_touch(self):
  c=Fake(data(1));local=self.put(data(1),2_000_000_000)
  r=self.sync(c,local);self.assertFalse(c.uploads);self.assertFalse(r['downloaded']);self.assertEqual(self.path.stat().st_mtime_ns,local.modified_ns)
 def test_equal_time_divergence_preserves_both_once(self):
  c=Fake(data(1),2_000_000_000);local=self.put(data(2),2_000_000_000)
  for _ in range(2):self.assertEqual(self.sync(c,local)['conflicts'],[KEY])
  self.assertEqual(len(list((self.base/'save-backups').rglob('*.gci'))),2);self.assertEqual(self.path.read_bytes(),data(2))
 def test_partial_or_recent_write_waits_for_quiet_period(self):
  c=Fake();clock=[0];self.put(data(1),2_000_000_000)
  worker=w.Watcher(self.base,CONFIG,c,clock=lambda:clock[0]);worker.tick()
  clock[0]=2;self.put(data(2),3_000_000_000);worker.tick();self.assertFalse(c.uploads)
  clock[0]=5;worker.tick();self.assertFalse(c.uploads)
  clock[0]=6;worker.tick();self.assertEqual(c.uploads[0][1],data(2))
 def test_offline_retries_unchanged_file_without_new_save(self):
  c=Fake();c.fail=True;clock=[0];self.put(data(1),2_000_000_000)
  worker=w.Watcher(self.base,CONFIG,c,clock=lambda:clock[0]);worker.tick();clock[0]=4;worker.tick()
  self.assertFalse(c.uploads);c.fail=False;clock[0]=20;worker.tick();self.assertEqual(len(c.uploads),1)
 def test_restart_finds_unsent_file(self):
  c=Fake(data(1));self.put(data(2),2_000_000_000);clock=[0]
  worker=w.Watcher(self.base,CONFIG,c,clock=lambda:clock[0]);worker.scan()
  clock[0]=5;worker.tick();self.assertEqual(len(c.uploads),1)
 def test_short_network_glitch_does_not_show_failure_popup(self):
  c=Fake(data(1));c.fail=True;clock=[0];self.put(data(1),2_000_000_000)
  worker=w.Watcher(self.base,CONFIG,c,clock=lambda:clock[0]);worker.tick();clock[0]=4;worker.tick()
  self.assertFalse(self.notice.called)
  c.fail=False;clock[0]=20;worker.tick();self.assertFalse(self.notice.called)
 def test_sustained_offline_popup_once_and_recovery_confirmed(self):
  c=Fake(data(1));c.fail=True;clock=[0];self.put(data(1),2_000_000_000)
  worker=w.Watcher(self.base,CONFIG,c,clock=lambda:clock[0]);worker.tick()
  for moment in [4,20,51]:clock[0]=moment;worker.tick()
  self.assertEqual([x.args[1] for x in self.notice.call_args_list],['error'])
  c.fail=False;clock[0]=112;worker.tick()
  self.assertEqual([x.args[1] for x in self.notice.call_args_list],['error','recovered'])
 def test_local_lock_blocks_download_during_launcher_startup(self):
  self.game.stop()
  with (self.base/'.launcher.lock').open('a') as lock:
   w.fcntl.flock(lock,w.fcntl.LOCK_EX|w.fcntl.LOCK_NB)
   self.assertTrue(w.game_running(self.base))
  self.game=patch.object(w,'game_running',return_value=False);self.game.start()
 def test_malformed_path_is_rejected(self):
  c=Fake();c.entries={'../../evil.gci':{'data':data(1),'sha':'bad','modified_ns':1}}
  with self.assertRaises(w.SyncError):self.sync(c,None)
if __name__=='__main__':unittest.main(verbosity=2)
