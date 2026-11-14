import hashlib, json, subprocess, sys
from pathlib import Path
from zucai.league_registry import provider_config, provider_entries, provider_ids, daily_codes
repo=Path(__file__).resolve().parent / 'regression'
def snapshot(root):
 path=root/'data/league_map.json'
 return {provider:{'config':provider_config(provider,path=path),'daily':provider_entries(provider,path=path,purpose='daily'),'backfill':provider_entries(provider,path=path,purpose='backfill'),'daily_codes':daily_codes(provider,path=path)} for provider in ('apif','fdorg','fd')}
old,new=(snapshot(repo/f'{label}-code') for label in ('baseline','current'))
def encoded(value): return (json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
assert encoded(old)==encoded(new)
root=Path(__file__).resolve().parent
(root/'request-plans-baseline.json').write_bytes(encoded(old))
(root/'request-plans-current.json').write_bytes(encoded(new))
result={'baseline_code':'b83d2cc7d975e41f418f59aad3341a7d1574f95a','same_ordered_bytes':True,'sha256':hashlib.sha256(encoded(old)).hexdigest(),'bytes':len(encoded(old)),'providers':{p:{'all_verified_routes':len(old[p]['config']),'daily':len(old[p]['daily']),'backfill':len(old[p]['backfill'])} for p in old},'apif_modes':{mode:sorted(provider_ids('apif',mode,path=repo/'current-code/data/league_map.json')) for mode in ('import','identity_only','index_only','nations')}}
(root/'request-plans-comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
