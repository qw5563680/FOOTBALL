"""T58 offline acceptance on an existing fixed archive and prepared copy only."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.daily_digest import generate
from zucai.digest_aliases import pending_name_lines
from zucai.db import get_engine
from zucai.jc_candidate_review import inspect_candidates, review_candidates
from zucai.sources.sporttery import JcMatch
from zucai.sources.sporttery_aliases import resolve_teams

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archive', type=Path, required=True)
parser.add_argument('--plan', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
root = args.out.resolve()
source = args.archive.resolve()
database = root / 'copy/feeds.db' 

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def tree_hashes(path):
    return {str(p.relative_to(path)): sha(p) for p in sorted(path.rglob('*')) if p.is_file() and '.git' not in p.parts}

plan = json.loads(args.plan.read_bytes())
assert sha(source / 'feed_manifest.json') == plan['source_feed_manifest_sha256']
archive_info(source)
before = tree_hashes(source)
root.mkdir(parents=True, exist_ok=False)
restore_feeds(source, database)
(root / 'inspection.json').write_text(json.dumps(inspect_candidates(database), ensure_ascii=False, indent=2) + '\n')
(root / 'db-before.sha256').write_text(sha(database) + '\n')
(root / 'queue-before.json').write_bytes((database.parent / 'state/alias_candidates/jc.json').read_bytes())
review_candidates(database, args.plan, root / 'first-five-review')
now = datetime.now(UTC)
assert sha(database) == (root / 'db-before.sha256').read_text().strip()
next_observations = []
engine = get_engine(database)
with Session(engine) as session:
    info = inspect_candidates(database)
    teams = {t['id']: t['name'] for t in info['teams']}
    for row in info['candidate_matches']:
        pending = []
        fixture = JcMatch(row['match_id'], date(2026, 11, 13), 'offline-review', row['league'],
            teams[row['home_team_id']], teams[row['away_team_id']],
            datetime.fromisoformat(row['kickoff_utc']), [])
        home, away = resolve_teams(session, fixture, now, aliases={}, candidates=pending)
        assert (home.id, away.id) == (row['home_team_id'], row['away_team_id']) and not pending
        next_observations.append(dict(match_id=row['match_id'], home_team_id=home.id,
            away_team_id=away.id, new_pending_candidates=len(pending)))
engine.dispose()
assert sha(database) == (root / 'db-before.sha256').read_text().strip()
archive = root / 'exported-copy'
shutil.copytree(source, archive, ignore=shutil.ignore_patterns('.git'))
manifest = export_feeds(database, archive, date(2026, 11, 13), 'T58-offline-candidate-review')
archive_info(archive)
restored = root / 'restored/feeds.db'
restore_feeds(archive, restored)
queue = 'state/alias_candidates/jc.json'
assert (restored.parent / queue).read_bytes() == (database.parent / queue).read_bytes()
assert inspect_candidates(restored)['inventory_sha256'] == info['inventory_sha256']
feed_before = archive_info(source)
lines_before = pending_name_lines(source, feed_before, now)
lines_after = pending_name_lines(archive, manifest, now)
historical = pending_name_lines(archive, manifest, '2026-11-13T10:10:46.658458+00:00')
assert '5 个' in lines_before[0] and '1 个' in lines_after[0] and '湘南海洋' in lines_after[0]
assert '5 个' in historical[0]
report_before = generate(source, root / 'digest-before', now=now, run_id='T58-review-before')
report_after = generate(archive, root / 'digest-after', now=now, run_id='T58-review-after')
assert lines_before[0] in report_before.read_text() and lines_after[0] in report_after.read_text()
after = tree_hashes(source)
assert before == after
frozen = {k:v for k,v in before.items() if k.startswith(('forecasts/','scoreboard/','parlay/','daily/','sfc/','reports/','runs/'))}
assert frozen
assert all(sha(archive / k) == v for k,v in frozen.items())
summary=dict(source_commit='e901c14b2c3aac1496832cc98cb2ab7e3bd7aeb2',
    checked_utc=now.isoformat(), checked_beijing=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).isoformat(),
    db_sha256_before=(root/'db-before.sha256').read_text().strip(),db_sha256_after=sha(database),
    db_bytes_unchanged=True, original_archive_file_count=len(before),original_archive_unchanged=True,
    copied_frozen_files_count=len(frozen),copied_frozen_files_unchanged=True,
    pending_before=5,pending_after=1,cleared=['哥伦比亚','巴拉圭','新加坡','阿根廷'],
    pending=['湘南海洋'],before_digest_line=lines_before[0],after_digest_line=lines_after[0],
    historical_review_not_backdated_line=historical[0],
    queue_roundtrip_bytes_equal=True,queue_after_sha256=sha(database.parent/queue),
    inventory_roundtrip_equal=True,next_observation_checks=next_observations,
    digest_before=str(report_before.relative_to(root)),digest_after=str(report_after.relative_to(root)),
    first_five_report='first-five-review/review.json',proof='first-five-review/identity_inventory.json')
(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
(root/'original-archive-SHA256SUMS').write_text(''.join(f'{v}  {k}\n' for k,v in before.items()))
(root/'queue-after.json').write_bytes((database.parent/queue).read_bytes())
print(json.dumps(summary,ensure_ascii=False,indent=2))
