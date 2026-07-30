"""Robustness scan for rev_yoy_3m fundamental factor (anti-overfit hardening).

One-shot (NOT a grid search) battery of stress tests on the surviving factor
rev_yoy_3m (and rev_yoy as reference), mirroring tej_fundamental_study.py:
  (1) universe stratification   large / mid / small tertile by median revenue
  (2) lookback variants         YoY 1m / 3m / 6m smoothing
  (3) industry neutralization   subtract per-day industry mean of the signal
  (4) cost sensitivity          4 / 8 / 15 bps per side
  (5) rebalance frequency       weekly (5d) vs monthly (21d)
  (6) decile monotonicity       Q1..Q10 forward-5d return, monotone check
  (7) sub-period stability      yearly Rank-IC

PIT identical to base study (annd-gated ffill, adjusted close returns, t+1 entry).
Reports which stress each survives vs breaks. Not investment advice.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "tej_fundamental_robustness.csv"
RNG = np.random.default_rng(42)

pb = pd.read_parquet(D/'tej_fundamental_pb.parquet')
sale = pd.read_parquet(D/'tej_fundamental_sale.parquet')
ind = pd.read_parquet(D/'tej_fundamental_industry.parquet')
pb['date'] = pd.to_datetime(pb['date'])
sale['annd'] = pd.to_datetime(sale['annd']); sale['period']=pd.to_datetime(sale['period'])

px = pb.pivot(index='date', columns='stock_id', values='close_adj').sort_index()
dates = px.index
stocks = list(px.columns)
ret = px.pct_change()

ind_map = ind.set_index('stock_id')['industry_category'].to_dict()
med_rev = sale.groupby('stock_id')['rev'].median()

# ---- build rev_yoy smoothed variants (1m/3m/6m) via annd-gated daily ffill ----
def rev_smoothed(win):
    mat = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, g in sale.groupby('stock_id'):
        if sid not in stocks: continue
        g = g.dropna(subset=['rev_yoy']).sort_values('annd')
        if len(g)==0: continue
        yoy = pd.Series(g['rev_yoy'].astype(float).values)
        arr = yoy.rolling(win).mean().values if win>1 else yoy.values
        s = pd.Series(arr, index=pd.to_datetime(g['annd'].values))
        s = s[~s.index.duplicated(keep='last')].sort_index()
        mat[sid] = s.reindex(dates, method='ffill')
    return mat.astype(float)

sig1 = rev_smoothed(1)   # rev_yoy (1m)
sig3 = rev_smoothed(3)   # rev_yoy_3m (champion factor)
sig6 = rev_smoothed(6)   # 6m smooth

# ---- IS/OOS split identical to base ----
valid = dates[dates >= pd.Timestamp('2021-02-01')]
split = valid[int(len(valid)*0.70)]

def sharpe(x):
    x = x.dropna()
    return np.nan if x.std()==0 or len(x)<20 else x.mean()/x.std()*np.sqrt(252)

def longshort_ret(sig, direction=1.0, block=5, cost=0.0004, sub=None, q=0.2):
    s = sig * direction
    if sub is not None:
        s = s[[c for c in s.columns if c in sub]]
        r_full = ret[[c for c in ret.columns if c in sub]]
    else:
        r_full = ret
    r = s.rank(axis=1, pct=True)
    cnt = s.notna().sum(axis=1)
    minn = max(6, int(0.25*max(cnt.max(),1))) if sub is not None else 10
    long = (r >= (1-q)) & (cnt.values[:,None] >= minn)
    short = (r <= q) & (cnt.values[:,None] >= minn)
    nl = long.sum(axis=1).replace(0,np.nan); ns = short.sum(axis=1).replace(0,np.nan)
    w = long.div(nl,axis=0)*0.5 - short.div(ns,axis=0)*0.5
    w = w.fillna(0.0)
    blk = np.arange(len(dates)) // block
    bstart_pos = np.array([np.where(blk==b)[0][0] for b in np.unique(blk)])
    bstart_dates = dates[bstart_pos]
    wb = w.reindex(bstart_dates).reindex(dates).ffill().fillna(0.0)
    turnover = (wb - wb.shift(1).fillna(0.0)).abs().sum(axis=1)
    turnover = turnover.where(dates.isin(bstart_dates), 0.0)
    port = (wb.shift(1) * r_full).sum(axis=1) - turnover*cost
    return port

def ic_series(sig, fwd):
    ics=[]; idx=[]
    for d in dates:
        a=sig.loc[d]; b=fwd.loc[d]
        m=a.notna()&b.notna()
        if m.sum()>=10:
            ics.append(a[m].rank().corr(b[m].rank())); idx.append(d)
    return pd.Series(ics, index=idx)

fwd5 = px.shift(-5)/px - 1

def perm_p_oos(sig, direction, block=5, cost=0.0004, sub=None, N=500):
    obs = sharpe(longshort_ret(sig,direction,block,cost,sub)[lambda p: p.index>=split])
    oos_mask=(dates>=split); cnt=0
    base = sig.copy(); arr0=base.values
    for _ in range(N):
        arr=arr0.copy()
        for r in np.where(oos_mask)[0]:
            row=arr[r]; ix=np.where(~np.isnan(row))[0]
            if len(ix)>1: arr[r,ix]=RNG.permutation(row[ix])
        pp=longshort_ret(pd.DataFrame(arr,index=dates,columns=stocks),direction,block,cost,sub)
        if sharpe(pp[pp.index>=split])>=obs: cnt+=1
    return (cnt+1)/(N+1)

def oos_is(port):
    return round(sharpe(port[port.index<split]),3), round(sharpe(port[port.index>=split]),3), round(sharpe(port),3)

rows=[]
def rec(test,variant,port,extra=''):
    si,so,sa=oos_is(port)
    rows.append(dict(test=test,variant=variant,sharpe_is=si,sharpe_oos=so,sharpe_all=sa,note=extra))
    print(f"[{test:12s}] {variant:22s} Sh_is={si:+.2f} Sh_oos={so:+.2f} Sh_all={sa:+.2f} {extra}")

# baseline reference (rev_yoy_3m, weekly, 4bps, full universe)
print("=== BASELINE ===")
rec('baseline','rev_yoy_3m weekly 4bps', longshort_ret(sig3))
rec('baseline','rev_yoy(1m) weekly 4bps', longshort_ret(sig1))

# (2) lookback variants
print("\n=== (2) LOOKBACK ===")
for nm,sg in [('yoy_1m',sig1),('yoy_3m',sig3),('yoy_6m',sig6)]:
    rec('lookback',nm, longshort_ret(sg))

# (1) universe stratification by median revenue tertile
print("\n=== (1) UNIVERSE (rev_yoy_3m) ===")
qt = med_rev.rank(pct=True)
large=set(qt[qt>=2/3].index); mid=set(qt[(qt>=1/3)&(qt<2/3)].index); small=set(qt[qt<1/3].index)
for nm,sub in [('large_tertile',large),('mid_tertile',mid),('small_tertile',small)]:
    rec('universe',f'{nm}(n={len(sub)})', longshort_ret(sig3,sub=sub))

# (3) industry neutralization (subtract per-day industry mean)
print("\n=== (3) INDUSTRY-NEUTRAL (rev_yoy_3m) ===")
ind_vec = pd.Series({c:ind_map.get(c,'?') for c in stocks})
def industry_neutralize(sig):
    out = sig.copy()
    for g in ind_vec.unique():
        cols=[c for c in stocks if ind_vec.get(c)==g]
        if len(cols)<3: continue
        out[cols] = sig[cols].sub(sig[cols].mean(axis=1),axis=0)
    return out
sig3_neu = industry_neutralize(sig3)
rec('industry','raw', longshort_ret(sig3))
rec('industry','neutralized', longshort_ret(sig3_neu))

# (4) cost sensitivity
print("\n=== (4) COST (rev_yoy_3m weekly) ===")
for c in [0.0004,0.0008,0.0015]:
    rec('cost',f'{int(c*1e4)}bps', longshort_ret(sig3,cost=c))

# (5) rebalance frequency
print("\n=== (5) REBALANCE (rev_yoy_3m 4bps) ===")
rec('rebalance','weekly_5d', longshort_ret(sig3,block=5))
rec('rebalance','monthly_21d', longshort_ret(sig3,block=21))

# permutation p (OOS) for key survivors
print("\n=== PERMUTATION (OOS Sharpe, N=500) ===")
perm_rows=[]
for nm,sg,bl,cs,sub in [
    ('yoy_3m_weekly4', sig3,5,0.0004,None),
    ('yoy_3m_monthly4', sig3,21,0.0004,None),
    ('yoy_3m_15bps', sig3,5,0.0015,None),
    ('yoy_3m_indneu', sig3_neu,5,0.0004,None),
    ('yoy_3m_small', sig3,5,0.0004,small),
]:
    p=perm_p_oos(sg,1.0,bl,cs,sub)
    perm_rows.append(dict(test='perm_p',variant=nm,perm_p_oos=round(p,4)))
    print(f"  {nm:20s} perm_p_oos={p:.4f}")

# (6) decile monotonicity (rev_yoy_3m): mean fwd5 by decile, IS+OOS
print("\n=== (6) DECILE MONOTONICITY (rev_yoy_3m, fwd5d %) ===")
def decile_profile(sig, mask):
    prof=np.full(10,np.nan); buckets=[[] for _ in range(10)]
    dd=[d for d in dates if mask(d)]
    for d in dd:
        a=sig.loc[d]; b=fwd5.loc[d]; m=a.notna()&b.notna()
        if m.sum()<20: continue
        rk=(a[m].rank(pct=True)*10).clip(upper=9.999).astype(int)
        for q in range(10):
            vals=b[m][rk==q]
            if len(vals): buckets[q].extend(vals.values)
    for q in range(10):
        if buckets[q]: prof[q]=np.mean(buckets[q])*100
    return prof
prof_is=decile_profile(sig3, lambda d:d<split)
prof_oos=decile_profile(sig3, lambda d:d>=split)
def mono_score(p):
    p=p[~np.isnan(p)]
    return np.corrcoef(np.arange(len(p)),p)[0,1] if len(p)>2 else np.nan
print("  decile:   "+" ".join(f"Q{i+1}" for i in range(10)))
print("  IS  fwd5%:"+" ".join(f"{v:+.2f}" for v in prof_is))
print("  OOS fwd5%:"+" ".join(f"{v:+.2f}" for v in prof_oos))
print(f"  monotone rank-corr IS={mono_score(prof_is):+.3f} OOS={mono_score(prof_oos):+.3f}"
      f"  spread(Q10-Q1) IS={prof_is[9]-prof_is[0]:+.2f} OOS={prof_oos[9]-prof_oos[0]:+.2f}")
for q in range(10):
    rows.append(dict(test='decile',variant=f'Q{q+1}',sharpe_is=round(prof_is[q],3),
                     sharpe_oos=round(prof_oos[q],3),sharpe_all=np.nan,
                     note='fwd5d_mean_pct_by_decile'))

# (7) sub-period yearly IC (rev_yoy_3m and rev_yoy)
print("\n=== (7) YEARLY IC ===")
ic3=ic_series(sig3,fwd5); ic1=ic_series(sig1,fwd5)
yr_rows=[]
for yr in range(2021,2027):
    m3=ic3[ic3.index.year==yr]; m1=ic1[ic1.index.year==yr]
    r3=m3.mean() if len(m3) else np.nan; r1=m1.mean() if len(m1) else np.nan
    n=len(m3)
    yr_rows.append(dict(test='yearly_ic',variant=str(yr),ic_yoy1m=round(r1,4),ic_yoy3m=round(r3,4),n_days=n))
    print(f"  {yr}: IC_yoy3m={r3:+.4f} IC_yoy1m={r1:+.4f} (n={n} days)")
pos_years=sum(1 for r in yr_rows if not np.isnan(r['ic_yoy3m']) and r['ic_yoy3m']>0)
print(f"  yoy_3m IC>0 in {pos_years}/{len([r for r in yr_rows if not np.isnan(r['ic_yoy3m'])])} years")

# ---- write ----
out = pd.concat([pd.DataFrame(rows), pd.DataFrame(perm_rows), pd.DataFrame(yr_rows)], ignore_index=True)
out.to_csv(OUTCSV, index=False)
print(f"\nsplit IS<{split.date()}<=OOS | {len(dates)} days {dates[0].date()}->{dates[-1].date()}")
print(f"wrote {OUTCSV}")
