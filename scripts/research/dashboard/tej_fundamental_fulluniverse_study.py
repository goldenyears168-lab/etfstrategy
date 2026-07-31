"""Full-universe validation of rev_yoy_3m (月營收YoY 3月均) — 139 liquid stocks.

Expands the original 90-stock study to a turnover-ranked 139-name universe
(65 original + 74 new) using TEJ clean adjusted close (close_adj) for returns
and FinMind monthly revenue for the signal (PIT-gated). Mirrors
tej_fundamental_study.py / tej_fundamental_robustness.py exactly:
  - daily cross-sectional rank, long top 20% / short bottom 20%, equal weight,
    weekly (5d) rebalance, t+1 entry (weights.shift(1)*ret), 4bps/side*turnover.
  - direction fixed by sign of IS Rank-IC (falsification-first).
  - IS/OOS 70/30 time split; permutation (shuffle signal across stocks/day, OOS Sharpe p).
  - Deflated-Sharpe (honest n_trials); decile Q1..Q10 monotonicity.
  - market-cap (median monthly revenue) tertile stratification.
  - industry-neutral residual (subtract per-day industry mean).
  - collinearity with champion fut_foreign_oi z60 market-timing pnl.

PIT: FinMind revenue `date` = 1st of publication month; announce by law <=10th.
annd = create_time when present (true announce date) else date + 12 days
(conservative; matches the adversarial-audit finding lag40 ~= true annd).
Returns use TEJ close_adj (clean; replica finmind adj_close diverges, median
daily-ret corr 0.990, so NOT used). Not investment advice.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
SCRATCH = Path("/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/452df84e-ae89-4b92-9258-802727472d3b/scratchpad")
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "tej_fundamental_fulluniverse_metrics.csv"
RNG = np.random.default_rng(42)
COST = 0.0004

# ---------- load ----------
prcd = pd.read_parquet(D/'tej_fund_full_prcd.parquet')
rev = pd.read_parquet(D/'tej_fund_full_finrev.parquet')
info = pd.read_parquet(SCRATCH/'finmind_stockinfo.parquet')
panel = pd.read_parquet(D.parent/'chip_macro'/'panel.parquet')

prcd['date'] = pd.to_datetime(prcd['date'])
panel['date'] = pd.to_datetime(panel['date'])

px = prcd.pivot(index='date', columns='stock_id', values='close_adj').sort_index()
dates = px.index
stocks = list(px.columns)
ret = px.pct_change(fill_method=None)

# industry map + names
info = info.drop_duplicates('stock_id')
ind_map = info.set_index('stock_id')['industry_category'].to_dict()

# ---------- revenue -> PIT rev_yoy signals ----------
rev = rev.copy()
rev['date'] = pd.to_datetime(rev['date'])
rev['revenue'] = pd.to_numeric(rev['revenue'], errors='coerce')
# true period (revenue month) for YoY alignment
rev['ym'] = rev['revenue_year'].astype(int)*100 + rev['revenue_month'].astype(int)
# annd: create_time if present else publication-month-1st + 12 days (conservative PIT)
ct = pd.to_datetime(rev['create_time'], errors='coerce')
rev['annd'] = ct.where(ct.notna(), rev['date'] + pd.Timedelta(days=12))

med_rev = rev.groupby('stock_id')['revenue'].median()  # size proxy (matches 90-study)

def rev_smoothed(win):
    """Per stock: monthly YoY (rev/rev_12m_ago -1), win-month rolling mean, annd-gated daily ffill."""
    mat = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, g in rev.groupby('stock_id'):
        if sid not in stocks: continue
        g = g.dropna(subset=['revenue']).sort_values('ym')
        if len(g) < 14: continue
        r = g.set_index('ym')['revenue']
        # YoY via 12-month shift on the ordered monthly series
        yoy = (r / r.shift(12) - 1.0) * 100.0
        arr = yoy.rolling(win).mean() if win > 1 else yoy
        s = pd.Series(arr.values, index=pd.to_datetime(g['annd'].values))
        s = s[~s.index.duplicated(keep='last')].sort_index().dropna()
        if len(s) == 0: continue
        mat[sid] = s.reindex(dates, method='ffill')
    return mat.astype(float)

sig1 = rev_smoothed(1)
sig3 = rev_smoothed(3)
sig6 = rev_smoothed(6)

# ---------- regime + champion ----------
pn = panel.set_index('date').reindex(dates).ffill()
ma200 = pn['ix_close'].rolling(200).mean()
bull = (pn['ix_close'] > ma200) & (ma200 > ma200.shift(20))
x = pn['fut_foreign_oi']
z60 = (x - x.rolling(60).mean()) / x.rolling(60).std()
champ_pnl = (z60 > 0).shift(1).astype(float) * pn['ix_close'].pct_change()

valid = dates[dates >= pd.Timestamp('2021-02-01')]
split = valid[int(len(valid)*0.70)]

def sharpe(s):
    s = s.dropna()
    return np.nan if s.std()==0 or len(s)<20 else s.mean()/s.std()*np.sqrt(252)

_blk = np.arange(len(dates)) // 5
_bpos = np.array([np.where(_blk==b)[0][0] for b in np.unique(_blk)])
_bdates = dates[_bpos]

def longshort_ret(sig, direction=1.0, sub=None, q=0.2, cost=COST, block=5):
    s = sig * direction
    if sub is not None:
        cols = [c for c in s.columns if c in sub]
        s = s[cols]; r_full = ret[cols]
        minn = 6
    else:
        r_full = ret; minn = 10
    r = s.rank(axis=1, pct=True)
    cnt = s.notna().sum(axis=1)
    long = (r >= (1-q)) & (cnt.values[:,None] >= minn)
    short = (r <= q) & (cnt.values[:,None] >= minn)
    nl = long.sum(axis=1).replace(0,np.nan); ns = short.sum(axis=1).replace(0,np.nan)
    w = (long.div(nl,axis=0)*0.5 - short.div(ns,axis=0)*0.5).fillna(0.0)
    blk = np.arange(len(dates)) // block
    bpos = np.array([np.where(blk==b)[0][0] for b in np.unique(blk)])
    bdates = dates[bpos]
    wb = w.reindex(bdates).reindex(dates).ffill().fillna(0.0)
    turn = (wb - wb.shift(1).fillna(0.0)).abs().sum(axis=1)
    turn = turn.where(dates.isin(bdates), 0.0)
    return (wb.shift(1) * r_full).sum(axis=1) - turn*cost

fwd5 = px.shift(-5)/px - 1

def rank_ic(sig, fwd, minn=10):
    ics=[]; idx=[]
    for d in dates:
        a=sig.loc[d]; b=fwd.loc[d]; m=a.notna()&b.notna()
        if m.sum()>=minn:
            ics.append(a[m].rank().corr(b[m].rank())); idx.append(d)
    return pd.Series(ics, index=idx)

def perm_p(sig, direction, sub=None, N=500):
    obs = sharpe(longshort_ret(sig,direction,sub)[lambda p:p.index>=split])
    oos = np.where(dates>=split)[0]; cnt=0; arr0=sig.values
    for _ in range(N):
        arr=arr0.copy()
        for r in oos:
            row=arr[r]; ix=np.where(~np.isnan(row))[0]
            if len(ix)>1: arr[r,ix]=RNG.permutation(row[ix])
        pp=longshort_ret(pd.DataFrame(arr,index=dates,columns=stocks),direction,sub)
        if sharpe(pp[pp.index>=split])>=obs: cnt+=1
    return (cnt+1)/(N+1)

def deflated_sharpe(port, n_trials):
    """Bailey-Lopez de Prado DSR (theoretical noise benchmark)."""
    from scipy.stats import norm, skew, kurtosis
    r = port[port.index>=split].dropna()
    if len(r) < 30: return np.nan, np.nan
    T = len(r); sr = r.mean()/r.std()  # per-period (daily) SR
    g3 = skew(r); g4 = kurtosis(r, fisher=False)
    emax = (1-np.euler_gamma)*norm.ppf(1-1/n_trials)+np.euler_gamma*norm.ppf(1-1/(n_trials*np.e))
    sr0 = emax/np.sqrt(T)  # benchmark SR under nulls
    denom = np.sqrt(1 - g3*sr + (g4-1)/4*sr**2)
    dsr = norm.cdf((sr - sr0)*np.sqrt(T-1)/denom)
    return dsr, (sr*np.sqrt(252))

# ============ MAIN: rev_yoy_3m headline (full universe) ============
rows=[]
print(f"universe={len(stocks)} stocks  dates {dates[0].date()}->{dates[-1].date()} ({len(dates)}d)  split={split.date()}")
print(f"signal coverage (rev_yoy_3m non-nan stock-days): {sig3.notna().sum().sum()}")

for name, sig in [('rev_yoy',sig1),('rev_yoy_3m',sig3),('rev_yoy_6m',sig6)]:
    ic = rank_ic(sig, fwd5)
    ic_is = ic[ic.index<split].mean(); ic_oos = ic[ic.index>=split].mean()
    direction = 1.0 if (ic_is if not np.isnan(ic_is) else 0)>=0 else -1.0
    port = longshort_ret(sig, direction)
    is_p=port[port.index<split]; oos_p=port[port.index>=split]
    sh_is,sh_oos=sharpe(is_p),sharpe(oos_p)
    sh_bull=sharpe(port.where(bull,0.0)); sh_bear=sharpe(port.where(~bull,0.0))
    p=perm_p(sig,direction)
    corr_c=port.corr(champ_pnl)
    dsr,sr_ann=deflated_sharpe(port, 6)
    rows.append(dict(factor=name,dir=int(direction),ic_is=round(ic_is,4),ic_oos=round(ic_oos,4),
        sharpe_is=round(sh_is,3),sharpe_oos=round(sh_oos,3),sharpe_bull=round(sh_bull,3),
        sharpe_bear=round(sh_bear,3),perm_p_oos=round(p,4),corr_champ=round(corr_c,3),
        dsr_oos=round(dsr,4),sr_ann_oos=round(sr_ann,3),
        ret_ann_oos=round(oos_p.mean()*252*100,3)))
    print(f"{name:11s} dir={int(direction):+d} IC_is={ic_is:+.4f} IC_oos={ic_oos:+.4f} "
          f"Sh_is={sh_is:+.2f} Sh_oos={sh_oos:+.2f} bull={sh_bull:+.2f} bear={sh_bear:+.2f} "
          f"p={p:.4f} corrChamp={corr_c:+.2f} DSR={dsr:.3f}")

# ---- decile monotonicity (rev_yoy_3m) ----
def decile_profile(sig, mask):
    buckets=[[] for _ in range(10)]
    for d in [d for d in dates if mask(d)]:
        a=sig.loc[d]; b=fwd5.loc[d]; m=a.notna()&b.notna()
        if m.sum()<20: continue
        rk=(a[m].rank(pct=True)*10).clip(upper=9.999).astype(int)
        for q in range(10):
            v=b[m][rk==q]
            if len(v): buckets[q].extend(v.values)
    return np.array([np.mean(x)*100 if x else np.nan for x in buckets])
pi=decile_profile(sig3,lambda d:d<split); po=decile_profile(sig3,lambda d:d>=split)
def mono(p):
    p=p[~np.isnan(p)]; return np.corrcoef(np.arange(len(p)),p)[0,1] if len(p)>2 else np.nan
print("\nDECILE fwd5% (rev_yoy_3m):")
print("  IS :"+" ".join(f"{v:+.2f}" for v in pi)+f"  mono={mono(pi):+.3f} Q10-Q1={pi[9]-pi[0]:+.2f}")
print("  OOS:"+" ".join(f"{v:+.2f}" for v in po)+f"  mono={mono(po):+.3f} Q10-Q1={po[9]-po[0]:+.2f}")
for q in range(10):
    rows.append(dict(factor=f'decile_Q{q+1}',ic_is=round(pi[q],3),ic_oos=round(po[q],3)))

# ---- market-cap (median revenue) tertiles ----
qt = med_rev.rank(pct=True)
large=set(qt[qt>=2/3].index)&set(stocks); mid=set(qt[(qt>=1/3)&(qt<2/3)].index)&set(stocks); small=set(qt[qt<1/3].index)&set(stocks)
print("\nSIZE TERTILE (rev_yoy_3m, median-revenue proxy):")
for nm,sub in [('large',large),('mid',mid),('small',small)]:
    port=longshort_ret(sig3,sub=sub)
    si,so,sa=sharpe(port[port.index<split]),sharpe(port[port.index>=split]),sharpe(port)
    p=perm_p(sig3,1.0,sub=sub)
    rows.append(dict(factor=f'size_{nm}',ic_is=len(sub),sharpe_is=round(si,3),sharpe_oos=round(so,3),
        sharpe_bull=round(sa,3),perm_p_oos=round(p,4)))
    print(f"  {nm:6s}(n={len(sub):3d}) Sh_is={si:+.2f} Sh_oos={so:+.2f} Sh_all={sa:+.2f} perm_p={p:.4f}")

# ---- industry-neutral ----
ind_vec = pd.Series({c:ind_map.get(c,'?') for c in stocks})
def ind_neutral(sig):
    out=sig.copy()
    for g in ind_vec.unique():
        cols=[c for c in stocks if ind_vec.get(c)==g]
        if len(cols)<3: continue
        out[cols]=sig[cols].sub(sig[cols].mean(axis=1),axis=0)
    return out
sig3n=ind_neutral(sig3)
port_raw=longshort_ret(sig3); port_neu=longshort_ret(sig3n)
p_neu=perm_p(sig3n,1.0)
print("\nINDUSTRY-NEUTRAL (rev_yoy_3m):")
for nm,port,pp in [('raw',port_raw,None),('neutralized',port_neu,p_neu)]:
    si,so,sa=sharpe(port[port.index<split]),sharpe(port[port.index>=split]),sharpe(port)
    rows.append(dict(factor=f'indneu_{nm}',sharpe_is=round(si,3),sharpe_oos=round(so,3),sharpe_bull=round(sa,3),
        perm_p_oos=round(pp,4) if pp else np.nan))
    print(f"  {nm:12s} Sh_is={si:+.2f} Sh_oos={so:+.2f} Sh_all={sa:+.2f}"+(f" perm_p={pp:.4f}" if pp else ""))
print(f"  n_industries with>=3 names: {sum(1 for g in ind_vec.unique() if sum(ind_vec==g)>=3)}")

# ---- yearly IC ----
ic3=rank_ic(sig3,fwd5)
print("\nYEARLY IC (rev_yoy_3m):")
for yr in range(2021,2027):
    m=ic3[ic3.index.year==yr]
    rows.append(dict(factor=f'ic_{yr}',ic_is=round(m.mean(),4) if len(m) else np.nan,ic_oos=len(m)))
    print(f"  {yr}: IC={m.mean():+.4f} (n={len(m)})" if len(m) else f"  {yr}: n/a")

# baseline
bh=ret.mean(axis=1)
print(f"\nuniverse B&H Sharpe all={sharpe(bh):+.2f} oos={sharpe(bh[bh.index>=split]):+.2f}")
pd.DataFrame(rows).to_csv(OUTCSV, index=False)
print(f"wrote {OUTCSV}")
