import pandas as pd, numpy as np
p=pd.read_parquet('data/research/dashboard/asquith/panel.parquet')
p=p[np.isfinite(p['size'])].copy()
def terc(s): return pd.qcut(s.rank(method='first'),3,labels=[1,2,3]).astype(int)
def neut(g,yc,xs=('size','mom20')):
    sub=g.dropna(subset=[yc]+list(xs))
    if len(sub)<10: g=g.copy(); g['_r']=np.nan; return g
    X=np.column_stack([np.ones(len(sub))]+[sub[c].values for c in xs]); b,*_=np.linalg.lstsq(X,sub[yc].values,rcond=None)
    r=pd.Series(np.nan,index=g.index); r.loc[sub.index]=sub[yc].values-X@b; g=g.copy(); g['_r']=r; return g
def wk(df,shortcol,yc):
    recs=[]
    for dt,g in df.groupby('date'):
        g=g.dropna(subset=[shortcol,yc,'size','mom20'])
        if len(g)<30: continue
        g=g.copy(); g['sT']=terc(g[shortcol]); g=neut(g,yc)
        recs.append((dt,g[g.sT==3]['_r'].mean()-g[g.sT==1]['_r'].mean()))
    s=pd.DataFrame(recs,columns=['date','sp']).dropna(); s['mon']=s.date.dt.month; return s

for col in ['si_total','si_margin','si_lend']:
    s=wk(p,col,'fwd20')
    sh=s.sp.mean()/s.sp.std()*np.sqrt(52)
    print(f"\n{col} short_spread fwd20 Sharpe={sh:+.2f} mean={s.sp.mean()*100:+.3f}%")
    # forced-covering season: Taiwan 融券強制回補 clusters around AGM (Apr-Jun) & ex-div (Jul-Sep)
    seas=s[s.mon.isin([4,5,6,7,8,9])]; off=s[~s.mon.isin([4,5,6,7,8,9])]
    print(f"  covering-season(Apr-Sep) mean={seas.sp.mean()*100:+.3f}% n={len(seas)} | off-season mean={off.sp.mean()*100:+.3f}% n={len(off)}")
    bymon=s.groupby('mon').sp.agg(['mean','size'])
    print('  by month mean%:',{m:round(r['mean']*100,2) for m,r in bymon.iterrows()})
