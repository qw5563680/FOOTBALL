"""Link ESPN all-competition team schedules; complete previous-season history using confirmed ESPN dates and FotMob daily scoreboards."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import unicodedata

from download import API, ROOT, CUTOFF, get, save_json

ESPN = 'https://site.api.espn.com/apis/site/v2/sports/soccer/'
FIXTURES = [('ita.1','Como','Venezia'),('esp.1','Getafe','Sevilla'),('ita.1','Frosinone','Torino'),
            ('ita.1','Lazio','Cagliari'),('fra.1','Strasbourg','Auxerre'),('ger.1','Union Berlin','Cologne'),
            ('fra.1','Paris FC','Monaco'),('eng.1','Liverpool','Arsenal'),('ger.1','Frankfurt','Hamburg'),
            ('ita.1','Lecce','Genoa'),('esp.1','Elche','Valencia'),('por.1','Maritimo','Santa Clara'),
            ('por.1','Viseu','Rio Ave'),('ita.1','Juventus','Napoli'),('fra.1','Marseille','Toulouse'),
            ('esp.1','Athletic','Sociedad')]


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFD',str(s)).casefold() if not unicodedata.combining(c))


def esp_event(e,tid,src):
    c=e['competitions'][0]
    sides={x['homeAway']:x for x in c['competitors']}
    return {'date':e['date'],'fixture_id':e['id'],'name':e.get('name'), 'comp':e['league']['name'], 'slug':e['league']['slug'],
            'home_id':sides['home']['id'],'away_id':sides['away']['id'],'home_name':sides['home']['team']['displayName'],
            'away_name':sides['away']['team']['displayName'],'finished':c.get('status',{}).get('type',{}).get('completed'),
            'status':c.get('status',{}).get('type',{}).get('name'), 'venue':c.get('venue'), 'neutral':c.get('neutralSite'),
            'score_home':sides['home'].get('score'), 'score_away':sides['away'].get('score'), 'team_schedule_url':src,
            'match_page':next((l['href'] for l in e.get('links',[]) if 'summary' in l.get('rel',[]) or 'gamecast' in l.get('rel',[])),f"https://www.espn.com/soccer/match/_/gameId/{e['id']}"),
            'summary_url': ESPN+f"{e['league']['slug']}/summary?event={e['id']}"}


def main():
    h=json.loads((ROOT/'histories.json').read_text(encoding='utf-8'))
    league_list=sorted({f[0] for f in FIXTURES})
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        scores=dict(zip(league_list,ex.map(lambda lg:get('esp_board_'+lg,ESPN+lg+'/scoreboard?dates=20261101&limit=100'),league_list)))
    for t in h:
        lg,hn,an=FIXTURES[t['target_id']-1]
        matching=[]
        for e in scores[lg]['events']:
            a={c['homeAway']:c for c in e['competitions'][0]['competitors']}
            if norm(hn) in norm(a['home']['team']['displayName']) and norm(an) in norm(a['away']['team']['displayName']):
                matching.append(a)
        if len(matching)!=1:
            raise ValueError((lg,t['target_id'],hn,an,[(e['name'],e.get('shortName')) for e in scores[lg]['events']]))
        t['espn_id']=matching[0][t['side']]['id']
    def fetch_season(args):
        t,year=args
        u=ESPN+f"all/teams/{t['espn_id']}/schedule?season={year}"
        p=get(f"esp_team_{t['espn_id']}_{year}",u)
        return t['id'],year,[esp_event(e,t['espn_id'],u) for e in p.get('events',[]) if e.get('league')]
    assignments=[(t,2026) for t in h]+[(t,2025) for t in h if len(t['fixtures'])<10 or any(str(f['status']['utcTime'])<'2026-07' for f in t['fixtures'])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        schedules=list(ex.map(fetch_season,assignments))
    save_json(ROOT/'espn_schedules.json',{f'{tid}_{yr}':events for tid,yr,events in schedules})
    prev={tid:events for tid,yr,events in schedules if yr==2025}
    print('prior events', {t['name']:[(e['date'],e['name'],e['comp']) for e in prev.get(t['id'],[])[:3]] for t in h if t['id'] in prev},flush=True)
    pending=[]
    for t in h:
        n=10-len(t['fixtures'])
        if n>0:
            past=[e for e in prev[t['id']] if e['finished'] and 'friendl' not in e['slug'].lower() and 'friendl' not in e['comp'].lower() and e['date']<t['fixtures'][-1]['status']['utcTime']]
            pending += [(t,e) for e in sorted(past,key=lambda e:e['date'], reverse=True)[:n]]
    dates=sorted({e['date'][:10].replace('-','') for t,e in pending})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        daily=dict(zip(dates,ex.map(lambda day:get('daily_'+day,API+f'matches?date={day}&timezone=UTC'),dates)))
    new=[]
    for t,e in pending:
        day=e['date'][:10].replace('-',''); sd=API+f'matches?date={day}&timezone=UTC'
        matches=[(m,l) for l in daily[day]['leagues'] for m in l['matches'] if t['id'] in [m.get('home',{}).get('id'),m.get('away',{}).get('id')] and m['status'].get('finished') and 'friendl' not in l['name'].lower()]
        if len(matches)!=1:
            raise ValueError((t['name'],e,'candidates',[(m['id'],l['name']) for m,l in matches]))
        m,l=matches[0]; side='home' if m['home']['id']==t['id'] else 'away'
        assert e[side+'_id']==t['espn_id']
        opp=m['away'] if side=='home' else m['home']
        f=dict(id=m['id'],pageUrl=m.get('pageUrl') or f"/matches/{m['id']}",opponent=opp,home=m['home'],away=m['away'],
               tournament={'name':l['name'],'leagueId':l.get('primaryId') or l['id']}, status=dict(m['status']),
               schedule_source_url=sd, prior_season_schedule_url=e['team_schedule_url'])
        t['fixtures'].append(f); new.append(f['id'])
    for t in h:
        t['fixtures'].sort(key=lambda f:f['status']['utcTime'],reverse=True)
        assert len(t['fixtures'])==10 and len({f['id'] for f in t['fixtures']})==10, (t['name'],len(t['fixtures']))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda fid:get(f'match_{fid}',API+f'matchDetails?matchId={fid}'),sorted(set(new))))
    save_json(ROOT/'histories.json',h)
    print('Historical results',len(pending),'new unique',len(set(new)),flush=True)


if __name__=='__main__':
    main()
