import pandas as pd, numpy as np
p=pd.read_parquet('data/research/dashboard/asquith/panel.parquet')
p=p[np.isfinite(p['size'])].copy()
def terc(s): return pd.qcut(s.rank(method='first'),3,labels=[1,2,3]).astype(int)
def neut(g,yc,xs=('size','mom20')):
    sub=g.dropna(subset=[yc]+list(xs))
    if len(sub)<10: g=g.copy(); g['_r']=np.nan; return g
    X=np.column_stack([np.ones(len(sub))]+[sub[c].values for c in xs]); b,*_=np.linalg.lstsq(X,sub[yc].values,rcond=None)
    r=pd.Series(np.nan,index=g.index); r.loc[sub.index]=sub[yc].values-X@b; g=g.copy(); g['_r']=r; return g

def weekly_legs(df,shortcol,owncol,yc):
    recs=[]
    for dt,g in df.groupby('date'):
        g=g.dropna(subset=[shortcol,owncol,yc,'size','mom20'])
        if len(g)<30: continue
        g=g.copy(); g['sT']=terc(g[shortcol]); g['oT']=terc(g[owncol]); g=neut(g,yc)
        c=lambda st,ot: g[(g.sT==st)&(g.oT==ot)]['_r'].mean()
        cS=lambda st: g[g.sT==st]['_r'].mean()
        cO=lambda ot: g[g.oT==ot]['_r'].mean()
        recs.append(dict(date=dt,
            short_spread=cS(3)-cS(1),           # long high-short  minus low-short
            own_spread=cO(1)-cO(3),             # long low-own minus high-own (Asquith supply)
            asquith_ls=(c(1,3))-(c(3,1)),       # Asquith strategy: long unconstrained(lowShort,highOwn) short constrained(highShort,lowOwn)
            interaction=(c(3,1)-c(3,3))-(c(1,1)-c(1,3)),  # pure interaction (double diff): does low-own hurt MORE when short is high
            con_cell=c(3,1)))
    return pd.DataFrame(recs).dropna()

def stats(x,ann=52):
    x=x.dropna(); m=x.mean(); s=x.std(); t=m/s*np.sqrt(len(x)); sh=m/s*np.sqrt(ann)
    return m*100,t,sh,len(x)

W=weekly_legs(p,'si_total','own_1000','fwd20')
for col in ['short_spread','own_spread','asquith_ls','interaction']:
    m,t,sh,n=stats(W[col]); print(f"{col:14s} mean={m:+.4f}% t={t:+.2f} Sharpe={sh:+.2f} n={n}")

# IS/OOS 70/30 on the dominant leg (short_spread) and asquith_ls
W=W.sort_values('date').reset_index(drop=True); cut=int(len(W)*0.7)
for col in ['short_spread','asquith_ls','interaction']:
    mi,ti,si,_=stats(W[col].iloc[:cut]); mo,to,so,_=stats(W[col].iloc[cut:])
    print(f"IS/OOS {col:12s} IS Sharpe={si:+.2f}(t{ti:+.2f}) | OOS Sharpe={so:+.2f}(t{to:+.2f})")

# Permutation test on interaction & on constrained-cell-vs-rest: shuffle ownership labels within week
def perm_interaction(df,shortcol,owncol,yc,nperm=1000,seed=1):
    rng=np.random.default_rng(seed)
    # observed
    obs=weekly_legs(df,shortcol,owncol,yc)['interaction'].mean()
    # precompute per-week neutral resid + sT + own values
    weeks=[]
    for dt,g in df.groupby('date'):
        g=g.dropna(subset=[shortcol,owncol,yc,'size','mom20'])
        if len(g)<30: continue
        g=g.copy(); g['sT']=terc(g[shortcol]); g=neut(g,yc)
        weeks.append(g[['sT','_r',owncol]].reset_index(drop=True))
    cnt=0
    for i in range(nperm):
        vals=[]
        for g in weeks:
            gg=g.copy(); gg['oT']=terc(pd.Series(rng.permutation(gg[owncol].values)))
            c=lambda st,ot: gg[(gg.sT==st)&(gg.oT==ot)]['_r'].mean()
            vals.append((c(3,1)-c(3,3))-(c(1,1)-c(1,3)))
        if abs(np.nanmean(vals))>=abs(obs): cnt+=1
    return obs, (cnt+1)/(nperm+1)
obs,pv=perm_interaction(p,'si_total','own_1000','fwd20',nperm=500)
print(f"\nPERMUTATION interaction: obs={obs*100:+.4f}%  p(2-sided, shuffle own)={pv:.3f}  (500 perms)")
