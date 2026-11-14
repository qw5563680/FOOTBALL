"""T65 manual decisions; mechanically copy only source metadata and observations.

This is a scratch evidence builder, not an approval or fuzzy-name matching tool.
The row -> ID decisions below were individually read against the fixed raw files.
"""

from collections import Counter, defaultdict
import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess

REPO = Path('/workspace/work/football-t65')
ARCHIVE = Path('/workspace/work/t65-fixed-archive')
OUT = Path('/workspace/work/t65-evidence/mappings')
COMMIT = 'e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4'
BASE = 'b83d2cc7d975e41f418f59aad3341a7d1574f95a'
APIF_PATH = 'state/catalogue/43539439430-1/apif-raw.json'
JC_PATH = 'state/catalogue/43539673769-1/jc.json'
DICT_PATH = 'state/catalogue/43539673769-1/jc/2025-11/2025-11-01-p1.json'
MASTER_PATH = 'docs/research/2026-11-14-jc-sfc-competition-master-table.csv'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def evidence(kind, path, pointer, claim, *, commit=COMMIT):
    base = REPO if commit == BASE else ARCHIVE
    return {'kind': kind, 'path': path, 'sha256': sha(base / path),
            'data_commit': commit, 'pointer': pointer, 'claim': claim}


# Explicit manual API selections in master order. Composite entries are proposals
# for source leagues / competing interpretations, not automatic playoff routing.
API_IDS = [
    [39], [140], [135], [78], [2], [40], [61], [3], [94], [113],
    [103], [253], [62], [88], [98], [5], [848], [244], [71], [292],
    [188], [79], [89], [99], [307], [41], [98], [235], [61, 62], [79, 80],
    [78, 79], [292, 293], [94, 95], [88, 89], [113, 114], [103, 104],
    [45], [81], [48], [101], [137], [66], [143], [102], [90], [46],
    [73], [294], [96], [547], [556], [529], [528], [105], [526], [115],
    [17], [13], [15], [18], [17], [531], [1168], [16], [32], [10], [30],
    [1], [6], [4], [7], [34], [9], [22], [960], [1222], [37], [23],
    [916, 10], [480], [532], [524], [490], [38, 850], [897], [803],
    [965], [1245], [1188], [],
]

# Dictionary entries considered manually. [] means no named dictionary identity
# found; zero observed results is distinct from dictionary absence.
OFFICIAL_IDS = [
    [25], [62], [40], [37], [69], [20], [32], [70], [55], [58],
    [51], [50], [81, 32], [17], [42], [127], [1033103], [2064839], [6], [48],
    [2], [34], [80], [43], [2068446], [21], [42], [100], [32, 81], [34],
    [37, 34], [48], [55], [17, 80], [58], [51], [23], [36], [24], [46],
    [38], [30], [61], [45], [15], [86], [7], [120], [54], [41], [66],
    [82], [26], [52], [29], [67], [1], [49], [14], [2068445], [1], [71],
    [76], [90], [73], [39], [73], [72], [3], [27], [4], [73], [13], [11],
    [28], [39, 73], [73], [78], [39], [93], [118], [94], [75], [19, 92],
    [2070739], [83], [], [106], [95], [],
]

OWNER_GROUPS = [
    ('france-playoffs', [7, 13, 29], '法国甲乙联赛及附加赛',
     '确定常规法甲/法乙与升降级附加赛的逐场划分；总表法乙别名还含法甲，不能把官方整联赛场数复制到各行。'),
    ('germany-playoffs', [4, 22, 30, 31], '德国两级升降级附加赛',
     '区分德甲/德乙、德乙/德丙附加赛与常规联赛；官方字典没有德丙专名，API上下级目录不能证明附加赛存在哪一联赛。'),
    ('korea-playoffs', [20, 32], '韩国K1/K2附加赛',
     '确定官方韩职中的常规赛/附加赛划分及K2来源；官方没有独立K2字典项。'),
    ('portugal-playoffs', [9, 33], '葡超/葡甲附加赛',
     '确定官方葡超中的附加赛范围及葡甲来源；不能由目录推定API赛季包含这些比赛。'),
    ('netherlands-playoffs', [14, 23, 34], '荷甲/荷乙附加赛',
     '总表附加赛来源只写荷乙；确认官方荷甲/荷乙的实际归属及与常规赛去重规则。'),
    ('sweden-playoffs', [10, 35], '瑞典升降级附加赛',
     '确定瑞超与Superettan附加赛的逐场归属；官方字典没有瑞典次级专名。'),
    ('norway-playoffs', [11, 36], '挪威升降级附加赛',
     '确定挪超与挪甲附加赛的逐场归属；官方字典没有挪甲专名。'),
    ('japan-centennial', [15, 27], '日职与J1百年构想联赛',
     '官方42日职与API98 J1均无法仅凭目录分开常规J1和2026百年构想联赛；确定赛季/阶段边界。'),
    ('afc-reform', [57, 61], '改制前亚冠与亚冠精英',
     '当前官方1和API17全称指向精英，API17又含改制前年份；确定旧亚冠与精英的时间/阶段边界，不把全目录重复算给两行。'),
    ('world-cup-regions', [65, 67, 72, 77], '世预赛分赛区与洲际附加赛',
     '官方73只有统一世预赛；确定赛区与洲际附加赛的逐场分类，不能把57场官方总数当作各赛区场数。'),
    ('generic-friendlies', [66, 76, 79], '国际赛、FIFA系列赛与麒麟挑战杯',
     '官方39国际赛覆盖多个口径；FIFA系列赛来源名世界杯预赛与赛事标题冲突。API916仅列Kirin Cup 2022/2026而总表为2025麒麟挑战杯，不核准为同一赛。'),
    ('generic-cups', [63], '官方杯赛与国际足联洲际杯',
     '官方76仅名杯赛，窗口两场对阵支持洲际杯候选，但需明确按逐场证据归类，不能通用映射杯赛。'),
    ('euro-qualifiers', [75], '欧洲杯预选赛的来源名',
     '总表赛事为欧洲杯预选赛，来源名只有欧洲杯；官方28预选赛与27正赛不同，须确认原工作簿分类是否正确。'),
    ('u21-finals-qualifiers', [84], 'U21欧洲杯正赛或预选赛',
     '总表标题U21欧洲杯、2025年28场及末见6月28日指向正赛，但来源名欧U21外指向预选赛；官方19/92、API38/850不可混并。'),
]
OWNER_BY_ROW = {row: group for group in OWNER_GROUPS for row in group[1]}

OFFICIAL_EXTRA_SEARCH = {
    30: ['德丙', '德国丙级联赛'], 32: ['K2联赛', '韩国K2联赛'],
    33: ['葡甲', '葡萄牙甲级联赛'], 35: ['瑞典超甲', '瑞典次级联赛'],
    36: ['挪甲', '挪威甲级联赛'], 63: ['国际足联洲际杯', '洲际杯', '杯赛'],
    76: ['FIFA系列赛', '国际足联系列赛', '国际赛', '世界杯预选赛'],
    79: ['麒麟挑战杯', '麒麟杯', '国际赛'], 87: ['U20亚洲杯', '亚洲U20锦标赛', '亚青赛'],
    90: ['熊猫杯', 'Panda Cup'],
}
API_EXTRA_SEARCH = {
    27: ['J1 Centennial League', 'J1 100 Year Vision League', '100 Year Vision', 'Centennial'],
    29: ['Ligue 1 Play-offs', 'Ligue 2 Play-offs'],
    30: ['2. Bundesliga Relegation Play-offs', '3. Liga Play-offs'],
    31: ['Bundesliga Relegation Play-offs'],
    32: ['K League Play-offs', 'K League 1 Relegation'],
    33: ['Primeira Liga Relegation Play-offs'],
    34: ['Eredivisie Relegation Play-offs', 'Eerste Divisie Play-offs'],
    35: ['Allsvenskan Relegation Play-offs', 'Superettan Play-offs'],
    36: ['Eliteserien Relegation Play-offs', '1. Division Play-offs'],
    57: ['AFC Champions League', 'AFC Champions League Elite'],
    61: ['AFC Champions League', 'AFC Champions League Elite'],
    64: ['CONCACAF Champions Cup', 'CONCACAF Champions League'],
    79: ['Kirin Challenge Cup', 'Kirin Cup', 'Friendlies'],
    84: ['UEFA U21 Championship', 'UEFA U21 Championship - Qualification'],
    90: ['Panda Cup', 'Panda', '熊猫杯'],
}


def ordered_unique(values):
    return list(dict.fromkeys(values))


def main():
    baseline = json.loads(subprocess.check_output(
        ['git', 'show', f'{BASE}:data/league_map.json'], cwd=REPO))
    registry = copy.deepcopy(baseline)
    raw = read(ARCHIVE / APIF_PATH)
    assert sha(ARCHIVE / APIF_PATH) == '173c44cd57bc04a0df30b56105dbe9ac077cdbb123e4e1145b7f620342f5d27a'
    assert len(raw['response']) == 1251
    assert sum(len(row['seasons']) for row in raw['response']) == 8816
    apif = {r['league']['id']: (i, r) for i, r in enumerate(raw['response'])}
    dictionary = read(ARCHIVE / DICT_PATH)['value']['leagueList']
    official = {r['leagueId']: (i, r) for i, r in enumerate(dictionary)}
    assert len(official) == 113
    observations = defaultdict(list)
    raw_page_index = []
    seen = set()
    for path in sorted((ARCHIVE / 'state/catalogue/43539673769-1/jc').rglob('*.json')):
        if path.name.endswith('.meta.json'):
            continue
        value = read(path)['value']
        assert value['leagueList'] == dictionary
        rel = path.relative_to(ARCHIVE).as_posix()
        matches = value.get('matchResult', [])
        raw_page_index.append({'path': rel, 'sha256': sha(path), 'match_count': len(matches)})
        for idx, match in enumerate(matches):
            assert match['matchId'] not in seen
            seen.add(match['matchId'])
            observations[match['leagueId']].append({
                'date': match['matchDate'], 'match_id': match['matchId'], 'path': rel,
                'pointer': f'/value/matchResult/{idx}', 'home': match['allHomeTeam'],
                'away': match['allAwayTeam'],
            })
    assert len(raw_page_index) == 406
    assert len(observations) == 54 and len(seen) == 4966
    parsed_counts = Counter()
    jc = read(ARCHIVE / JC_PATH)
    for month in jc['months']:
        for league in month['leagues']:
            parsed_counts[league['league_id']] += league['match_count']
    assert dict(parsed_counts) == {k: len(v) for k, v in observations.items()}

    def api_entry(code):
        idx, source = apif[code]
        return {'code': str(code), 'name': source['league']['name'],
                'country': source['country']['name'], 'type': source['league']['type'],
                'catalogue_seasons': [s['year'] for s in source['seasons']],
                'evidence': [evidence('apif_raw_catalogue', APIF_PATH, f'/response/{idx}',
                    'Manually selected identity; name/country/type and the complete season-year list are copied exactly from this raw entry. Catalogue visibility is not access permission.')]}

    def official_entry(code):
        idx, source = official[code]
        matches = sorted(observations.get(code, []), key=lambda v: (v['date'], v['match_id']))
        refs = [evidence('jc_raw_dictionary', DICT_PATH, f'/value/leagueList/{idx}',
                 'Official ID, abbreviation and full name copied exactly; the same 113-entry dictionary occurs in all 406 archived pages.'),
                evidence('jc_observed_counts', JC_PATH, '/months',
                 f'Sum match_count for league_id={code} over all 12 month.leagues arrays. Counts describe this entire official ID, not a disaggregated master row. No observation is not dictionary absence.')]
        if matches:
            for label, match in [('first', matches[0]), ('last', matches[-1])]:
                refs.append(evidence(f'jc_{label}_observation', match['path'], match['pointer'],
                    f"{label.title()} observed matchDate for official ID {code}; matchId={match['match_id']}. Dates use official matchDate, not master sale-number day."))
        return {'league_id': code, 'league_abbr': source['leagueAbbName'],
                'league_name': source['leagueAllName'], 'observed_match_count': len(matches),
                'first_match_date': matches[0]['date'] if matches else None,
                'last_match_date': matches[-1]['date'] if matches else None, 'evidence': refs}

    assert len(API_IDS) == len(OFFICIAL_IDS) == 90
    for number, row in enumerate(registry['competitions'], 1):
        fields = row['master_fields']
        official_entries = [official_entry(code) for code in OFFICIAL_IDS[number - 1]]
        group = OWNER_BY_ROW.get(number)
        if not official_entries:
            proposed = 'not_found'
            reason = '在113项官方字典中未查到该赛事专名；窗口内无独立赛事ID证据。不得用U23或其他年龄组/泛称替代，也不等于接口无历史数据。'
        elif group:
            proposed = 'candidate'
            reason = group[3] + ' 下列官方场数是整个leagueId的窗口汇总，尚未划分为本总表行。'
        else:
            proposed = 'verified'
            reason = ('人工按总表赛事含义、原始来源名称与官方全称对照：仅简称/别名不同，提案为同一赛事；仍待独立审查，不改变任何已核准路由。'
                      if row['master_name'] != official_entries[0]['league_name'] else
                      '总表赛事名称与官方全称一致；仍待独立审查，不改变任何已核准路由。')
            if not official_entries[0]['observed_match_count']:
                reason += ' 官方字典有该项，但2025-11-01至2026-10-31未观察到该ID比赛；这不构成所有者决策或接口无数据。'
        searched_official = ordered_unique(
            [row['master_name'], row['project_name'], *row['source_names'],
             *OFFICIAL_EXTRA_SEARCH.get(number, []),
             *[entry['league_name'] for entry in official_entries],
             *[entry['league_abbr'] for entry in official_entries]])
        master_ref = evidence('master_table_row', MASTER_PATH, f'csv:data-row:{number}',
            'Immutable master row: source names, 2024-2026 sale-position totals and first/last sale-number day or SFC issue; different window/basis from official matchDate totals.', commit=BASE)
        official_refs = [master_ref]
        if not official_entries:
            official_refs.append(evidence('jc_dictionary_search', DICT_PATH, '/value/leagueList',
                'Manually inspected the complete 113-entry dictionary using searched_names; no dedicated official identity found.'))
        row['official_catalogue'] = {
            'status': 'not_found' if proposed == 'not_found' else 'candidate',
            'proposed_status': proposed, 'review_status': 'pending',
            'searched_names': searched_official, 'reason': reason,
            'owner_decision_required': group is not None, 'entries': official_entries,
            'master_context': {
                'source_names': row['source_names'], 'jc_match_count': int(fields['竞彩合计']),
                'jc_first_sale_day': fields['竞彩首见编号日'] or None,
                'jc_last_sale_day': fields['竞彩末见编号日'] or None,
                'sfc_match_count': int(fields['14场合计']),
                'sfc_first_issue': fields['14场首见计入期'] or None,
                'sfc_last_issue': fields['14场末见计入期'] or None,
            },
            'observation_window': {
                'start_beijing': '2025-11-01', 'end_beijing': '2026-10-31',
                'count_basis': 'Official matchDate in Asia/Shanghai; entire leagueId counts, never additive across candidate master rows. Master counts are 2024-2026 sale positions, by sale-number day / first-sale SFC issue; no equal-count assertion.',
            }, 'evidence': official_refs,
        }
        entries = [api_entry(code) for code in API_IDS[number - 1]]
        approved_codes = {mapping['code'] for mapping in row['mappings']
                          if mapping['provider'] == 'apif' and mapping['status'] == 'verified'}
        existing = entries and all(entry['code'] in approved_codes for entry in entries)
        api_status = 'existing_verified' if existing else 'candidate' if entries else 'not_found'
        api_reason = '真实目录名称、国家与赛事类型支持此人工候选；赛季年份逐项保留原件值。需审查者核对身份；目录不证明赛程/赛果下载权限。'
        if existing:
            api_reason = '保留原有已核准API身份与全部路由配置；本次只追加真实目录元数据证据，未重核账号权限或扩张赛季范围。'
        if number == 27:
            api_reason = '目录只有J1 League 98，没有百年构想专名；98为待核候选，不能据目录断言2026季包含哪些百年构想比赛。'
        elif 29 <= number <= 36:
            api_reason = '只找到对应上下级联赛；这些是附加赛所需历史来源的候选，不是已证明的独立附加赛ID。需按fixtures的赛季/round和逐场对阵确定附加赛归属与去重。'
        elif number in {57, 61}:
            api_reason = '17现名AFC Champions League Elite但赛季列表含2016起历史；同一ID可能跨改制时期。旧亚冠/精英分行的赛季、阶段边界仍待核，不能合并或重复导入。'
        elif number == 79:
            api_reason = '916原文是Kirin Cup，仅列2022/2026，不能证明总表2025麒麟挑战杯。10 Friendlies是另一候选来源；保留二者待逐场核验，绝不把Kirin Cup自动等同Kirin Challenge Cup。'
        elif number == 84:
            api_reason = '总表标题和28场/2025-06-28末见支持U21正赛38，但来源名欧U21外指向预选赛850。两种解释均仅候选，须确认总表赛事口径后再选；不能合并青年正赛与预选赛。'
        elif not entries:
            api_reason = '已在归档1251赛事全目录按Panda Cup、Panda、熊猫杯检查，未找到专名；不推断账号权限、其他端点或历史数据不存在。'
        searched_api = ordered_unique([row['master_name'], row['project_name'], *row['source_names'],
                                       *API_EXTRA_SEARCH.get(number, []), *[entry['name'] for entry in entries]])
        row['api_catalogue_proposal'] = {
            'status': api_status, 'review_status': 'existing_verified' if existing else 'pending',
            'searched_names': searched_api, 'reason': api_reason, 'entries': entries,
        }
        if not entries:
            # The agreed shape has no proposal-level evidence: attach search
            # provenance to the unchanged candidate mapping evidence instead.
            for mapping in row['mappings']:
                if mapping['provider'] == 'apif':
                    mapping['evidence'] = [evidence('apif_raw_catalogue_search', APIF_PATH,
                        '/response', 'Complete 1251-entry raw catalogue manually searched for Panda Cup, Panda and 熊猫杯; no dedicated named entry found.')]
        if number > 21 and entries:
            row['mappings'] = [
                *[{'provider': 'apif', 'code': entry['code'], 'status': 'candidate',
                   'evidence': copy.deepcopy(entry['evidence'])} for entry in entries],
                *[m for m in row['mappings'] if m['provider'] != 'apif'],
            ]
        if number > 21:
            row['missing_reason'] = api_reason + ' 所有新增映射保持candidate，待独立审查；daily/backfill均不新增启用。'
        for entry in entries:
            code = entry['code']
            if code in registry['providers']['apif']:
                continue
            registry['providers']['apif'][code] = {
                'status': 'candidate', 'mode': 'import', 'groups': ['A'], 'relevant': True,
                'daily': False, 'backfill': False,
                'config': {'league': entry['name'], 'api_name': entry['name'],
                           'api_country': entry['country'], 'api_type': entry['type'],
                           'seasons_available': [], 'catalogue_seasons': entry['catalogue_seasons']},
                'evidence': copy.deepcopy(entry['evidence']), 'access_status': 'unverified',
            }

    # Every lower league requested by the eight playoff rows, including leagues
    # already represented by a master row. All are candidate *support proposals*.
    supporting = [
        ('法乙', 62, [29]), ('德乙', 79, [31]), ('德丙', 80, [30]),
        ('韩职K2', 293, [32]), ('葡甲', 95, [33]), ('荷乙', 89, [34]),
        ('瑞典超甲', 114, [35]), ('挪甲', 104, [36]),
    ]
    registry['supporting_scope']['playoff_leagues'] = []
    for label, code, rows in supporting:
        entry = api_entry(code)
        registry['supporting_scope']['playoff_leagues'].append({
            'status': 'candidate', 'master_row_ids': [f'master-{i:03}' for i in rows],
            'league_name': label, 'code': str(code), 'evidence': copy.deepcopy(entry['evidence']),
        })
        assert str(code) not in baseline['providers']['apif']
        registry['providers']['apif'][str(code)]['groups'] = ['A', 'B']

    # Prevent accidental changes to reviewed routes and the 21 legacy row bodies.
    for provider, entries in baseline['providers'].items():
        for code, value in entries.items():
            assert registry['providers'][provider][code] == value
    for original, updated in zip(baseline['competitions'][:21], registry['competitions'][:21]):
        assert {k: updated[k] for k in original} == original
    write(REPO / 'data/league_map.json', registry)
    write(OUT / 'raw-page-index.json', {'data_commit': COMMIT, 'pages': raw_page_index})
    decisions = [
        {'decision_id': code, 'master_row_ids': [f'master-{i:03}' for i in rows],
         'topic': title, 'decision': reason}
        for code, rows, title, reason in OWNER_GROUPS
    ]
    write(OUT / 'owner-decisions.json', decisions)
    write(OUT / 'official-observations.json', {
        'data_commit': COMMIT, 'dictionary_count': len(dictionary), 'page_count': len(raw_page_index),
        'observed_league_count': len(observations), 'observed_match_count': len(seen),
        'entries': [official_entry(code) for code in sorted(official)],
    })
    with (OUT / 'mapping-evidence.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            'master_row_id', 'master_name', 'source_names', 'master_jc_count', 'master_jc_first',
            'master_jc_last', 'master_sfc_count', 'master_sfc_first_issue', 'master_sfc_last_issue',
            'official_proposed_status', 'official_ids', 'official_abbreviations', 'official_full_names',
            'official_counts', 'official_first_dates', 'official_last_dates', 'official_reason',
            'api_status', 'api_ids', 'api_names', 'api_countries', 'api_types', 'api_seasons', 'api_reason',
        ])
        writer.writeheader()
        for row in registry['competitions']:
            off, api = row['official_catalogue'], row['api_catalogue_proposal']
            master = off['master_context']
            writer.writerow({
                'master_row_id': row['master_row_id'], 'master_name': row['master_name'],
                'source_names': row['source_names_raw'], 'master_jc_count': master['jc_match_count'],
                'master_jc_first': master['jc_first_sale_day'], 'master_jc_last': master['jc_last_sale_day'],
                'master_sfc_count': master['sfc_match_count'], 'master_sfc_first_issue': master['sfc_first_issue'],
                'master_sfc_last_issue': master['sfc_last_issue'], 'official_proposed_status': off['proposed_status'],
                'official_ids': '|'.join(str(e['league_id']) for e in off['entries']),
                'official_abbreviations': '|'.join(e['league_abbr'] for e in off['entries']),
                'official_full_names': '|'.join(e['league_name'] for e in off['entries']),
                'official_counts': '|'.join(str(e['observed_match_count']) for e in off['entries']),
                'official_first_dates': '|'.join(e['first_match_date'] or '' for e in off['entries']),
                'official_last_dates': '|'.join(e['last_match_date'] or '' for e in off['entries']),
                'official_reason': off['reason'], 'api_status': api['status'],
                'api_ids': '|'.join(e['code'] for e in api['entries']),
                'api_names': '|'.join(e['name'] for e in api['entries']),
                'api_countries': '|'.join(e['country'] for e in api['entries']),
                'api_types': '|'.join(e['type'] for e in api['entries']),
                'api_seasons': '|'.join(','.join(map(str, e['catalogue_seasons'])) for e in api['entries']),
                'api_reason': api['reason'],
            })
    counts = {
        'master_rows': 90, 'old_verified_rows_unchanged': 21,
        'official_proposals': dict(Counter(r['official_catalogue']['proposed_status'] for r in registry['competitions'])),
        'api_proposals': dict(Counter(r['api_catalogue_proposal']['status'] for r in registry['competitions'])),
        'candidate69_api_proposals': dict(Counter(r['api_catalogue_proposal']['status'] for r in registry['competitions'][21:])),
        'new_candidate_apif_provider_entries': len(registry['providers']['apif']) - len(baseline['providers']['apif']),
        'playoff_supporting_leagues': len(supporting), 'owner_decision_groups': len(decisions),
        'owner_decision_rows': sum(r['official_catalogue']['owner_decision_required'] for r in registry['competitions']),
        'official_dictionary_entries': 113, 'official_observed_leagues': 54, 'official_observed_matches': 4966,
    }
    write(OUT / 'summary.json', counts)
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
