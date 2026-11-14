"""Append review evidence only; leave the data branch's original trees intact."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
PUBLISH = Path('/workspace/work/t63-acceptance-publish')
DEST = PUBLISH / 'acceptance/2026-11-14-t63-review'
REPO = Path('/workspace/work/football-t63')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selected(relative):
    parts = relative.parts
    if any(part in {'__pycache__', '.pytest_cache', '.ruff_cache'} for part in parts):
        return False
    if relative.name.endswith(('.db', '.db-wal', '.db-shm', '.pyc')):
        return False
    if parts[0] == 'observability' and 'e584-restored' in parts:
        return False
    if parts[0] == 'acceptance' and len(parts) > 1 and parts[1] == 'work':
        return False
    return parts[0] in {
        'acceptance', 'observability', 'stats', 'fd-stats', 'internal-review', 'integration',
        'README.md', 'package_evidence.py',
    }


if __name__ == '__main__':
    if DEST.exists():
        raise SystemExit(f'Refuse to overwrite existing evidence: {DEST}')
    DEST.mkdir(parents=True)
    included, excluded = {}, []
    for path in sorted(SOURCE.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(SOURCE)
        if not selected(relative):
            excluded.append({'path': str(relative), 'bytes': path.stat().st_size, 'sha256': sha(path)})
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink not allowed: {relative}')
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        assert sha(path) == sha(target)
        included[str(relative)] = {'sha256': sha(target), 'bytes': target.stat().st_size}
    (DEST / '.gitattributes').write_text('* -text -whitespace\n')
    (DEST / 'exclusions.json').write_text(json.dumps({
        'reason': 'Reproducible temporary databases/caches and repeated working archives are not published. Original backups stay on disk.',
        'files': excluded,
    }, ensure_ascii=False, indent=2) + '\n')
    code = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    (DEST / 'provenance.json').write_text(json.dumps({
        'implementation_commit': code,
        'base_main': '6be41045827f232f1546556f490e5c0f14c82779',
        't66_dependency': '8c1f83162ba96e7046126ba610ab45304e8d72fc',
        'input_archive': 'e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4',
        'saved_fd_run': 43543648051,
        't66_replay_evidence': 'ecef4198e632c78d893930fe439bff822b1704d4/acceptance/2026-11-14-t66-review',
        'sports_http_requests': 0, 'formal_key_use': 0, 'production_writes': 0,
    }, ensure_ascii=False, indent=2) + '\n')
    files = {str(p.relative_to(DEST)): {'sha256': sha(p), 'bytes': p.stat().st_size}
             for p in sorted(DEST.rglob('*')) if p.is_file()}
    (DEST / 'file-sha256.json').write_text(json.dumps(files, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'files': len(files)+1, 'bytes': sum(v['bytes'] for v in files.values()),
                      'root_manifest_sha256': sha(DEST / 'file-sha256.json'),
                      'destination': str(DEST), 'implementation_commit': code}, ensure_ascii=False))
