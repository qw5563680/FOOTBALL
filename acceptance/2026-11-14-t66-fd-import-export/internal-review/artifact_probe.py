import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
ROOT=Path('/workspace/work/football-t66')
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_t66_fd_links import seed, import_result
from test_fd_stats_archive import LocalRelease
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.stats_rows import publish_rows, restore_rows
out={}
for transition in (False,True):
    with tempfile.TemporaryDirectory(prefix='t66-artifact-review-') as path:
        root=Path(path)
        db=seed(root)
        if transition: import_result(db)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE match_stats SET raw_sha256=?", ('b'*64,))
        release=LocalRelease(root/'release')
        try:
            publish_rows(db,root/'assets',release=release,tag='stats-raw-202611',run_id='review')
            archive=root/'archive'
            export_feeds(db,archive,date(2026,11,14),'review')
            restored=root/'restored/feeds.db'
            restore_feeds(archive,restored)
            counts=restore_rows(restored,archive,release=release)
            with sqlite3.connect(restored) as conn:
                hashes=conn.execute('SELECT DISTINCT raw_sha256 FROM match_stats').fetchall()
            out[str(transition)]={'publish_restore':'ACCEPTED','restored':counts,'distinct_hashes':hashes}
        except ValueError as exc:
            out[str(transition)]={'publish_restore':'REJECTED','error':str(exc)}
print(json.dumps(out,ensure_ascii=False,indent=2))
