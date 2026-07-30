import pandas as pd, numpy as np
p=pd.read_parquet('data/research/dashboard/asquith/panel.parquet')
p=p[np.isfinite(p['size'])].copy()

def terciles(s):
    # rank into 1,2,3 within group; robust to ties
    try:
        return pd.qcut(s.rank(method='first'),3,labels=[1,2,3]).astype(int)
    except: return pd.Series(np.nan,index=s.index)

def neutralize(g, ycol, xcols=('size','mom20')):
    sub=g.dropna(subset=[ycol]+list(xcols))
    if len(sub)<10: 
        g=g.copy(); g['_res']=np.nan; return g
    X=np.column_stack([np.ones(len(sub))]+[sub[c].values for c in xcols])
    y=sub[ycol].values
    beta,*_=np.linalg.lstsq(X,y,rcond=None)
    res=pd.Series(np.nan,index=g.index)
    res.loc[sub.index]=y-X@beta
    g=g.copy(); g['_res']=res; return g

def build_cells(df, shortcol, owncol, ycol):
    rows=[]
    for dt,g in df.groupby('date'):
        g=g.dropna(subset=[shortcol,owncol,ycol,'size','mom20'])
        if len(g)<30: continue
        g=g.copy()
        g['sT']=terciles(g[shortcol])   # 3=high short demand
        g['oT']=terciles(g[owncol])     # 1=low ownership (constrained supply)
        g=neutralize(g,ycol)
        for st in (1,2,3):
            for ot in (1,2,3):
                cell=g[(g.sT==st)&(g.oT==ot)]
                if len(cell)==0: continue
                rows.append(dict(date=dt,sT=st,oT=ot,n=len(cell),
                                 raw=cell[ycol].mean(), res=cell['_res'].mean()))
    return pd.DataFrame(rows)

def report(df, shortcol, owncol, ycol, label):
    cells=build_cells(df,shortcol,owncol,ycol)
    if cells.empty: 
        print(label,'NO DATA'); return None
    # time-avg per cell
    grid_raw=cells.pivot_table(index='sT',columns='oT',values='raw',aggfunc='mean')
    grid_res=cells.pivot_table(index='sT',columns='oT',values='res',aggfunc='mean')
    print(f"\n===== {label}  ({ycol}) =====")
    print("rows/week weeks:",cells.date.nunique(),"avg cell n:",cells.n.mean().round(1))
    print("RAW cell mean % (rows=shortT 1..3 low->high demand, cols=ownT 1..3 low->high own):")
    print((grid_raw*100).round(3).to_string())
    print("SIZE+MOM NEUTRAL cell mean %:")
    print((grid_res*100).round(3).to_string())
    # constrained cell = high short (3) low own (1)
    con=cells[(cells.sT==3)&(cells.oT==1)].set_index('date')['res']
    rest=cells[~((cells.sT==3)&(cells.oT==1))].groupby('date')['res'].mean()
    j=pd.concat([con.rename('con'),rest.rename('rest')],axis=1).dropna()
    spread=j['con']-j['rest']   # constrained minus rest (should be NEGATIVE if theory holds)
    # within high-short: lowOwn - highOwn
    hs=cells[cells.sT==3]
    lo=hs[hs.oT==1].set_index('date')['res']; hi=hs[hs.oT==3].set_index('date')['res']
    inter=pd.concat([lo.rename('lo'),hi.rename('hi')],axis=1).dropna()
    intspread=inter['lo']-inter['hi']  # constrained-within-highshort minus unconstrained (NEG if theory)
    def stat(x):
        x=x.dropna(); 
        if len(x)<5: return (np.nan,)*4
        t=x.mean()/x.std()*np.sqrt(len(x))
        sharpe=x.mean()/x.std()*np.sqrt(52)  # weekly->annual
        return x.mean()*100, t, sharpe, len(x)
    m,t,sh,n=stat(spread)
    print(f"CONSTRAINED - REST (neutral {ycol}): mean={m:.4f}%  t={t:.2f}  ann.Sharpe={sh:.2f}  wks={n}")
    m2,t2,sh2,n2=stat(intspread)
    print(f"WITHIN-HIGHSHORT lowOwn - highOwn: mean={m2:.4f}%  t={t2:.2f}  Sharpe={sh2:.2f}")
    return dict(spread=spread,intspread=intspread,cells=cells)

# Main: si_total x own_1000
for yc in ['fwd5','fwd20']:
    R=report(p,'si_total','own_1000',yc,'si_total x own_1000')
# robustness metrics
report(p,'si_margin','own_1000','fwd20','si_margin only x own_1000')
report(p,'si_lend','own_1000','fwd20','si_lend only x own_1000')
report(p,'si_total','own_400','fwd20','si_total x own_400')
