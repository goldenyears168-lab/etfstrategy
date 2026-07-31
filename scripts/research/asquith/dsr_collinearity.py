import pandas as pd, numpy as np
from scipy import stats as st
p=pd.read_parquet('data/research/dashboard/asquith/panel.parquet')
p=p[np.isfinite(p['size'])].copy()
def terc(s): return pd.qcut(s.rank(method='first'),3,labels=[1,2,3]).astype(int)
def neut(g,yc,xs):
    sub=g.dropna(subset=[yc]+list(xs))
    if len(sub)<10: g=g.copy(); g['_r']=np.nan; return g
    X=np.column_stack([np.ones(len(sub))]+[sub[c].values for c in xs]); b,*_=np.linalg.lstsq(X,sub[yc].values,rcond=None)
    r=pd.Series(np.nan,index=g.index); r.loc[sub.index]=sub[yc].values-X@b; g=g.copy(); g['_r']=r; return g

# ---- PIT rev_yoy_3m per stock aligned to weeks ----
sale=pd.read_parquet('data/research/dashboard/tej_revfam_sale.parquet')
sale['annd']=pd.to_datetime(sale['annd']); sale=sale.sort_values(['stock_id','annd'])
sale['rev_yoy_3m']=sale.groupby('stock_id')['rev_yoy'].transform(lambda s:s.rolling(3).mean())
sale=sale.dropna(subset=['rev_yoy_3m'])
def attach_rev(df):
    out=[]
    for sid,g in df.groupby('stock_id'):
        s=sale[sale.stock_id==sid][['annd','rev_yoy_3m']]
        if s.empty:
            g=g.copy(); g['rev_yoy_3m']=np.nan; out.append(g); continue
        m=pd.merge_asof(g.sort_values('date'), s.sort_values('annd'), left_on='date',right_on='annd',direction='backward')
        out.append(m)
    return pd.concat(out,ignore_index=True)
p=attach_rev(p)
print('rev_yoy_3m coverage in panel:',p.rev_yoy_3m.notna().mean().round(3))

def weekly(df,shortcol,yc,extra_ctrl=()):
    recs=[]
    for dt,g in df.groupby('date'):
        need=[shortcol,yc,'size','mom20']+list(extra_ctrl)
        g=g.dropna(subset=need)
        if len(g)<30: continue
        g=g.copy(); g['sT']=terc(g[shortcol]); g=neut(g,yc,('size','mom20')+tuple(extra_ctrl))
        recs.append(g[g.sT==3]['_r'].mean()-g[g.sT==1]['_r'].mean())
    return np.array([r for r in recs if not np.isnan(r)])

base=weekly(p,'si_total','fwd20')
ctrl=weekly(p,'si_total','fwd20',extra_ctrl=('rev_yoy_3m',))
def shp(x): return x.mean()/x.std()*np.sqrt(52), x.mean()/x.std()*np.sqrt(len(x))
print(f"short_spread base:          Sharpe={shp(base)[0]:+.2f} t={shp(base)[1]:+.2f} n={len(base)}")
print(f"short_spread +rev_yoy ctrl: Sharpe={shp(ctrl)[0]:+.2f} t={shp(ctrl)[1]:+.2f} n={len(ctrl)}  (survives control for rev_yoy_3m alpha)")

# cross-sectional rank corr short-demand vs rev_yoy_3m
cs=[]
for dt,g in p.groupby('date'):
    g=g.dropna(subset=['si_total','rev_yoy_3m'])
    if len(g)>30: cs.append(g['si_total'].corr(g['rev_yoy_3m'],method='spearman'))
print(f"cross-sectional Spearman(si_total, rev_yoy_3m): mean={np.nanmean(cs):+.3f}  (near 0 => independent axes)")

# ---- Deflated Sharpe on short_spread (weekly) ----
def dsr(returns, n_trials, sr_benchmark=0.0):
    r=returns; n=len(r); sr=r.mean()/r.std()  # per-period
    sk=st.skew(r); ku=st.kurtosis(r,fisher=False)
    # variance of estimated SR across trials ~ approximate with (1/(n-1))? Use SR std across a small grid:
    # Expected max SR under null (Bailey & Lopez de Prado)
    e=np.euler_gamma
    sr_std=np.sqrt((1 - sk*sr + (ku-1)/4*sr**2)/(n-1))  # std of SR estimator
    # variance across trials proxy: use sr_std (conservative single-trial estimator variance)
    z=st.norm.ppf
    maxZ=(1-e)*z(1-1.0/n_trials)+e*z(1-1.0/(n_trials*np.e))
    sr0=sr_std*maxZ  # deflated benchmark
    dsr_stat=(sr - sr0)*np.sqrt(n-1)/np.sqrt(1 - sk*sr + (ku-1)/4*sr**2)
    return st.norm.cdf(dsr_stat), sr, sr0, sk, ku
for nt in [20,50,100]:
    pval,sr,sr0,sk,ku=dsr(base,nt)
    print(f"DSR short_spread (trials={nt}): P(SR>0)={pval:.3f}  SR/wk={sr:.3f} defl.bench={sr0:.3f} skew={sk:.2f} kurt={ku:.2f}")
