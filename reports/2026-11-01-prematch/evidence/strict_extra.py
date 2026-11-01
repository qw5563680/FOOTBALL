"""Validate non-scoring final passes from individual ESPN attempts, against ESPN's own shot and scorer totals."""
from __future__ import annotations
from collections import Counter
import json
import re

from download import ROOT,save_json
from extract import raw_file

DEFINITION='https://www.statsperform.com/opta-event-definitions/'
NONGOAL={'shot-off-target','shot-hit-woodwork','shot-on-target','shot-blocked','penalty---saved','penalty---missed'}
GOALS={'goal','goal---free-kick','goal---header','goal---volley','penalty---scored'}


def validated_plays(r):
    eid=r['espn_fixture_id']
    if not eid: return {'reason':'No matching ESPN fixture'}
    if r['extra_time_played']: return {'reason':'ESPN boxscore includes extra time; no same-period team shot comparator'}
    d=raw_file('esp_summary_'+eid);sel=d.get('header',{}).get('competitions',[{}])[0];teams={x['homeAway']:x for x in sel.get('competitors',[])}
    u='https://www.espn.com/soccer/commentary/_/gameId/'+eid
    info={'source_url':u,'espn_fixture_id':eid}
    def reject(reason): return {**info,'reason':reason}
    if set(teams)!={'home','away'}:return reject('Missing participants')
    tn=[teams[side]['team']['displayName'] for side in ('home','away')]
    teamid=[teams[side]['team']['id'] for side in ('home','away')]
    own_shots=[None,None]
    for g in d.get('boxscore',{}).get('teams',[]):
        n=g['team']['id'];v=next((s.get('displayValue') for s in g.get('statistics',[]) if s['name']=='totalShots'),None)
        if n in teamid and v is not None and str(v).isdigit():own_shots[teamid.index(n)]=int(v)
    if any(v is None for v in own_shots) or not sum(own_shots):return reject('No complete published shot counts for both clubs')
    info['boxscore_shots']=own_shots
    rs=[None,None]
    for g in d.get('rosters',[]):
        if g.get('team',{}).get('id') not in teamid:continue
        order=teamid.index(g['team']['id']); vals=[]
        for pl in g.get('roster',[]):
            st={v['name']:v.get('value') for v in pl.get('stats',[])}
            if st.get('appearances',0)>0 or pl.get('starter'):
                if 'goalAssists' not in st: vals=[];break
                vals.append(st['goalAssists'])
        if len(vals)>=11 and all(isinstance(v,(int,float)) for v in vals):rs[order]=int(sum(vals))
    info['roster_goal_assists']=rs
    if None in rs:return reject('Missing complete starting/appearing player goal-assist comparator')
    counts=[0,0];goals=[0,0];kp=[0,0];ga=[0,0];h1kp=[0,0];events=[];seen=set(); full=False
    for ev in d.get('commentary',[]):
        if (ev.get('text') or '').startswith('Second Half ends,'):
            full=True
        p=ev.get('play') or {};t=(p.get('type') or {}).get('type');period=(p.get('period') or {}).get('number')
        if period not in (1,2): continue
        if t=='end-regular-time':full=True
        if t not in NONGOAL|GOALS|{'own-goal'}:continue
        pid=p.get('id');name=(p.get('team') or {}).get('displayName')
        if not pid or pid in seen or name not in tn:return reject('Missing or duplicate attempt identity or club')
        seen.add(pid);i=tn.index(name)
        if t=='own-goal':goals[i]+=1;continue
        text=p.get('text') or '';parts=p.get('participants') or []; assisted=bool(re.search(r'\bAssisted by\s+\S',text,re.I))
        if not text or re.search(r'\bat\s+\d+[\'+]',text,re.I) or (not assisted and len(parts)>1):return reject('One or more attempts have commentary without a verifiable final-pass attribution')
        if not (text.startswith(('Attempt ', 'Goal!', 'Penalty ')) or re.search(r'\b(?:hits|hit) (?:the )?(?:right |left )?(?:bar|post|woodwork|crossbar)\b',text,re.I)):
            return reject('Non-standard attempt narrative')
        counts[i]+=1
        if t in GOALS:
            goals[i]+=1;ga[i]+=int(assisted)
        elif assisted:
            kp[i]+=1;h1kp[i]+=int(period==1)
        passer=parts[1].get('athlete',{}).get('displayName') if assisted and len(parts)>=2 else None
        if assisted and not passer:
            passer=re.split(r'\s+(?:with|following|after)\s+|\.',re.split(r'\bAssisted by\s+',text,flags=re.I)[1].strip(),maxsplit=1)[0]
        events.append({'id':pid,'period':period,'minute':(p.get('clock') or {}).get('displayValue'),'team':name,'type':t,'explicit_final_pass':assisted,'passer':passer})
    expected=r['score90'] if r['designated_side']=='home' else r['score90'][::-1]
    info.update({'enumerated_shots':counts,'enumerated_goals':goals,'enumerated_goal_assists':ga})
    if not full:return reject('Missing end-of-regulation marker')
    if counts!=own_shots or goals!=expected or ga!=rs:return reject('Event count failed reconciliation against team total shots, goals or roster assists')
    return {**info,'teams':tn,'key_passes':kp,'key_passes_h1':h1kp,'goal_assists':ga,'chances_created':[a+b for a,b in zip(kp,ga)],'evidence':events}


def main():
    rows=json.loads((ROOT/'team_last10.json').read_text(encoding='utf8')); ps=json.loads((ROOT/'playerstats_homeaway.json').read_text(encoding='utf8'))
    byid={r['fixture_id']:r for r in rows}; processed={fid:validated_plays(r) for fid,r in byid.items()};save_json(ROOT/'espn_keypass_evidence.json',processed)
    out=[]; rejected=Counter()
    for r in rows:
        fid=r['fixture_id'];p=processed[fid];a=ps.get(str(fid),{});rev=r['designated_side']=='away'
        orient=lambda v:None if v is None else (v[::-1] if rev else v)
        if 'reason'in p:rejected[p['reason']]+=1
        out.append({'target_id':r['target_id'],'side':r['side'],'rank':r['rank'],'team_id':r['team_id'],'team':r['team'],'fixture_id':fid,
                    'key_passes':orient(p.get('key_passes')),'key_passes_h1':orient(p.get('key_passes_h1')),'goal_assists':orient(p.get('goal_assists')),
                    'chances_created':orient(p.get('chances_created')),'free_kicks':orient(a.get('free_kicks')),
                    'key_passes_source_url':p.get('source_url'),'free_kicks_source_url':a.get('source_url'),'source_url':a.get('source_url'),
                    'key_passes_missing_reason':p.get('reason'),'pair_order':'team,opponent'})
    save_json(ROOT/'team_last10_extra.json',out)
    coverage={k:sum(q[k] is not None for q in out) for k in ('key_passes','key_passes_h1','goal_assists','chances_created','free_kicks')}
    summary={'records':len(out),'coverage':coverage,'unaccepted_reason_team_rows':dict(rejected),'published_definition':DEFINITION,
             'key_passes':'Count of ESPN first/second-half non-goal attempts with explicit Assisted by narrative and identified passing player. All regulation attempts must match each ESPN team shot total, all goals must match the actual score, and goal-assist events must match each team ESPN player roster sum. Generic commentary or ambiguous helper fields prevent certification. Raw event ids in espn_keypass_evidence.json.',
             'chances_created':'Sum of verified non-scoring key passes and verified scoring goal assists from the same fixture provider; stored separately.',
             'free_kicks':'PlayerStats team-level JSON-LD additionalProperty named free kicks. No indirect calculation from fouls. Extra-time aggregates excluded.',
             'unverified_fields':['attacks','dangerous_attacks','possession_lost','one_on_one_wins','tackles_won'],
             'ignored_vendor_key_passes':'The originally displayed PlayerStats label includes final passes that scored in many matches and outputs placeholder zeros when a player feed is absent. Never use playerstats_homeaway.json.key_passes as the non-scoring statistic.',
             'scripts':['download.py','supplement.py','extract.py','playerstats_extra.py','espn_play_cache.py','strict_extra.py']}
    save_json(ROOT/'extra_coverage.json',summary);print(coverage,dict(rejected),flush=True)


if __name__=='__main__':main()
