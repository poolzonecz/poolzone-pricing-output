import io,zipfile,hashlib,unittest,os
from pathlib import Path
import pull_output as p
from test_pull_regression import Tests as Fixtures,Fake

class Source:
 def __init__(self,run,blob,artifact,missing=False):self.run=run;self.blob=blob;self.artifact=artifact;self.missing=missing;self.downloads=0
 def call(self,m,path,body=None,missing=False):
  assert m=='GET'
  if '/actions/workflows/' in path:return {'workflow_runs':[self.run]}
  if path.endswith('/artifacts?per_page=100'):return {'artifacts':[] if self.missing else [self.artifact]}
  if '/actions/artifacts/' in path:return self.artifact
  if '/actions/runs/' in path:return self.run
  raise AssertionError(path)
 def archive(self,i):self.downloads+=1;assert i==self.artifact['id'];return self.blob

class Automation(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  Fixtures.setUpClass();obj=Fixtures();cls.blob,cls.art=obj.archive(Fixtures.payload);cls.fixture_run=Fixtures.fixture_run
 @classmethod
 def tearDownClass(cls):Fixtures.tearDownClass()
 def source(self,**kw):return Source({**self.fixture_run,**kw},self.blob,self.art)
 def test_success_publish(self):
  t=Fake();r=p.process(self.source(),t);self.assertEqual(r['PUBLIC_PUBLISH'],'YES');self.assertEqual(t.ref,'new');self.assertEqual(len(t.entries),6)
 def test_in_progress(self):
  t=Fake();p.process(self.source(status='in_progress',conclusion=None),t);self.assertEqual(t.calls,[])
 def test_queued(self):
  t=Fake();p.process(self.source(status='queued',conclusion=None),t);self.assertEqual(t.calls,[])
 def test_failure(self):
  t=Fake();p.process(self.source(conclusion='failure'),t);self.assertEqual(t.calls,[])
 def test_cancelled(self):
  t=Fake();p.process(self.source(conclusion='cancelled'),t);self.assertEqual(t.calls,[])
 def test_skipped(self):
  t=Fake();p.process(self.source(conclusion='skipped'),t);self.assertEqual(t.calls,[])
 def test_same_run(self):
  t=Fake();t.message='pricing: publish 123\n';s=self.source();p.process(s,t);self.assertEqual(s.downloads,0);self.assertFalse(any(m!='GET' for m,_,_ in t.calls))
 def test_older_run(self):
  t=Fake();t.message='pricing: publish 124\n';p.process(self.source(),t);self.assertEqual(t.ref,'old');self.assertFalse(any(m!='GET' for m,_,_ in t.calls))
 def test_newer_run(self):self.test_success_publish()
 def test_missing_artifact(self):
  t=Fake();s=self.source();s.missing=True
  with self.assertRaises(ValueError):p.process(s,t)
  self.assertEqual(t.ref,'old');self.assertFalse(any(m!='GET' for m,_,_ in t.calls))
 def test_incomplete_set(self):
  t=Fake();obj=Fixtures();b,a=obj.archive({k:v for k,v in Fixtures.payload.items() if k!=p.FILES[0]})
  with self.assertRaises(ValueError):p.process(Source(self.fixture_run,b,a),t)
  self.assertEqual(t.ref,'old')
 def test_wrong_encryption(self):
  t=Fake();obj=Fixtures();b,a=obj.archive({**Fixtures.payload,p.FILES[0]:b'plaintext'})
  with self.assertRaises(ValueError):p.process(Source(self.fixture_run,b,a),t)
  self.assertEqual(t.ref,'old')
 def test_failed_commit(self):
  class Fail(Fake):
   def call(self,m,path,body=None,missing=False):
    if path=='/git/commits':raise ValueError('Commit rejected')
    return super().call(m,path,body,missing)
  t=Fail()
  with self.assertRaises(ValueError):p.process(self.source(),t)
  self.assertEqual(t.ref,'old')
 def test_unknown_published_state(self):
  t=Fake();t.message='unknown'
  with self.assertRaises(ValueError):p.process(self.source(),t)
  self.assertEqual(t.ref,'old')
 def test_timezone(self):
  import yaml
  path=os.environ.get('PRIVATE_WORKFLOW_PATH')
  if not path:self.skipTest('Set PRIVATE_WORKFLOW_PATH to audit the private schedule offline')
  w=yaml.load(Path(path).read_text(),Loader=yaml.BaseLoader)
  self.assertEqual(w['on']['schedule'],[{'cron':'0 22 * * *','timezone':'Europe/Prague'}])
 def test_polling(self):
  import yaml
  w=yaml.load((Path(__file__).parent/'.github/workflows/pull.yml').read_text(),Loader=yaml.BaseLoader)
  self.assertEqual(w['on']['schedule'],[{'cron':'*/5 * * * *'}]);self.assertNotIn('if',w['jobs']['pull']);self.assertEqual(w['permissions'],{'contents':'write'})
