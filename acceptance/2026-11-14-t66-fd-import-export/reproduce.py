"""Reproduce T66 only in a newly restored copy; all HTTP is blocked."""
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import responses

from zucai.cloud_feeds import check_db, export_feeds, restore_feeds
from zucai.fd_stats_links import validate_fd_links
from zucai.sources.fd_ingest import fetch_fd

ROOT = Path('/workspace/work/football-t66')
ARCHIVE = Path('/workspace/work/t65-fixed-archive')
OUT = Path('/workspace/work/t66-evidence/baseline')
DB = Path('/workspace/work/t66-baseline-working/data/zucai.db')
FD_ID = 'fd:N1:20261101:zwolle:den-haag'
assert not DB.exists()

with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
    restore_feeds(ARCHIVE, DB)
    initial_sha = hashlib.sha256(DB.read_bytes()).hexdigest()
    with sqlite3.connect(DB) as db:
        check_db(db)
        links = validate_fd_links(db)
        initial = db.execute('SELECT counterpart_id FROM matches WHERE match_id=?', ('jc:2042418',)).fetchone()[0]
        assert len(links) == 52 and initial is None
        rows = db.execute('SELECT l.provider_fixture_id,l.match_id,m.counterpart_id,f.match_id FROM fd_stats_links l JOIN matches m ON l.match_id=m.match_id LEFT JOIN matches f ON m.counterpart_id=f.match_id ORDER BY l.match_id').fetchall()
        db.execute('UPDATE matches SET counterpart_id=? WHERE match_id=?', (FD_ID, 'jc:2042418'))
        try:
            validate_fd_links(db)
        except ValueError as exc:
            mutation_error = str(exc)
        else:
            raise AssertionError('Baseline must reproduce FD counterpart rejection')
        finally:
            db.rollback()
    assert hashlib.sha256(DB.read_bytes()).hexdigest() == initial_sha
    raw = ROOT / 'data/fixtures/t66/fd-n1-zwolle-den-haag.csv'
    imported = fetch_fd(div='N1', seasons=['2627'], assume=timedelta(hours=3), db=DB, raw_file=raw, raw_root=DB.parent/'raw', audit_dir=DB.parent/'state/audit', leagues_path=ROOT/'data/football_data_co_uk/leagues.json', aliases_path=ROOT/'data/team_aliases/football_data_co_uk.json')
    assert imported['linked_existing'] == 1 and imported['rejected_rows'] == 0
    try:
        export_feeds(DB, Path('/workspace/work/t66-baseline-export'), date(2026,11,14), 't66-baseline-fails')
    except ValueError as exc:
        export_error = str(exc)
    else:
        raise AssertionError('Baseline export must fail after real FD import')
    assert mutation_error == export_error == 'FD 目标已有其他来源 counterpart 声明'
    report = dict(code_baseline='d6a52bbb15df4ed22f9c6b57ee2e0936c31ca861', archive_commit='e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4', restored_sha256=initial_sha, links=52, original_empty_pointer=initial, mutation_error=mutation_error, full_import=imported, export_error=export_error, fdorg_references=51, fdorg_reference_rows_still_present=sum(r[3] is not None for r in rows), all_links=rows, http_calls=len(http.calls), production_writes=0)
    (OUT/'reproduction.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in {'all_links','full_import'}},ensure_ascii=False))
