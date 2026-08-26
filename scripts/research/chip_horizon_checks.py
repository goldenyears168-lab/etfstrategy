import numpy as np, pandas as pd, sys
from importlib.machinery import SourceFileLoader
sys.path.insert(0,"scripts/research")
LAB=SourceFileLoader("lab","scripts/research/chip_lab.py").load_module()
HZ=SourceFileLoader("hz","scripts/research/chip_horizon.py").load_module()
d=pd.read_pickle(LAB.DIR/"chip_horizon_panel.pkl")
F={n:LAB.signed(d,n,"xs") for n in LAB.FACTORS}
F={k:v for k,v in F.items() if not v.isna().all()}
def wf_ic(k,win=250,minp=120,lag=2):
    out={}
    for n,f in F.items():
        x=pd.DataFrame({"t":d.trade_date,"f":f,"y":d[f"n{k}"]}).dropna()
        ic=x.groupby("t").apply(lambda g: g.f.corr(g.y,method="spearman"),include_groups=False)
        out[n]=ic.rolling(win,min_periods=minp).mean().shift(lag+k)
    return pd.DataFrame(out)
K=5
icw=wf_ic(K)
w=icw.reindex(d.trade_date).reset_index(drop=True)
M=pd.DataFrame({n:F[n].reset_index(drop=True) for n in F})
ww=w.clip(lower=0).where(M.notna())
s=((ww*M).sum(axis=1)/ww.abs().sum(axis=1).replace(0,np.nan)); s.index=d.index
print("【1】ICpos 在 K=5 實際挑了誰（OOS 期平均權重，只列 >1%）")
dates=np.sort(d.trade_date.unique()); oos=set(dates[250:])
mask=d.trade_date.isin(oos).values
wn=ww[mask].div(ww[mask].abs().sum(axis=1),axis=0).mean().sort_values(ascending=False)
for n,v in wn.items():
    if v>0.01: print(f"    {n:<12}{v*100:>6.1f}%")
print(f"    有效因子數（權重>1%）: {(wn>0.01).sum()} / {len(wn)}")
print(f"    最大單一權重: {wn.max()*100:.1f}%  → {'角點解，等於單因子' if wn.max()>0.6 else '真的有分散'}")

print("\n【2】檔數敏感度（K=5, ICpos）")
orig=HZ.FR
print(f"{'檔數':>6}{'命中率':>8}{'每趟gross':>10}{'NW t':>7}{'日均換手':>9}{'淨值/年':>9}{'損平':>8}")
for fr in (0.02,0.04,0.058,0.08,0.12,0.20):
    HZ.FR=fr; r=HZ.evaluate_k(d,s,K)
    if "error" in r: continue
    print(f"{fr*450:>6.0f}{r['hit']:>7.1f}%{r['gross_trade']:>+9.4f}%{r['t_nw']:>+7.2f}"
          f"{r['turn_daily']:>8.2f}%{r['net_ann']:>+8.2f}%{r['breakeven']:>7.3f}%")
HZ.FR=orig

print("\n【3】分期穩定性（K=5, ICpos，OOS 切成四段）")
x=d.assign(_s=s).dropna(subset=["_s",f"n{K}"])
rows=[];hist=[]
for t,g in x.groupby("trade_date",sort=True):
    if len(g)<120: continue
    n=max(3,int(round(len(g)*0.058))); q=g.sort_values("_s",ascending=False)
    L=set(q.stock_id.head(n))
    tk=len(L-hist[-K])/n if len(hist)>=K else np.nan
    hist.append(L)
    if t in oos: rows.append({"t":t,"long":q[f"n{K}"].head(n).mean(),"turn":tk})
r=pd.DataFrame(rows)
q4=np.array_split(r,4)
for i,seg in enumerate(q4,1):
    g_=seg.long.mean()*100; tau=seg.turn.mean()/K
    print(f"  第{i}段 {seg.t.iloc[0]}~{seg.t.iloc[-1]}  gross {g_:+.4f}%  "
          f"NW t {HZ.nw_t(seg.long,K):+.2f}  淨值/年 {(g_/K-tau*0.471)*242:+.2f}%")

print("\n【4】安慰劑對照（K=5，同樣的評估流程）")
rng=np.random.default_rng(20260826)
pl={}
pl["隨機分數"]=pd.Series(rng.standard_normal(len(d)),index=d.index)
pl["市值(小→大)"]=(d.groupby("trade_date").mcap.rank(pct=True)-0.5)*2
pl["波動(低→高)"]=-(d.groupby("trade_date").vol60.rank(pct=True)-0.5)*2
pl["單用 sbl_pct"]=LAB.signed(d,"sbl_pct","xs")
for nm,sc in pl.items():
    r=HZ.evaluate_k(d,sc,K)
    if "error" in r: continue
    print(f"  {nm:<14}命中 {r['hit']:>5.1f}%  gross {r['gross_trade']:>+8.4f}%  "
          f"t {r['t_nw']:>+6.2f}  換手 {r['turn_daily']:>5.2f}%  淨值/年 {r['net_ann']:>+7.2f}%  損平 {r['breakeven']:.3f}%")
