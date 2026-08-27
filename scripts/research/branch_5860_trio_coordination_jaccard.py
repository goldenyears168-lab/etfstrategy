import pandas as pd, numpy as np, itertools, random
rng = np.random.default_rng(7)
d = pd.read_pickle('/tmp/yy/pool_daily.pkl')
meta = pd.read_pickle('/tmp/yy/pool_meta.pkl')
meta['brand'] = meta.name.str.replace(r'[-－]','',regex=True).str[:2]
name = meta.name.to_dict(); brand = meta.brand.to_dict()
TRIO=['5860','5861','5862']
d['vol']=d.buy+d.sell
# sets per (tid, date)
S = d.groupby(['tid','trade_date']).stock_id.apply(frozenset)
DAYS = sorted(d.trade_date.unique())
tids = sorted(d.tid.unique())
sets = {t: S.loc[t].to_dict() for t in tids if t in S.index.get_level_values(0)}
print('branches', len(sets), 'days', len(DAYS))

def jac(a,b):
    u=len(a|b)
    return len(a&b)/u if u else np.nan

def pair_stats(A,B,nperm=30):
    sa,sb = sets[A], sets[B]
    common = sorted(set(sa)&set(sb))
    if len(common)<100: return None
    obs=[jac(sa[x],sb[x]) for x in common]
    # date-shuffle null: pair A_d with B_d' (d' random other common day)
    nulls=[]
    for _ in range(nperm):
        perm=list(common); rng.shuffle(perm)
        nulls.append(np.mean([jac(sa[x],sb[y]) for x,y in zip(common,perm) if x!=y]))
    o=float(np.mean(obs)); nm=float(np.mean(nulls)); ns=float(np.std(nulls))
    return dict(A=A,B=B,ndays=len(common),
                setA=float(np.mean([len(sa[x]) for x in common])),
                setB=float(np.mean([len(sb[x]) for x in common])),
                jac=o, jac_null=nm, lift=o/nm if nm>0 else np.nan,
                z=(o-nm)/ns if ns>0 else np.nan)

rows=[]
for A,B in itertools.combinations(tids,2):
    r=pair_stats(A,B)
    if r: rows.append(r)
R=pd.DataFrame(rows)
R['same_brand']=[brand.get(a)==brand.get(b) for a,b in zip(R.A,R.B)]
R['is_yy']=[a in TRIO and b in TRIO for a,b in zip(R.A,R.B)]
R['lab']=[f'{name.get(a)}|{name.get(b)}' for a,b in zip(R.A,R.B)]
R.to_pickle('/tmp/yy/pairs.pkl')
pd.set_option('display.width',250)
print('\n=== (a) 同日標的重疊 Jaccard ===')
print('全部配對 n=%d'%len(R))
print('盈溢三對：'); print(R[R.is_yy][['lab','ndays','setA','setB','jac','jac_null','lift','z']].to_string(index=False))
print('\n同券商他組（%d 對）：'%((R.same_brand&~R.is_yy).sum()))
print(R[R.same_brand&~R.is_yy].nlargest(12,'lift')[['lab','ndays','setA','setB','jac','jac_null','lift','z']].to_string(index=False))
print('\n分布比較（lift）：')
for lab,sub in [('盈溢三對',R[R.is_yy]),('同券商其他',R[R.same_brand&~R.is_yy]),('跨券商隨機',R[~R.same_brand])]:
    print(f'  {lab:<10} n={len(sub):>4}  lift med={sub.lift.median():.3f}  p90={sub.lift.quantile(.9):.3f}  max={sub.lift.max():.3f}  jac med={sub.jac.median():.4f}')
print('\n盈溢三對的 lift 在「跨券商隨機」分布中的百分位：')
base=R[~R.same_brand].lift
for _,r in R[R.is_yy].iterrows():
    print(f'  {r.lab:<20} lift={r.lift:.3f}  pctile={(base<r.lift).mean()*100:5.1f}%   z={r.z:.2f}')
