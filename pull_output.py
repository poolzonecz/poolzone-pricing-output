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
        marker=re.match(r'^pricing: publish ([1-9][0-9]*)\n',old.get('message',''))
        if not marker:raise ValueError('Published source run unknown')
        if run['id']<=int(marker.group(1)):return {'PUBLIC_PUBLISH':'SKIPPED','REASON':'SAME_OR_OLDER_RUN','DUPLICATE_RUN':run['id']==int(marker.group(1)),'COMMIT_SHA':parent}
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

def latest_run(source):
    runs=source.call('GET','/actions/workflows/pricing.yml/runs?branch=main&per_page=100')['workflow_runs']
    relevant=[r for r in runs if r.get('head_branch')=='main' and r.get('event') in ('schedule','workflow_dispatch')]
    if not relevant:return None
    candidate=max(relevant,key=lambda r:int(r['id']))
    return source.call('GET','/actions/runs/'+str(candidate['id']))

def published_state(target):
    repo=target.call('GET','')
    if repo.get('full_name')!=TARGET or repo.get('private') is not False:raise ValueError('Wrong public destination')
    ref=target.call('GET','/git/ref/heads/main',missing=True)
    if not ref:return None
    commit=target.call('GET','/git/commits/'+ref['object']['sha'])
    match=re.match(r'^pricing: publish ([1-9][0-9]*)\n',commit.get('message',''))
    if not match:raise ValueError('Published source run cannot be determined')
    return int(match.group(1))

def process(source,target):
    selected=latest_run(source)
    if selected is None or selected.get('status')!='completed' or selected.get('conclusion')!='success':
        return {'PUBLIC_PUBLISH':'SKIPPED','REASON':'LATEST_RUN_NOT_SUCCESSFUL'}
    check_run(selected,selected.get('head_sha',''))
    old=published_state(target)
    if old is not None and selected['id']<=old:
        return {'PUBLIC_PUBLISH':'SKIPPED','REASON':'SAME_OR_OLDER_RUN','RUN_ID':selected['id']}
    artifacts=source.call('GET','/actions/runs/'+str(selected['id'])+'/artifacts?per_page=100')['artifacts']
    expected='pricing-encrypted-'+str(selected['id'])+'-'+str(selected['run_attempt'])
    matches=[a for a in artifacts if a.get('name')==expected and not a.get('expired')]
    if len(matches)!=1:raise ValueError('One encrypted artifact from exact run required')
    pinned=matches[0]
    artifact=source.call('GET','/actions/artifacts/'+str(pinned['id']))
    if artifact['id']!=pinned['id'] or artifact['name']!=expected:raise ValueError('Artifact pin mismatch')
    payload=validate_artifact(source.archive(artifact['id']),artifact,selected)
    latest=latest_run(source)
    if latest is None or latest['id']!=selected['id'] or latest['head_sha']!=selected['head_sha'] or latest.get('status')!='completed' or latest.get('conclusion')!='success':
        return {'PUBLIC_PUBLISH':'SKIPPED','REASON':'SOURCE_CHANGED_DURING_PULL'}
    result=publish(payload,selected,artifact,target)
    result.update({'APP_AUTHENTICATION':'PASS','ARTIFACT_DOWNLOAD':'PASS','ARTIFACT_INTEGRITY':'PASS','ENCRYPTED_FILES':6,'PLAINTEXT_FILES':0,'NAMES_VALID':'PASS','ENCRYPTION_VALIDATION':'PASS','SOURCE_COMMIT_SHA':selected['head_sha'],'ARTIFACT_ID':artifact['id'],'ARTIFACT_NAME':artifact['name']})
    return result

def main():
    if os.environ.get('GITHUB_REPOSITORY')!=TARGET:raise ValueError('Public repository only')
    print(json.dumps(process(API(SOURCE,os.environ.get('PRIVATE_ACTIONS_READ_TOKEN')),API(TARGET,os.environ.get('PUBLIC_GITHUB_TOKEN')))))
if __name__=='__main__':
    try:main()
    except Exception:
        print('PUBLIC_PUBLISH: NO; authentication/validation/publication failed; main preserved unless atomic update was already accepted')
        raise SystemExit(1)
