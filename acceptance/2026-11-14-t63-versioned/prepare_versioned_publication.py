"""Publish append-only T63 v2 evidence; never copy production DBs or credentials."""
import hashlib
import json
import re
import shutil
from pathlib import Path

SOURCE = Path('/workspace/work/t63-versioned-evidence')
FIXTURES = Path('/workspace/work/t63-version-evidence')
TARGET = Path('/workspace/work/t63-acceptance-publish/acceptance/2026-11-14-t63-versioned')
REPORT = Path('/workspace/work/football-t63/reports/t63-apif-results-for-existing-15-leagues')
OMIT = {'__pycache__', '.pytest_cache', 'test-tmp'}


def copy(source, destination):
    if source.is_symlink():
        raise ValueError(f'Symlink: {source}')
    if source.is_dir():
        for child in sorted(source.iterdir()):
            if child.name not in OMIT:
                copy(child, destination / child.name)
        return
    if source.suffix in {'.pyc', '.db', '.sqlite'} or '.db-' in source.name or source.name == '.env':
        raise ValueError(f'Unexpected database/configuration: {source}')
    raw = source.read_bytes()
    if re.search(rb'(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})', raw):
        raise ValueError(f'Credential-shaped content: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def main():
    if TARGET.exists():
        raise ValueError('Refusing to overwrite a prepared evidence directory')
    copy(SOURCE / 'legacy-compatibility', TARGET / 'legacy-compatibility')
    copy(SOURCE / 'legacy-compatibility-final', TARGET / 'legacy-compatibility-final')
    copy(FIXTURES, TARGET / 'fixture-verification')
    for name in ('owner-version-scope.json', 'owner-scope-merged.json', 'implementation-receipt.json', 'root-fixture-verification.json', 'post-sfc-source-audit.json', 'prepare_versioned_publication.py'):
        copy(SOURCE / name, TARGET / name)
    readme = '''# T63 revised version-policy acceptance

This directory appends evidence for the PR136 owner scope revision. The earlier
acceptance/2026-11-14-t63-sources directory and all production archives are unchanged.

- legacy-compatibility-final/final/acceptance.json: final 39 fits and 105 predictions equal
  the b83d2cc baseline, 14 targets without a trusted cutoff retained.
- legacy-compatibility/: preserved earlier comparison and baseline, not overwritten
  by the final run after the future-APIF-import compatibility correction.
- fixture-verification/full-suite.log and XML: final complete offline test run.
- fixture-verification/full-suite.red.*: retained first run and its three failures;
  IMPLEMENTATION-NOTES.md explains the scope-dependent old-test adaptations.
- fixture-verification/source_version_artifacts/: complete SYNTHETIC archives,
  including failed candidates. Not live HTTP data, benefit evidence or publication.
- implementation-receipt.json: actual implementation revision and verification.
- file-sha256.json: hashes relative to this directory, excluding this hash file itself.

For actual scope and limitations see the T63 report in the implementation branch.
No database, formal credentials, new sports fetch, production replacement or PR merge.
'''
    (TARGET / 'README.md').write_text(readme)
    fingerprints = {str(p.relative_to(TARGET)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(TARGET.rglob('*')) if p.is_file()}
    (TARGET / 'file-sha256.json').write_text(json.dumps(fingerprints, ensure_ascii=False, indent=2) + '\n')
    fingerprints['file-sha256.json'] = hashlib.sha256((TARGET/'file-sha256.json').read_bytes()).hexdigest()
    # Anchor the complete file inventory instead of duplicating hundreds of lines
    # in reports; the SHA of file-sha256.json authenticates every evidence file.
    anchors = ('file-sha256.json', 'implementation-receipt.json',
               'legacy-compatibility-final/final/acceptance.json',
               'fixture-verification/full-suite.xml', 'root-fixture-verification.json')
    (REPORT/'SHA256SUMS-VERSIONED').write_text(''.join(f'{fingerprints[name]}  {name}\n' for name in anchors))
    print(json.dumps({'files':len(fingerprints), 'bytes':sum(p.stat().st_size for p in TARGET.rglob('*') if p.is_file()),
                      'report_manifest_bytes':(REPORT/'SHA256SUMS-VERSIONED').stat().st_size}))


if __name__ == '__main__':
    main()
