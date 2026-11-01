"""Acquire PlayerStats' explicitly named Key Passes and Free Kicks fields via linked historical fixtures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import gzip
import json
import re
from pathlib import Path
import time
import urllib.error
import urllib.request

from lxml import html
from download import ROOT, save_json

BASE='https://playerstats.football'
PS_CACHE=ROOT/'playerstats_raw';PS_CACHE.mkdir(exist_ok=True)
SCHEDULES=[('serie-a','como','venezia'),('la-liga','getafe','sevilla'),('serie-a','frosinone','torino'),
           ('serie-a','lazio','cagliari'),('ligue-1','strasbourg','auxerre'),('bundesliga','fc-union-berlin','fc-koln'),
           ('ligue-1','paris','monaco'),('premier-league','liverpool','arsenal'),('bundesliga','eintracht-frankfurt','hamburger-sv'),
           ('serie-a','lecce','genoa'),('la-liga','elche','valencia'),('liga-portugal','maritimo','santa-clara'),
           ('liga-portugal','academico-viseu','rio-ave'),('serie-a','juventus','napoli'),('ligue-1','olympique-marseille','toulouse'),
           ('la-liga','athletic-club','real-sociedad')]


def fetch(tag,url):
    p=PS_CACHE/(tag+'.html.gz')
    if p.is_file(): return html.fromstring(gzip.decompress(p.read_bytes()))
    for i in range(3):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept-Encoding':'gzip'})
            with urllib.request.urlopen(req,timeout=28) as r:
                b=r.read()
                if r.headers.get('Content-Encoding','').lower()=='gzip': b=gzip.decompress(b)
            p.write_bytes(gzip.compress(b,compresslevel=3)); return html.fromstring(b)
        except Exception as e:
            if i==2 or isinstance(e,urllib.error.HTTPError) and e.code in (403,404): raise
            time.sleep(1+i*2)


def lookup(args):
    t,lg,slug=args; url=BASE+'/'+lg+'/'+slug; doc=fetch('team_'+str(t['id']),url)
    possible=[a.get('href') for a in doc.xpath('//a[@href]') if '/fixture/' in a.get('href','')]
    out={}
    for f in t['fixtures']:
        day=f['status']['utcTime'][:10]; side='home' if int(f['home']['id'])==t['id'] else 'away'; idx=2 if side=='home' else 3
        links=list(dict.fromkeys(a for a in possible if a.endswith('/'+day) and len(a.split('/'))==5 and a.split('/')[idx]==slug))
        if len(links)==1: out[f['id']]=BASE+links[0]
        elif len(links)>1: raise ValueError((t['name'],f,links))
    return t['id'],out


def extract_panel(doc,title,output_label):
    h=doc.xpath(f'//h3[substring(normalize-space(), string-length(normalize-space()) - {len(title)-1})="{title}"]')
    if len(h)!=1: return None,None
    p=h[0].getparent();text=' '.join(' '.join(x.itertext()) for x in p.findall('p')).strip()
    # Explicit statements are in home, then away order. A raw placeholder zero is never used by itself.
    vals=[(int(m[1]) if m[1].isdigit() else 0) for m in re.finditer(r'\bhad (\d+|no) '+output_label+r'\b',text,flags=re.IGNORECASE)]
    # The nonzero grid is the accompanying per-player evidence. A missing player feed also emits zero placeholders.
    grid=p.find('div'); nplayers=(len(grid.xpath('.//a[@href]')) if grid is not None else 0)
    return (vals if len(vals)==2 else None), (text if len(vals)==2 else '')


def one(args):
    fid,u,row=args; d=fetch('match_'+str(fid),u)
    ld=json.loads(d.xpath('//script[@type="application/ld+json"]')[0].text)
    assert ld.get('eventStatus','').endswith('EventCompleted') and ld['startDate'][:10]==row['date'], (fid,ld['name'],ld['startDate'])
    side_slug=[p.split('/')[-1] for p in u.split('/fixture/')[1].strip('/').split('/')[:2]]
    rep=[ld[s]['url'].rstrip('/').split('/')[-1] for s in ['homeTeam','awayTeam']]
    assert side_slug==rep,(fid,side_slug,rep)
    props=[{it['name']:it['value'] for it in ld[s]['additionalProperty']} for s in ('homeTeam','awayTeam')]
    direct=lambda name: [int(p[name]) for p in props] if all(p.get(name,'').isdigit() for p in props) else None
    key,txt=extract_panel(d,'Key Passes',r'key passes?')
    shots,shot_text=extract_panel(d,'Shots',r'shots?')
    if key==[0,0] and (not shots or shots==[0,0]): key=None
    out={'fixture_id':fid,'source_url':u,'home':ld['homeTeam']['name'],'away':ld['awayTeam']['name'],'kickoff':ld['startDate'],
         'key_passes':key,'free_kicks':direct('free kicks'),'shots_team_table':direct('shots total'),'shots_players':shots,
         'corners_team_table':direct('corners'),'source_text_key_passes':txt,'source_text_shots':shot_text,
         'goals_published':direct('goals'),'pair_order':'designated_home,designated_away'}
    if row['extra_time_played']:
        out['key_passes']=None;out['free_kicks']=None;out['notes']='Full-match source includes extra time. No period-specific free-kicks or key-passes field.'
    return out


def main():
    h=json.loads((ROOT/'histories.json').read_text(encoding='utf-8'));rows=json.loads((ROOT/'team_last10.json').read_text(encoding='utf-8'))
    scope={r['fixture_id']:r for r in rows}; links={};errors={}
    args=[]
    for t in h:
        lg,home,away=SCHEDULES[t['target_id']-1];args.append((t,lg,home if t['side']=='home' else away))
    with ThreadPoolExecutor(max_workers=7) as pool:
        futs={pool.submit(lookup,x):x for x in args}
        for f in as_completed(futs):
            t,lg,slug=futs[f]
            try: tid,dd=f.result(); links.update(dd)
            except Exception as ex: errors['team_'+str(t['id'])]=str(ex)
    print('historical page URLs',len(links),'missing',len(scope)-len(links),'team_error',errors,flush=True)
    save_json(ROOT/'playerstats_links.json',links)
    out={}
    with ThreadPoolExecutor(max_workers=7) as pool:
        futs={pool.submit(one,(fid,u,scope[fid])):fid for fid,u in links.items()}
        for i,f in enumerate(as_completed(futs),1):
            fid=futs[f]
            try: out[fid]=f.result()
            except Exception as ex: errors['match_'+str(fid)]=str(ex)
            if i%50==0: print('additional pages',i,'/',len(futs),'err',len(errors),flush=True)
    save_json(ROOT/'playerstats_homeaway.json',out);save_json(ROOT/'playerstats_errors.json',errors)
    sr=[]
    for r in rows:
        p=out.get(r['fixture_id'],{});i=int(r['designated_side']=='away')
        def reord(pair): return (pair[::-1] if i else pair) if pair is not None else None
        sr.append({'target_id':r['target_id'],'team_id':r['team_id'],'team':r['team'],'side':r['side'],'fixture_id':r['fixture_id'],
                   'rank':r['rank'],'key_passes':reord(p.get('key_passes')),'free_kicks':reord(p.get('free_kicks')),
                   'source_url':p.get('source_url'),'pair_order':'team,opponent'})
    save_json(ROOT/'team_last10_extra.json',sr)
    print('result',len(out),len(sr),'direct key passes',sum(x['key_passes'] is not None for x in sr),
          'direct free kicks',sum(x['free_kicks'] is not None for x in sr),'sample errors',list(errors.items())[:8],flush=True)


if __name__=='__main__': main()
