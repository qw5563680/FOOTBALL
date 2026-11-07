"""Owner-authorized T48 publication, executed under the existing Actions write mutex.
No sports requests, no training, no model/ledger changes, no forced updates.
"""
import base64,concurrent.futures,hashlib,json,os,shutil,socket,sqlite3,sys,zipfile
from pathlib import Path
from unittest.mock import patch
from github_publication_client import GitHub,dump,digest
ROOT=Path.cwd();E=ROOT/'publication-evidence';E.mkdir(exist_ok=True)
J={'status':'started','production_ref_updated':False,'sports_requests':0,'uploads':[]}
g=GitHub()
def save():dump(E/'publication-result.json',J)
def require(value,message):
 if not value:raise RuntimeError(message)
def sha(b):return hashlib.sha256(b).hexdigest()
def blobsha(b):return hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def main():
 bundle=ROOT/'verified_candidate.zip';require(digest(bundle)=='d49571447dc2350334b2c91fb1c40d4790d9cfd683a68047c3fe0965763873aa','Candidate ZIP changed')
 with zipfile.ZipFile(bundle) as z:
  inventory=json.loads(z.read('FILES_SHA256.json'))
  require(set(z.namelist())==set(inventory)|{'FILES_SHA256.json'},'Bundle inventory differs')
  for name,meta in inventory.items():
   p=Path(name);require(not p.is_absolute() and '..' not in p.parts,'Unsafe bundle path')
   b=z.read(name);require(sha(b)==meta['sha256'] and len(b)==meta['size'],'Bundle hash mismatch '+name)
  z.extractall(ROOT/'candidate')
 candidate=ROOT/'candidate';plan=read(candidate/'publication-plan.json');expected=read(ROOT/'expected.json')
 head=plan['expected_production_head'];code=plan['code_commit']
 require(g.get('/git/ref/heads/main')['object']['sha']==code,'Main changed; rerun review')
 require(g.get('/git/ref/heads/data/football-feeds')['object']['sha']==head,'Production advanced; rebuild on latest')
 before=g.tree(head);dump(E/'production-tree-before.json',before)
 require(g.get('/git/commits/'+head)['tree']['sha']==expected['original_tree_sha'],'Original production tree differs')
 J.update({'original_head':head,'code_commit':code,'run_id':os.environ['GITHUB_RUN_ID'],'run_url':'https://github.com/qw5563680/FOOTBALL/actions/runs/'+os.environ['GITHUB_RUN_ID']});save()
 print('Pinned production/code heads verified',flush=True)
 release=g.get('/releases/tags/stats-raw-202611');assets=g.pages('/releases/'+str(release['id'])+'/assets');index={a['name']:a for a in assets}
 cache=ROOT/'asset-cache';cache.mkdir()
 # Append-only immutable assets. Existing different bytes are a hard stop, never delete/replace.
 for want in plan['new_release_assets']:
  name=want['name'];local=candidate/'release_upload'/want['tag']/name
  require(digest(local)==want['sha256'] and local.stat().st_size==want['size'],'Upload source differs '+name)
  created=False
  if name not in index:
   url=release['upload_url'].split('{')[0]+'?name='+name
   a=g.request(url,'POST',local.read_bytes(),content_type='application/octet-stream')[0];index[name]=a;created=True
  a=index[name];require(a['size']==want['size'],'Existing asset size differs '+name)
  b=g.get(a['url'],accept='application/octet-stream');require(sha(b)==want['sha256'] and len(b)==want['size'],'Uploaded readback differs '+name)
  (cache/name).write_bytes(b);J['uploads'].append({'name':name,'asset_id':a['id'],'sha256':sha(b),'size':len(b),'newly_uploaded':created,'readback_verified':True});save()
  print('Uploaded/read back '+name,flush=True)
 base_manifest=json.loads(g.raw('feed_manifest.json',head));feed=ROOT/'candidate-feed';feed.mkdir()
 def base_file(item):
  name,h=item;p=feed/name;p.parent.mkdir(parents=True,exist_ok=True);b=g.raw(name,head);require(sha(b)==h,'Current feed hash differs '+name);p.write_bytes(b)
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(base_file,base_manifest['files'].items()))
 for p in (candidate/'feed_updates').rglob('*'):
  if p.is_file():dest=feed/p.relative_to(candidate/'feed_updates');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
 manifest=read(feed/'feed_manifest.json')
 require(manifest['counts']==base_manifest['counts'],'Core counts changed')
 for name,h in manifest['files'].items():require(digest(feed/name)==h,'Candidate feed hash differs '+name)
 tech=manifest['technical_data'];needed=tech['row_shards']+tech['injury_receipts']
 for tag in {x['tag'] for x in needed}:
  if tag!=release['tag_name']:
   other=g.get('/releases/tags/'+tag)
   for a in g.pages('/releases/'+str(other['id'])+'/assets'):
    require(a['name'] not in index or index[a['name']]['id']==a['id'],'Ambiguous asset name across tags')
    index[a['name']]=a
 def asset(want):
  p=cache/want['name']
  if not p.exists():p.write_bytes(g.get(index[want['name']]['url'],accept='application/octet-stream'))
  require(digest(p)==want['sha256'] and p.stat().st_size==want['size'],'Technical asset differs '+want['name'])
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(asset,needed))
 print('All current data and full-history assets verified',flush=True)
 sys.path[:0]=[str(ROOT/'code/src'),str(ROOT/'code/examples')]
 import t48_verify_replay as v
 from zucai.cloud_feeds import restore_feeds
 from zucai.stats_rows import restore_rows
 class LocalRelease:
  def download(self,tag,name,dest):
   from zucai.stats_release import validate_tag,validate_name
   validate_tag(tag);validate_name(name);Path(dest).parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(cache/name,dest);return dest
 def check_database(directory,label):
  database=ROOT/label/'input.db'
  with patch.object(socket.socket,'connect',v.deny_network),patch.object(socket,'create_connection',v.deny_network):
   restore_feeds(directory,database);restored=restore_rows(database,directory,release=LocalRelease())
   actual=v.canonical_tables(database);features=v.build_features(database,expected['targets'])
   dump(E/(label+'-tables.json'),actual);dump(E/(label+'-features.json'),features)
   require(actual==expected['tables'],label+' database contents differ from fixed acceptance')
   require(features==expected['features'],label+' target features differ')
   with sqlite3.connect(database) as db:
    require(db.execute('PRAGMA integrity_check').fetchall()==[('ok',)],'Database integrity failed')
    require(not db.execute('PRAGMA foreign_key_check').fetchall(),'Foreign keys failed')
  J[label]={'all_14_tables_exact':True,'all_21_target_features_exact':True,'integrity_ok':True,'foreign_keys_clean':True,'restored_counts':restored};save()
 check_database(feed,'prepublication')
 print('Linux prepublication restore passed, without compatibility adapters',flush=True)
 require(g.get('/git/ref/heads/data/football-feeds')['object']['sha']==head,'Production advanced before backup')
 backup='backup/t48-production-20261108-'+head[:7]
 refs=g.get('/git/matching-refs/heads/'+backup)
 if refs:require(len(refs)==1 and refs[0]['ref']=='refs/heads/'+backup and refs[0]['object']['sha']==head,'Backup ref conflict')
 else:g.request('/git/refs','POST',{'ref':'refs/heads/'+backup,'sha':head})
 J['backup_branch']=backup;save()
 entries=[]
 for name in expected['allowed_changes']:
  b=(candidate/'feed_updates'/name).read_bytes();item=g.request('/git/blobs','POST',{'content':base64.b64encode(b).decode(),'encoding':'base64'})[0]
  require(item['sha']==blobsha(b),'Created Git blob differs '+name)
  entries.append({'path':name,'type':'blob','mode':'100644','sha':item['sha']})
 tree=g.request('/git/trees','POST',{'base_tree':expected['original_tree_sha'],'tree':entries})[0]
 after=g.tree(tree['sha']);dump(E/'production-tree-after.json',after)
 def files(t):return {x['path']:{k:x[k] for k in ('type','mode','sha')} for x in t['tree'] if x['type']!='tree'}
 old,new=files(before),files(after);changed=sorted(p for p in old.keys()|new.keys() if old.get(p)!=new.get(p))
 require(changed==sorted(expected['allowed_changes']),'Unexpected production tree changes: '+str(changed))
 protected={p:a for p,a in old.items() if p not in changed};require(all(new.get(p)==a for p,a in protected.items()),'Protected file changed')
 J.update({'changed_files':changed,'protected_files_verified':len(protected),'prediction_models_ledgers_all_preserved':True});save()
 commit=g.request('/git/commits','POST',{'message':'Publish T48 verified historical statistics; preserve current predictions and ledgers','tree':tree['sha'],'parents':[head]})[0]
 J['prepared_commit']=commit['sha'];save()
 require(g.get('/git/ref/heads/main')['object']['sha']==code,'Main changed before publication')
 require(g.get('/git/ref/heads/data/football-feeds')['object']['sha']==head,'Production changed before publication')
 # Fast-forward-only update: a racing commit makes this fail, never force-overwrite.
 g.request('/git/refs/heads/data/football-feeds','PATCH',{'sha':commit['sha'],'force':False})
 J.update({'production_ref_updated':True,'published_commit':commit['sha'],'published_manifest_sha256':plan['candidate_manifest_sha256']});save()
 require(g.get('/git/ref/heads/data/football-feeds')['object']['sha']==commit['sha'],'Live branch differs after update')
 live=ROOT/'live-readback-feed';live.mkdir()
 live_manifest=g.raw('feed_manifest.json',commit['sha']);require(sha(live_manifest)==plan['candidate_manifest_sha256'],'Live manifest mismatch');(live/'feed_manifest.json').write_bytes(live_manifest)
 def live_file(item):
  name,h=item;b=g.raw(name,commit['sha']);require(sha(b)==h,'Live file mismatch '+name);p=live/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(live_file,manifest['files'].items()))
 # Independently download the newly published shards again for the live restore.
 for want in plan['new_release_assets']:
  b=g.get(index[want['name']]['url'],accept='application/octet-stream');require(sha(b)==want['sha256'],'Final live asset mismatch');(cache/want['name']).write_bytes(b)
 check_database(live,'online-readback')
 require(g.get('/git/ref/heads/data/football-feeds')['object']['sha']==commit['sha'],'Unexpected concurrent external update')
 J.update({'status':'published_and_online_readback_passed','next_normal_run_verified':False,'business_source_modified':False,'linux_unmodified_restore':True,'whole_production_tree_protected':True});save()
 print(json.dumps(J,ensure_ascii=False,indent=2),flush=True)
 with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:
  f.write('# T48 publication and online readback passed\n\nCommit: `'+commit['sha']+'`\n\nBackup: `'+backup+'`\n\nOnly four planned data files changed; '+str(len(protected))+' other files preserved. All 14 tables and 21 frozen target features match. No sports requests, training, or ledger updates. Next normal run is NOT yet verified.\n')
if __name__=='__main__':
 try:main()
 except Exception as e:
  J['status']='failed';J['error']=str(e).replace(g.token,'[REDACTED]');save();print(json.dumps(J,ensure_ascii=False,indent=2),flush=True);raise
