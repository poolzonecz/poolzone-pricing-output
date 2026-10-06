"""Public-repo pull only: private Actions READ token, own-repo WRITE token."""
import base64, hashlib, io, json, os, re, zipfile
from pathlib import Path
import requests, olefile
SOURCE='poolzonecz/poolzone-pricing'
TARGET='poolzonecz/poolzone-pricing-output'
FILES=('ND_cenotvorba.xlsx','technologie_cenotvorba.xlsx','chemie_cenotvorba.xlsx','bazeny_cenotvorba.xlsx','MOC_cenotvorba.xlsx','vyjimky_cenotvorba.xlsx')
MAX_ZIP=100_000_000
MAX_FILE=50_000_000

def check_run(run, approved_sha):
    if (run.get('conclusion')!='success' or run.get('status')!='completed' or
        run.get('path')!='.github/workflows/pricing.yml' or run.get('head_branch')!='main' or
        run.get('event') not in ('schedule','workflow_dispatch') or
        run.get('repository',{}).get('full_name')!=SOURCE or
        run.get('head_repository',{}).get('full_name')!=SOURCE or
        run.get('head_sha')!=approved_sha):
        raise ValueError('Unapproved pricing run')
    if not re.fullmatch(r'[0-9a-f]{40}',approved_sha):raise ValueError('Approved commit SHA required')

def validate_artifact(blob,artifact,run):
    expected='pricing-encrypted-'+str(run['id'])+'-'+str(run['run_attempt'])
    if artifact.get('name')!=expected or artifact.get('expired'):
        raise ValueError('Artifact identity invalid')
    provenance=artifact.get('workflow_run',{})
    if provenance.get('id')!=run['id'] or provenance.get('head_sha')!=run['head_sha'] or provenance.get('head_branch')!='main':
        raise ValueError('Artifact run mismatch')
    digest=artifact.get('digest','')
    if not digest.startswith('sha256:') or hashlib.sha256(blob).hexdigest()!=digest[7:]:
        raise ValueError('Artifact integrity mismatch')
    if len(blob)>MAX_ZIP:raise ValueError('Artifact too large')
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        infos=archive.infolist()
        if len(infos)!=6 or {i.filename for i in infos}!=set(FILES):raise ValueError('Six exact files required')
        result={}
        for info in infos:
            if info.is_dir() or info.file_size>MAX_FILE or ((info.external_attr>>16)&0o170000)==0o120000:
                raise ValueError('Invalid archive member')
            data=archive.read(info)  # ZIP CRC validation included.
            if not olefile.isOleFile(io.BytesIO(data)) or zipfile.is_zipfile(io.BytesIO(data)):
                raise ValueError('Plaintext/non-Office payload rejected')
            with olefile.OleFileIO(io.BytesIO(data)) as office:
                if not office.exists('EncryptionInfo') or not office.exists('EncryptedPackage'):
                    raise ValueError('Required Office encrypted streams missing')
                encryption=office.openstream('EncryptionInfo').read()
                if encryption[:4]!=b'\x04\x00\x04\x00':raise ValueError('ECMA-376 Agile required')
                from xml.etree import ElementTree as ET
                root=ET.fromstring(encryption[8:])
                ns={'e':'http://schemas.microsoft.com/office/2006/encryption','p':'http://schemas.microsoft.com/office/2006/keyEncryptor/password'}
                key=root.find('e:keyData',ns);password_key=root.find('.//p:encryptedKey',ns)
                if key is None or password_key is None or key.get('cipherAlgorithm')!='AES' or key.get('cipherChaining')!='ChainingModeCBC':
                    raise ValueError('Password-encrypted Agile structure required')
                if len(office.openstream('EncryptedPackage').read())<24:raise ValueError('Truncated encrypted package')
            result[info.filename]=data
    return result

class API:
    def __init__(self,repository,token):
        if repository not in (SOURCE,TARGET) or not token:raise ValueError('Missing scoped authentication')
        self.repository=repository;self.session=requests.Session()
        self.session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'})
    def call(self,method,path,body=None,missing=False):
        if self.repository==SOURCE and method!='GET':raise ValueError('Private source is read only')
        r=self.session.request(method,'https://api.github.com/repos/'+self.repository+path,json=body,timeout=60,allow_redirects=False)
        if missing and r.status_code in (404,409):return None
        if not 200<=r.status_code<300:raise ValueError('GitHub API failed')
        return r.json()
    def archive(self,artifact_id):
        if self.repository!=SOURCE:raise ValueError('Wrong artifact source')
        # requests strips Authorization on redirect to the signed artifact host.
        with self.session.get('https://api.github.com/repos/'+SOURCE+'/actions/artifacts/'+str(artifact_id)+'/zip',stream=True,timeout=60) as r:
            if r.status_code!=200:raise ValueError('Artifact download failed')
            data=bytearray()
            for chunk in r.iter_content(65536):
                data.extend(chunk)
                if len(data)>MAX_ZIP:raise ValueError('Artifact download too large')
        return bytes(data)

def publish(payload,run,artifact,target):
    if set(payload)!=set(FILES):raise ValueError('Complete six-file payload required')
    identity=f"pricing-source-run: {run['id']} attempt: {run['run_attempt']} artifact: {artifact['id']} digest: {artifact['digest']}"
    repo=target.call('GET','')
    if repo.get('full_name')!=TARGET or repo.get('private') is not False:raise ValueError('Wrong public destination')
    ref=target.call('GET','/git/ref/heads/main',missing=True)
    parent=ref['object']['sha'] if ref else None
    if parent:
        old=target.call('GET','/git/commits/'+parent)
        if identity in old.get('message','').splitlines():
            tree=target.call('GET','/git/trees/'+old['tree']['sha']+'?recursive=1')
            if tree.get('truncated') or {e['path'] for e in tree['tree']}!=set(FILES):raise ValueError('Published state altered')
            for entry in tree['tree']:
                expected=hashlib.sha1(b'blob '+str(len(payload[entry['path']])).encode()+b'\0'+payload[entry['path']]).hexdigest()
                if entry.get('sha')!=expected or entry.get('type')!='blob':raise ValueError('Published content altered')
            return {'PUBLIC_PUBLISH':'SKIPPED','DUPLICATE_RUN':True,'COMMIT_SHA':parent}
    entries=[]
    for name in sorted(payload):
        blob=target.call('POST','/git/blobs',{'content':base64.b64encode(payload[name]).decode(),'encoding':'base64'})
        entries.append({'path':name,'mode':'100644','type':'blob','sha':blob['sha']})
    tree=target.call('POST','/git/trees',{'tree':entries})
    commit=target.call('POST','/git/commits',{'message':'pricing: publish '+str(run['id'])+'\n\n'+identity,'tree':tree['sha'],'parents':[parent] if parent else []})
    if parent:target.call('PATCH','/git/refs/heads/main',{'sha':commit['sha'],'force':False})
    else:target.call('POST','/git/refs',{'ref':'refs/heads/main','sha':commit['sha']})
    return {'PUBLIC_PUBLISH':'YES','COMMIT_SHA':commit['sha'],'RUN_ID':run['id'],'FILES':list(FILES)}

def main():
    if os.environ.get('GITHUB_REPOSITORY')!=TARGET:raise ValueError('Public repository only')
    approved=os.environ.get('APPROVED_PRICING_SHA','')
    if not re.fullmatch(r'[0-9a-f]{40}',approved):raise ValueError('Approved pricing SHA not configured')
    source=API(SOURCE,os.environ.get('PRIVATE_ACTIONS_READ_TOKEN'))
    selected=source.call('GET','/actions/runs/37391325560')
    check_run(selected,approved)
    if selected['id']!=37391325560:raise ValueError('Pinned run mismatch')
    artifact=source.call('GET','/actions/artifacts/11389068698')
    if artifact['id']!=11389068698 or artifact['name']!='pricing-encrypted-37391325560-1':raise ValueError('Pinned artifact mismatch')
    payload=validate_artifact(source.archive(artifact['id']),artifact,selected)
    result=publish(payload,selected,artifact,API(TARGET,os.environ.get('PUBLIC_GITHUB_TOKEN')))
    result.update({'APP_AUTHENTICATION':'PASS','ARTIFACT_DOWNLOAD':'PASS','ARTIFACT_INTEGRITY':'PASS','ENCRYPTED_FILES':6,'PLAINTEXT_FILES':0,'NAMES_VALID':'PASS','ENCRYPTION_VALIDATION':'PASS','SOURCE_COMMIT_SHA':selected['head_sha']})
    print(json.dumps(result))
if __name__=='__main__':
    try:main()
    except Exception:
        print('PUBLIC_PUBLISH: NO; artifact/authentication/validation/publication failed')
        raise SystemExit(1)
