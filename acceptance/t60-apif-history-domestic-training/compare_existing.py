"""Run unmodified train functions with identical complete parameters and inputs."""
import hashlib,json,os,shutil,sqlite3,sys
from datetime import UTC,datetime,timedelta
from pathlib import Path
from zucai.predict.command import train
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes

ROOT=Path('/workspace/work/t60-evidence');DB=ROOT/'static.db'
PARAMS=Path('/workspace/work/football-t60/data/model/params.json')
CUTOFF=datetime(2026,11,6,tzinfo=UTC)
label=sys.argv[1];out=ROOT/label;out.mkdir(exist_ok=False)
common=ROOT/'same-output';common.mkdir(exist_ok=True)
config=json.loads(PARAMS.read_text());leagues={k:v for k,v in config['leagues'].items() if k not in {'韩职','澳超'}}
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
initial={'database_sha256':sha(DB),'params_sha256':sha(PARAMS),'params_path':str(PARAMS),'cutoff_utc':CUTOFF.isoformat(),'since_utc':(CUTOFF-timedelta(days=56)).isoformat(),'pythonhashseed':os.environ['PYTHONHASHSEED']}
summary={'inputs':initial,'models':{}}
c=sqlite3.connect(f'file:{DB}?mode=ro',uri=True);c.row_factory=sqlite3.Row
for league,entry in leagues.items():
 print('START',label,league,datetime.now(UTC).isoformat(),flush=True)
 output=common/f'{league}.pkl'
 result=train(db=DB,league=league,source=','.join(entry['sources']),since=CUTOFF-timedelta(days=56),as_of=CUTOFF,output=output,params_file=PARAMS)
 training=common/f'{league}.train.json';training.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
 model=DixonColes.load(output)
 records=[];covered=[]
 for target in entry.get('leagues',[league]):
  rows=c.execute('''select m.match_id,m.source,m.league,m.kickoff_utc,h.name home,a.name away from matches m join teams h on h.id=m.home_team_id join teams a on a.id=m.away_team_id where m.league=? and m.kickoff_utc>? order by m.kickoff_utc,m.match_id''',(target,CUTOFF.isoformat()))
  successes=0
  for raw in rows:
   row=dict(raw)
   if row['source'] not in model.sources:continue
   try:
    grid,metadata=model.predict_with_metadata(row)
    records.append({**row,'model_probabilities':had(grid),'score_matrix':grid.tolist(),'metadata':metadata});successes+=1
   except ValueError as exc:
    records.append({**row,'error':str(exc)})
   if successes==5:break
  if successes:covered.append(target)
  else:raise AssertionError(f'No actual future target predicted for {target}')
 probabilities=common/f'{league}.probabilities.json';probabilities.write_text(json.dumps(records,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
 artifacts={}
 for path in (output,training,probabilities):
  artifacts[path.suffix if path.suffix=='.pkl' else '.train.json' if path.name.endswith('.train.json') else '.probabilities.json']=sha(path)
  shutil.copy2(path,out/path.name)
 summary['models'][league]={'artifacts':artifacts,'training_rows':result['training_rows'],'sources':result['sources'],'targets':covered,'probability_records':len(records)}
 (out/'manifest.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
 print('DONE',label,league,artifacts,flush=True)
assert sha(DB)==initial['database_sha256'] and sha(PARAMS)==initial['params_sha256']
print('COMPLETE',label,len(summary['models']),sum(len(v['targets']) for v in summary['models'].values()),flush=True)
