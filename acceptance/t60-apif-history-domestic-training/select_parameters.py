"""Offline T60 execution of the existing T15/T16 protocol; no odds tables read."""
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy import event
from sqlalchemy.orm import Session
from zucai.db import get_engine
from zucai.predict.backtest import read_history, evaluate
from zucai.predict.parameters import TRAINING_POLICY, p2_gate
from zucai.predict.tuning import rolling_origin
from zucai.predict.seasons import season_for

ROOT=Path('/workspace/work/t60-evidence')
DB=ROOT/'static.db'
PARAMS=Path('/workspace/work/football-t60/data/model/params.json')
CUTOFF='2026-11-14T00:00:00Z'
digest=hashlib.sha256(DB.read_bytes()).hexdigest()
engine=get_engine(DB)
statements=[]
@event.listens_for(engine,'before_cursor_execute')
def guard(conn,cursor,statement,parameters,context,executemany):
    if 'odds' in statement.lower():
        raise AssertionError('Market data forbidden in T60 selection')
    statements.append(statement)
try:
    for league in ['韩职','澳超']:
        print('START',league,datetime.now(UTC).isoformat(),flush=True)
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA query_only=ON')
            connection.exec_driver_sql('BEGIN')
            with Session(bind=connection) as session:
                history=read_history(session,league,'apif,jc')
        result=rolling_origin(history,{},grid=[90,180,270,365,540,730],as_of=CUTOFF)
        mapping={w['validation_season']:w['half_life_days'] for w in result['windows']}
        calibration={w['validation_season']:w['draw_calibration'] for w in result['windows']}
        weekly=evaluate(history,{},closing={},seasons=list(mapping),as_of=CUTOFF,fold='weekly',parameters_by_season=mapping,calibration_by_season=calibration)
        gate=p2_gate(weekly,league=league,as_of=CUTOFF)
        entry=dict(
            half_life_days=result['half_life_days'],selection_basis=result['selection_basis'],
            calibration_policy=result['calibration_policy'],selection_season=result['windows'][-1]['selection_season'],
            selection_as_of_utc=result['windows'][-1]['selection_as_of_utc'],
            evaluation_windows=[{k:w[k] for k in ('selection_season','validation_season','selection_as_of_utc','half_life_days','draw_calibration','selection_draw_bias')} for w in result['windows']],
            generated_utc=datetime.now(UTC).isoformat(),database_sha256=digest,
            training_policy=TRAINING_POLICY,sources=history.attrs['sources'],
            unknown_team_prior='weakest_quartile_half_shrinkage',draw_calibration=result['windows'][-1]['draw_calibration'],
            p2_ready=False,p2_evidence=gate,
        )
        # Keep the published gate and existing entries untouched; only add this league.
        document=json.loads(PARAMS.read_text())
        document['leagues'][league]=entry
        PARAMS.write_text(json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        result.update(database_sha256=digest,as_of_utc=CUTOFF,weekly=weekly,parameters=entry,
            source_commit='1657093073ea0e231cef476e1eac4e9ac58ebcb8',
            historical_season_counts=history.groupby(history.kickoff_utc.map(lambda stamp:season_for(stamp,league))).size().to_dict(),
            source_counts=history.source.value_counts().to_dict(),
            linked_rows_excluded=history.attrs.get('linked_rows_excluded',[]),
            no_market_queries=True,descriptive_only=True)
        (ROOT/f'{league}-selection.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        print('DONE',league,entry['half_life_days'],gate,flush=True)
finally:
    engine.dispose()
assert hashlib.sha256(DB.read_bytes()).hexdigest()==digest
(ROOT/'selection-sql.json').write_text(json.dumps(statements,ensure_ascii=False,indent=2)+'\n')
