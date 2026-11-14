"""Pin code snapshots, restore fixed archive independently, and compare every table/model input."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve()
HERE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else SCRIPT.parent
if any((HERE / name).exists() for name in ('baseline-code', 'current-code', 'comparison.json')):
    raise SystemExit('Evidence already exists; pass a fresh output directory to preserve this run.')
HERE.mkdir(parents=True, exist_ok=True)
if HERE != SCRIPT.parent:
    shutil.copy2(SCRIPT, HERE / SCRIPT.name)
    shutil.copy2(SCRIPT.with_name('worker.py'), HERE / 'worker.py')
REPO = Path('/workspace/work/football-t61')
ARCHIVE = Path('/workspace/work/t60-fixed-archive')
SOURCE_COMMIT = '1657093073ea0e231cef476e1eac4e9ac58ebcb8'
BASE_COMMIT = 'b03e00f'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def snapshot(root):
    return {str(path.relative_to(root)): {'bytes': path.stat().st_size, 'sha256': sha(path)} for path in sorted(root.rglob('*')) if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def execute(label, mode):
    code = HERE / f'{label}-code'
    folder = HERE / f'{label}-{mode}'
    output = HERE / f'{label}-{mode}.json'
    arguments = dict(label=label, mode=mode, folder=str(folder), archive=str(ARCHIVE), code_root=str(code), params=str(code / 'data/model/params.json'), output=str(output))
    args = HERE / f'{label}-{mode}-arguments.json'
    save(args, arguments)
    environment = {**os.environ, 'PYTHONPATH': str(code / 'src') + os.pathsep + str(REPO / 'tests'), 'PYTHONHASHSEED': '0', 'PYTHONDONTWRITEBYTECODE': '1'}
    environment.pop('ZUCAI_NATIONS_LEAGUE', None)
    environment.pop('ZUCAI_APIF_DAILY_CAP', None)
    with (HERE / f'{label}-{mode}.log').open('w') as stream:
        subprocess.run([sys.executable, str(HERE / 'worker.py'), str(args)], cwd=code, env=environment, check=True, stdout=stream, stderr=subprocess.STDOUT)
    return json.loads(output.read_bytes())


assert subprocess.check_output(['git', '-C', str(ARCHIVE), 'rev-parse', 'HEAD'], text=True).strip() == SOURCE_COMMIT
started = datetime.now(UTC).isoformat()
before = snapshot(ARCHIVE)
save(HERE / 'source-archive-before.json', before)
base = HERE / 'baseline-code'
base.mkdir(exist_ok=False)
packed = subprocess.check_output(['git', '-C', str(REPO), 'archive', BASE_COMMIT, 'src', 'data'])
with tarfile.open(fileobj=io.BytesIO(packed)) as stream:
    for member in stream.getmembers():
        assert not member.name.startswith('/') and '..' not in Path(member.name).parts
    stream.extractall(base)
current = HERE / 'current-code'
current.mkdir(exist_ok=False)
for directory in ('src', 'data'):
    shutil.copytree(REPO / directory, current / directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
save(HERE / 'baseline-code-files.json', snapshot(base))
save(HERE / 'current-code-files.json', snapshot(current))
assert sha(base / 'data/model/params.json') == sha(current / 'data/model/params.json')
results = {label: execute(label, 'static') for label in ('baseline', 'current')}
daily = {label: execute(label, 'daily') for label in ('baseline', 'current')}
after = snapshot(ARCHIVE)
save(HERE / 'source-archive-after.json', after)
assert before == after
old, new = results['baseline'], results['current']
assert old['tables'] == new['tables']
assert old['sqlite_schema_sha256'] == new['sqlite_schema_sha256']
assert old['model_inputs'] == new['model_inputs']
assert old['model_count'] == new['model_count'] == 17
assert old['params_sha256'] == new['params_sha256']
assert daily['baseline']['tables'] == daily['current']['tables']
assert daily['baseline']['source_inventory'] == daily['current']['source_inventory']
assert daily['baseline']['requests'] == daily['current']['requests']
result = {
    'started_utc': started, 'completed_utc': datetime.now(UTC).isoformat(),
    'source_commit': SOURCE_COMMIT, 'baseline_code_commit': subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', BASE_COMMIT], text=True).strip(),
    'current_code': 'Pinned uncommitted T61 snapshot; exact source/config hashes in current-code-files.json',
    'source_archive_files_unchanged': len(before), 'source_archive_bytes_unchanged': sum(value['bytes'] for value in before.values()),
    'all_source_archive_hashes_unchanged': True,
    'database_table_count': len(old['tables']), 'all_table_canonical_bytes_equal': True,
    'database_row_counts': {name: value['rows'] for name, value in old['tables'].items()},
    'canonical_table_hashes': {name: value['sha256'] for name, value in old['tables'].items()},
    'database_schemas_equal': True, 'model_input_count': 17, 'all_model_input_canonical_bytes_equal': True,
    'model_input_hashes': {name: value['sha256'] for name, value in old['model_inputs'].items()},
    'model_params_bytes_unchanged': True,
    'new_real_matches': 0, 'real_collection_performed': False,
    'restored_matches_delta': new['tables']['matches']['rows'] - old['tables']['matches']['rows'],
    'restored_database_delta_bytes': new['database_bytes'] - old['database_bytes'],
    'restores': {label: {key: result[key] for key in ('restore_seconds', 'database_bytes', 'database_sha256')} for label, result in results.items()},
    'offline_daily_fixture': {label: {key: result[key] for key in ('description', 'elapsed_seconds', 'database_bytes', 'match_count', 'requests')} for label, result in daily.items()},
    'offline_daily_fixture_tables_equal': True,
    'production_daily_runtime': 'Not measured: reviewer Actions acceptance remains outstanding.',
    'limitations': ['Read-history equality compares full existing model corpora and attributes; no model training or prediction occurs.', 'Timing observations are one baseline/current run each on a shared development machine; they are not performance guarantees.', 'The offline daily fixture replay is a tiny synthetic API subset, not the complete production daily workflow.', 'No new real matches were collected; catalogue permissions and complete A/B/C backfill remain reviewer acceptance work.'],
}
save(HERE / 'comparison.json', result)
print(json.dumps({key: result[key] for key in ('source_archive_files_unchanged', 'database_table_count', 'model_input_count', 'restores', 'offline_daily_fixture_tables_equal')}, ensure_ascii=False, indent=2), flush=True)
