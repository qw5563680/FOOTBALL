import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
ROOT=Path('/workspace/work/football-t63')
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_t66_fd_apif import SEED,SEEN,ingest_fd,clone
from zucai.db import get_engine,init_db,session_scope
from zucai.models import Match,Result
from zucai.predict.backtest import read_history
from zucai.predict.pooled import select_training_history
from zucai.source_result_reconcile import reconcile_source_results
with tempfile.TemporaryDirectory(prefix='t63-review-') as tmp:
    database=Path(tmp)/'feeds.db'
    engine=get_engine(database)
    init_db(engine)
    with engine.begin() as con:
        for table,section in SEED['tables'].items():
            columns=','.join(section['columns'])
            marks=','.join('?' for _ in section['columns'])
            con.exec_driver_sql(f'INSERT INTO {table} ({columns}) VALUES ({marks})',[tuple(row) for row in section['rows']])
    case=SEED['cases'][0]
    with session_scope(engine) as s:
        assert ingest_fd(s,case)['linked_apif']==1
        s.get(Result,case['fd_id']).available_utc=SEEN+timedelta(minutes=3)
    evidence=reconcile_source_results(database,SEEN+timedelta(minutes=1))
    outputs={}
    with session_scope(engine) as s:
        history=read_history(s,'芬超','jc,fd,apif')
        for seconds in (59,60,61,180,181):
            outputs[str(seconds)]=list(select_training_history(history,SEEN+timedelta(seconds=seconds)).match_id)
        assert outputs=={'59':[],'60':[],'61':[case['apif_id']],'180':[case['apif_id']],'181':[case['fd_id']]},outputs
        clone(s,s.get(Match,case['apif_id']),'apif:777777')
    with session_scope(engine) as s:
        try:
            read_history(s,'芬超','jc,fd,apif')
        except ValueError as exc:
            guard=str(exc)
        else:
            raise AssertionError('A T63 timed proof hid a scheduled competing APIF claimant')
    engine.dispose()
    print(json.dumps({'scenario':'T66 real FIN seed + simulated later FD availability and T63 verification clock','cutoffs_seconds_after_seen':outputs,'timed_proof_cannot_hide_scheduled_competitor':guard,'reconciliation':evidence},ensure_ascii=False,indent=2))
