"""Copy only T63 acceptance evidence, never working databases or credentials."""
import hashlib
import json
import shutil
from pathlib import Path

SOURCE = Path('/workspace/work/t63-evidence')
TARGET = Path('/workspace/work/t63-acceptance-publish/acceptance/2026-11-14-t63-sources')


def copy(source, relative):
    source, destination = SOURCE / source, TARGET / relative
    if source.is_symlink():
        raise ValueError(f'Symlink not accepted: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache', 'restored', '*.db', '*.db-*', '*.sqlite'))
    else:
        shutil.copyfile(source, destination)


def main():
    TARGET.mkdir(parents=True, exist_ok=True)
    direct = [
        'protocol-reviews.json', 'input-inventory-final-schema.json',
        'pytest-cutoff-final.log', 'pytest-cutoff-final.xml',
        'ruff-cutoff-check.log', 'ruff-cutoff-format.log',
        'registry-final.log', 'review-fixes-tests.log',
        'legacy-integration-tests-passed.log', 'legacy-integration-tests-passed.xml',
        'verify_archive_copy.py', 'prepare_publication.py',
    ]
    for name in direct:
        copy(name, name)
    for label in ('baseline', 'current-cutoff-final', 'post-reverification-cutoff-final'):
        copy(f'final-regression/{label}', f'model-compatibility/{label}')
        copy(f'final-regression/{label}.log', f'model-compatibility/{label}.log')
    for name in ('comparison.json', 'europe-removed-reasons.json'):
        copy(f'final-regression/{name}', f'model-compatibility/{name}')
    copy('regression/worker.py', 'model-compatibility/worker.py')
    for name in ('summary.json', 'tables-before.json', 'tables-after.json', 'tables-roundtrip.json'):
        copy(f'normalized-integration/{name}', f'normalized-integration/{name}')
    copy('normalized-integration/working/state/audit/apif_results_crosscheck.csv',
         'normalized-integration/apif_results_crosscheck.csv')
    for name in ('result_source_links.jsonl', 'result_source_identities.jsonl'):
        copy(f'normalized-integration/exported/state/{name}', f'normalized-integration/proof-extract/{name}')
    copy('normalized-integration/exported/feed_manifest.json', 'normalized-integration/export-manifest-only.json')
    copy('experiment-preparation', 'experiment')
    # No *.db, raw credential configuration or original state archive is copied.
    prohibited = [path for path in TARGET.rglob('*') if path.is_file() and (path.suffix == '.db' or path.name == '.env')]
    if prohibited:
        raise ValueError(f'Unexpected files: {prohibited}')
    fingerprints = {
        str(path.relative_to(TARGET)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(TARGET.rglob('*')) if path.is_file() and path.name != 'file-sha256.json'
    }
    (TARGET / 'file-sha256.json').write_text(json.dumps(fingerprints, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'files':len(fingerprints),'bytes':sum(p.stat().st_size for p in TARGET.rglob('*') if p.is_file())}))


if __name__ == '__main__':
    main()
