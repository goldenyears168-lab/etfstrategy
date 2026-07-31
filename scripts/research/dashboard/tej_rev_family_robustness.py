"""Compact robustness addendum for the two candidate L1 revenue second-legs
(mom, surprise) that passed collinearity+residual-IC vs rev_yoy_3m.

One-shot (NOT a grid search) stress: liquidity subset (top-150 by median $vol),
monthly (21d) rebalance, higher cost (15bps), and the RESIDUALIZED-vs-rev_yoy_3m
L/S under each. Guards against microcap / turnover / restatement artifacts.
Reuses the same PIT machinery as tej_rev_family_secondleg_study.py.
"""
from __future__ import annotations
import sqlite3, json
from pathlib import Path
import numpy as np, pandas as pd

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "tej_rev_family_robustness.csv"

pb = pd.read_parquet(D/'tej_revfam_pb.parquet')
sale = pd.read_parquet(D/'tej_revfam_sale.parquet')
pb['date']=pd.to_datetime(pb['date'],errors='coerce'); pb=pb.dropna(subset=['date'])
pb['close_adj']=pd.to_numeric(pb['close_adj'],errors='coerce')
sale['annd']=pd.to_datetime(sale['annd'],errors='coerce')
sale['period']=pd.to_datetime(sale['period'],errors='coerce')
sale['rev']=pd.to_numeric(sale['rev'],errors='coerce'); sale['rev_yoy']=pd.to_numeric(sale['rev_yoy'],errors='coerce')
sale=sale.dropna(subset=['annd','period']).sort_values(['stock_id','period'])

px=pb.pivot(index='date',columns='stock_id',values='close_adj').sort_index()
dates=px.index; stocks=list(px.columns); ret=px.pct_change(fill_method=None)
fwd5=px.shift(-5)/px-1

# liquidity: top-150 by median dollar-volume from local db (amount)
c=sqlite3.connect(str(ROOT/'data'/'stocks.db'))
q='select stock_id,avg(amount) av from stock_daily_bars where trade_date>="2024-01-01" group by stock_id'
liq=pd.read_sql(q,c).set_index('stock_id')['av']
top150=set(liq.reindex(stocks).dropna().sort_values(ascending=False).head(150).index)

def daily_ffill(ps):
    m=pd.DataFrame(index=dates,columns=stocks,dtype=float)
    for sid,s in ps.items():
        if sid not in stocks or s is None or len(s)==0: continue
        s=s[~s.index.duplicated(keep='last')].sort_index(); m[sid]=s.reindex(dates,method='ffill')
    return m.astype(float)

revyoy3m={}; mom={}; surprise={}
for sid,g in sale.groupby('stock_id'):
    if sid not in stocks: continue
    g=g.sort_values('period'); annd=pd.to_datetime(g['annd'].values)
    rev=g['rev'].astype(float).values; yoy=g['rev_yoy'].astype(float).values; n=len(g)
    revyoy3m[sid]=pd.Series(pd.Series(yoy).rolling(3).mean().values,index=annd)
    with np.errstate(divide='ignore',invalid='ignore'): mm=rev[1:]/rev[:-1]-1.0
    mm=np.concatenate([[np.nan],mm]); mm[~np.isfinite(mm)]=np.nan
    mom[sid]=pd.Series(mm,index=annd)
    lrev=np.log(np.where(rev>0,rev,np.nan)); sur=np.full(n,np.nan)
    for i in range(n):
        if i-12<0: continue
        yv=lrev[i-12:i]; mf=np.isfinite(yv)
        if mf.sum()<10: continue
        b=np.polyfit(np.arange(len(yv))[mf],yv[mf],1); pred=np.polyval(b,len(yv))
        sd=(yv[mf]-np.polyval(b,np.arange(len(yv))[mf])).std(ddof=1)
        if np.isfinite(lrev[i]) and sd>0: sur[i]=(lrev[i]-pred)/sd
    surprise[sid]=pd.Series(sur,index=annd)
REF=daily_ffill(revyoy3m); MOM=daily_ffill(mom); SUR=daily_ffill(surprise)

valid=dates[dates>=pd.Timestamp('2021-03-01')]; split=valid[int(len(valid)*0.70)]
def sharpe(x):
    x=x.dropna(); return np.nan if len(x)<20 or x.std()==0 else x.mean()/x.std()*np.sqrt(252)

def ls(sig,direction=1.0,block=5,cost=0.0004,sub=None,q=0.2):
    s=sig*direction
    if sub is not None: s=s[[c for c in s.columns if c in sub]]; rr=ret[[c for c in ret.columns if c in sub]]
    else: rr=ret
    r=s.rank(axis=1,pct=True); cnt=s.notna().sum(axis=1)
    minn=max(6,int(0.20*max(cnt.max(),1))) if sub is not None else 20
    long=(r>=1-q)&(cnt.values[:,None]>=minn); short=(r<=q)&(cnt.values[:,None]>=minn)
    nl=long.sum(axis=1).replace(0,np.nan); ns=short.sum(axis=1).replace(0,np.nan)
    w=(long.div(nl,axis=0)*0.5-short.div(ns,axis=0)*0.5).fillna(0.0)
    blk=np.arange(len(dates))//block; bs=dates[np.array([np.where(blk==b)[0][0] for b in np.unique(blk)])]
    wb=w.reindex(bs).reindex(dates).ffill().fillna(0.0)
    to=(wb-wb.shift(1).fillna(0.0)).abs().sum(axis=1).where(dates.isin(bs),0.0)
    return (wb.shift(1)*rr).sum(axis=1)-to*cost

def residualize(sig):
    out=pd.DataFrame(index=dates,columns=stocks,dtype=float)
    for d in dates:
        a=sig.loc[d]; b=REF.loc[d]; m=a.notna()&b.notna()
        if m.sum()<20: continue
        ar=a[m].rank(); br=b[m].rank(); beta=np.cov(ar,br)[0,1]/np.var(br) if np.var(br)>0 else 0
        out.loc[d,ar.index]=(ar-(ar.mean()+beta*(br-br.mean()))).values
    return out

def oos(p): return round(sharpe(p[p.index<split]),3),round(sharpe(p[p.index>=split]),3)

rows=[]
for nm,sig in [('mom',MOM),('surprise',SUR)]:
    resid=residualize(sig)
    for tag,kw in [('base_weekly4bps',dict()),('top150liq',dict(sub=top150)),
                   ('monthly21d',dict(block=21)),('cost15bps',dict(cost=0.0015))]:
        si,so=oos(ls(sig,**kw))
        rows.append(dict(form=nm,test=tag,sharpe_is=si,sharpe_oos=so))
        print(f"{nm:9s} {tag:16s} Sh_is={si:+.2f} Sh_oos={so:+.2f}",flush=True)
    # residualized-vs-rev_yoy3m under same stresses
    for tag,kw in [('resid_weekly4bps',dict()),('resid_top150',dict(sub=top150)),
                   ('resid_monthly21d',dict(block=21)),('resid_cost15bps',dict(cost=0.0015))]:
        si,so=oos(ls(resid,**kw))
        rows.append(dict(form=nm,test=tag,sharpe_is=si,sharpe_oos=so))
        print(f"{nm:9s} {tag:16s} Sh_is={si:+.2f} Sh_oos={so:+.2f}",flush=True)
    print(flush=True)
pd.DataFrame(rows).to_csv(OUTCSV,index=False)
print(f"top150 liq universe n={len(top150 & set(stocks))}; split IS<{split.date()}<=OOS")
print(f"wrote {OUTCSV}")
