import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path('/workspace/work/football-t66')
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from test_t66_fd_links import seed, import_result, RAW
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_archive import check_stats
from zucai.stats_storage import ingest_fd

out = {}
for transitioned in [False, True]:
    with tempfile.TemporaryDirectory(prefix='t66-review-') as directory:
        workspace = Path(directory)
        database = seed(workspace)
        if transitioned:
            import_result(database)
        raw = RawStore(workspace / 'later').save(
            'football-data-co-uk', 'N1-2627', RAW.read_bytes() + b'\n', 'csv',
            captured_at=instant('2026-11-05T14:07:19Z'),
            url='https://football-data.co.uk/mmz4281/2627/N1.csv', status_code=200,
        )
        try:
            result = ingest_fd(
                database, raw, div='N1', season='2627',
                leagues_path=ROOT / 'data/football_data_co_uk/leagues.json',
                aliases_path=ROOT / 'data/team_aliases/football_data_co_uk.json',
            )
            out[f'later_stats_capture_transitioned_{transitioned}'] = {'result': result}
        except Exception as exc:
            out[f'later_stats_capture_transitioned_{transitioned}'] = {
                'error_type': type(exc).__name__, 'error': str(exc),
            }
        with sqlite3.connect(database) as db:
            evidence = json.loads(db.execute('SELECT evidence FROM fd_stats_links').fetchone()[0])
            out[f'later_stats_capture_transitioned_{transitioned}']['observation_count'] = len(evidence['observations'])
for column, value in [
    ('raw_sha256', 'b' * 64),
    ('raw_location', 'forged://raw.csv'),
    ('captured_utc', '2026-11-05T12:00:00Z'),
]:
    for transitioned in [False, True]:
        with tempfile.TemporaryDirectory(prefix='t66-review-') as directory:
            workspace = Path(directory)
            database = seed(workspace)
            if transitioned:
                import_result(database)
            with sqlite3.connect(database) as db:
                db.execute(f'UPDATE match_stats SET {column}=?', (value,))
                try:
                    check_stats(db)
                    out[f'tamper_{column}_transitioned_{transitioned}'] = 'ACCEPTED'
                except ValueError as exc:
                    out[f'tamper_{column}_transitioned_{transitioned}'] = str(exc)
out['reviewed_sha256'] = {
    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [
        ROOT / 'src/zucai/fd_stats_links.py', ROOT / 'src/zucai/stats_storage.py',
        ROOT / 'src/zucai/stats_archive.py', ROOT / 'src/zucai/fd_collection.py',
        ROOT / 'src/zucai/cloud_apis.py', ROOT / 'src/zucai/cloud_feeds.py',
    ]
}
print(json.dumps(out, ensure_ascii=False, indent=2))
