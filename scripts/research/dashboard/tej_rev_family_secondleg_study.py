"""L1 revenue-family SECOND-LEG study: is the L1 revenue layer one leg (rev_yoy)
or does an independent second leg exist?

Tests three revenue FORMS distinct from YoY, on the wide top-~420 liquid universe,
all PIT via TEJ EWSALE annd (公告日) forward-filled to daily, adjusted-close returns:
  (a) mom      = 月營收環比 MoM 動能  (rev_t / rev_{t-1} - 1, latest announced)
  (b) surprise = 營收超預期  (residual of log(rev) vs trailing-12m log-linear trend,
                 standardized by trend residual std; a beat/miss-vs-own-trend signal)
  (c) streak   = 連續 YoY 正成長月數 (count of consecutive most-recent +YoY months)

For each: direction fixed by IS Rank-IC (falsification-first); IS/OOS 70/30 time split;
weekly (5d) top/bottom-20% equal-weight L/S, t+1 entry, 4bps/side*turnover cost;
permutation (shuffle signal across stocks each OOS day, keep exposure); Deflated-Sharpe
with honest n_trials.

KEY (does a SECOND leg exist beyond rev_yoy_3m):
  - collinearity: cross-sectional Spearman corr(form, rev_yoy_3m); corr of daily L/S returns.
  - residual IC: per-day regress form on rev_yoy_3m in rank space, take residual,
    measure residual Rank-IC on fwd5 (IS/OOS) and residualized-L/S Sharpe.
    If residual IC ~ 0 -> the form is a restatement of rev_yoy, NOT an independent leg.

Not investment advice. Honest coverage recorded.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
OUTCSV = ROOT / "reports" / "research" / "dashboard-completeness" / "tej_rev_family_secondleg_metrics.csv"
RNG = np.random.default_rng(42)
COST = 0.0004

# ---------- load wide universe ----------
pb = pd.read_parquet(D/'tej_revfam_pb.parquet')
sale = pd.read_parquet(D/'tej_revfam_sale.parquet')
pb['date'] = pd.to_datetime(pb['date'], errors='coerce')
sale['annd'] = pd.to_datetime(sale['annd'], errors='coerce')
sale['period']=pd.to_datetime(sale['period'], errors='coerce')
sale['rev']=pd.to_numeric(sale['rev'], errors='coerce')
sale['rev_yoy']=pd.to_numeric(sale['rev_yoy'], errors='coerce')
sale = sale.dropna(subset=['annd','period']).sort_values(['stock_id','period'])
pb = pb.dropna(subset=['date'])
pb['close_adj']=pd.to_numeric(pb['close_adj'], errors='coerce')

px = pb.pivot(index='date', columns='stock_id', values='close_adj').sort_index()
dates = px.index
stocks = list(px.columns)
ret = px.pct_change()
fwd5 = px.shift(-5)/px - 1

# ---------- build per-stock monthly revenue features, index by annd ----------
# returns dict stock -> Series(value, index=annd), then daily ffill
def daily_ffill(per_stock):
    mat = pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for sid, s in per_stock.items():
        if sid not in stocks or s is None or len(s)==0: continue
        s = s[~s.index.duplicated(keep='last')].sort_index()
        mat[sid] = s.reindex(dates, method='ffill')
    return mat.astype(float)

rev_yoy3m={}; mom={}; surprise={}; streak={}
for sid, g in sale.groupby('stock_id'):
    if sid not in stocks: continue
    g = g.sort_values('period')
    annd = pd.to_datetime(g['annd'].values)
    rev = g['rev'].astype(float).values
    yoy = g['rev_yoy'].astype(float).values
    n=len(g)
    # (ref) rev_yoy_3m
    r3 = pd.Series(yoy).rolling(3).mean().values
    rev_yoy3m[sid]=pd.Series(r3, index=annd)
    # (a) MoM
    with np.errstate(divide='ignore',invalid='ignore'):
        mm = rev[1:]/rev[:-1]-1.0
    mm = np.concatenate([[np.nan], mm])
    mm[~np.isfinite(mm)] = np.nan
    mom[sid]=pd.Series(mm, index=annd)
    # (b) surprise vs trailing-12m log-linear trend (uses months < current, PIT)
    sur=np.full(n,np.nan)
    lrev=np.log(np.where(rev>0,rev,np.nan))
    for i in range(n):
        lo=i-12
        if lo<0: continue
        yv=lrev[lo:i]           # trailing 12 months, EXCLUDING current
        if np.isfinite(yv).sum()<10: continue
        xh=np.arange(len(yv)); mfin=np.isfinite(yv)
        b=np.polyfit(xh[mfin], yv[mfin], 1)
        pred=np.polyval(b, len(yv))          # extrapolate to current month
        resid=yv[mfin]-np.polyval(b, xh[mfin])
        sd=resid.std(ddof=1)
        if not np.isfinite(lrev[i]) or sd==0 or not np.isfinite(sd): continue
        sur[i]=(lrev[i]-pred)/sd
    surprise[sid]=pd.Series(sur, index=annd)
    # (c) consecutive positive-YoY streak as of each announced month
    st=np.zeros(n); c=0
    for i in range(n):
        if np.isfinite(yoy[i]) and yoy[i]>0: c+=1
        elif np.isfinite(yoy[i]): c=0
        st[i]=c
    streak[sid]=pd.Series(st, index=annd)

REF = daily_ffill(rev_yoy3m)
FORMS = {'mom': daily_ffill(mom), 'surprise': daily_ffill(surprise), 'streak': daily_ffill(streak)}

# ---------- machinery ----------
valid = dates[dates >= pd.Timestamp('2021-03-01')]
split = valid[int(len(valid)*0.70)]

def sharpe(x):
    x=x.dropna()
    return np.nan if len(x)<20 or x.std()==0 else x.mean()/x.std()*np.sqrt(252)

_block = np.arange(len(dates))//5
_bstart = dates[np.array([np.where(_block==b)[0][0] for b in np.unique(_block)])]

def longshort_ret(sig, direction, q=0.2, minn=20):
    s = sig*direction
    r = s.rank(axis=1, pct=True)
    cnt = s.notna().sum(axis=1)
    long=(r>=(1-q))&(cnt.values[:,None]>=minn); short=(r<=q)&(cnt.values[:,None]>=minn)
    nl=long.sum(axis=1).replace(0,np.nan); ns=short.sum(axis=1).replace(0,np.nan)
    w=(long.div(nl,axis=0)*0.5 - short.div(ns,axis=0)*0.5).fillna(0.0)
    wb=w.reindex(_bstart).reindex(dates).ffill().fillna(0.0)
    to=(wb-wb.shift(1).fillna(0.0)).abs().sum(axis=1).where(dates.isin(_bstart),0.0)
    return (wb.shift(1)*ret).sum(axis=1) - to*COST

def rank_ic(sig, mask=None):
    ics=[]; idx=[]
    for d in dates:
        if mask is not None and not mask(d): continue
        a=sig.loc[d]; b=fwd5.loc[d]; m=a.notna()&b.notna()
        if m.sum()>=20: ics.append(a[m].rank().corr(b[m].rank())); idx.append(d)
    return pd.Series(ics, index=idx)

def dsr(port, n_trials, sr_trials):
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado). port=daily returns."""
    x=port.dropna(); x=x[x!=0]
    T=len(x)
    if T<30: return np.nan
    sr=x.mean()/x.std()                       # non-annualized per-obs Sharpe
    sk=stats.skew(x); ku=stats.kurtosis(x, fisher=False)
    var_sr=np.var([s for s in sr_trials if np.isfinite(s)], ddof=1) if len(sr_trials)>1 else 0.0
    if var_sr<=0: var_sr=(1.0/T)   # fallback
    N=max(n_trials,2); e=np.e; g=0.5772156649
    sr0=np.sqrt(var_sr)*((1-g)*stats.norm.ppf(1-1.0/N)+g*stats.norm.ppf(1-1.0/(N*e)))
    denom=np.sqrt(1 - sk*sr + (ku-1)/4.0*sr**2)
    z=(sr - sr0)*np.sqrt(T-1)/denom
    return stats.norm.cdf(z)

# reference leg L/S (direction +, YoY momentum is long-high)
ref_ic_is = rank_ic(REF, lambda d:d<split).mean()
ref_dir = 1.0 if ref_ic_is>=0 else -1.0
ref_port = longshort_ret(REF, ref_dir)

rows=[]; ports={}
# first pass: compute per-form directional L/S ports (need Sharpe set for DSR n_trials)
prelim={}
for name, sig in FORMS.items():
    ic_is=rank_ic(sig, lambda d:d<split).mean()
    direction=1.0 if (ic_is if np.isfinite(ic_is) else 0)>=0 else -1.0
    prelim[name]=(sig, direction, ic_is)

sr_trials=[]
for name,(sig,direction,_) in prelim.items():
    p=longshort_ret(sig,direction); ports[name]=p
    x=p.dropna(); x=x[x!=0]
    if len(x)>=30: sr_trials.append(x.mean()/x.std())
# include ref in trial family (honest: 4 forms considered in this study + 2 directions)
xr=ref_port.dropna(); xr=xr[xr!=0]
if len(xr)>=30: sr_trials.append(xr.mean()/xr.std())
N_TRIALS = 8   # 4 forms x 2 direction choices considered (honest upper bound)

for name,(sig,direction,ic_is) in prelim.items():
    ic_all=rank_ic(sig); ic_oos=ic_all[ic_all.index>=split].mean()
    port=ports[name]
    is_p=port[port.index<split]; oos_p=port[port.index>=split]
    sh_is,sh_oos,sh_all=sharpe(is_p),sharpe(oos_p),sharpe(port)
    # permutation OOS
    obs=sh_oos; cnt=0; Nperm=500; oos_mask=(dates>=split); arr0=sig.values
    for _ in range(Nperm):
        arr=arr0.copy()
        for r in np.where(oos_mask)[0]:
            row=arr[r]; ix=np.where(~np.isnan(row))[0]
            if len(ix)>1: arr[r,ix]=RNG.permutation(row[ix])
        pp=longshort_ret(pd.DataFrame(arr,index=dates,columns=stocks),direction)
        if sharpe(pp[pp.index>=split])>=obs: cnt+=1
    perm_p=(cnt+1)/(Nperm+1)
    dsr_all=dsr(port, N_TRIALS, sr_trials)
    # collinearity vs rev_yoy_3m
    xs_corr=[]
    for d in dates:
        a=sig.loc[d]; b=REF.loc[d]; m=a.notna()&b.notna()
        if m.sum()>=20: xs_corr.append(a[m].rank().corr(b[m].rank()))
    xs_corr=np.nanmean(xs_corr)
    ret_corr=port.corr(ref_port)
    # RESIDUAL: per-day regress form-rank on ref-rank, residual, then IC + L/S
    resid=pd.DataFrame(index=dates, columns=stocks, dtype=float)
    for d in dates:
        a=sig.loc[d]; b=REF.loc[d]; m=a.notna()&b.notna()
        if m.sum()<20: continue
        ar=a[m].rank(); br=b[m].rank()
        beta=np.cov(ar,br)[0,1]/np.var(br) if np.var(br)>0 else 0.0
        e=ar-(ar.mean()+beta*(br-br.mean()))
        resid.loc[d, e.index]=e.values
    ric=rank_ic(resid)
    ric_is=ric[ric.index<split].mean(); ric_oos=ric[ric.index>=split].mean()
    rdir=1.0 if (ric_is if np.isfinite(ric_is) else 0)>=0 else -1.0
    rport=longshort_ret(resid, rdir)
    rsh_is=sharpe(rport[rport.index<split]); rsh_oos=sharpe(rport[rport.index>=split])
    cov=int(min(sig.notna().sum(axis=1).max(), REF.notna().sum(axis=1).max()))
    rows.append(dict(form=name, dir=int(direction),
        ic_is=round(ic_is,4), ic_oos=round(ic_oos,4),
        sharpe_is=round(sh_is,3), sharpe_oos=round(sh_oos,3), sharpe_all=round(sh_all,3),
        perm_p_oos=round(perm_p,4), dsr=round(dsr_all,4) if np.isfinite(dsr_all) else np.nan,
        xs_corr_revyoy3m=round(xs_corr,3), retcorr_revyoy3m=round(ret_corr,3),
        resid_ic_is=round(ric_is,4), resid_ic_oos=round(ric_oos,4),
        resid_sharpe_is=round(rsh_is,3), resid_sharpe_oos=round(rsh_oos,3),
        max_xs_cov=cov))
    print(f"{name:9s} dir={int(direction):+d} IC_is={ic_is:+.4f} IC_oos={ic_oos:+.4f} "
          f"Sh_is={sh_is:+.2f} Sh_oos={sh_oos:+.2f} p={perm_p:.3f} DSR={dsr_all:.3f} | "
          f"xsCorr={xs_corr:+.2f} retCorr={ret_corr:+.2f} | "
          f"residIC_is={ric_is:+.4f} residIC_oos={ric_oos:+.4f} residSh_oos={rsh_oos:+.2f}",
          flush=True)

# reference leg summary
print(f"\n[ref] rev_yoy_3m dir={int(ref_dir):+d} IC_is={ref_ic_is:+.4f} "
      f"Sh_is={sharpe(ref_port[ref_port.index<split]):+.2f} Sh_oos={sharpe(ref_port[ref_port.index>=split]):+.2f} "
      f"(wide universe, n_stocks={len(stocks)})", flush=True)

pd.DataFrame(rows).to_csv(OUTCSV, index=False)
print(f"\nsplit IS<{split.date()}<=OOS | {len(dates)} days {dates[0].date()}->{dates[-1].date()} "
      f"| universe {len(stocks)} stocks", flush=True)
print(f"wrote {OUTCSV}", flush=True)
