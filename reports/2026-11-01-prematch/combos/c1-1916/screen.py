"""Rebuild both 990-ticket screens with Python 3, standard library only.

Usage: python screen.py [directory containing frozen_inputs.json and market_had.json]
All odds come from a single official HAD response. Do not interpret scores as bets.
"""
import csv
import hashlib
import itertools
import json
import sys
from decimal import Decimal, localcontext
from fractions import Fraction as F
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
O = 'HDA'
COST = 400  # cents, two equal basic bets of RMB 2
CAP = 20_000_000  # cents, cap per winning 2x1 basic bet


def money(cents):
    return format(Decimal(cents)/100, '.2f')


def rate(x):
    with localcontext() as ctx:
        ctx.prec = 36
        return format(Decimal(x.numerator)/Decimal(x.denominator), '.18f')


def settle(a, b):
    # a,b are exact quoted odds in hundredths. Examine only decimal place 3,
    # even when place 4 is nonzero, as in the official calculator rundFunc.
    unrounded = 2*a*b
    cents, digit3 = unrounded//100, (unrounded//10)%10
    return min(CAP, cents + int(digit3 > 5 or (digit3 == 5 and cents % 2 == 1)))


def unit_string(a, b, stake=1):
    n = stake*a*b
    return f'{n//10000}.{n%10000:04d}'


def pmarket(sp):
    inv = [F(1, v) for v in sp]
    return tuple(x/sum(inv) for x in inv)


def inputs():
    frozen = json.loads((ROOT/'frozen_inputs.json').read_text(encoding='utf-8'))
    market = json.loads((ROOT/'market_had.json').read_text(encoding='utf-8-sig'))
    source = {x['id']:x for x in frozen['matches']}
    allup = {}
    assert market['poolCode'] == 'HAD'
    for item in market['matches']:
        i = item['id']
        line = source[i]
        assert str(line['event_id']) == str(item['espnId'])
        assert item['marketKey']=='HAD' and item['handicapGoalLine']=='' and item['fixedOddsType']=='F'
        assert item['poolStatus']=='Selling' and item['matchStatus']=='Selling' and item['allUp']
        sc = tuple(int(Decimal(v)*100) for v in item['sp'])
        ps = tuple(F(v) for v in line['probabilities'])
        assert len(ps)==len(sc)==3 and abs(sum(ps)-1)<F(1,10**12) and min(ps)>0 and max(ps)<1
        allup[i] = (sc, ps, pmarket(sc))
    assert len(allup)==len(source)==11
    return allup


def enumerate_pool(M, level):
    candidates=[]
    seen=set()
    for a,b in itertools.permutations(sorted(M),2):
        sa, pa = M[a][0],M[a][level]
        sb, pb = M[b][0],M[b][level]
        for x in range(3):
            if sa[x]<120:
                continue
            for y,z in itertools.combinations(range(3),2):
                base=tuple(sorted((tuple(sorted(((a,x),(b,j)))) for j in (y,z))))
                if base in seen:
                    continue
                seen.add(base)
                q1, q2 = pa[x]*pb[y], pa[x]*pb[z]
                cov = q1+q2
                g1,g2 = settle(sa[x], sb[y]),settle(sa[x], sb[z])
                profit = q1*(g1>COST)+q2*(g2>COST)
                ev = q1*g1+q2*g2-COST
                miss = 1-cov
                assert 0<=profit<=cov<=1 and miss>=0
                assert ev == q1*(g1-COST)+q2*(g2-COST)+miss*(-COST)
                key=(a,x,b,y,z)
                candidates.append(dict(key=key,ref=f'{a}{O[x]}-{b}{O[y]}{O[z]}', single=a, single_out=O[x], double=b,double_out=O[y]+O[z],
                    sp_a=money(sa[x]),sp_b1=money(sb[y]),sp_b2=money(sb[z]),p_a=pa[x],p_b1=pb[y],p_b2=pb[z],excluded_out=O[3-y-z],p_excluded=pb[3-y-z],
                    q1=q1,q2=q2,q_miss=miss,coverage=cov,profit_pretax=profit,nonprofit_hit=cov-profit,break_even=q1*(g1==COST)+q2*(g2==COST),
                    o1=unit_string(sa[x],sb[y]),o2=unit_string(sa[x],sb[z]),unrounded1_cny=unit_string(sa[x],sb[y],2),unrounded2_cny=unit_string(sa[x],sb[z],2),
                    g1_cny=money(g1),g2_cny=money(g2),r1_cny=money(g1-COST),r2_cny=money(g2-COST),r0_cny='-4.00',stake_cny='4.00',
                    ev_cny=ev/100,roi=ev/COST))
    assert len(seen)==len(candidates)==990
    profit_order=sorted(candidates,key=lambda r:(-r['profit_pretax'],r['key']))
    for rank,r in enumerate(profit_order,1):
        if rank==1 or r['profit_pretax']!=profit_order[rank-2]['profit_pretax']:
            profit_rank=rank
        r['profit_rank']=profit_rank
    ranked=sorted(candidates,key=lambda r:(-r['coverage'],r['key']))
    for rank,r in enumerate(ranked,1):
        if rank==1 or r['coverage']!=ranked[rank-2]['coverage']:
            coverage_rank=rank
        r['coverage_rank']=coverage_rank
        r['coverage_order']=rank
    return ranked


def serial(r):
    return {k:rate(v) if isinstance(v,F) else v for k,v in r.items() if k!='key'}


def main():
    assert settle(136,198)==538 and settle(125,125)==312 and settle(175,175)==612 and settle(130,135)==351
    assert settle(125,131)==328 and settle(100,200)==400
    pool = inputs()
    short = {}
    for level, label in [(1,'P2'),(2,'P3')]:
        ranked=enumerate_pool(pool,level)
        view=[serial(r) for r in ranked]
        with (ROOT/f'candidates_{label}.csv').open('w',encoding='utf-8',newline='') as f:
            fields=['coverage_order','coverage_rank','profit_rank']+[k for k in view[0] if k not in ('coverage_order','coverage_rank','profit_rank')]
            w=csv.DictWriter(f,fieldnames=fields,lineterminator='\n');w.writeheader();w.writerows(view)
        disjoint=[]
        used=set()
        for r in view:
            if not used.intersection([r['single'],r['double']]):
                disjoint.append(r);used.update([r['single'],r['double']])
                if len(disjoint)==3:break
        short[label]=dict(n=len(ranked),coverage_leaders=[r for r in view if r['coverage_rank']==1],
            profit_leaders=[r for r in view if r['profit_rank']==1],global_top3=view[:3],three_without_shared_matches=disjoint,
            count_with_covered_nonprofitable_branch=sum(r['nonprofit_hit']>0 for r in ranked),
            count_with_exact_break_even=sum(r['break_even']>0 for r in ranked),
            csv_sha256=hashlib.sha256((ROOT/f'candidates_{label}.csv').read_bytes()).hexdigest())
    (ROOT/'ranking.json').write_text(json.dumps(short,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for k,v in short.items():
        print(k,'global top three, then disjoint comparison',[(x['ref'],x['coverage_rank'],x['profit_rank'],x['coverage'][:8],x['profit_pretax'][:8],x['r1_cny'],x['r2_cny']) for x in v['global_top3']+v['three_without_shared_matches']])
        print('profit leaders',[(x['ref'],x['coverage_rank'],x['profit_pretax'][:12]) for x in v['profit_leaders']])


if __name__=='__main__':
    main()
