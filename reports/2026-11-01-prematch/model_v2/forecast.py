"""Freeze present-time, odds-free TEAM forecasts; repeat with saved input/targets for offline reproduction."""
from datetime import UTC, datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import csv, json, hashlib, math, sys, importlib.metadata
import numpy as np
import pandas as pd
import requests
from scipy.stats import skellam
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.derive import had

ROOT=Path(__file__).resolve().parent
LEAGUES={'I1':'ita.1','SP1':'esp.1','F1':'fra.1','D1':'ger.1','E0':'eng.1','P1':'por.1'}
IDS=[
 ('I1','401875002','2572','17530','Como','Venezia','科木','威尼斯'),
 ('SP1','401882821','2922','243','Getafe','Sevilla','赫塔费','塞维利亚'),
 ('I1','401874782','4057','239','Frosinone','Torino','弗洛西诺尼','都灵'),
 ('I1','401874983','112','2925','Lazio','Cagliari','拉齐奥','卡利亚里'),
 ('F1','401876414','180','172','Strasbourg','Auxerre','斯特拉斯堡','欧塞尔'),
 ('D1','401884750','598','122','Union Berlin','FC Koln','柏林联','科隆'),
 ('F1','401876415','6851','174','Paris FC','Monaco','巴黎FC','摩纳哥'),
 ('E0','401879246','364','359','Liverpool','Arsenal','利物浦','阿森纳'),
 ('D1','401884752','125','127','Ein Frankfurt','Hamburg','法兰克福','汉堡'),
 ('I1','401874968','113','3263','Lecce','Genoa','莱切','热那亚'),
 ('SP1','401882823','3751','94','Elche','Valencia','埃尔切','瓦伦西亚'),
 ('P1','401885406','552','12215','Maritimo','Santa Clara','马里迪莫','圣塔克莱拉'),
 ('P1','401885411','21607','3822','Academico Viseu','Rio Ave','维塞乌','里奥阿维'),
 ('I1','401874949','111','114','Juventus','Napoli','尤文图斯','那不勒斯'),
 ('F1','401876417','176','179','Marseille','Toulouse','马赛','图卢兹'),
 ('SP1','401882826','93','89','Ath Bilbao','Sociedad','毕尔巴鄂竞技','皇家社会')]
VERSION='T05-ddcf3da/DC90-full-1X2-v1.1'; EXCLUDED={
 ('D1','2024-12-14','Union Berlin','Bochum'):'administrative decision after interrupted match; omit',
 ('F1','2025-03-16','Montpellier','St Etienne'):'permanently interrupted; omit',
 ('F1','2026-05-17','Nantes','Toulouse'):'league ratified 0-0 at permanent abandonment after 22 minutes; no 90-minute result'
}
def dump(file,obj): file.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
def now(): return datetime.now(UTC).isoformat()

def freeze_targets():
    def get(l):
        u=f'https://site.api.espn.com/apis/site/v2/sports/soccer/{LEAGUES[l]}/scoreboard?dates=20261101'
        r=requests.get(u,timeout=15);r.raise_for_status(); t=now()
        return l,(r.json()['events'],u,t)
    with ThreadPoolExecutor(4) as ex: src=dict(ex.map(get,LEAGUES))
    out=[]
    for i,(l,fid,hid,aid,hn,an,hcn,acn) in enumerate(IDS,1):
        events,url,taken=src[l]; event=next(g for g in events if g['id']==fid); comp=event['competitions'][0]
        cs={x['homeAway']:x for x in comp['competitors']}
        assert cs['home']['team']['id']==hid and cs['away']['team']['id']==aid
        assert not comp.get('neutralSite',False)
        assert event['status']['type']['state']=='pre' and not event['status']['type']['completed']
        out.append(dict(id=i,league=l,event_id=fid,home_id=hid,away_id=aid,home=hn,away=an,home_cn=hcn,away_cn=acn,kickoff_utc=event['date'],venue=comp.get('venue',{}).get('fullName'),event_status=event['status']['type']['name'],identity_source=url,identity_read_utc=taken))
    cutoff=now(); assert all(pd.Timestamp(cutoff)<pd.Timestamp(g['kickoff_utc']) for g in out)
    dump(ROOT/'targets.json',dict(cutoff_utc=cutoff,targets=out));return cutoff,out

def prepare(cutoff):
    manifest=json.loads((ROOT/'model_sources.json').read_text(encoding='utf8')); output=[]; omitted=[]
    for m in manifest['sources']:
        assert 'error' not in m
        file=ROOT/'snapshots'/m['file']; assert hashlib.sha256(file.read_bytes()).hexdigest()==m['sha256']
        assert pd.Timestamp(m['received_utc'])<pd.Timestamp(cutoff)
        with file.open(encoding=m['encoding'],newline='') as fh:
            for j,r in enumerate(csv.DictReader(fh),2):
                if not r.get('Date'): continue
                day=datetime.strptime(r['Date'],'%d/%m/%Y').date().isoformat()
                k=(m['league'],day,r['HomeTeam'],r['AwayTeam'])
                if k in EXCLUDED: omitted.append(dict(key=k,why=EXCLUDED[k]));continue
                if not r.get('Time') or not r['FTHG'].isdigit() or not r['FTAG'].isdigit():omitted.append(dict(key=k,why='score or kickoff unavailable'));continue
                at=datetime.fromisoformat(day+'T'+r['Time']).replace(tzinfo=ZoneInfo('Europe/London')).astimezone(UTC).isoformat()
                if not at < m['received_utc']:omitted.append(dict(key=k,why='time not before observation'));continue
                output.append(dict(match_id='fd:'+':'.join(k),league=m['league'],season=m['season'],source='Football-Data.co.uk',home=r['HomeTeam'],away=r['AwayTeam'],home_goals=int(r['FTHG']),away_goals=int(r['FTAG']),kickoff_utc=at,available_utc=m['received_utc'],source_file=m['file'],source_line=j))
    assert len({v['match_id'] for v in output})==len(output)
    pd.DataFrame(output).to_csv(ROOT/'training_input.csv',index=False)
    dump(ROOT/'excluded.json',omitted)

def sample_dc(lam,mu,rho,seed,n=100000):
    rng=np.random.default_rng(seed)
    tau=np.array([[1.-lam*mu*rho,1+lam*rho],[1+mu*rho,1-rho]])
    assert np.min(tau)>=0
    M=max(1.,float(np.max(tau))); blocks=[];left=n
    while left:
        batch=int(math.ceil(left*M*1.012))+12
        h=rng.poisson(lam,batch);a=rng.poisson(mu,batch)
        coeff=np.ones(batch); idx=(h<=1)&(a<=1);coeff[idx]=tau[h[idx],a[idx]]
        acc=rng.random(batch)*M<coeff; picked=np.column_stack([h[acc],a[acc]])[:left]
        blocks.append(picked);left-=len(picked)
    ha=np.concatenate(blocks);return [int(np.sum(ha[:,0]>ha[:,1])),int(np.sum(ha[:,0]==ha[:,1])),int(np.sum(ha[:,0]<ha[:,1]))],int(np.sum((ha>=10).any(axis=1)))

def run():
    if '--capture' in sys.argv:
        if (ROOT/'targets.json').exists(): raise RuntimeError('refusing to replace existing frozen targets')
        cutoff,targets=freeze_targets();prepare(cutoff)
    else:
        m=json.loads((ROOT/'targets.json').read_text(encoding='utf8'));cutoff=m['cutoff_utc'];targets=m['targets']
    data=pd.read_csv(ROOT/'training_input.csv',dtype={'season':str});data['kickoff_utc']=pd.to_datetime(data.kickoff_utc,utc=True);data['available_utc']=pd.to_datetime(data.available_utc,utc=True)
    assert data.available_utc.max()<pd.Timestamp(cutoff)
    forecasts=[]; params={}; features=[];log={}
    for l in LEAGUES:
        df=data[data.league==l].sort_values(['kickoff_utc','match_id']).copy()
        engine=DixonColes(half_life_days=90).fit(df,as_of=pd.Timestamp(cutoff)); pp=engine.engine.get_params()
        params[l]={k:float(v) for k,v in pp.items()}; params[l]['weighted_loglikelihood']=float(engine.engine.loglikelihood)
        log[l]=dict(rows=len(df),teams=len(engine.teams),first=str(df.kickoff_utc.min()),last=str(df.kickoff_utc.max()),seasons={str(k):int(v) for k,v in df.season.value_counts().items()})
        for tar in (g for g in targets if g['league']==l):
            h,a=tar['home'],tar['away']; row=dict(tar)
            g10,meta=engine.predict_with_metadata(tar); conditional10=had(g10); ref=engine.engine.predict(h,a,max_goals=36,normalize=False);lam=float(ref.home_goal_expectation);mu=float(ref.away_goal_expectation);rho=float(pp['rho'])
            adj=rho*lam*mu*math.exp(-lam-mu)
            prob=np.array([skellam.sf(0,lam,mu)+adj,skellam.pmf(0,lam,mu)-2*adj,skellam.cdf(-1,lam,mu)+adj])
            grid=ref.grid; wide=np.array([np.tril(grid,-1).sum(),np.trace(grid),np.triu(grid,1).sum()])
            assert np.max(np.abs(prob-wide))<1e-10 and np.min(prob)>=0 and abs(prob.sum()-1)<1e-11
            seed=2026110100+tar['id']; counts,tailcount=sample_dc(lam,mu,rho,seed)
            assert sum(counts)==100000
            order=np.argsort(-prob,kind='stable');assert not np.isclose(prob[order[0]],prob[order[1]],rtol=0,atol=1e-10)
            hstats={}
            for side,name in [('home',h),('away',a)]:
                sub=df[(df.home==name)|(df.away==name)].copy()
                days=(pd.Timestamp(cutoff).normalize()-sub.kickoff_utc.dt.normalize()).dt.days; w=2.**(-days.to_numpy()/90)
                hstats[side]=dict(team=name,n=len(sub),current=int((sub.season=='2627').sum()),by_season={str(k):int(v) for k,v in sub.season.value_counts().items()},weight_sum=float(w.sum()),kish_n=float(w.sum()**2/(w**2).sum()),attack=float(pp['attack_'+name]),defence=float(pp['defence_'+name]))
            features.append(dict(id=tar['id'],league=l,home=hstats['home'],away=hstats['away'],home_log_advantage=float(pp['home_advantage']),rho=rho,home_lambda=lam,away_lambda=mu))
            row.update(model_version=VERSION,status='experimental_unvalidated',cutoff_utc=cutoff,probabilities=prob.tolist(),highest='HDA'[order[0]],gap_pp=float(100*(prob[order[0]]-prob[order[1]])),conditional10=conditional10,tail10=meta['excluded_score_mass'],seed=seed,mc_n=100000,mc_counts=counts,mc_outside10=tailcount,mc_max_diff_pp=float(100*np.max(np.abs(np.array(counts)/100000-prob))),wide_check_maxabs=float(np.max(np.abs(prob-wide))),training_n=len(df),support_h=hstats['home']['n'],support_a=hstats['away']['n'])
            forecasts.append(row)
    generated=now()
    if '--capture' in sys.argv: assert all(pd.Timestamp(generated)<pd.Timestamp(f['kickoff_utc']) for f in forecasts)
    payload=dict(generated_utc=generated,cutoff_utc=cutoff,version=VERSION,forecasts=sorted(forecasts,key=lambda d:d['id']))
    if '--capture' not in sys.argv and (ROOT/'predictions.json').exists():
        old=json.loads((ROOT/'predictions.json').read_text(encoding='utf8'));assert all(np.allclose(prev['probabilities'],new['probabilities'],rtol=0,atol=1e-10) and prev['mc_counts']==new['mc_counts'] for prev,new in zip(old['forecasts'],payload['forecasts'],strict=True));print('Offline replay matches frozen probabilities and all 16 sets of counts.');return
    dump(ROOT/'predictions.json',payload);dump(ROOT/'parameters.json',params);dump(ROOT/'features.json',sorted(features,key=lambda z:z['id']));dump(ROOT/'run_log.json',dict(model=VERSION,cutoff_utc=cutoff,generated_utc=generated,environment={k:importlib.metadata.version(k) for k in ('numpy','scipy','pandas','penaltyblog')},leagues=log,model_rows=len(data)))
    print('CUT',cutoff,'GEN',generated,'ROWS',len(data))
    for z in payload['forecasts']: print(z['id'],z['home'],z['away'],[round(p*100,2) for p in z['probabilities']],round(z['gap_pp'],2),z['support_h'],z['support_a'])
if __name__=='__main__':run()
