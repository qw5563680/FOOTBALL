import csv
import io
import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
ROOT=Path('/workspace/work/football-t66')
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_t66_fd_links import seed, import_result, RAW
from test_fd_stats_archive import LocalRelease
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.sources.base import RawStore
from zucai.sources.apif_stats import instant
from zucai.stats_rows import publish_rows, restore_rows
from zucai.stats_storage import ingest_fd
with tempfile.TemporaryDirectory(prefix='t66-corrected-review-') as directory:
    root=Path(directory)
    db=seed(root)
    import_result(db)
    reader=csv.DictReader(io.StringIO(RAW.read_text()))
    rows=list(reader)
    rows[0]['HS']='25'
    output=io.StringIO(newline='')
    writer=csv.DictWriter(output,fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    raw=RawStore(root/'later').save('football-data-co-uk','N1-2627',output.getvalue().encode(),'csv',captured_at=instant('2026-11-05T14:07:19Z'),url='https://football-data.co.uk/mmz4281/2627/N1.csv',status_code=200)
    result=ingest_fd(db,raw,div='N1',season='2627',daily=True,leagues_path=ROOT/'data/football_data_co_uk/leagues.json',aliases_path=ROOT/'data/team_aliases/football_data_co_uk.json')
    again=ingest_fd(db,raw,div='N1',season='2627',daily=True,leagues_path=ROOT/'data/football_data_co_uk/leagues.json',aliases_path=ROOT/'data/team_aliases/football_data_co_uk.json')
    assert result=={'inserted':1,'rejected':0}
    assert again=={'inserted':0,'rejected':0}
    release=LocalRelease(root/'release')
    publish_rows(db,root/'assets',release=release,tag='stats-raw-202611',run_id='corrected-review')
    archive=root/'archive'
    export_feeds(db,archive,date(2026,11,14),'review')
    restored=root/'restored/feeds.db'
    restore_feeds(archive,restored)
    restored_counts=restore_rows(restored,archive,release=release)
    with sqlite3.connect(restored) as conn:
        history=conn.execute("SELECT stat_key,value,version,captured_utc,available_utc,availability_basis FROM match_stats WHERE stat_key IN (SELECT stat_key FROM match_stats WHERE version=2) AND side='home' ORDER BY version").fetchall()
        evidence=json.loads(conn.execute('SELECT evidence FROM fd_stats_links').fetchone()[0])
    assert [row[1] for row in history]==[24.0,25.0]
    assert evidence['counterpart_id'] is None
    assert len(evidence['observations'])==2
    print(json.dumps({'synthetic_variation':'real fixture HS=24→25 with simulated new capture; no live request','ingest':result,'repeat':again,'restored':restored_counts,'history':history,'observation_count':len(evidence['observations'])},ensure_ascii=False,indent=2))
