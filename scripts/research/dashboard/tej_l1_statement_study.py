"""L1 financial-statement (non-revenue) factor study — TEJ EWIFINQ quarterly.

Falsification-first, mirrors tej_fundamental_study.py methodology exactly.
Factors (PIT via a0003 財報發布日, per-stock QoQ/YoY diff of single-quarter figures,
daily annd-gated ffill):
  gm_qoq   營業毛利率 QoQ change   (毛利率趨勢, 非水位)
  opm_qoq  營業利益率 QoQ change   (營業利益率趨勢)
  roe_qoq  ROE(A)稅後 QoQ change   (ROE趨勢, 非水位)
  eps_yoy  單季EPS YoY diff        (EPS動能, 季節性乾淨)
Seasonality-clean robustness variants (margins/roe are seasonal QoQ):
  gm_yoy opm_yoy roe_yoy eps_qoq

Per factor: IS/OOS 70/30, Rank-IC direction, weekly 20% L/S t+1 4bps,
permutation OOS (same-exposure shuffle, N=500), Deflated-Sharpe (honest n_trials
= all 8 variants, cross-trial variance), collinearity vs champion(fut_foreign_oi)
AND vs rev_yoy_3m (must prove it is not a revenue-momentum restatement).
Universe = same 90 liquid stocks as rev_yoy study (price PIT already fetched).
Not investment advice.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "tej_l1_statement_metrics.csv"
RNG = np.random.default_rng(42)
COST = 0.0004
GAMMA = 0.5772156649  # Euler-Mascheroni

# ---------- load ----------
pb   = pd.read_parquet(D/'tej_fundamental_pb.parquet')      # daily close_adj (90 stk)
stmt = pd.read_parquet(D/'tej_l1_statement.parquet')        # quarterly gm/opm/roe/eps + annd
sale = pd.read_parquet(D/'tej_fundamental_sale.parquet')    # monthly rev_yoy (for collinearity)
panel= pd.read_parquet(D.parent/'chip_macro'/'panel.parquet')
pb['date']=pd.to_datetime(pb['date'])
for c in ['period','annd']: stmt[c]=pd.to_datetime(stmt[c])
sale['annd']=pd.to_datetime(sale['annd'])
panel['date']=pd.to_datetime(panel['date'])

px = pb.pivot(index='date', columns='stock_id', values='close_adj').sort_index()
dates = px.index; stocks=list(px.columns)
ret = px.pct_change()

# ---------- build PIT daily factor matrices from per-stock quarterly diffs ----------
def stmt_factor(valcol, lag):
    """per-stock: diff of single-quarter figure vs `lag` quarters back, gated to annd, daily ffill."""
    mat = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid,g in stmt.groupby('stock_id'):
        if sid not in stocks: continue
        g=g.dropna(subset=[valcol]).sort_values('period')
        if len(g)<=lag: continue
        v=g[valcol].astype(float).reset_index(drop=True)
        diff=(v - v.shift(lag)).values
        s=pd.Series(diff, index=pd.to_datetime(g['annd'].values))
        s=s[~s.index.duplicated(keep='last')].sort_index()
        mat[sid]=s.reindex(dates, method='ffill')
    return mat.astype(float)

FACTORS = {
    'gm_qoq':  stmt_factor('gm',1),  'opm_qoq': stmt_factor('opm',1),
    'roe_qoq': stmt_factor('roe',1), 'eps_yoy': stmt_factor('eps',4),
    'gm_yoy':  stmt_factor('gm',4),  'opm_yoy': stmt_factor('opm',4),
    'roe_yoy': stmt_factor('roe',4), 'eps_qoq': stmt_factor('eps',1),
}
PRIMARY = ['gm_qoq','opm_qoq','roe_qoq','eps_yoy']

# rev_yoy_3m matrix (collinearity reference)
def rev_yoy_3m_mat():
    mat=pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid,g in sale.groupby('stock_id'):
        if sid not in stocks: continue
        g=g.dropna(subset=['rev_yoy']).sort_values('annd')
        if len(g)==0: continue
        arr=pd.Series(g['rev_yoy'].astype(float).values).rolling(3).mean().values
        s=pd.Series(arr,index=pd.to_datetime(g['annd'].values))
        s=s[~s.index.duplicated(keep='last')].sort_index()
        mat[sid]=s.reindex(dates, method='ffill')
    return mat.astype(float)
rev3 = rev_yoy_3m_mat()

# ---------- regime + champion ----------
pn=panel.set_index('date').reindex(dates).ffill()
x=pn['fut_foreign_oi']; z60=(x-x.rolling(60).mean())/x.rolling(60).std()
champ_on=(z60>0); ix_ret=pn['ix_close'].pct_change()
champ_pnl=champ_on.shift(1).astype(float)*ix_ret
ma200=pn['ix_close'].rolling(200).mean()
bull=(pn['ix_close']>ma200)&(ma200>ma200.shift(20))

valid=dates[dates>=pd.Timestamp('2021-06-01')]
split=valid[int(len(valid)*0.70)]

def sharpe(x, ann=True):
    x=x.dropna()
    if x.std()==0 or len(x)<20: return np.nan
    s=x.mean()/x.std()
    return s*np.sqrt(252) if ann else s

_block=np.arange(len(dates))//5
_bpos=np.array([np.where(_block==b)[0][0] for b in np.unique(_block)])
_bdates=dates[_bpos]

def longshort_ret(sig, direction, q=0.2):
    s=sig*direction; r=s.rank(axis=1,pct=True); cnt=s.notna().sum(axis=1)
    long=(r>=(1-q))&(cnt.values[:,None]>=10); short=(r<=q)&(cnt.values[:,None]>=10)
    nl=long.sum(axis=1).replace(0,np.nan); ns=short.sum(axis=1).replace(0,np.nan)
    w=(long.div(nl,axis=0)*0.5 - short.div(ns,axis=0)*0.5).fillna(0.0)
    wb=w.reindex(_bdates).reindex(dates).ffill().fillna(0.0)
    turnover=(wb-wb.shift(1).fillna(0.0)).abs().sum(axis=1).where(dates.isin(_bdates),0.0)
    return (wb.shift(1)*ret).sum(axis=1) - turnover*COST

def rank_ic(sig, fwd):
    ics=[]; idx=[]
    for d in dates:
        a=sig.loc[d]; b=fwd.loc[d]; m=a.notna()&b.notna()
        if m.sum()>=10: ics.append(a[m].rank().corr(b[m].rank())); idx.append(d)
    return pd.Series(ics,index=idx)

fwd5=px.shift(-5)/px-1

# ---------- pass 1: portfolios, IC, direction ----------
ports={}; sig_dir={}; rows=[]
for name,sig in FACTORS.items():
    ic=rank_ic(sig,fwd5)
    ic_is=ic[ic.index<split].mean(); ic_oos=ic[ic.index>=split].mean()
    direction=1.0 if (0 if np.isnan(ic_is) else ic_is)>=0 else -1.0
    sig_dir[name]=direction
    ports[name]=longshort_ret(sig,direction)

# cross-trial daily-Sharpe set for DSR n_trials variance (all 8 variants)
trial_sr_daily=np.array([sharpe(ports[n], ann=False) for n in FACTORS])
V=np.nanvar(trial_sr_daily, ddof=1); Ntr=len(FACTORS)
SR0_daily=np.sqrt(V)*((1-GAMMA)*stats.norm.ppf(1-1/Ntr)+GAMMA*stats.norm.ppf(1-1/(Ntr*np.e)))

def dsr(port_oos):
    r=port_oos.dropna().values; T=len(r)
    if T<20 or r.std()==0: return np.nan
    sr=r.mean()/r.std()  # daily
    sk=stats.skew(r); ku=stats.kurtosis(r,fisher=False)
    denom=np.sqrt(1 - sk*sr + (ku-1)/4*sr**2)
    return float(stats.norm.cdf((sr-SR0_daily)*np.sqrt(T-1)/denom))

def perm_p_oos(sig, direction, obs, N=500):
    oos_mask=(dates>=split); cnt=0; arr0=sig.values
    for _ in range(N):
        arr=arr0.copy()
        for r in np.where(oos_mask)[0]:
            row=arr[r]; ix=np.where(~np.isnan(row))[0]
            if len(ix)>1: arr[r,ix]=RNG.permutation(row[ix])
        pp=longshort_ret(pd.DataFrame(arr,index=dates,columns=stocks),direction)
        if sharpe(pp[pp.index>=split])>=obs: cnt+=1
    return (cnt+1)/(N+1)

rev3_port=longshort_ret(rev3, 1.0)  # rev_yoy_3m is long-positive momentum

for name,sig in FACTORS.items():
    p=ports[name]; direction=sig_dir[name]
    ic=rank_ic(sig,fwd5)
    ic_is=ic[ic.index<split].mean(); ic_oos=ic[ic.index>=split].mean()
    is_p=p[p.index<split]; oos_p=p[p.index>=split]
    sh_is,sh_oos=sharpe(is_p),sharpe(oos_p)
    sh_bull=sharpe(p.where(bull,0.0)); sh_bear=sharpe(p.where(~bull,0.0))
    obs=sh_oos
    pp=perm_p_oos(sig,direction,obs)
    ds=dsr(oos_p)
    corr_champ=p.corr(champ_pnl)
    corr_rev3=p.corr(rev3_port)
    rows.append(dict(factor=name, primary=name in PRIMARY, dir=int(direction),
        ic_is=round(ic_is,4), ic_oos=round(ic_oos,4),
        ret_ann_oos=round(oos_p.mean()*252*100,3),
        sharpe_is=round(sh_is,3), sharpe_oos=round(sh_oos,3),
        sharpe_bull=round(sh_bull,3), sharpe_bear=round(sh_bear,3),
        perm_p_oos=round(pp,4), dsr_oos=round(ds,4),
        corr_champ=round(corr_champ,3), corr_rev3m=round(corr_rev3,3)))
    print(f"{name:8s} {'*' if name in PRIMARY else ' '} dir={int(direction):+d} "
          f"IC_is={ic_is:+.4f} IC_oos={ic_oos:+.4f} Sh_is={sh_is:+.2f} Sh_oos={sh_oos:+.2f} "
          f"bull={sh_bull:+.2f} bear={sh_bear:+.2f} p={pp:.3f} DSR={ds:.3f} "
          f"champ={corr_champ:+.2f} rev3m={corr_rev3:+.2f}")

print(f"\nDSR SR0_daily(null max over {Ntr} trials)={SR0_daily:.4f}  "
      f"cross-trial daily-SR std={np.sqrt(V):.4f}")
bh=ret.mean(axis=1)
print(f"universe B&H Sharpe oos={sharpe(bh[bh.index>=split]):+.2f}  "
      f"rev_yoy_3m ref Sharpe oos={sharpe(rev3_port[rev3_port.index>=split]):+.2f}")

pd.DataFrame(rows).to_csv(OUTCSV,index=False)
print(f"\nsplit IS<{split.date()}<=OOS | {len(dates)} days {dates[0].date()}->{dates[-1].date()}")
print(f"wrote {OUTCSV}")
