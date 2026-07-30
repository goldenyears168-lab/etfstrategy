"""Adversarial PIT audit of rev_yoy_3m (month-revenue momentum look-ahead).

Re-runs the rev_yoy / rev_yoy_3m factor under THREE revenue-timing regimes to
isolate whether the reported alpha is a point-in-time artifact:

  1. annd   (CORRECT PIT)  : revenue usable only from TEJ announcement date (annd_s).
  2. month  (LOOK-AHEAD)   : revenue usable from the FIRST day of its own revenue
                             month M  -> peeks ~37-40 trading days into the future.
  3. lag40  (CONSERVATIVE) : revenue usable from period-month-start + 40 calendar days
                             (paper rule "次月10日前公布", ignores the real annd).

For each timing: IC_is/oos, Sharpe_is/oos, OOS permutation p (exposure-preserving),
and Deflated-Sharpe (N=6 trials, per-timing trial dispersion, skew/kurt adjusted).

If (1) and (3) survive but (2) is much higher, the study did NOT commit look-ahead.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np, pandas as pd
from scipy.stats import norm, skew as sp_skew, kurtosis as sp_kurt

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
RNG = np.random.default_rng(42)
COST = 0.0004

pb = pd.read_parquet(D/'tej_fundamental_pb.parquet')
sale = pd.read_parquet(D/'tej_fundamental_sale.parquet')
fin = pd.read_parquet(D/'tej_fundamental_fin.parquet')
panel = pd.read_parquet(D.parent/'chip_macro'/'panel.parquet')
pb['date']=pd.to_datetime(pb['date'])
sale['annd']=pd.to_datetime(sale['annd']); sale['period']=pd.to_datetime(sale['period'])
fin['annd']=pd.to_datetime(fin['annd'])
panel['date']=pd.to_datetime(panel['date'])

px = pb.pivot(index='date',columns='stock_id',values='close_adj').sort_index()
pbmat = pb.pivot(index='date',columns='stock_id',values='pb_ratio').sort_index()
dates=px.index; stocks=list(px.columns)
ret=px.pct_change()

def pit_daily(df,valcol,annd='annd'):
    out=pd.DataFrame(index=dates,columns=stocks,dtype=float)
    for sid,g in df.groupby('stock_id'):
        if sid not in out.columns: continue
        g=g.dropna(subset=[valcol]).sort_values(annd)
        if len(g)==0: continue
        s=pd.Series(g[valcol].values,index=pd.to_datetime(g[annd].values))
        s=s[~s.index.duplicated(keep='last')].sort_index()
        out[sid]=s.reindex(dates,method='ffill')
    return out.astype(float)

def rev_features(timecol):
    """Build rev_yoy(level) + rev_yoy_3m(smoothed) daily matrices using `timecol`
    as the availability date."""
    latest=pd.DataFrame(index=dates,columns=stocks,dtype=float)
    sm3=pd.DataFrame(index=dates,columns=stocks,dtype=float)
    for sid,g in sale.groupby('stock_id'):
        if sid not in stocks: continue
        g=g.dropna(subset=['rev_yoy']).sort_values('period')  # order by revenue month
        if len(g)==0: continue
        yoy=g['rev_yoy'].astype(float).values
        avail=pd.to_datetime(g[timecol].values)
        roll3=pd.Series(yoy).rolling(3).mean().values
        for mat,arr in [(latest,yoy),(sm3,roll3)]:
            s=pd.Series(arr,index=avail)
            s=s[~s.index.duplicated(keep='last')].sort_index()
            mat[sid]=s.reindex(dates,method='ffill')
    return latest.astype(float), sm3.astype(float)

# timing columns
sale['month']=sale['period']                       # revenue-month start (look-ahead)
sale['lag40']=sale['period']+pd.Timedelta(days=40) # conservative paper lag

roe=pit_daily(fin,'roe')
pb_inv=1.0/pbmat.replace(0,np.nan)

pn=panel.set_index('date').reindex(dates).ffill()
valid=dates[dates>=pd.Timestamp('2021-02-01')]
split=valid[int(len(valid)*0.70)]
oos_mask=(dates>=split)

def sharpe(x):
    x=x.dropna(); return np.nan if x.std()==0 or len(x)<20 else x.mean()/x.std()*np.sqrt(252)

_block=np.arange(len(dates))//5
_bstart=dates[np.array([np.where(_block==b)[0][0] for b in np.unique(_block)])]

def longshort_ret(sig,direction,q=0.2):
    s=sig*direction; r=s.rank(axis=1,pct=True); cnt=s.notna().sum(axis=1)
    long=(r>=(1-q))&(cnt.values[:,None]>=10); short=(r<=q)&(cnt.values[:,None]>=10)
    nl=long.sum(axis=1).replace(0,np.nan); ns=short.sum(axis=1).replace(0,np.nan)
    w=long.div(nl,axis=0)*0.5-short.div(ns,axis=0)*0.5; w=w.fillna(0.0)
    wb=w.reindex(_bstart).reindex(dates).ffill().fillna(0.0)
    to=(wb-wb.shift(1).fillna(0.0)).abs().sum(axis=1)
    to=to.where(dates.isin(_bstart),0.0)
    return (wb.shift(1)*ret).sum(axis=1)-to*COST

fwd5=px.shift(-5)/px-1
def rank_ic(sig):
    ics=[]
    for d in dates:
        a=sig.loc[d]; b=fwd5.loc[d]; m=a.notna()&b.notna()
        if m.sum()>=10: ics.append(a[m].rank().corr(b[m].rank()))
    return pd.Series(ics,index=dates[:len(ics)])

def perm_p(sig,direction,obs_sh,N=500):
    cnt=0; base=sig.values
    rows=np.where(oos_mask)[0]
    for _ in range(N):
        arr=base.copy()
        for r in rows:
            row=arr[r]; idx=np.where(~np.isnan(row))[0]
            if len(idx)>1: arr[r,idx]=RNG.permutation(row[idx])
        pp=longshort_ret(pd.DataFrame(arr,index=dates,columns=stocks),direction)
        if sharpe(pp[pp.index>=split])>=obs_sh: cnt+=1
    return (cnt+1)/(N+1)

def dsr(port_oos, trial_sharpes_ann, N=6, var_mode='crosstrial'):
    """Deflated Sharpe (Bailey & Lopez de Prado). port_oos daily returns.
    var_mode='crosstrial' -> textbook: empirical variance of the N trial Sharpes.
    var_mode='theory'      -> estimation-noise variance of Sharpe under null (~1/T),
                              the common default that the original report reproduces."""
    r=port_oos.dropna().values
    T=len(r); sr=r.mean()/r.std()                    # per-period SR
    sk=sp_skew(r); ku=sp_kurt(r,fisher=False)        # non-excess kurtosis
    if var_mode=='crosstrial':
        var_sr=np.nanvar(np.array(trial_sharpes_ann)/np.sqrt(252),ddof=1)
    else:  # theoretical sampling variance of the Sharpe estimator (per period)
        var_sr=(1-sk*sr+(ku-1)/4.0*sr**2)/T
    gamma=0.5772156649
    emax=np.sqrt(var_sr)*((1-gamma)*norm.ppf(1-1.0/N)+gamma*norm.ppf(1-1.0/(N*np.e)))
    num=(sr-emax)*np.sqrt(T-1)
    den=np.sqrt(1-sk*sr+(ku-1)/4.0*sr**2)
    return norm.cdf(num/den), sr*np.sqrt(252), emax*np.sqrt(252), sk, ku

results={}
for timing in ['annd','month','lag40']:
    rev_yoy, rev_yoy_3m = rev_features(timing)
    FAC={'rev_yoy':rev_yoy,'rev_yoy_3m':rev_yoy_3m,'roe':roe,'pb_inv':pb_inv}
    # need 6 trials for DSR dispersion -> add rev_accel + composite proxies
    rev_accel = rev_yoy - rev_yoy_3m
    zc=lambda m:(m.sub(m.mean(1),0)).div(m.std(1).replace(0,np.nan),0)
    comp=(zc(rev_yoy_3m).fillna(0)+zc(roe).fillna(0)+zc(pb_inv).fillna(0))
    comp=comp.where(rev_yoy_3m.notna()|roe.notna()|pb_inv.notna())
    FAC['rev_accel']=rev_accel; FAC['composite']=comp
    trial_sh=[]; detail={}
    for name,sig in FAC.items():
        ic=rank_ic(sig); ic_is=ic[ic.index<split].mean(); ic_oos=ic[ic.index>=split].mean()
        direction=1.0 if (ic_is if not np.isnan(ic_is) else 0)>=0 else -1.0
        port=longshort_ret(sig,direction)
        sh_oos=sharpe(port[port.index>=split])
        trial_sh.append(sh_oos)
        detail[name]=(direction,ic_is,ic_oos,sharpe(port[port.index<split]),sh_oos,port,sig)
    # focus factors
    for name in ['rev_yoy_3m','rev_yoy']:
        direction,ic_is,ic_oos,sh_is,sh_oos,port,sig=detail[name]
        p=perm_p(sig,direction,sh_oos)
        oos=port[port.index>=split]
        dsr_ct,sr_ann,emax_ct,sk,ku=dsr(oos,trial_sh,var_mode='crosstrial')
        dsr_th,_,emax_th,_,_=dsr(oos,trial_sh,var_mode='theory')
        results[(timing,name)]=dict(ic_is=ic_is,ic_oos=ic_oos,sh_is=sh_is,sh_oos=sh_oos,
                                    perm_p=p,dsr_ct=dsr_ct,dsr_th=dsr_th,sr_ann=sr_ann,sk=sk,ku=ku)
        print(f"[{timing:6s}] {name:11s} dir={int(direction):+d} IC_is={ic_is:+.4f} IC_oos={ic_oos:+.4f} "
              f"Sh_is={sh_is:+.2f} Sh_oos={sh_oos:+.2f} perm_p={p:.4f} "
              f"DSR_theory={dsr_th:.3f} DSR_crosstrial={dsr_ct:.3f} "
              f"(SR_ann={sr_ann:+.2f}, skew={sk:+.2f} kurt={ku:.1f})")

print(f"\nsplit IS<{split.date()}<=OOS | {len(dates)} days {dates[0].date()}->{dates[-1].date()}")
print("annd = correct PIT | month = look-ahead (~37-40d peek) | lag40 = conservative paper lag")
