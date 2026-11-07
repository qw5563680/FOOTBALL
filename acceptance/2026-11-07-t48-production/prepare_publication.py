"""Prepare an offline production candidate after the complete replay gate passes."""
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path('/workspace/work/t48-production')
CODE = Path('/workspace/work/football-t48-production')
sys.path[:0] = [str(CODE / 'src'), str(CODE / 'examples')]
import t48_verify_replay as verify
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.stats_archive import write_rows_manifest
from zucai.stats_release import upload_checksums
from zucai.stats_rows import restore_rows

def main():
    validated = ROOT / 'validation'
    summary = json.loads((validated / 'evidence/acceptance-summary.json').read_text())
    assert summary['status'] == 'passed'
    assert summary['provenance_210']['verified_histories'] == 210
    assert summary['complete_targets'] == 16 and all(summary['idempotent'].values())
    before = ROOT / 'backup/feed'
    candidate = ROOT / 'candidate/feed'
    assert not candidate.exists(), 'Preserve previous candidate and inspect it first'
    shutil.copytree(before, candidate, ignore=shutil.ignore_patterns('.git'))
    work = ROOT / 'candidate/database'
    shutil.copytree(validated / 'repair', work)
    database = work / 'input.db'
    run_id = 't48-production-20261107-813287f'
    local = verify.LocalRelease(validated / 'local-release', ROOT / 'backup/assets')
    block = json.loads((validated / 'evidence/published-technical-manifest.json').read_text())
    block['checksum_asset'] = upload_checksums(
        local, 'stats-raw-202611', validated / 'published', run_id,
        block['row_shards'] + block['injury_receipts'])
    write_rows_manifest(database, block)
    old_feed = json.loads((before / 'feed_manifest.json').read_text())
    os.environ['ZUCAI_OPERATION'] = 'replay-fd-history'
    new_feed = export_feeds(database, candidate,
        through=date.fromisoformat(old_feed['through_beijing']), run_id=run_id,
        collection=old_feed['collection'])
    assert new_feed['collection'] == old_feed['collection']
    assert new_feed['counts'] == old_feed['counts']
    assert new_feed['sources'] == old_feed['sources']
    # The exporter normalizes CRLF in existing SFC JSON. Preserve those original
    # bytes after proving that their complete JSON content is identical.
    preserved_sfc = []
    for key, old_hash in old_feed['files'].items():
        if key.startswith('state/sfc/') and key.endswith('.json'):
            source, target = before / key, candidate / key
            assert json.loads(source.read_text()) == json.loads(target.read_text()), key
            if verify.digest(target) != old_hash:
                shutil.copyfile(source, target)
                new_feed['files'][key] = old_hash
                preserved_sfc.append(key)
    verify.write_json(candidate / 'feed_manifest.json', new_feed)
    a = verify.relative_tree_hashes(before); a.pop('.git', None)
    b = verify.relative_tree_hashes(candidate)
    changed = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    allowed = {'feed_manifest.json', 'state/stats_rows_manifest.json',
               'state/fd_stats_links.jsonl', 'state/audit/stats_ingest_audit.csv'}
    verify.write_json(ROOT / 'evidence/candidate-file-diff.json',
        {'changed': changed, 'unexpected': sorted(set(changed)-allowed),
         'before': {k:a.get(k) for k in changed}, 'after': {k:b.get(k) for k in changed}})
    assert not set(changed)-allowed, 'Unexpected candidate file changes'
    restored = ROOT / 'candidate/readback/input.db'
    restore_feeds(candidate, restored)
    restore_rows(restored, candidate, release=local)
    expected = verify.canonical_tables(database)
    actual = verify.canonical_tables(restored)
    assert actual == expected, 'Candidate readback changed SQL data'
    plan = verify.validate_plan(ROOT / 'inputs/selected-raw-manifest.json')
    check = verify.verify_gap_rows(restored,
        Path('/workspace/work/football_handover_20261107/详细排查证据/history-gaps.csv'),
        plan, ROOT / 'candidate/evidence')
    assert check['verified_histories'] == 210
    experiment = json.loads((before / verify.EXPERIMENT).read_text())
    targets = [x for x in experiment['predictions']
               if x['model_name'] == 'dc-sot-v1' and x['features'] is not None]
    features = verify.build_features(restored, targets)
    assert features == json.loads((validated / 'evidence/features-after.json').read_text())
    old_assets = {x['name']: x for x in
        old_feed['technical_data']['row_shards'] + old_feed['technical_data']['injury_receipts']}
    assets = block['row_shards'] + block['injury_receipts'] + [block['checksum_asset']]
    new_assets = [x for x in assets if x['name'] not in old_assets]
    for x in assets:
        path = validated / 'local-release' / x['tag'] / x['name']
        if not path.exists(): path = ROOT / 'backup/assets' / x['name']
        assert verify.digest(path) == x['sha256'] and path.stat().st_size == x['size']
    result = {'status': 'ready_for_release_upload_not_published', 'run_id': run_id,
        'main': 'a664318450675946a578c041cb1f24219981e771',
        'expected_feed_head': '813287f61afc7613c0c280169525f46697503ac3',
        'changed_files': changed, 'all_other_files_unchanged': True,
        'original_sfc_serialization_preserved': preserved_sfc,
        'preserved_file_count': len(a)-len([x for x in changed if x in a]),
        'candidate_manifest_sha256': verify.digest(candidate / 'feed_manifest.json'),
        'new_release_assets': new_assets, 'all_release_assets': assets,
        'candidate_readback_210': check, 'candidate_readback_tables_exact': True,
        'candidate_readback_features_exact': True, 'published': False}
    verify.write_json(ROOT / 'evidence/publication-plan.json', result)
    print(json.dumps({k:v for k,v in result.items()
        if k not in ('new_release_assets','all_release_assets')}, ensure_ascii=False), flush=True)
    print('New Release assets:',len(new_assets), flush=True)

if __name__ == '__main__':
    main()
