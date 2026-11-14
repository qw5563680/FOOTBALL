import csv,hashlib,json
from pathlib import Path
root=Path('/workspace/work/football-t61')
master_path='docs/research/2026-11-14-jc-sfc-competition-master-table.csv'
rows=list(csv.DictReader((root/master_path).open(encoding='utf-8-sig',newline='')))
paths={'apif':'data/api_football/leagues.json','fdorg':'data/football_data_org/competitions.json','fd':'data/football_data_co_uk/leagues.json'}
providers={}
for provider,relative in paths.items():
 raw=(root/relative).read_bytes(); config=json.loads(raw); providers[provider]={}
 for code,value in config.items():
  mode='identity_only' if value.get('identity_only') else 'index_only' if value.get('index_only') else 'nations' if provider=='apif' and code=='5' else 'import'
  status='candidate' if provider=='fd' and value['status']!='verified' else 'verified'
  groups=['A'] if any(r['项目内赛事名']==value['league'] for r in rows) else ['B']
  if mode=='nations': groups.append('C')
  evidence=[{'kind':'legacy_config','path':relative,'sha256':hashlib.sha256(raw).hexdigest(),'pointer':'/'+code,'claim':'Preserves the reviewed legacy route and every config value; this is not a new catalogue or access probe.'}]
  if provider=='apif' and mode=='identity_only': evidence.append({'kind':'metadata_probe','path':'data/fixtures/t23/apif_league_probes.csv','claim':'Real seven-league metadata audit from Actions 41612517821-1; approves identity routing only, not fixture importing.'})
  if provider=='apif' and code=='2': evidence.append({'kind':'legacy_index_policy','path':'docs/reviews/2026-11-04-t31-review.md','claim':'Legacy Champions League index route only. Original verification_status remains index_permission_pending; entitlement is unverified.'})
  if provider=='apif' and code=='5': evidence.append({'kind':'reviewed_live_run','path':value['verification_run'],'claim':'T50 existing Nations League route; docs/reviews/2026-11-09-t50-review.md records five editions and the separate collector.'})
  if provider=='fdorg': evidence.append({'kind':'legacy_live_verification','path':'docs/DATA_SOURCES.md','claim':'T09 nine domestic competitions plus T20 CL: recorded successful seasons 2023–2026 and denied 2022 boundary; no claim about unprobed seasons.'})
  if provider=='fd': evidence.append({'kind':'legacy_time_identity_verification','path':'docs/DATA_SOURCES.md','claim':'T08 observed timezone/identity evidence. D2 remains unverified and blocked; no catalogue absence claim.'})
  entry={'status':status,'mode':mode,'groups':groups,'relevant':True,'daily':status=='verified' and mode=='import','backfill':status=='verified' and mode=='import','config':value,'evidence':evidence}
  if provider=='apif' and mode=='index_only': entry.update(verified_scope='legacy_index_route_only',access_status='unverified')
  if provider=='apif' and mode=='identity_only': entry.update(verified_scope='legacy_identity_route_only',access_status='unverified')
  providers[provider][code]=entry
competitions=[]
for i,row in enumerate(rows,1):
 mappings=[]
 for provider,entries in providers.items():
  known=[(code,entry) for code,entry in entries.items() if entry['config']['league']==row['项目内赛事名']]
  if known:
   for code,entry in known:
    mappings.append({'provider':provider,'code':code,'status':entry['status'],'evidence':[{'kind':'legacy_project_name_mapping','path':master_path,'csv_row':i,'claim':'Exact project name from this master row agrees with the existing reviewed provider config; source-name aliases are not used to collapse rows.'},*entry['evidence']]})
  else:
   mappings.append({'provider':provider,'code':None,'status':'candidate','evidence':[]})
 verified=any(m['status']=='verified' for m in mappings)
 missing='' if verified else 'awaiting_catalogue_and_review: no reviewed provider mapping; absence is not established'
 if row['项目内赛事名']=='德乙': missing='awaiting_catalogue_and_review: FD D2 is known but timezone/team identity is ambiguous and import remains blocked'
 if i==27: missing='awaiting_catalogue_and_review: source name 日职 is shared with regular J1; transition competition must be reviewed separately'
 competitions.append({'master_row_id':f'master-{i:03}','master_name':row['赛事'],'project_name':row['项目内赛事名'],'source_names':row['来源出现过的名称'].split('、'),'source_names_raw':row['来源出现过的名称'],'master_fields':row,'status':'verified' if verified else 'candidate','mappings':mappings,'missing_reason':missing})
country_rows={'England':[37,39,46,53],'Germany':[38,52],'Japan':[40,44],'Italy':[41,50],'France':[42,55],'Spain':[43,51],'Netherlands':[45],'Brazil':[47],'South Korea':[48],'Portugal':[49],'Norway':[54],'Sweden':[56]}
countries=[{'country':country,'status':'candidate','master_row_ids':[f'master-{i:03}' for i in ids],'evidence':[{'kind':'master_domestic_cup_scope','path':master_path,'claim':'Domestic cup identifies a required participant country. All divisions and domestic cups require catalogue inventory and individual mapping approval; this does not approve provider IDs.'}]} for country,ids in country_rows.items()]
national=[('World Cup and all confederations of World Cup qualifying',[65,67,68,72,77]),('Continental championships and every qualifying competition',[69,70,71,73,74,75]),('Nations leagues',[16]),('National-team friendlies and invitational competitions',[66,76,78,79])]
registry={'version':1,'master_table':{'path':master_path,'sha256':hashlib.sha256((root/master_path).read_bytes()).hexdigest(),'row_count':len(rows),'columns':list(rows[0])},'download_policy':{'scope':'related','daily_reserve':500,'max_requests':500},'scope_policy':{'A':'All 90 master rows without collapsing repeated source names. Every provider mapping requires PR review against original evidence.','B':'All divisions and domestic cups in every country represented by A cup/intercontinental participants. Country lists are a review worklist, not an exclusion allowlist; unresolved participant countries must be enumerated from evidence.','C':'World Cup qualifying in every confederation, continental cups and qualifiers, nations leagues and national-team friendlies. Youth/women identities remain distinct.','unrelated':'Catalogue only while scope=related. Changing scope to all includes reviewed unrelated entries; candidates are never enabled by scope.'},'supporting_scope':{'countries':countries,'national_teams':[{'category':category,'status':'candidate','master_row_ids':[f'master-{i:03}' for i in ids],'evidence':[{'kind':'task_scope','path':'docs/tasks/T61-all-leagues-catalogue-and-download.md','claim':'Required C scope; only existing APIF 5 has an approved route. Remaining competition IDs/access need catalogue evidence and PR review.'}]} for category,ids in national],'participant_country_gaps':[{'status':'candidate','master_row_ids':[f'master-{i:03}' for i,r in enumerate(rows,1) if r['赛事类型']=='跨国俱乐部赛事'],'reason':'Complete participant-country inventory is pending; no arbitrary country exclusions and no invented provider IDs.'}]},'providers':providers,'competitions':competitions}
(root/'data/league_map.json').write_text(json.dumps(registry,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
summary={'master_rows':len(rows),'verified_master_rows':sum(r['status']=='verified' for r in competitions),'candidate_master_rows':sum(r['status']=='candidate' for r in competitions),'legacy_configs':{provider:{'path':path,'sha256':hashlib.sha256((root/path).read_bytes()).hexdigest(),'entries':len(providers[provider])} for provider,path in paths.items()}}
Path('/workspace/work/t61-evidence/registry/build_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(summary)
