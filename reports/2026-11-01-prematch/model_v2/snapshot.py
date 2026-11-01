"""Public source capture; no betting prices are exported into the training manifest."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
import csv, hashlib, io, json, time
import requests

ROOT=Path(__file__).resolve().parent
RAW=ROOT/'snapshots'; RAW.mkdir(exist_ok=True)
LEAGUES={'I1':'ita.1','SP1':'esp.1','F1':'fra.1','D1':'ger.1','E0':'eng.1','P1':'por.1'}
def fetch(pair):
    season,lg=pair; url=f'https://www.football-data.co.uk/mmz4281/{season}/{lg}.csv'
    for attempt in range(3):
        try:
            r=requests.get(url,timeout=22,headers={'User-Agent':'FOOTBALL private research/1.0'});r.raise_for_status()
            b=r.content
            s=next(s for s in ('utf-8-sig','cp1252','latin1') if _decode(b,s) is not None)
            rows=list(csv.DictReader(io.StringIO(b.decode(s)))); assert rows and 'FTHG' in rows[0] and 'FTAG' in rows[0]
            p=RAW/f'{lg}_{season}.csv'; p.write_bytes(b)
            return dict(league=lg,season=season,url=url,sha256=hashlib.sha256(b).hexdigest(),file=p.name,encoding=s,rows=len(rows),received_utc=datetime.now(UTC).isoformat(),http_last_modified=r.headers.get('Last-Modified'),first=rows[0]['Date'],last=rows[-1]['Date'],teams=sorted({x[k] for x in rows for k in ('HomeTeam','AwayTeam') if x.get(k)}))
        except Exception as e:
            if attempt==2: return dict(league=lg,season=season,url=url,error=str(e))
            time.sleep(1+attempt)
def _decode(b,s):
    try:return b.decode(s)
    except UnicodeDecodeError:return None
if __name__=='__main__':
    items=[(s,l) for s in ('2425','2526','2627') for l in LEAGUES]
    with ThreadPoolExecutor(4) as ex: out=list(ex.map(fetch,items))
    as_of=datetime.now(UTC).isoformat()
    (ROOT/'model_sources.json').write_text(json.dumps(dict(cutoff_utc=as_of,sources=out),ensure_ascii=False,indent=2),encoding='utf8')
    for d in out:print(d['league'],d['season'],d.get('rows'),d.get('last'),d.get('error',''),d.get('teams','') if d['season']=='2627' else '')
