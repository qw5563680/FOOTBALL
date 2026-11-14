"""T66 offline full archive acceptance; never write the source archive or fetch HTTP."""
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import responses
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zucai.cloud_feeds import check_db, export_feeds, restore_feeds
from zucai.fd_stats_links import validate_fd_links
from zucai.predict.backtest import read_history
from zucai.sources.fd_ingest import fetch_fd

ROOT = Path('/workspace/work/football-t66')
ARCHIVE = Path('/workspace/work/t65-fixed-archive')
OUT = Path('/workspace/work/t66-evidence/acceptance')
OUT.mkdir(exist_ok=True)
JC_ID = 'jc:2042418'
FD_ID = 'fd:N1:20261101:zwolle:den-haag'
INPUTS = json.loads(Path('/workspace/work/t48-production/inputs/selected-raw-manifest.json').read_text())['files']


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))+'\n').encode()


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(db):
    output = {}
    for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        assert name.replace('_','').isalnum()
        cursor=db.execute(f'SELECT * FROM "{name}" ORDER BY '+','.join(str(i+1) for i in range(len(db.execute(f'PRAGMA table_info("{name}")').fetchall()))))
        digest=hashlib.sha256();count=0
        for row in cursor:
            digest.update(encoded(row));count+=1
        output[name]={'rows':count,'sha256':digest.hexdigest()}
    return output


def jc_results(db):
    digest=hashlib.sha256()
    for row in db.execute("SELECT r.* FROM results r JOIN matches m USING(match_id) WHERE m.source='jc' ORDER BY r.match_id"):
        digest.update(encoded(row))
    return digest.hexdigest()


def histories(database):
    config=json.loads((ROOT/'data/model/params.json').read_text())['leagues']
    engine=create_engine('sqlite://',creator=lambda:sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True))
    result={}
    try:
        with Session(engine) as session:
            for league,entry in config.items():
                if set(entry['sources']) != {'fd','jc'}:
                    continue
                frame=read_history(session,league=league,source=','.join(entry['sources']),training_leagues=entry.get('training_leagues'))
                result[league]={'rows':len(frame),'ids':sorted(frame['match_id'].tolist())}
    finally:
        engine.dispose()
    assert len(result)==15,len(result)
    return result


def source_frozen():
    roots=[ARCHIVE/x for x in ['forecasts','models','scoreboard','sfc','daily','state/publication','data/runs']]
    return {p.relative_to(ARCHIVE).as_posix():sha(p) for root in roots if root.exists() for p in sorted(root.rglob('*')) if p.is_file()}


def import_file(database, item, raw=None):
    return fetch_fd(div=item['div'],seasons=[item['season']],assume=timedelta(hours=3),db=database,raw_file=raw or Path(item['path']),raw_root=database.parent/'raw',audit_dir=database.parent/'state/audit',leagues_path=ROOT/'data/football_data_co_uk/leagues.json',aliases_path=ROOT/'data/team_aliases/football_data_co_uk.json')


before_files=source_frozen()
with TemporaryDirectory(prefix='t66-acceptance-work-',dir='/workspace/work') as temp, responses.RequestsMock(assert_all_requests_are_fired=False) as http:
    work=Path(temp); database=work/'source/zucai.db'
    restore_feeds(ARCHIVE,database)
    restored_sha=sha(database)
    before_hist=histories(database)
    with sqlite3.connect(database) as db:
        check_db(db);before_links=validate_fd_links(db);before_tables=snapshot(db);original_results=jc_results(db)
        before_pointers=dict(db.execute('SELECT match_id,counterpart_id FROM matches'))
        original_link_rows=db.execute('SELECT * FROM fd_stats_links ORDER BY provider_fixture_id').fetchall()
        original_fd_ids={r[0] for r in db.execute("SELECT match_id FROM matches WHERE source='fd'")}
    sample=next(r for r in INPUTS if r['div']=='N1')
    first=import_file(database,sample,ROOT/'data/fixtures/t66/fd-n1-zwolle-den-haag.csv')
    assert first['linked_existing']==1 and first['rejected_rows']==0
    with sqlite3.connect(database) as db:
        check_db(db);assert len(validate_fd_links(db))==52
    export_feeds(database,work/'single-export',date(2026,11,14),'t66-real-single')
    # The single-row export is only an intermediate assertion; no need to retain its large copy.
    import shutil
    shutil.rmtree(work/'single-export')
    imports=[]
    for item in INPUTS:
        assert sha(Path(item['path']))==item['sha256']
        assert sha(Path(item['sidecar_path']))==item['sidecar_sha256']
        result=import_file(database,item)
        imports.append({'div':item['div'],'season':item['season'],'raw_sha256':item['sha256'],'counts':{k:v for k,v in result.items() if type(v)is int},'outcomes':result['files'][0]['outcomes']})
        with sqlite3.connect(database) as db:
            check_db(db);assert len(validate_fd_links(db))==52
    after_hist=histories(database)
    with sqlite3.connect(database) as db:
        first_tables=snapshot(db)
        assert db.execute('SELECT * FROM fd_stats_links ORDER BY provider_fixture_id').fetchall()==original_link_rows
        assert jc_results(db)==original_results
        after_pointers=dict(db.execute('SELECT match_id,counterpart_id FROM matches'))
        fd_ids={r[0] for r in db.execute("SELECT match_id FROM matches WHERE source='fd'")}
        link_audit=[]
        for key,link in sorted(before_links.items()):
            target=link['match_id'];expected=FD_ID if target==JC_ID else before_pointers[target]
            assert after_pointers[target]==expected,(target,after_pointers[target],expected)
            link_audit.append({'provider_fixture_id':key,'match_id':target,'before_counterpart':before_pointers[target],'after_counterpart':after_pointers[target],'evidence_unchanged':True,'same_fixture_found_in_import_outcomes':any(o['match_id']==target for r in imports for o in r['outcomes'])})
        assert all(r['same_fixture_found_in_import_outcomes'] for r in link_audit)
    repeats=[]
    for item in INPUTS:
        result=import_file(database,item)
        repeats.append({'div':item['div'],'counts':{k:v for k,v in result.items() if type(v)is int}})
    with sqlite3.connect(database) as db:
        assert snapshot(db)==first_tables
        check_db(db)
    exported=export_feeds(database,work/'export',date(2026,11,14),'t66-real-nine')
    dest=work/'roundtrip/zucai.db';restore_feeds(work/'export',dest)
    with sqlite3.connect(dest) as db:
        roundtrip_tables=snapshot(db);assert roundtrip_tables==first_tables;check_db(db)
        assert len(validate_fd_links(db))==52
    training={}
    for league,before in before_hist.items():
        after=after_hist[league];added=set(after['ids'])-set(before['ids']);removed=set(before['ids'])-set(after['ids'])
        assert not removed,(league,removed)
        assert added <= (fd_ids-original_fd_ids),(league,added-(fd_ids-original_fd_ids))
        training[league]={'before':before['rows'],'after':after['rows'],'added_count':len(added),'added_ids':sorted(added),'removed_ids':sorted(removed)}
    report={'input_archive_commit':'e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4','input_raw_date':'2026-11-04','failed_nov14_artifact_replayed':False,'restored_sha256':restored_sha,'baseline_link_count':len(before_links),'one_row_import_counts':{k:v for k,v in first.items() if type(v)is int},'one_row_export_passed':True,'all_52_links_found_and_reimported':True,'original_52_link_rows_unchanged':True,'original_51_fdorg_pointers_unchanged':True,'only_empty_target_promoted':JC_ID,'reliable_jc_results_unchanged':True,'nine_file_imports':imports,'repeat_counts':repeats,'repeat_all_14_tables_equal':True,'export_restore_all_14_tables_equal':True,'new_fd_matches':len(fd_ids-original_fd_ids),'training':training,'before_tables':before_tables,'after_tables':first_tables,'roundtrip_tables':roundtrip_tables,'http_calls':len(http.calls),'production_writes':0,'model_fits':0,'new_training_available_during_historical_predictions_claimed':False}
    (OUT/'summary.json').write_bytes(encoded(report));(OUT/'all-52-link-audit.json').write_bytes(encoded(link_audit));(OUT/'export-manifest.json').write_bytes(encoded(exported))
assert source_frozen()==before_files
(OUT/'frozen-source-files.json').write_bytes(encoded(before_files))
print(json.dumps({'result':'passed','links':52,'raw_files':9,'new_fd_matches':report['new_fd_matches'],'new_training_rows':sum(r['added_count'] for r in training.values()),'repeat_added':sum(r['counts']['inserted'] for r in repeats),'frozen_files_unchanged':len(before_files),'requests':report['http_calls']},ensure_ascii=False))
