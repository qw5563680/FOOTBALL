import hashlib, json, shutil
from pathlib import Path
root=Path(__file__).resolve().parent
out=Path('/workspace/work/t65-acceptance-publish/acceptance/2026-11-14-t65-catalogue')
out.mkdir(parents=True,exist_ok=True)
for name in ('apif','mappings'):
 for path in (root/name).iterdir():
  if path.is_file():
   target=out/name/path.name; target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
for path in (root/'jc').iterdir():
 if path.is_file() and not path.name.startswith('pre-mapping'):
  target=out/'jc'/path.name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
source=root/'jc/final-replay'
if source.exists(): shutil.copytree(source,out/'jc/final-replay',dirs_exist_ok=True)
for path in (root/'regression').iterdir():
 if path.is_file():
  target=out/'regression'/path.name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
for path in root.iterdir():
 if path.is_file() and path.suffix in ('.json','.log','.xml','.py'):
  shutil.copy2(path,out/path.name)
files={str(path.relative_to(out)):{'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size} for path in sorted(out.rglob('*')) if path.is_file() and path.name not in ('SHA256SUMS','manifest.json')}
(out/'manifest.json').write_text(json.dumps({'source_data_commit':'e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4','baseline_code_commit':'b83d2cc7d975e41f418f59aad3341a7d1574f95a','files':files},ensure_ascii=False,indent=2)+'\n')
(out/'SHA256SUMS').write_text(''.join(f"{value['sha256']}  {name}\n" for name,value in files.items()))
print(json.dumps({'files':len(files),'bytes':sum(v['bytes'] for v in files.values()),'out':str(out)},ensure_ascii=False))
