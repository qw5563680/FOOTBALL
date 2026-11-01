"""Cache public FotMob team fixture lists and endpoint snapshots for each pre-cutoff match."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import gzip
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'raw'
CACHE.mkdir(exist_ok=True)
FM = 'https://www.fotmob.com'
API = FM + '/api/data/'
CUTOFF = '2026-11-01T10:30:00.000Z'
TARGETS = [5749733, 5868116, 5749734, 5749736, 5802980, 5881213,
           5802979, 5795501, 5881210, 5749737, 5868114, 5887668,
           5887663, 5749735, 5802977, 5868111]
lock = threading.Lock()


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def get(tag: str, url: str):
    dest = CACHE / (tag + '.json.gz')
    if dest.exists():
        return json.loads(gzip.decompress(dest.read_bytes()))
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=22) as response:
                payload = response.read()
                if response.headers.get('Content-Encoding','').lower() == 'gzip':
                    payload = gzip.decompress(payload)
                data = json.loads(payload)
                if not isinstance(data, (dict,list)) or not data:
                    raise ValueError('No data')
                source_metadata = {'tag': tag, 'url': url, 'status': response.status,
                                   'retrieved_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                                   'bytes_uncompressed': len(payload)}
                if 'Date' in response.headers:
                    source_metadata['http_date'] = response.headers['Date']
            temp = dest.with_suffix('.tmp')
            temp.write_bytes(gzip.compress(payload, compresslevel=3)); temp.replace(dest)
            with lock, (ROOT / 'request_log.jsonl').open('a',encoding='utf-8') as fh:
                print(json.dumps(source_metadata, ensure_ascii=False), file=fh)
            return data
        except Exception as e:
            if attempt == 3:
                raise RuntimeError(f'{tag} {url}: {e}') from e
            if isinstance(e, urllib.error.HTTPError) and e.code in (404, 410):
                raise
            time.sleep((1,3,7)[attempt])


def competitive(f):
    s = f['status']; t = f['tournament']['name'].lower()
    return s.get('finished') and s.get('started') and not s.get('cancelled') and not s.get('awarded') and s['utcTime'] < CUTOFF and not ('friendl' in t or 'test match' in t or 'friendly' in t or 'summer series' in t or 'emirates cup' in t)


def team_history(team):
    tid = team['id']
    url = API + f'teams?id={tid}'
    team_data = get(f'team_{tid}', url)['fixtures']
    all_fixtures = team_data['allFixtures']['fixtures']
    sources = {f['id']: url for f in all_fixtures}
    prev_url = team_data.get('previousFixturesUrl')
    previous = []
    while len({f['id'] for f in all_fixtures if competitive(f)}) < 10 and prev_url and prev_url not in previous and len(previous) < 4:
        previous.append(prev_url)
        try:
            p = get(f'team_{tid}_prev{len(previous)}',prev_url)
        except Exception as e:
            print('previous schedule error', tid, e, flush=True); break
        if 'fixtures' in p and isinstance(p['fixtures'], list):
            matches = p['fixtures']
        elif isinstance(p.get('allFixtures'),dict):
            matches = p['allFixtures']['fixtures']
        elif isinstance(p.get('data'),dict) and 'fixtures' in p['data']:
            matches = p['data']['fixtures']
        else:
            print('previous schema', tid, list(p)[:6], flush=True); break
        for f in matches:
            if f['id'] not in sources:
                all_fixtures.append(f); sources[f['id']] = prev_url
        prev_url = p.get('previousFixturesUrl') or p.get('nextUrl')
    selected = sorted({f['id']: f for f in all_fixtures if competitive(f)}.values(), key=lambda f:f['status']['utcTime'], reverse=True)[:10]
    return {**team, 'team_schedule_url': url, 'fixtures': [dict(f, schedule_source_url=sources[f['id']]) for f in selected]}


def main():
    daily = get('daily_20261101', API+'matches?date=20261101&timezone=UTC')
    match_by_id = {m['id']:m for l in daily['leagues'] for m in l['matches']}
    teams = [dict(target_id=i, target_fixture_id=mid, side=side, id=match_by_id[mid][side]['id'], name=match_by_id[mid][side]['name'])
             for i,mid in enumerate(TARGETS, start=1) for side in ('home','away')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        histories = list(pool.map(team_history,teams))
    print('HISTORIES',[(h['name'],len(h['fixtures']),h['fixtures'][-1]['status']['utcTime'] if h['fixtures'] else None) for h in histories], flush=True)
    save_json(ROOT / 'histories.json', histories)
    fixture_ids = sorted({f['id'] for h in histories for f in h['fixtures']},reverse=True)
    print('Fixtures',len(fixture_ids),flush=True)
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        jobs = {pool.submit(get,'match_'+str(fid),API+f'matchDetails?matchId={fid}'):fid for fid in fixture_ids}
        for i,future in enumerate(concurrent.futures.as_completed(jobs), start=1):
            try:
                data = future.result()
                if not data.get('general',{}).get('finished'):
                    raise ValueError('Not marked finished in detail')
            except Exception as e:
                errors.append([jobs[future], str(e)]); print('detail error',errors[-1],flush=True)
            if i % 35 == 0 or i == len(jobs):
                print('Downloaded',i,'/',len(jobs),flush=True)
    save_json(ROOT/'download_errors.json', errors)


if __name__ == '__main__':
    main()
