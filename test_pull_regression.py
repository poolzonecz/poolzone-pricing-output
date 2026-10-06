import base64,hashlib,importlib.util,io,json,secrets,tempfile,unittest,zipfile
from pathlib import Path
from openpyxl import Workbook
from msoffcrypto.format.ooxml import OOXMLFile
spec=importlib.util.spec_from_file_location('pull_output',Path(__file__).resolve().parent/'pull_output.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
FILES=p.FILES
class Fake:
 def __init__(self,fail=False):self.calls=[];self.ref='old';self.message='pricing: publish 100\n';self.entries=[];self.fail=fail
 def call(self,m,path,body=None,missing=False):
  self.calls.append((m,path,body))
  if path=='':return {'full_name':p.TARGET,'private':False}
  if path=='/git/ref/heads/main':return {'object':{'sha':self.ref}}
  if path.startswith('/git/commits/') :return {'message':self.message,'tree':{'sha':'tree'}}
  if path.startswith('/git/trees/') :return {'tree':self.entries}
  if path=='/git/blobs':
   data=base64.b64decode(body['content']);return {'sha':hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()}
  if path=='/git/trees':self.entries=body['tree'];return {'sha':'tree'}
  if path=='/git/commits':self.message=body['message'];return {'sha':'new'}
  if path=='/git/refs/heads/main':
   if self.fail:raise ValueError('injected')
   self.ref=body['sha'];return {}
  raise AssertionError(path)
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.payload={}
  for name in FILES:
   workbook=Workbook();workbook.active['A1']='offline fixture';plain=io.BytesIO();workbook.save(plain);workbook.close();encrypted=io.BytesIO();OOXMLFile(io.BytesIO(plain.getvalue())).encrypt(secrets.token_urlsafe(32),encrypted);cls.payload[name]=encrypted.getvalue()
  cls.fixture_run={'id':123,'run_attempt':1,'head_sha':'a'*40,'head_branch':'main','status':'completed','conclusion':'success','event':'workflow_dispatch','path':'.github/workflows/pricing.yml','repository':{'full_name':p.SOURCE},'head_repository':{'full_name':p.SOURCE}}
 @classmethod
 def tearDownClass(cls):pass
 def archive(self,payload):
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w') as z:
   for n,v in payload.items():z.writestr(n,v)
  blob=buf.getvalue();art={'id':9,'name':'pricing-encrypted-123-1','expired':False,'workflow_run':{'id':123,'head_sha':'a'*40,'head_branch':'main'},'digest':'sha256:'+hashlib.sha256(blob).hexdigest()};return blob,art
 def test_six_names_agile_encrypted(self):
  b,a=self.archive(self.payload);self.assertEqual(p.validate_artifact(b,a,self.fixture_run),self.payload)
 def test_incomplete_block(self):
  b,a=self.archive({k:v for k,v in self.payload.items() if k!=FILES[0]})
  with self.assertRaises(ValueError):p.validate_artifact(b,a,self.fixture_run)
 def test_extra_block(self):
  b,a=self.archive({**self.payload,'README.md':b'not allowed'})
  with self.assertRaises(ValueError):p.validate_artifact(b,a,self.fixture_run)
 def test_wrong_filename_block(self):
  x=dict(self.payload);x['wrong.xlsx']=x.pop(FILES[0]);b,a=self.archive(x)
  with self.assertRaises(ValueError):p.validate_artifact(b,a,self.fixture_run)
 def test_corrupted_digest_block(self):
  b,a=self.archive(self.payload)
  with self.assertRaises(ValueError):p.validate_artifact(b+b'corruption',a,self.fixture_run)
 def test_plaintext_block(self):
  b,a=self.archive({**self.payload,FILES[0]:b'not encrypted'})
  with self.assertRaises(ValueError):p.validate_artifact(b,a,self.fixture_run)
 def test_run_consistency(self):
  b,a=self.archive(self.payload);a['workflow_run']['id']=124
  with self.assertRaises(ValueError):p.validate_artifact(b,a,self.fixture_run)
 def test_unapproved_failure_block(self):
  p.check_run(self.fixture_run,'a'*40)
  with self.assertRaises(ValueError):p.check_run({**self.fixture_run,'conclusion':'failure'},'a'*40)
  with self.assertRaises(ValueError):p.check_run(self.fixture_run,'b'*40)
 def test_atomic_duplicate_no_new_commit(self):
  b,a=self.archive(self.payload);target=Fake();p.publish(p.validate_artifact(b,a,self.fixture_run),self.fixture_run,a,target)
  self.assertEqual(target.ref,'new');self.assertEqual({x['path'] for x in target.entries},set(FILES));self.assertEqual(sum(m=='PATCH' for m,_,_ in target.calls),1)
  target.calls=[];result=p.publish(self.payload,self.fixture_run,a,target);self.assertTrue(result['DUPLICATE_RUN']);self.assertFalse(any(m!='GET' for m,_,_ in target.calls))
 def test_failed_ref_preserves_old(self):
  b,a=self.archive(self.payload);target=Fake(True)
  with self.assertRaises(ValueError):p.publish(self.payload,self.fixture_run,a,target)
  self.assertEqual(target.ref,'old')
 def test_private_api_write_blocked(self):
  client=p.API(p.SOURCE,secrets.token_urlsafe(24))
  with self.assertRaises(ValueError):client.call('POST','/git/refs',{})
if __name__=='__main__':unittest.main()
