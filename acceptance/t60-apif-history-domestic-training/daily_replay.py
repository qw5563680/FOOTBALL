"""Actual daily workflow, offline, with a proven historical slate in a private copy."""
import hashlib,json,shutil,sqlite3
from datetime import datetime
from pathlib import Path
from zucai import cloud_predict
from zucai.predict.backtest import read_history
from zucai.db import get_engine
from sqlalchemy.orm import Session
ROOT=Path('/workspace/work/t60-evidence');SOURCE=Path('/workspace/work/t60-fixed-archive');COPY=ROOT/'replay-archive'
PARAMS=Path('/workspace/work/football-t60/data/model/params.json')
CUTOFF=datetime.fromisoformat('2026-11-06T03:39:54.531777+00:00')
RUN='t60-offline-historical-20261106'
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
def snapshot():return {str(p.relative_to(SOURCE)):sha(p) for p in sorted(SOURCE.rglob('*')) if p.is_file()}
before=snapshot();(ROOT/'source-archive-before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2)+'\n')
shutil.copytree(SOURCE,COPY)
prior=SOURCE/'forecasts/2026-11-07/42157651542-1/forecast.json'
proof=json.loads(prior.read_text());assert datetime.fromisoformat(proof['as_of_utc'])==CUTOFF
ids={'jc:2042585','jc:2042586'};targets={r['match_id']:r for r in proof['predictions'] if r['match_id'] in ids}
assert set(targets)==ids and all(r['reason']=='缺少同来源同联赛的模型' for r in targets.values())
changes=[];dedup={};restore=cloud_predict.restore_feeds

def historical_restore(source,destination):
 feed=restore(source,destination)
 with sqlite3.connect(destination) as connection:
  connection.row_factory=sqlite3.Row
  for key,previous in targets.items():
   row=dict(connection.execute('select * from matches where match_id=?',(key,)).fetchone())
   assert row['kickoff_utc']==previous['kickoff_utc'] and row['league']==previous['league']
   assert datetime.fromisoformat(row['kickoff_utc'].replace('Z','+00:00'))>CUTOFF
   result=dict(connection.execute('select * from results where match_id=?',(key,)).fetchone())
   connection.execute('update matches set status=? where match_id=?',('scheduled',key))
   after=dict(connection.execute('select * from matches where match_id=?',(key,)).fetchone())
   assert {k for k in row if row[k]!=after[k]}=={'status'}
   assert dict(connection.execute('select * from results where match_id=?',(key,)).fetchone())==result
   changes.append({'match_before':row,'match_after':after,'unchanged_result':result,'prior_forecast_path':str(prior.relative_to(SOURCE)),'prior_forecast_sha256':sha(prior),'prior_prediction':previous})
  connection.commit()
 engine=get_engine(destination)
 try:
  with Session(engine) as session:
   for league in ('韩职','澳超'):
    history=read_history(session,league,'apif,jc')
    keys=set(history.match_id);pairs=[{'match_id':row.match_id,'counterpart_id':row.counterpart_id} for row in history.itertuples() if row.counterpart_id in keys]
    assert not pairs
    eligible=history.loc[(history.kickoff_utc<CUTOFF)&(history.available_utc<CUTOFF)&(history.available_utc>history.kickoff_utc)]
    dedup[league]={'history_rows':len(history),'eligible_rows':len(eligible),'eligible_source_counts':eligible.source.value_counts().to_dict(),'eligible_seasons':sorted({str(x.year) if league=='韩职' else f'{(x.year-(x.month<7))%100:02d}{(x.year-(x.month<7)+1)%100:02d}' for x in eligible.kickoff_utc}),'linked_rows_excluded':history.attrs.get('linked_rows_excluded',[]),'remaining_counterpart_pairs':pairs,'max_eligible_available_utc':eligible.available_utc.max().isoformat()}
 finally:engine.dispose()
 return feed
cloud_predict.restore_feeds=historical_restore;cloud_predict._now=lambda:CUTOFF
run=cloud_predict.run(COPY,PARAMS,RUN,sporttery_status='skipped',feed_commit='1657093073ea0e231cef476e1eac4e9ac58ebcb8')
(ROOT/'daily-run.json').write_text(json.dumps(run,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
batch=json.loads((COPY/run['forecasts']['2026-11-07']['path']/'forecast.json').read_text())
selected=[r for r in batch['predictions'] if r['match_id'] in ids]
assert len(selected)==2 and all(r['status']=='predicted' and r['p2_label']=='P2 实验' and not r['p2_ready'] for r in selected)
assert all(run['leagues'][league]['sources']==['apif','jc'] for league in ('韩职','澳超'))
for league in ('韩职','澳超'):assert run['leagues'][league]['training_rows']==dedup[league]['eligible_rows']
after=snapshot();assert before==after
(ROOT/'source-archive-after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2)+'\n')
(ROOT/'daily-replay-evidence.json').write_text(json.dumps({'description':'Offline historical operational replay only, not predictions that existed then and not live future predictions. After normal archive restore, only two proven historical target statuses change; all result and availability fields remain unchanged.','cutoff_utc':CUTOFF.isoformat(),'actual_execution_after':'2026-11-14T00:00:00Z','status_changes':changes,'dedup':dedup,'predictions':selected,'source_archive_files_unchanged':len(before),'source_commit':'1657093073ea0e231cef476e1eac4e9ac58ebcb8'},ensure_ascii=False,indent=2,allow_nan=False)+'\n')
print(json.dumps({'status':run['status'],'new_leagues':{k:run['leagues'][k] for k in ('韩职','澳超')},'predicted_ids':[r['match_id'] for r in selected],'original_files_unchanged':len(before)},ensure_ascii=False),flush=True)
