import hashlib,io,json,os,sys,tarfile,urllib.request
from pathlib import Path
base='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']
ref=os.environ['GITHUB_SHA']
headers={'Authorization':'Bearer '+os.environ['FOOTBALL_PUBLISH_TOKEN'],'User-Agent':'football-t48-bootstrap','Accept':'application/vnd.github.raw+json'}
with urllib.request.urlopen(urllib.request.Request(base+'/contents/handoff/github_publication_client.py?ref='+ref,headers=headers),timeout=120) as r:
 Path('github_publication_client.py').write_bytes(r.read())
from github_publication_client import GitHub,digest
client=GitHub()
for name in ['t48_publish.py','verified_candidate.zip','expected.json']:
 Path(name).write_bytes(client.raw('handoff/'+name,ref))
assert digest('verified_candidate.zip')=='d49571447dc2350334b2c91fb1c40d4790d9cfd683a68047c3fe0965763873aa'
code='a664318450675946a578c041cb1f24219981e771'
assert client.get('/git/ref/heads/main')['object']['sha']==code
archive=client.get('/tarball/'+code,accept='application/octet-stream')
Path('code').mkdir()
with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
 members=tar.getmembers();prefix=members[0].name.split('/')[0]+'/'
 for m in members:
  if m.isdir():continue
  assert m.isfile() and m.name.startswith(prefix)
  name=m.name[len(prefix):];assert name and '..' not in Path(name).parts and not Path(name).is_absolute()
  dest=Path('code')/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(tar.extractfile(m).read())
files=[e for e in client.tree(code)['tree'] if e['type']=='blob']
for e in files:
 b=(Path('code')/e['path']).read_bytes()
 assert hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()==e['sha'],e['path']
print('Pinned business source verified:',len(files),'files. No clone, source modification, or sports request.',flush=True)
