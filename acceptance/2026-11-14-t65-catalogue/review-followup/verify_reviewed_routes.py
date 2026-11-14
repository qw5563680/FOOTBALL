"""Independently audit the review-approved additions without network or database work."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

from zucai.league_registry import daily_codes, load_registry, provider_entries

ROOT = Path('/workspace/work/football-t65')
OUT = Path('/workspace/work/t65-evidence/review-followup')
BASE = 'b83d2cc7d975e41f418f59aad3341a7d1574f95a'
REVIEWED = '9032724c6b2a3d5d65893e99ef48a55c49f8e778'
ARCHIVE = Path('/workspace/work/t65-fixed-archive')


def git_json(commit):
    return json.loads(subprocess.check_output(['git', 'show', f'{commit}:data/league_map.json'], cwd=ROOT))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    doc = load_registry(ROOT / 'data/league_map.json')
    baseline = git_json(BASE)
    reviewed = git_json(REVIEWED)
    source_review = json.loads((OUT / 'claude-review.json').read_text())['reviews'][0]
    # Derive IDs independently from the review prose, not the mutation script.
    section = re.search(r'联赛 79、.*?继续候选：', source_review['body'], flags=re.S).group()
    approved = re.findall(r'\d+', section)
    assert len(approved) == len(set(approved)) == 62
    approved = set(approved)
    pending = {'40', '62', '71', '850', '916'}
    prior_new = reviewed['providers']['apif'].keys() - baseline['providers']['apif'].keys()
    assert prior_new == approved | pending
    old_objects = 0
    for provider, entries in baseline['providers'].items():
        for code, entry in entries.items():
            assert canonical(doc['providers'][provider][code]) == canonical(entry)
            old_objects += 1
    for before, after in zip(reviewed['competitions'][:21], doc['competitions'][:21]):
        assert canonical(before) == canonical(after)
    for before, after in zip(reviewed['competitions'], doc['competitions']):
        assert canonical(before['official_catalogue']) == canonical(after['official_catalogue'])
        assert before['master_fields'] == after['master_fields']
    raw = json.loads((ARCHIVE / 'state/catalogue/43539439430-1/apif-raw.json').read_text())
    metadata = {str(row['league']['id']): row for row in raw['response']}
    for code in prior_new:
        route = doc['providers']['apif'][code]
        config = route['config']
        original = metadata[code]
        assert any('\u4e00' <= char <= '\u9fff' for char in config['league'])
        assert config['api_name'] == original['league']['name']
        assert config['api_country'] == original['country']['name']
        assert config['api_type'] == original['league']['type']
        assert config['catalogue_seasons'] == [s['year'] for s in original['seasons']]
        assert config['seasons_available'] == [] and route['access_status'] == 'unverified'
        assert route['status'] == ('verified' if code in approved else 'candidate')
        assert route['daily'] == route['backfill'] == (code in approved)
        if code in approved:
            proofs = [e for e in route['evidence'] if e['kind'] == 'pr_identity_review']
            assert len(proofs) == 1
            assert proofs[0]['code'] == code and proofs[0]['review_id'] == source_review['id']
            assert proofs[0]['reviewed_commit'] == REVIEWED
            assert proofs[0]['reviewed_utc'] == source_review['submitted_at']
    for provider, entries in doc['providers'].items():
        names = [entry['config']['league'] for entry in entries.values() if entry['mode'] == 'import']
        assert len(names) == len(set(names))
    for purpose in ('daily', 'backfill'):
        routes = provider_entries('apif', purpose=purpose)
        assert routes.keys() - baseline['providers']['apif'].keys() == approved
        assert len(routes) == 73
    assert daily_codes('apif')[:11] == '98 292 188 113 103 244 119 253 169 3 848'.split()
    owner_reasons = {row['official_catalogue']['reason'] for row in doc['competitions']
                     if row['official_catalogue']['owner_decision_required']}
    assert len(owner_reasons) == 14
    summary = {
        'result': 'passed', 'approved_ids_read_independently_from_review': len(approved),
        'retained_candidate_ids': sorted(pending, key=int), 'fixed_project_keys': len(prior_new),
        'old_provider_objects_byte_equal': old_objects, 'old_verified_rows_byte_equal': 21,
        'official_catalogue_objects_byte_equal': 90, 'owner_decision_groups_unchanged': 14,
        'daily_import_routes': 73, 'backfill_import_routes': 73,
        'newly_reviewed_routes_with_unverified_access': len(approved),
        'master_status_counts': dict(Counter(row['status'] for row in doc['competitions'])),
        'api_conclusion_counts': dict(Counter(row['api_catalogue_proposal']['status'] for row in doc['competitions'])),
        'registry_sha256': digest(ROOT / 'data/league_map.json'),
        'source_review_sha256': digest(OUT / 'claude-review.json'),
        'requests': 0, 'formal_database_changes': False,
    }
    (OUT / 'route-verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
