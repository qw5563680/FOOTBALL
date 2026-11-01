"""Cache public, already-linked ESPN match play-by-play for verification of actual non-scoring shot assists."""
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
from download import ROOT,get,save_json


def main():
    rows=json.loads((ROOT/'team_last10.json').read_text(encoding='utf8'))
    ee=json.loads((ROOT/'espn_schedules.json').read_text(encoding='utf8'))
    urls={e['fixture_id']:e['summary_url'] for events in ee.values() for e in events}
    ids=sorted({str(r['espn_fixture_id']) for r in rows if r.get('espn_fixture_id')})
    errs={}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures={pool.submit(get,'esp_summary_'+eid,urls[eid]):eid for eid in ids}
        for i,f in enumerate(as_completed(futures),1):
            try:f.result()
            except Exception as e:errs[futures[f]]=str(e)
            if i%65==0 or i==len(ids):print(i,'/',len(ids),flush=True)
    save_json(ROOT/'esp_play_download_errors.json',errs)


if __name__=='__main__':main()
