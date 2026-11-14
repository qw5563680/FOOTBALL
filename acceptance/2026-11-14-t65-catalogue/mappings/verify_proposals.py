"""Independent checks of T65 proposal metadata against the immutable source archive."""

from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import subprocess

REPO = Path('/workspace/work/football-t65')
ARCHIVE = Path('/workspace/work/t65-fixed-archive')
OUT = Path('/workspace/work/t65-evidence/mappings')
BASE = 'b83d2cc7d975e41f418f59aad3341a7d1574f95a'
SOURCE = 'e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    registry = load(REPO / 'data/league_map.json')
    old = json.loads(subprocess.check_output(['git', 'show', f'{BASE}:data/league_map.json'], cwd=REPO))
    original = list(csv.DictReader((REPO / registry['master_table']['path']).open(encoding='utf-8-sig')))
    for old_row, row, master in zip(old['competitions'], registry['competitions'], original):
        assert row['master_fields'] == old_row['master_fields'] == master
        context = row['official_catalogue']['master_context']
        assert context['jc_match_count'] == int(master['竞彩合计'])
        assert context['sfc_match_count'] == int(master['14场合计'])
        assert context['jc_first_sale_day'] == (master['竞彩首见编号日'] or None)
        assert context['jc_last_sale_day'] == (master['竞彩末见编号日'] or None)
        assert context['sfc_first_issue'] == (master['14场首见计入期'] or None)
        assert context['sfc_last_issue'] == (master['14场末见计入期'] or None)
        assert context['source_names'] == master['来源出现过的名称'].split('、')
    for provider, entries in old['providers'].items():
        for code, value in entries.items():
            assert registry['providers'][provider][code] == value
    for old_row, new_row in zip(old['competitions'][:21], registry['competitions'][:21]):
        assert {key: new_row[key] for key in old_row} == old_row
    for row in registry['competitions'][21:]:
        assert row['status'] == 'candidate'
        assert all(entry['status'] == 'candidate' for entry in row['mappings'])
    new_codes = registry['providers']['apif'].keys() - old['providers']['apif'].keys()
    for code in new_codes:
        provider = registry['providers']['apif'][code]
        assert provider['status'] == 'candidate'
        assert provider['daily'] is False and provider['backfill'] is False
        assert provider['config']['seasons_available'] == []
        assert provider['access_status'] == 'unverified'
    raw = load(ARCHIVE / 'state/catalogue/43539439430-1/apif-raw.json')
    apif = {str(row['league']['id']): row for row in raw['response']}
    dictionary = load(ARCHIVE / 'state/catalogue/43539673769-1/jc/2025-11/2025-11-01-p1.json')['value']['leagueList']
    official = {row['leagueId']: row for row in dictionary}
    matches = defaultdict(dict)
    for path in sorted((ARCHIVE / 'state/catalogue/43539673769-1/jc').rglob('*.json')):
        if path.name.endswith('.meta.json'):
            continue
        for match in load(path)['value'].get('matchResult', []):
            assert match['matchId'] not in matches[match['leagueId']]
            matches[match['leagueId']][match['matchId']] = match['matchDate']
    seen_evidence = set()
    checked_pointers = 0

    def check_evidence(value):
        nonlocal checked_pointers
        if isinstance(value, dict):
            if {'kind', 'sha256', 'data_commit', 'pointer', 'path'} <= value.keys():
                key = (value['data_commit'], value['path'], value['sha256'], value['pointer'])
                if key not in seen_evidence:
                    seen_evidence.add(key)
                    assert value['data_commit'] in {BASE, SOURCE}
                    root = REPO if value['data_commit'] == BASE else ARCHIVE
                    path = root / value['path']
                    assert digest(path) == value['sha256']
                    if value['pointer'].startswith('/'):
                        target = load(path)
                        for component in value['pointer'].split('/')[1:]:
                            target = target[int(component)] if isinstance(target, list) else target[component]
                        checked_pointers += 1
            for item in value.values():
                check_evidence(item)
        elif isinstance(value, list):
            for item in value:
                check_evidence(item)

    all_official_codes = set()
    for row in registry['competitions']:
        proposal = row['api_catalogue_proposal']
        assert proposal['searched_names']
        for entry in proposal['entries']:
            source = apif[entry['code']]
            assert entry['name'] == source['league']['name']
            assert entry['country'] == source['country']['name']
            assert entry['type'] == source['league']['type']
            assert entry['catalogue_seasons'] == [s['year'] for s in source['seasons']]
        if proposal['status'] == 'existing_verified':
            assert all(any(m['provider'] == 'apif' and m['code'] == entry['code'] and m['status'] == 'verified'
                           for m in row['mappings']) for entry in proposal['entries'])
        else:
            assert proposal['review_status'] == 'pending'
        off = row['official_catalogue']
        assert off['review_status'] == 'pending' and off['status'] != 'verified'
        for entry in off['entries']:
            code = entry['league_id']
            all_official_codes.add(code)
            source = official[code]
            assert entry['league_abbr'] == source['leagueAbbName']
            assert entry['league_name'] == source['leagueAllName']
            dates = list(matches.get(code, {}).values())
            assert entry['observed_match_count'] == len(dates)
            assert entry['first_match_date'] == (min(dates) if dates else None)
            assert entry['last_match_date'] == (max(dates) if dates else None)
        check_evidence(off)
        check_evidence(proposal)
        if not proposal['entries']:
            check_evidence(row['mappings'])
    assert set(matches) <= all_official_codes
    # Fresh JSON serialization of old routes is also byte identical, not just key equality.
    for provider, entries in old['providers'].items():
        preserved = {code: registry['providers'][provider][code] for code in entries}
        assert json.dumps(entries, ensure_ascii=False, indent=2).encode() == json.dumps(preserved, ensure_ascii=False, indent=2).encode()
    result = {
        'result': 'passed', 'master_rows_checked': len(registry['competitions']),
        'existing_route_objects_unchanged': sum(len(v) for v in old['providers'].values()),
        'existing_verified_row_bodies_unchanged': 21,
        'new_routes_all_candidate_and_disabled': len(new_codes),
        'distinct_evidence_references_hashed': len(seen_evidence),
        'raw_json_pointers_resolved': checked_pointers,
        'official_dictionary_rows': len(dictionary), 'official_observed_rows': len(matches),
        'official_matches_recounted': sum(len(v) for v in matches.values()),
        'unmapped_observed_official_ids': sorted(set(matches) - all_official_codes),
    }
    (OUT / 'verification.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
