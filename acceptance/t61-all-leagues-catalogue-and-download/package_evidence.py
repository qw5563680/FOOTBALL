"""Package only inspected acceptance artifacts; never copies databases or credentials."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path('/workspace/work/t61-evidence-publish')
PREFIX = Path('acceptance/t61-all-leagues-catalogue-and-download')
SELECT = {
    'pytest-final.xml': 'pytest.xml', 'pytest-final.log': 'pytest.log',
    'ruff-final.log': 'ruff.log', 'ruff-format-final.log': 'ruff-format.log',
    'implementation-plan.md': 'implementation-plan.md',
    'review-findings.json': 'review-findings.json',
    'final-additional-tests.log': 'final-additional-tests.log',
    'review-fixes-tests.log': 'review-fixes-tests.log',
    'backfill-red.log': 'backfill-red.log',
    'backfill-tests.log': 'backfill-tests.log',
    'technical-scope-red.log': 'technical-scope-red.log',
    'package_evidence.py': 'package_evidence.py',
}
for folder in ('coverage', 'public-policies', 'registry', 'catalogue', 'workflow-jc'):
    for path in sorted((ROOT / folder).glob('*')):
        if path.is_file() and path.name != 'full-suite.log':
            SELECT[str(path.relative_to(ROOT))] = str(path.relative_to(ROOT))
for path in sorted((ROOT / 'regression-final').glob('*')):
    if path.is_file():
        SELECT[str(path.relative_to(ROOT))] = 'regression/' + path.name
files = []
for source_name, target_name in sorted(SELECT.items()):
    source = ROOT / source_name
    payload = source.read_bytes()
    assert source.resolve().is_relative_to(ROOT)
    assert not (os.environ.get('GH_TOKEN') and os.environ['GH_TOKEN'].encode() in payload)
    target = OUT / PREFIX / target_name
    assert target.resolve().is_relative_to(OUT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    files.append(dict(source_path=source_name, target_path=str(PREFIX / target_name),
        sha256=hashlib.sha256(payload).hexdigest(), size=len(payload)))
manifest = dict(source_root=str(ROOT), target_branch='data/t61-catalogue-acceptance-20261114',
    files=files, total_bytes=sum(item['size'] for item in files),
    source_commit='1657093073ea0e231cef476e1eac4e9ac58ebcb8',
    note='Offline fixture and fixed-archive acceptance. No real sports API calls or production changes.')
raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
(OUT / PREFIX / 'evidence-manifest.json').write_bytes(raw)
checksum = '\n'.join(f"{item['sha256']}  {item['target_path']}" for item in files)
checksum += f"\n{hashlib.sha256(raw).hexdigest()}  {PREFIX}/evidence-manifest.json\n"
Path('/workspace/work/football-t61/reports/t61-all-leagues-catalogue-and-download/SHA256SUMS').write_text(checksum)
print(json.dumps({'files':len(files)+1, 'bytes':manifest['total_bytes']+len(raw)}))
