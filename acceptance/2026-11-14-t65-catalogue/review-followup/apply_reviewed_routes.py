"""Apply the explicit PR 135 identity approvals after assigning distinct project keys."""

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path('/workspace/work/football-t65')
OUT = Path('/workspace/work/t65-evidence/review-followup')
HEAD = '9032724c6b2a3d5d65893e99ef48a55c49f8e778'
REVIEW_ID = 'PRR_kwDOVeyMJc8AAAABX7eAxQ'
REVIEW_TIME = '2026-11-14T02:21:53Z'
APPROVED = set('79 89 99 307 41 235 80 95 114 104 293 45 81 48 101 137 66 143 102 90 46 73 294 96 547 556 529 528 105 526 115 17 13 15 18 531 1168 16 32 30 34 37 10 1 6 4 7 9 22 960 1222 23 480 532 524 490 38 897 803 965 1245 1188'.split())
PENDING = {'40', '62', '71', '850', '916'}
NAMES = {
    '40': ('英冠', 20), '62': ('法乙', 81), '71': ('巴甲', 6),
    '79': ('德乙', 34), '89': ('荷乙', 80), '99': ('日乙', 43),
    '307': ('沙职', 2068446), '41': ('英甲', 21), '235': ('俄超', 100),
    '80': ('德丙', None), '293': ('韩职K2', None), '95': ('葡甲', None),
    '114': ('瑞典超甲', None), '104': ('挪甲', None),
    '45': ('英足总杯', 23), '81': ('德国杯', 36), '48': ('英联赛杯', 24),
    '101': ('日联赛杯', 46), '137': ('意大利杯', 38), '66': ('法国杯', 30),
    '143': ('西国王杯', 61), '102': ('日天皇杯', 45), '90': ('荷兰杯', 15),
    '46': ('英锦标赛', 86), '73': ('巴西杯', 7), '294': ('韩国杯', 120),
    '96': ('葡萄牙杯', 54), '547': ('意超杯', 41), '556': ('西超杯', 66),
    '529': ('德超杯', 82), '528': ('英社区盾', 26), '105': ('挪威杯', 52),
    '526': ('法超杯', 29), '115': ('瑞典杯', 67),
    '17': ('亚冠精英', 1), '13': ('解放者杯', 49), '15': ('俱世界杯', 14),
    '18': ('亚冠乙', 2068445), '531': ('欧超杯', 71),
    '1168': ('国际足联洲际杯', None), '16': ('中北美冠', 90),
    '32': ('世预赛欧洲区', None), '10': ('国际友谊赛', None),
    '30': ('世预赛亚洲区', None), '1': ('世界杯', 72), '6': ('非洲杯', 3),
    '4': ('欧洲杯', 27), '7': ('亚洲杯', 4), '34': ('世预赛南美区', None),
    '9': ('美洲杯', 13), '22': ('金杯赛', 11), '960': ('欧预赛', 28),
    '1222': ('国际足联系列赛', None), '37': ('世预赛洲际附加赛', None),
    '23': ('东亚锦', 78), '916': ('麒麟杯', None), '480': ('奥运男足', 93),
    '532': ('亚洲杯23', 118), '524': ('奥运女足', 94), '490': ('世界杯20', 75),
    '38': ('欧锦赛21', 19), '850': ('欧锦预21', 92), '897': ('女亚洲杯', 2070739),
    '803': ('亚运男足', 83), '965': ('U20亚洲杯', None), '1245': ('亚运女足', 106),
    '1188': ('女东亚锦', 95),
}
# These master associations still require an owner boundary / competing-ID decision;
# approving the provider identity itself does not silently resolve that question.
UNRESOLVED_ROWS = {27, *range(29, 37), 57, 61, 75, 76, 79, 84}


def review_evidence(code, claim):
    return {
        'kind': 'pr_identity_review',
        'url': 'https://github.com/qw5563680/FOOTBALL/pull/135',
        'review_id': REVIEW_ID, 'reviewed_utc': REVIEW_TIME,
        'reviewed_commit': HEAD, 'provider': 'apif', 'code': code,
        'claim': claim,
    }


def main():
    old = json.loads(subprocess.check_output(['git', 'show', f'{HEAD}:data/league_map.json'], cwd=ROOT))
    doc = copy.deepcopy(old)
    review = json.loads((OUT / 'claude-review.json').read_text())['reviews'][0]
    assert review['id'] == REVIEW_ID and review['submitted_at'] == REVIEW_TIME
    assert len(APPROVED) == 62 and not APPROVED & PENDING
    candidates = {code for code, entry in old['providers']['apif'].items() if entry['status'] == 'candidate'}
    assert candidates == APPROVED | PENDING == NAMES.keys()
    official = {entry['league_id']: entry for row in old['competitions']
                for entry in row['official_catalogue']['entries']}
    changed_names = []
    for code, (name, official_id) in NAMES.items():
        entry = doc['providers']['apif'][code]
        english = entry['config']['league']
        entry['config']['league'] = name
        if official_id:
            source = official[official_id]
            assert name == source['league_abbr']
            naming = copy.deepcopy(source['evidence'][0])
            naming.update(kind='project_league_key',
                          claim=f'Project import key is the exact official abbreviation {name}; raw API English name stays in api_name. Official row boundaries remain separately pending where stated.')
        else:
            naming = review_evidence(code,
                f'Fixed project import key {name} identifies this API competition. Generic official names or lower-division dictionary gaps cannot supply a unique key; no fuzzy linkage or owner classification is implied.')
            naming['kind'] = 'project_league_key'
        entry['evidence'].append(naming)
        if code in APPROVED:
            entry.update(status='verified', daily=True, backfill=True,
                         verified_scope='pr_identity_review_only_access_unverified')
            entry['evidence'].append(review_evidence(code,
                f'PR 135 explicitly approved API-Football identity {code}. T61 enables daily/backfill for reviewed related import routes. The review list contains 62 unique IDs despite saying 60; this approval grants no claim of fixture-season account access.'))
        assert entry['access_status'] == 'unverified'
        assert entry['config']['seasons_available'] == []
        changed_names.append({'code': code, 'api_name': english, 'project_league': name,
                              'official_id': official_id, 'status': entry['status']})
    reviewed_rows = []
    for number, row in enumerate(doc['competitions'], 1):
        if number <= 21:
            continue
        proposal = row['api_catalogue_proposal']
        codes = [entry['code'] for entry in proposal['entries']]
        if len(codes) == 1 and codes[0] in APPROVED and number not in UNRESOLVED_ROWS:
            code = codes[0]
            for mapping in row['mappings']:
                if mapping['provider'] == 'apif' and mapping['code'] == code:
                    mapping['status'] = 'verified'
                    mapping['evidence'].append(review_evidence(code,
                        f'PR 135 reviewed this one-to-one master/API identity. The separate official_catalogue owner-decision flag, if present, remains unchanged.'))
            row['status'] = 'verified'
            proposal.update(status='verified', review_status='verified',
                reason='PR135审查者已逐项对照真实原件核准此一对一API身份；目录年份仍不是账号权限，竞彩官方拆分/归类结论单独保留。')
            proposal['entries'][0]['evidence'].append(review_evidence(code,
                'Explicit one-to-one master/API identity approved in PR 135; season-access status stays unverified.'))
            row['missing_reason'] = ('API身份已核准；竞彩官方归类仍需所有者决定：' + row['official_catalogue']['reason']
                                     if row['official_catalogue']['owner_decision_required'] else '')
            reviewed_rows.append(row['master_row_id'])
        elif codes:
            proposal['reason'] += ' PR135仅核准清单内provider身份；本行的多候选、时期边界或来源口径仍未决定，行映射继续candidate。'
            row['missing_reason'] = proposal['reason']
    for support in doc['supporting_scope']['playoff_leagues']:
        code = support['code']
        if code in APPROVED:
            support['status'] = 'verified'
            support['evidence'].append(review_evidence(code,
                'The lower-league provider identity is approved; this does not settle playoff fixture assignment in the master row.'))
    for provider, entries in old['providers'].items():
        for code, entry in entries.items():
            if provider != 'apif' or code not in NAMES:
                assert doc['providers'][provider][code] == entry
    for original, updated in zip(old['competitions'][:21], doc['competitions'][:21]):
        assert original == updated
    for original, updated in zip(old['competitions'], doc['competitions']):
        assert original['official_catalogue'] == updated['official_catalogue']
        assert original['master_fields'] == updated['master_fields']
    (ROOT / 'data/league_map.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
    report = {
        'review_id': REVIEW_ID, 'reviewed_utc': REVIEW_TIME, 'reviewed_commit': HEAD,
        'review_source_sha256': hashlib.sha256((OUT / 'claude-review.json').read_bytes()).hexdigest(),
        'review_says_approved_count': 60, 'explicit_approved_unique_count': len(APPROVED),
        'approved_provider_ids': sorted(APPROVED, key=int), 'pending_provider_ids': sorted(PENDING, key=int),
        'renamed_routes': changed_names, 'one_to_one_master_mappings_approved': reviewed_rows,
        'master_status_counts': dict(Counter(row['status'] for row in doc['competitions'])),
        'api_proposal_status_counts': dict(Counter(row['api_catalogue_proposal']['status'] for row in doc['competitions'])),
        'official_owner_decision_rows_unchanged': sum(row['official_catalogue']['owner_decision_required'] for row in doc['competitions']),
        'network_requests': 0, 'download_or_database_changes': False,
    }
    (OUT / 'reviewed-route-changes.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key not in {'renamed_routes', 'approved_provider_ids', 'one_to_one_master_mappings_approved'}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
