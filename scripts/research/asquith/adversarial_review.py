import pandas as pd, numpy as np, sqlite3
p=pd.read_parquet('data/research/dashboard/asquith/panel.parquet')
p=p[np.isfinite(p['size'])].copy()

# ---- pull volatility + turnover to add as controls (challenge 1) ----
ids=tuple(sorted(p.stock_id.unique()))
con=sqlite3.connect('data/replica/stocks.db')
bars=pd.read_sql(f"select stock_id,trade_date,adj_close,close,volume,amount from stock_daily_bars where stock_id in {ids}",con)
con.close()
bars['trade_date']=pd.to_datetime(bars.trade_date)
bars=bars.sort_values(['stock_id','trade_date'])
bars['ret']=bars.groupby('stock_id')['adj_close'].pct_change()
bars['vol20']=bars.groupby('stock_id')['ret'].transform(lambda s:s.rolling(20).std())
bars['dollar20']=bars.groupby('stock_id')['amount'].transform(lambda s:s.rolling(20).mean())
feat=bars[['stock_id','trade_date','vol20','dollar20']]

out=[]
for sid,g in p.groupby('stock_id'):
    f=feat[feat.stock_id==sid]
    if f.empty:
        gg=g.copy(); gg['vol20']=np.nan; gg['dollar20']=np.nan; out.append(gg); continue
    m=pd.merge_asof(g.sort_values('date'), f.sort_values('trade_date'),
                    left_on='date',right_on='trade_date',by='stock_id',direction='backward')
    out.append(m)
p=pd.concat(out,ignore_index=True)
p['illiq']=-np.log(p.dollar20.replace(0,np.nan))  # higher = more illiquid
print('vol20 cov',p.vol20.notna().mean().round(3),'dollar20 cov',p.dollar20.notna().mean().round(3))

def terc(s): return pd.qcut(s.rank(method='first'),3,labels=[1,2,3]).astype(int)
def neut(g,yc,xs):
    sub=g.dropna(subset=[yc]+list(xs))
    if len(sub)<10: g=g.copy(); g['_r']=np.nan; return g
    X=np.column_stack([np.ones(len(sub))]+[sub[c].values for c in xs])
    b,*_=np.linalg.lstsq(X,sub[yc].values,rcond=None)
    r=pd.Series(np.nan,index=g.index); r.loc[sub.index]=sub[yc].values-X@b
    g=g.copy(); g['_r']=r; return g

def short_leg(df,yc,ctrls):
    recs=[]
    for dt,g in df.groupby('date'):
        need=['si_total',yc,'size','mom20']+[c for c in ctrls if c not in ('size','mom20')]
        g=g.dropna(subset=need)
        if len(g)<30: continue
        g=g.copy(); g['sT']=terc(g['si_total']); g=neut(g,yc,ctrls)
        recs.append((dt,g[g.sT==3]['_r'].mean()-g[g.sT==1]['_r'].mean()))
    s=pd.DataFrame(recs,columns=['date','sp']).dropna()
    return s

def sh(s):
    x=s.sp; return x.mean()/x.std()*np.sqrt(52), x.mean()/x.std()*np.sqrt(len(x)), x.mean()*100, len(x)

print('\n=== CHALLENGE 1: is reverse short-demand leg just size/illiq/vol? ===')
for name,ctrls in [('size+mom (orig)',['size','mom20']),
                   ('+vol20',['size','mom20','vol20']),
                   ('+illiq',['size','mom20','illiq']),
                   ('+vol+illiq',['size','mom20','vol20','illiq']),
                   ('+vol+illiq+mom60',['size','mom20','mom60','vol20','illiq'])]:
    s=short_leg(p,'fwd20',ctrls)
    a,t,m,n=sh(s)
    print(f"  {name:22s} Sharpe={a:+.2f} t={t:+.2f} mean={m:+.3f}% n={n}")

print('\n=== CHALLENGE 4: does low-own tercile = large caps? cell size profile ===')
# per week assign terciles, look at mktcap by cell
rows=[]
for dt,g in p.groupby('date'):
    g=g.dropna(subset=['si_total','own_1000','fwd20','size','mom20'])
    if len(g)<30: continue
    g=g.copy(); g['sT']=terc(g['si_total']); g['oT']=terc(g['own_1000'])
    for st in(1,2,3):
        for ot in(1,2,3):
            c=g[(g.sT==st)&(g.oT==ot)]
            if len(c): rows.append(dict(date=dt,sT=st,oT=ot,n=len(c),
                mcap=c.mktcap.median(),vol=c.vol20.median(),own=c.own_1000.median()))
cs=pd.DataFrame(rows)
print('median mktcap (bn TWD) by cell:')
piv=cs.pivot_table(index='sT',columns='oT',values='mcap',aggfunc='mean')/1e9
print((piv).round(1).to_string())
print('median vol20 by cell:')
print((cs.pivot_table(index='sT',columns='oT',values='vol',aggfunc='mean')*100).round(2).to_string())
# constrained cell distinct stocks
con_ids=set()
for dt,g in p.groupby('date'):
    g=g.dropna(subset=['si_total','own_1000','fwd20','size','mom20'])
    if len(g)<30: continue
    g=g.copy(); g['sT']=terc(g['si_total']); g['oT']=terc(g['own_1000'])
    con_ids|=set(g[(g.sT==3)&(g.oT==1)].stock_id)
print(f'distinct stocks EVER in constrained cell (3,1): {len(con_ids)} of {p.stock_id.nunique()}')
print('own_1000 range overall: p10/p50/p90 =',
      np.nanpercentile(p.own_1000,[10,50,90]).round(1))

print('\n=== CHALLENGE 3: honest trial count for this whole gap-C effort ===')
# legs actually examined: shortcol{si_total,si_margin,si_lend} x own{own_1000,own_400}
#   x fwd{5,10,20} x leg{short,own,asquith,interaction} = 3*2*3*4 = 72 configs
# short-demand leg is the survivor; count only plausibly-reported family
base=short_leg(p,'fwd20',['size','mom20']).sp.values
from scipy import stats as st
def dsr(r,nt):
    r=np.asarray(r); n=len(r); sr=r.mean()/r.std()
    sk=st.skew(r); ku=st.kurtosis(r,fisher=False); e=np.euler_gamma; z=st.norm.ppf
    sr_std=np.sqrt((1-sk*sr+(ku-1)/4*sr**2)/(n-1))
    maxZ=(1-e)*z(1-1/nt)+e*z(1-1/(nt*np.e))
    sr0=sr_std*maxZ
    d=(sr-sr0)*np.sqrt(n-1)/np.sqrt(1-sk*sr+(ku-1)/4*sr**2)
    return st.norm.cdf(d),sr,sr0
for nt in [72,150,300]:
    pv,sr,sr0=dsr(base,nt)
    print(f'  DSR short_spread trials={nt}: P(SR>0)={pv:.3f} SR/wk={sr:.3f} bench={sr0:.3f}')

print('\n=== CHALLENGE 3b: strict walk-forward (expanding, 26wk step) ===')
s=short_leg(p,'fwd20',['size','mom20']).sort_values('date').reset_index(drop=True)
n=len(s); step=26; oos=[]
start=52
i=start
while i<n:
    tr=s.iloc[:i]; te=s.iloc[i:i+step]
    if len(te)<8: break
    # trade next block only if IS Sharpe>0
    if tr.sp.mean()>0: oos.append(te.sp)
    i+=step
if oos:
    oo=pd.concat(oos)
    print(f'  walk-forward OOS (trade when IS mean>0): Sharpe={oo.mean()/oo.std()*np.sqrt(52):+.2f} n={len(oo)} mean={oo.mean()*100:+.3f}%')
# fraction of 26wk blocks positive
blocks=[s.sp.iloc[j:j+step].mean() for j in range(0,n-step,step)]
print(f'  26wk blocks positive: {np.mean(np.array(blocks)>0):.0%} of {len(blocks)}')
