"""Read-only T65 review: offline fixtures, temporary outputs, no sports network."""

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import types
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import requests

from zucai import cloud_catalogue as catalogue
from zucai import cloud_catalogue_backfill as backfill
from zucai.cloud_aliases import write_candidates
from zucai.cloud_api_backfill import api_backfill_scopes
from zucai.db import get_engine, init_db
from zucai.league_registry import daily_provider_config, load_registry
from zucai.sources.apif_command import fetch_apif
from zucai.sources.apif_ingest import COUNT_KEYS
from zucai.sources.base import RawStore

ROOT = Path('/workspace/work/football-t65')
OUT = Path('/workspace/work/t65-final-evidence/internal-review')
RAW = Path('/workspace/work/t65-fixed-archive/state/catalogue/43539439430-1/apif-raw.json')
BASE = '5c1384c0059553ace8ee58e4c1b7566f6120a715'
PRIOR = 'b83d2cc'
CONFLICTS = {('397', 2018), ('276', 2018), ('379', 2020), ('402', 2019), ('542', 2021)}
report = {}


def git_file(revision, name):
    return subprocess.check_output(['git', 'show', f'{revision}:{name}'], cwd=ROOT)


def encode(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True) + '\n').encode()


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def rejects(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError('Expected rejection')


def no_network(*args, **kwargs):
    raise AssertionError('Independent review must never request network')


def catalogue_file(root, document, run='review'):
    folder = root / 'state/catalogue' / run
    folder.mkdir(parents=True)
    payload = encode(document)
    (folder / 'apif.json').write_bytes(payload)
    pointer = dict(path=f'state/catalogue/{run}/apif.json', sha256=sha(payload))
    (folder.parent / 'latest.json').write_bytes(encode(dict(version=1, providers={'apif': pointer})))
    return folder, pointer


def database_snapshot(path):
    with sqlite3.connect(path) as con:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted(con.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr) for name in names}


def capture(root, doc):
    return RawStore(root).save(
        'api-football', 'review', encode(doc), 'json',
        captured_at=datetime(2026, 11, 3, tzinfo=UTC),
        url='https://v3.football.api-sports.io/fixtures?date=2026-11-01', status_code=200,
    )


def check_catalogue(root):
    raw = RAW.read_bytes()
    assert sha(raw) == '173c44cd57bc04a0df30b56105dbe9ac077cdbb123e4e1145b7f620342f5d27a'
    source = json.loads(raw)
    parsed = catalogue.parse_apif_catalogue(raw, raw_path='state/catalogue/43539439430-1/apif-raw.json')
    assert len(parsed['leagues']) == 1251
    assert parsed['raw_season_count'] == 8816
    assert parsed['conflicted_season_count'] == 5
    actual_conflicts = {(row['id'], s['year']) for row in parsed['leagues'] for s in row['seasons'] if s.get('status') == 'conflict'}
    assert actual_conflicts == CONFLICTS
    for warning in parsed['warnings']:
        if warning['code'] == 'duplicate_season_year':
            originals = []
            for pointer in warning['record_pointers']:
                node = source
                for part in pointer.strip('/').split('/'):
                    node = node[int(part)] if isinstance(node, list) else node[part]
                originals.append(node)
            assert originals == warning['original_records']
            assert warning['raw_sha256'] == sha(raw)
            assert warning['raw_path'] == 'state/catalogue/43539439430-1/apif-raw.json'

    legacy = types.ModuleType('t65_review_legacy_catalogue')
    exec(compile(git_file(BASE, 'src/zucai/cloud_catalogue.py'), 'legacy-cloud_catalogue.py', 'exec'), legacy.__dict__)
    unaffected = copy.deepcopy(source)
    for row in unaffected['response']:
        row['seasons'] = [s for s in row['seasons'] if (str(row['league']['id']), s['year']) not in CONFLICTS]
    old = legacy.parse_apif_catalogue(encode(unaffected))
    new_unaffected = copy.deepcopy(parsed['leagues'])
    for row in new_unaffected:
        row['seasons'] = [s for s in row['seasons'] if s.get('status') != 'conflict']
    assert old['leagues'] == new_unaffected
    assert sum(len(row['seasons']) for row in new_unaffected) == 8806
    date_warnings = [w for w in parsed['warnings'] if w['code'] == 'invalid_season_dates']
    assert date_warnings == old['warnings'] and len(date_warnings) == 1

    fixture = json.loads((ROOT / 'data/fixtures/t65/apif-catalogue-duplicate-years-real.json').read_bytes())
    expected_rows = [row for row in source['response'] if str(row['league']['id']) in {code for code, _ in CONFLICTS}]
    assert fixture['response'] == expected_rows
    mutation_results = []
    for mutation in ('missing-date', 'date-type', 'year-type', 'coverage-type', 'nested-flag-type', 'missing-name', 'duplicate-league'):
        doc = copy.deepcopy(fixture)
        row = doc['response'][0]
        duplicate = [s for s in row['seasons'] if s['year'] == 2018][-1]
        if mutation == 'missing-date':
            duplicate.pop('end')
        elif mutation == 'date-type':
            duplicate['end'] = []
        elif mutation == 'year-type':
            duplicate['year'] = True
        elif mutation == 'coverage-type':
            duplicate['coverage'] = []
        elif mutation == 'nested-flag-type':
            duplicate['coverage']['fixtures']['events'] = 'true'
        elif mutation == 'missing-name':
            row['league'].pop('name')
        else:
            doc['response'][1]['league']['id'] = row['league']['id']
        rejects(lambda: catalogue.parse_apif_catalogue(encode(doc)))
        mutation_results.append(mutation)

    # Old normalized output has neither new counter. Its verified old pointer must survive.
    old_raw = (ROOT / 'data/fixtures/t61/catalogue-apif-synthetic.json').read_bytes()
    old_doc = legacy.parse_apif_catalogue(old_raw)
    run = 'pre-fix'
    provenance = dict(raw_path=f'state/catalogue/{run}/apif-raw.json', meta_path=f'state/catalogue/{run}/apif-meta.json', raw_sha256=sha(old_raw), fetched_at_utc='2026-11-14T00:00:00+00:00', url='https://v3.football.api-sports.io/leagues')
    old_doc['provenance'] = provenance
    folder, pointer = catalogue_file(root / 'legacy', old_doc, run=run)
    (folder / 'apif-raw.json').write_bytes(old_raw)
    (folder / 'apif-meta.json').write_bytes(encode(dict(status_code=200, sha256=sha(old_raw), fetched_at_utc=provenance['fetched_at_utc'], url=provenance['url'])))
    pointer.update(run_id=run, raw_sha256=sha(old_raw))
    (folder.parent / 'latest.json').write_bytes(encode(dict(version=1, providers={'apif': pointer})))
    assert catalogue._previous_pointers(root / 'legacy') == ({'apif': pointer}, [])

    # Artificial authorization tests routing; it does not authorize real five-league downloads.
    five = catalogue.parse_apif_catalogue(encode(fixture))
    registry = dict(download_policy=dict(scope='related', max_requests=500, daily_reserve=500), providers={'apif': {row['id']: dict(status='verified', mode='import', backfill=True, relevant=True, config={'api_name': row['name'], 'league': row['name']}) for row in five['leagues']}})
    plan_root = root / 'plans'
    catalogue_file(plan_root, five)
    expected = {(row['id'], s['year']) for row in five['leagues'] for s in row['seasons'] if s.get('status') != 'conflict'}
    for policy in ('related', 'all'):
        registry['download_policy']['scope'] = policy
        for codes in (None, ','.join(sorted(registry['providers']['apif']))):
            plan = backfill.catalogue_scopes(plan_root, 'apif', codes, registry=registry)
            assert {(code, scope['season']) for code, scope in plan['scopes']} == expected
            assert {(e['code'], e['season']) for e in plan['excluded']} == CONFLICTS
            assert plan['access_is_verified'] is False
    for code, year in CONFLICTS:
        rejects(lambda: backfill.catalogue_scopes(plan_root, 'apif', code, year, year, registry=registry))
        rejects(lambda: api_backfill_scopes('apif', code, year, year))

    dispatched = []
    def spy_download(database, run_id, **kwargs):
        selected = {(code, scope['season']) for code, scope in kwargs['scopes']}
        assert selected == expected and not selected & CONFLICTS
        dispatched.append(dict(run=run_id, scopes=len(selected)))
        kwargs['source'].session.close()
        result = dict.fromkeys(COUNT_KEYS, 0)
        result.update(status='complete', scopes=[dict(status='skipped', scope=scope) for _, scope in kwargs['scopes']], processed_files=0, errors=[])
        return result
    with patch.object(backfill, 'require_probe_actions', lambda: None), patch.object(backfill, 'load_registry', lambda: registry), patch.object(backfill, 'provider_config', lambda provider: {code: entry['config'] for code, entry in registry['providers'][provider].items()}), patch.object(backfill, 'collect_planned_api_backfill', spy_download):
        for resume in (True, False):
            result = backfill.collect_catalogue_backfill(plan_root / 'sentinel.db', f'review-resume-{resume}', provider='apif', resume=resume)
            assert result['planned_scopes'] == 52 and result['requests'] == 0
        rejects(lambda: backfill.collect_catalogue_backfill(plan_root / 'sentinel.db', 'review-conflict', provider='apif', codes='397', first=2018, last=2018))
    assert not (plan_root / 'sentinel.db').exists()
    report['catalogue'] = dict(original_sha256=sha(raw), leagues=1251, raw_seasons=8816, normalized_seasons=8811, nonconflicting_seasons_equal_legacy=8806, conflicts=sorted(CONFLICTS), date_warning_equal_legacy=True, old_pointer_compatible=True, structural_mutations_rejected=mutation_results, dispatch=dispatched, real_fixture_rows_exact=True)


def check_daily(root):
    registry = load_registry()
    base = json.loads(git_file(BASE, 'data/league_map.json'))
    prior = json.loads(git_file(PRIOR, 'data/league_map.json'))
    changes = []
    for provider, entries in base['providers'].items():
        for code, entry in entries.items():
            after = registry['providers'][provider][code]
            if entry != after:
                expected = copy.deepcopy(entry)
                expected['daily'] = False
                assert after == expected and entry['daily'] is True and after['backfill'] is True
                changes.append((provider, code))
    assert len(changes) == 62 and all(provider == 'apif' for provider, _ in changes)
    assert registry['competitions'] == base['competitions']
    assert all(registry['providers'][p][c] == entry for p, entries in prior['providers'].items() for c, entry in entries.items())
    verified_old = [row for row in prior['competitions'] if row['status'] == 'verified']
    current_rows = {row['master_row_id']: row for row in registry['competitions']}
    assert len(verified_old) == 21
    assert all(current_rows[row['master_row_id']]['mappings'] == row['mappings'] for row in verified_old)
    codes = daily_provider_config('apif')
    assert set(codes) == set('98 292 188 113 103 244 119 253 169 3 848 5'.split())
    codes['98']['league'] = 'mutated review copy'
    assert daily_provider_config('apif')['98']['league'] != 'mutated review copy'

    original = json.loads((ROOT / 'data/fixtures/t14/apif.json').read_bytes())
    original['response'] = original['response'][:1]
    original['results'] = 1
    variants = {'clean': original}
    for order in ('before', 'after'):
        mixed = copy.deepcopy(original)
        extra = copy.deepcopy(original['response'][0])
        extra['league']['id'] = 79
        extra['fixture']['id'] = 293001
        extra['fixture']['status']['short'] = 'AWD'
        extra['teams']['home']['id'] = 393001
        extra['teams']['away']['id'] = 393002
        mixed['response'] = ([extra] + mixed['response']) if order == 'before' else (mixed['response'] + [extra])
        mixed['results'] = 2
        variants[order] = mixed
    import_outputs, candidate_outputs = [], []
    for name, doc in variants.items():
        folder = root / name
        raw_root = folder / 'raw'
        raw_file = capture(raw_root, doc)
        database = folder / 'import.db'
        result = fetch_apif(date='2026-11-01', raw_file=raw_file, db=database, audit_dir=folder / 'audit', quota_path=folder / 'quota.json')
        assert result['requests'] == result['rejected_rows'] == 0
        import_outputs.append((database_snapshot(database), {p.name: p.read_bytes() for p in (folder / 'audit').rglob('*.csv')}, {k:v for k,v in result.items() if k != 'files'}))
        official_db = folder / 'official.db'
        engine = get_engine(official_db)
        init_db(engine)
        engine.dispose()
        with sqlite3.connect(official_db) as con:
            con.execute("INSERT INTO teams VALUES (1,'Home','[]'),(2,'Away','[]')")
            con.execute("INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,kickoff_utc,status) VALUES ('jc:1','jc','日职',1,2,'2026-11-01T11:00:00+00:00','scheduled')")
        assert write_candidates('apif', official_db, raw_root, [dict(scope=dict(date='2026-11-01'), raw_sha256=sha(raw_file.read_bytes()))], folder / 'candidates') == 2
        candidate = json.loads((folder / 'candidates/apif.json').read_bytes())
        assert set(candidate) == {'日职'}
        for record in candidate['日职'].values():
            for evidence in record['evidence']:
                assert evidence['raw_sha256'] == sha(raw_file.read_bytes())
                assert (raw_root / evidence['sidecar']).is_file()
                evidence.pop('raw_sha256')
                evidence.pop('sidecar')
        candidate_outputs.append(candidate)
    assert import_outputs[0] == import_outputs[1] == import_outputs[2]
    assert candidate_outputs[0] == candidate_outputs[1] == candidate_outputs[2]

    # Manual date config deliberately includes a history-only approved route.
    manual = copy.deepcopy(original)
    manual['response'][0]['league']['id'] = 79
    manual_root = root / 'manual'
    raw_file = capture(manual_root / 'raw', manual)
    config_file = manual_root / 'explicit-leagues.json'
    config_file.write_bytes(encode({'79': {'league': 'Explicit review league'}}))
    with patch('zucai.sources.apif_command.daily_provider_config', side_effect=AssertionError('Explicit path must bypass daily defaults')):
        result = fetch_apif(date='2026-11-01', leagues_path=config_file, raw_file=raw_file, db=manual_root / 'manual.db', audit_dir=manual_root / 'audit', quota_path=manual_root / 'quota.json')
    assert result['inserted'] == 1 and result['requests'] == 0
    with sqlite3.connect(manual_root / 'manual.db') as con:
        assert con.execute('SELECT league FROM matches').fetchall() == [('Explicit review league',)]
    report['daily'] = dict(changed_route_flags=len(changes), only_daily_true_to_false=True, old_provider_objects_unchanged=sum(map(len, prior['providers'].values())), old_verified_mapping_rows_unchanged=len(verified_old), all_90_competition_rows_unchanged_from_head=True, daily_route_codes=list(daily_provider_config('apif')), new_bad_row_before_after_same_tables_audits_candidates=True, database_tables_compared=len(import_outputs[0][0]), explicit_manual_date_config_preserved=True, helper_returns_independent_copy=True)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='t65-independent-', dir=OUT) as temporary, patch.object(requests.sessions.Session, 'request', no_network):
        root = Path(temporary)
        check_catalogue(root / 'catalogue')
        check_daily(root / 'daily')
    report.update(base=BASE, baseline=PRIOR, requirements_commit='d6a52bbb15df4ed22f9c6b57ee2e0936c31ca861', sports_network_requests=0)
    (OUT / 'independent-checks.json').write_bytes(encode(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
