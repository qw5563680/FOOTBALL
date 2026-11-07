"""Bounded GitHub client. Credentials only go to API/upload hosts, never redirects."""
import hashlib,json,os,urllib.request,urllib.error,urllib.parse
from pathlib import Path
BASE='https://api.github.com/repos/qw5563680/FOOTBALL'
class SafeRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  new=super().redirect_request(req,fp,code,msg,headers,newurl)
  if new is not None and urllib.parse.urlparse(req.full_url).netloc!=urllib.parse.urlparse(newurl).netloc:
   new.remove_header('Authorization')
  return new
class GithubError(RuntimeError):pass
class GitHub:
 def __init__(self,token=None):
  self.token=token or os.environ['FOOTBALL_PUBLISH_TOKEN'];self.opener=urllib.request.build_opener(SafeRedirect())
 def request(self,path,method='GET',data=None,accept='application/vnd.github+json',content_type=None,binary=False):
  url=path if path.startswith('https://') else BASE+path
  parsed=urllib.parse.urlparse(url)
  assert parsed.hostname in ('api.github.com','uploads.github.com') and not parsed.username
  headers={'Authorization':'Bearer '+self.token,'User-Agent':'football-t48-authorized-publication','Accept':accept,'X-GitHub-Api-Version':'2022-11-28'}
  if data is not None and not isinstance(data,bytes):data=json.dumps(data).encode();content_type='application/json'
  if content_type:headers['Content-Type']=content_type
  req=urllib.request.Request(url,headers=headers,data=data,method=method)
  try:
   with self.opener.open(req,timeout=120) as r:
    b=r.read();h=dict(r.headers)
    if binary or accept=='application/octet-stream' or accept=='application/vnd.github.raw+json':return b,h
    return (json.loads(b) if b else None),h
  except urllib.error.HTTPError as e:
   body=e.read().decode('utf-8','replace').replace(self.token,'[REDACTED]')[:600]
   raise GithubError(f'GitHub {method} {parsed.hostname}{parsed.path}: HTTP {e.code}: {body}') from None
 def get(self,path,**kw):return self.request(path,**kw)[0]
 def pages(self,path):
  result=[];page=1;sep='&' if '?' in path else '?'
  while True:
   b=self.get(f'{path}{sep}per_page=100&page={page}');assert isinstance(b,list);result+=b
   if len(b)<100:return result
   page+=1
 def raw(self,path,commit):return self.get('/contents/'+urllib.parse.quote(path,safe='/')+'?ref='+commit,accept='application/vnd.github.raw+json')
 def tree(self,sha):
  r=self.get('/git/trees/'+sha+'?recursive=1');assert not r['truncated'];return r
 def active(self):
  results={}
  for state in ['in_progress','queued','waiting','pending','requested']:
   page=1
   while True:
    doc=self.get('/actions/runs?status='+state+'&per_page=100&page='+str(page))
    for r in doc['workflow_runs']:results[r['id']]=r
    if len(doc['workflow_runs'])<100:break
    page+=1
  return list(results.values())
def dump(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
