"""Build field-traceable, team-first, regulation-only observations for 32 teams, 10 each."""
from __future__ import annotations

from collections import Counter, defaultdict
import gzip
import json
import re
import urllib.request

from lxml import html
from download import API, CACHE, CUTOFF, FM, ROOT, save_json

STAT_KEYS = {
    'corners':'corners', 'xg':'expected_goals', 'shots':'total_shots',
    'shots_on_target':'ShotsOnTarget', 'possession':'BallPossesion',
    'fouls':'fouls', 'yellow_cards':'yellow_cards', 'red_cards':'red_cards',
    'offsides':'Offsides', 'big_chances':'big_chance', 'big_chances_missed':'big_chance_missed_title',
    'touches_opp_box':'touches_opp_box', 'accurate_passes':'accurate_passes',
    'passes':'passes', 'xgot':'expected_goals_on_target', 'xg_open_play':'expected_goals_open_play',
    'xg_set_play':'expected_goals_set_play', 'xg_non_penalty':'expected_goals_non_penalty',
    'keeper_saves':'keeper_saves', 'shots_inside_box':'shots_inside_box', 'shots_outside_box':'shots_outside_box',
    'shots_off_target':'ShotsOffTarget', 'blocked_shots':'blocked_shots', 'woodwork':'shots_woodwork',
    'tackles':'matchstats.headers.tackles', 'interceptions':'interceptions', 'clearances':'clearances',
    'duels_won':'duel_won', 'dribbles_succeeded':'dribbles_succeeded', 'accurate_crosses':'accurate_crosses',
    'throw_ins':'player_throws', 'ground_duels_won':'ground_duels_won', 'aerial_duels_won':'aerials_won',
    'shot_blocks':'shot_blocks', 'accurate_long_balls':'long_balls_accurate',
}
PS_KEYS = {'corners':'corners', 'shots':'shots total','shots_on_target':'shots on target',
           'shots_off_target':'shots off target','possession':'possession','fouls':'fouls',
           'yellow_cards':'yellow cards','red_cards':'red cards','offsides':'offsides',
           'keeper_saves':'saves','throw_ins':'throw ins'}
PS_PAGES = {6315940:('plfatima','fatima/academico-viseu/2026-10-18'),
            6315944:('plmarit','alcochetense/maritimo/2026-10-18'),
            6315946:('plrio','louletano/rio-ave/2026-10-17'),
            6315948:('plmafra','mafra/santa-clara/2026-10-18')}
CLUB_VENUES = {5985964:('https://csmaritimo.pt/noticias/maritimo-lutou-ate-ao-fim','Estádio José Alvalade'),
               5985963:('https://academicodeviseu.pt/noticia.php?n=1408','Estádio Do Dragão')}
FPF_CUPS = {6315940:2687281,6315944:2687269,6315946:2687280,6315948:2687274}


def raw_file(tag):
    return json.loads(gzip.decompress((CACHE/(tag+'.json.gz')).read_bytes()))


def numeric(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool)


def pair(a,b):
    return [a,b] if numeric(a) and numeric(b) else None


def reverse(pair_or_null,idx):
    return (pair_or_null[::-1] if idx else pair_or_null) if pair_or_null is not None else None


def snap(period):
    out={}
    for group in (period or {}).get('stats',[]):
        for item in group.get('stats',[]):
            if item.get('type')=='title':
                continue
            val=item.get('rawStats')
            if isinstance(val,list) and len(val)==2:
                v=pair(*[(x or {}).get('value') for x in val])
                if v is not None:
                    if item['key'] in out: assert out[item['key']]==v
                    out[item['key']]=v
    return out


def playerstats(fid):
    name,path=PS_PAGES[fid]; u='https://playerstats.football/fixture/'+path
    dest=ROOT/('probe_'+name)
    if not dest.is_file():
        with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=30) as r:
            dest.write_bytes(r.read())
    d=html.fromstring(dest.read_bytes()); ld=json.loads(d.xpath('//script[@type="application/ld+json"]')[0].text)
    assert ld['url']==u and ld['eventStatus'].endswith('EventCompleted')
    teamstat=[{it['name']:int(it['value'].replace('%','')) for it in ld[s]['additionalProperty'] if str(it.get('value','')).strip('%').isdigit()} for s in ('homeTeam','awayTeam')]
    stats={out:pair(t1.get(k),t2.get(k)) for out,k in PS_KEYS.items() for t1,t2 in [teamstat]}
    half=None; events=[]; totals=defaultdict(lambda:[0,0]); next_num=1
    for block in d.xpath('//*[@id="corners-panel"]/div'):
        text=' '.join(block.text_content().split())
        if text in ('1st-half','2nd-half','extra-time'):
            half=text
            if half=='extra-time': next_num=1
            continue
        m=re.fullmatch(r"(\d+(?:\+\d+)?)'\s+(\d+)(?:st|nd|rd|th) Corner(?: ET)?",text)
        if m:
            assert half is not None and int(m[2])==next_num, (fid,text,next_num)
            i=int('flex-row-reverse' in (block.get('class') or '').split())
            totals[half][i]+=1;next_num+=1
            events.append({'period':half,'minute':m[1], 'side':('home','away')[i],'sequence':int(m[2])})
    assert stats['corners'] and stats['corners']==[sum(v[i] for v in totals.values()) for i in (0,1)], (fid,stats['corners'],totals)
    h1=totals['1st-half']; c90=[h1[i]+totals['2nd-half'][i] for i in (0,1)]
    return dict(url=u,stat=stats,first_corners=h1,regular_corners=c90,corner_events=events, teams=[ld[s]['name'] for s in ('homeTeam','awayTeam')])


def main():
    histories=json.loads((ROOT/'histories.json').read_text(encoding='utf-8'))
    esp_schedules=json.loads((ROOT/'espn_schedules.json').read_text(encoding='utf-8'))
    supplement={fid:playerstats(fid) for fid in PS_PAGES}
    save_json(ROOT/'cup_corners_evidence.json',supplement)
    records=[]; missing=Counter(); cross_issues=[]; event_notes=[]
    for t in histories:
        matches=t['fixtures']; assert len(matches)==10 and len(set(f['id'] for f in matches))==10
        assert all(matches[i]['status']['utcTime']>matches[i+1]['status']['utcTime'] for i in range(9))
        esp_events=[e for year in (2025,2026) for e in esp_schedules.get(f"{t['id']}_{year}",[])]
        for rank,f in enumerate(matches,1):
            fid=f['id'];d=raw_file(f'match_{fid}');g=d['general']; hdr=d['header']; cc=d['content']; facts=cc.get('matchFacts') or {}; info=facts.get('infoBox') or {}
            assert int(g['matchId'])==fid and g.get('gender')=='male' and g.get('started') and g.get('finished') and g['matchTimeUTCDate']<CUTOFF
            assert g['parentLeagueId']==int(f['tournament']['leagueId']) or g['leagueId']==int(f['tournament']['leagueId']), (fid,g['parentLeagueId'],f['tournament'])
            home=g['homeTeam']; away=g['awayTeam']; idx=(home['id']!=t['id']); designated=('away' if idx else 'home'); opp=home if idx else away
            assert t['id'] in (home['id'],away['id']) and opp['id']==int(f['opponent']['id'])
            ev=[e for e in (facts.get('events') or {}).get('events',[]) if e.get('type')=='Half']
            breaks={e['time']:pair(e.get('homeScore'),e.get('awayScore')) for e in ev}
            assert breaks.get(45) is not None and breaks.get(90) is not None, (t['name'],fid,breaks)
            cb=hdr['status'];extra=bool(cb.get('halfs',{}).get('firstExtraHalfStarted') or 120 in breaks or cb.get('reason',{}).get('short')=='AET')
            if not extra:
                assert breaks[90]==[x['score'] for x in hdr['teams']], (fid,breaks[90],hdr['teams'])
            status=cb.get('reason',{}).get('short'); pens=cb.get('reason',{}).get('penalties'); dto=g['matchTimeUTCDate']
            esp=[e for e in esp_events if e['date'][:10]==dto[:10] and e[designated+'_id']==t['espn_id'] and e.get('finished')]
            assert len(esp)<=1, (fid,esp)
            e=esp[0] if esp else {}; neutral=e.get('neutral')
            if e and (e['date'][:16]!=dto[:16] or [e['score_home'].get('value'),e['score_away'].get('value')] != (breaks.get(120) if extra else breaks[90])):
                cross_issues.append({'target_id':t['target_id'],'team':t['name'],'fotmob_id':fid,'espn_id':e['fixture_id'],
                                      'fm_kickoff':dto,'espn_kickoff':e['date'],'fm_90':breaks[90],'fm_120':breaks.get(120),
                                      'espn_score':[e['score_home'].get('value'),e['score_away'].get('value')], 'espn_status':e.get('status')})
            canonical=(d.get('seo',{}).get('eventJSONLD') or {}).get('@id') or f'{FM}/match/{fid}'
            page=FM+f['pageUrl'] if '#' in f.get('pageUrl','') else canonical
            sources={'match_page':page,'match_api':API+f'matchDetails?matchId={fid}', 'team_schedule':t['team_schedule_url'],
                     'fixture_selection':f['schedule_source_url'], 'espn_match_page':e.get('match_page'), 'espn_team_schedule':e.get('team_schedule_url'),
                     'prior_season_all_competitions':f.get('prior_season_schedule_url')}
            sources={k:v for k,v in sources.items() if v}
            if fid in CLUB_VENUES and neutral is None:
                club_url,stadium=CLUB_VENUES[fid]
                assert info['Stadium']['name'].casefold()==stadium.casefold() and designated=='away'
                sources['away_club_venue']=club_url
                neutral=False
            if fid in FPF_CUPS:
                sources['portuguese_federation']='https://centroderesultados-as-prd.azurewebsites.net/Match/GetMatchInformation?matchId='+str(FPF_CUPS[fid])
            fieldsrc={'team':'FM.identity', 'opponent':'FM.identity','kickoff_utc':'FM.time','comp':'FM.competition',
                      'designated_side':'FM.identity','venue':'FM.stadium','neutral_venue':('CLUB.homeground' if 'away_club_venue' in sources else ('ESPN.neutral' if neutral is not None else None)),
                      'halftime':'FM.break.45','score90':'FM.break.90','score_aet':('FM.break.120' if 120 in breaks else None),
                      'penalties':('FM.penaltyShootout' if pens else None)}
            periods=(cc.get('stats') or {}).get('Periods') or {}; allp=snap(periods.get('All')); p1=snap(periods.get('FirstHalf')); p2=snap(periods.get('SecondHalf'))
            stats={}; stats_sources={}; notes=[]
            for out,k in STAT_KEYS.items():
                if not extra:
                    v=allp.get(k);code=f'FM.All.{k}' if v is not None else None
                elif out!='possession' and k in p1 and k in p2:
                    v=[round(p1[k][i]+p2[k][i],2) for i in range(2)]; code=f'FM.FirstHalf+SecondHalf.{k}'
                else:
                    v=None;code=None
                stats[out]=v;stats_sources[out]=code
                for suffix,period,data in [('_h1','FirstHalf',p1),('_h2','SecondHalf',p2)]:
                    stats[out+suffix]=data.get(k); stats_sources[out+suffix]=f'FM.{period}.{k}' if k in data else None
            if fid in supplement:
                ps=supplement[fid];sources['playerstats_match_page']=ps['url']
                if not extra:
                    for k,v in ps['stat'].items():
                        if stats[k] is None and v is not None:
                            stats[k]=v;stats_sources[k]='PS.ld.'+PS_KEYS[k]
                for key,val,field in [('corners',ps['regular_corners'],'PS.timeline.90'),('corners_h1',ps['first_corners'],'PS.timeline.45'),
                                    ('corners_h2',[a-b for a,b in zip(ps['regular_corners'],ps['first_corners'])],'PS.timeline.2H')]:
                    stats[key]=val;stats_sources[key]=field
                notes.append('FotMob competition coverage is lower. PlayerStats provides period-tagged corners; only directly reported compatible team statistics are included.')
            for k in list(stats):
                stats[k]=reverse(stats[k],idx)
                if stats[k] is None: missing[k]+=1
            for suffix in ('_h1','_h2'):
                if stats.get('corners') is not None and stats.get('corners'+suffix) is not None:
                    assert all(x<=y for x,y in zip(stats['corners'+suffix],stats['corners'])),(fid,stats)
            if extra:
                notes.append('Extra time played. Goals and technical statistics stop at the end of the second half. Full-match aggregates that also include extra time are excluded.')
            elif pens:
                notes.append('Penalty shootout followed regulation. Shootout goals are excluded from both scores and statistics.')
            if not e:
                if 'away_club_venue' in sources:
                    notes.append('No same-date ESPN record. The away club identifies the trip to its opponent’s home stadium, corroborated by the FotMob match venue.')
                else:
                    notes.append('No same-date ESPN all-competition record. Home/away is the FotMob fixture designation; a neutral-site flag was unavailable.')
            if t['id']==9941 and rank==9:
                notes.append('The later Nantes–Toulouse fixture on 17 May stopped permanently after 22 minutes. Official 0–0 classification is excluded from full-length historical samples.')
                sources['excluded_abandoned_match']='https://ligue1.com/fr/articles/l1_article_5189-discipline-toutes-les-decisions-l1-270526'
            venue=info.get('Stadium') or {};kind= ('neutral' if neutral is True else designated)
            row={'target_id':t['target_id'],'target_fixture_id':t['target_fixture_id'],'side':t['side'],'rank':rank,
                 'team':t['name'],'team_id':t['id'],'opponent':opp['name'],'opponent_id':opp['id'],'kickoff_utc':dto,'date':dto[:10],
                 'comp':g['leagueName'],'comp_id':g['parentLeagueId'],'country':g['countryCode'],'round':g.get('leagueRoundName'),
                 'venue_side':kind,'designated_side':designated,'neutral_venue':neutral,'venue':venue.get('name'),'venue_city':venue.get('city'),
                 'fixture_id':fid,'espn_fixture_id':e.get('fixture_id'),'match_status':status,'extra_time_played':extra,
                 'halftime':reverse(breaks[45],idx),'score90':reverse(breaks[90],idx),'score_aet':reverse(breaks.get(120),idx),
                 'penalties':reverse(pens,idx),'stats':stats,'sources':sources,'field_sources':fieldsrc,'stats_sources':stats_sources,
                 'notes':notes,'stat_pair_order':'team,opponent'}
            records.append(row)
    save_json(ROOT/'team_last10.json',records)
    save_json(ROOT/'cross_source_issues.json',cross_issues)
    summary={'asof_cutoff_utc':CUTOFF,'records':len(records),'teams':len(histories),'unique_fixtures':len({r['fixture_id'] for r in records}),
             'rows_with_matched_espn_schedule':sum(bool(r['espn_fixture_id']) for r in records),
             'rows_with_explicit_venue_state':sum(r['neutral_venue'] is not None for r in records),
             'last10_each':all(sum(r['team_id']==t['id'] for r in records)==10 for t in histories),
             'stats_complete_count':{k:len(records)-missing[k] for k in records[0]['stats']}, 'rows_with_halftime':sum(r['halftime'] is not None for r in records),
             'rows_with_score90':sum(r['score90'] is not None for r in records),'raw_period_stats_unique_games':len({r['fixture_id'] for r in records if r['stats_sources']['corners_h1'].startswith('FM.')}),
             'competition_rows':dict(Counter(r['comp'] for r in records)),
             'excluded_examples':{'4830760':{'event':'Nantes v Toulouse, 17 May 2026','reason':'Permanently abandoned after 22 minutes; 0–0 ratified by competition only.',
                                             'club':'https://www.fcnantes.com/articles/article2809.php?num=49200','league':'https://ligue1.com/fr/articles/l1_article_5189-discipline-toutes-les-decisions-l1-270526'}}}
    assert summary['records']==320 and summary['teams']==32 and summary['last10_each']
    save_json(ROOT/'coverage.json',summary)
    save_json(ROOT/'field_dictionary.json',{
        'cutoff_utc':CUTOFF,'order':'All statistics and score pairs in a record are [team, opponent]. The raw provider order is [designated home, designated away]. rank 1 is newest.',
        'missing':'JSON null if either value of a pair is unreported or there is no supported 90-minute statistic. Published zeros remain 0. Source null denotes absence. Statistics marked H1 and H2 include first- or second-half stoppage time. No projections replace missing measurements.',
        'field_source_conventions':{'FM.identity':'sources.match_api: general.homeTeam, general.awayTeam. Team selection uses sources.team_schedule or prior-season ESPN all-competition schedule plus FotMob daily.',
            'FM.time':'sources.match_api: general.matchTimeUTCDate','FM.competition':'sources.match_api: general.leagueName, parentLeagueId, countryCode, leagueRoundName',
            'FM.stadium':'sources.match_api: content.matchFacts.infoBox.Stadium.name and .city','ESPN.neutral':'sources.espn_team_schedule: events[id==espn_fixture_id].competitions[0].neutralSite.',
            'CLUB.homeground':'sources.away_club_venue: away club confirms playing/travelling to the other club’s home stadium; checked against FotMob match stadium.',
            'FM.break.<n>':'sources.match_api: content.matchFacts.events.events[type==Half,time==n].homeScore, .awayScore','FM.penaltyShootout':'sources.match_api: header.status.reason.penalties (separate from score90)',
            'FM.<period>.<key>':'sources.match_api: content.stats.Periods[period].stats[].stats[key==key,type!=title].rawStats[].value. All for no extra time; if extra time key FirstHalf+SecondHalf denotes sum of the two direct period values.',
            'PS.ld.<label>':'sources.playerstats_match_page: JSON-LD homeTeam and awayTeam additionalProperty[name==label].value. Full match used only without extra time.',
            'PS.timeline.<45|2H|90>':'sources.playerstats_match_page: #corners-panel rows with ordered corner numbers and 1st-half/2nd-half/extra-time headings. Left-aligned rows home and right-aligned rows away, validated against JSON-LD home/away totals. 45 sums first-half only, 2H sums second-half only, 90 sums both. Full event sequence stored in cup_corners_evidence.json with all extra-time events separately labelled.'},
        'regular_stat_keys':STAT_KEYS,'suffixes':{'_h1':'first half incl. stoppage','_h2':'second half incl. stoppage'},
        'venue_side':'neutral if ESPN explicitly records true, otherwise designated home/away. neutral_venue independently distinguishes unreported (null) from verified false.',
        'extraction_commands':['python download.py','python supplement.py','python extract.py'], 'external_dependency':'lxml for alternative HTML source parsing; remainder is Python standard library.'})
    print('rows',len(records),'unique',summary['unique_fixtures'],'ht',summary['rows_with_halftime'],'xg',summary['stats_complete_count']['xg'],
          'corn1',summary['stats_complete_count']['corners_h1'],'espn',summary['rows_with_matched_espn_schedule'],'cross differences',cross_issues)


if __name__=='__main__':
    main()
