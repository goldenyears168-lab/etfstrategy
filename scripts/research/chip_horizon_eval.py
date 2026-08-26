import numpy as np, pandas as pd, sys
from pathlib import Path
from importlib.machinery import SourceFileLoader
sys.path.insert(0,"scripts/research")
LAB=SourceFileLoader("lab","scripts/research/chip_lab.py").load_module()
HZ=SourceFileLoader("hz","scripts/research/chip_horizon.py").load_module()
d=pd.read_pickle(LAB.DIR/"chip_horizon_panel.pkl")
BASIS="xs"
# 因子矩陣（統一 basis，方向先照原假設）
F={n:LAB.signed(d,n,BASIS) for n in LAB.FACTORS}
F={k:v for k,v in F.items() if not v.isna().all()}
print(f"面板 {len(d):,} · {d.trade_date.nunique()} 日 · 因子 {len(F)}\n")

def wf_ic(k, win=250, minp=120, lag=2):
    """walk-forward IC：只用過去資料，並落後 lag 日（K 日報酬要 T+K 才實現）。"""
    lagd = lag + k                     # K 日報酬在 T+K 收盤才知道
    out={}
    for n,f in F.items():
        x=pd.DataFrame({"t":d.trade_date,"f":f,"y":d[f"n{k}"]}).dropna()
        ic=x.groupby("t").apply(lambda g: g.f.corr(g.y,method="spearman"),include_groups=False)
        out[n]=ic.rolling(win,min_periods=minp).mean().shift(lagd)
    return pd.DataFrame(out)

def build(k, scheme):
    icw=wf_ic(k)
    W=d.trade_date.map(lambda t: t)     # placeholder
    w=icw.reindex(d.trade_date).reset_index(drop=True)
    M=pd.DataFrame({n:F[n].reset_index(drop=True) for n in F})
    if scheme=="ICpos":
        ww=w.clip(lower=0)
    elif scheme=="IC":
        ww=w
    elif scheme=="EW_sig":                # 只用 |IC| 夠大的，等權，方向照 IC 符號
        ww=np.sign(w)*(w.abs()>0.01)
    else: raise ValueError(scheme)
    ww=ww.where(M.notna())
    num=(ww*M).sum(axis=1); den=ww.abs().sum(axis=1)
    s=(num/den.replace(0,np.nan))
    s.index=d.index
    return s

print(f"{'K':>3}{'方案':<9}{'命中率':>8}{'每趟gross':>10}{'NW t':>7}{'單次換手':>9}"
      f"{'日均換手':>9}{'日gross':>9}{'日成本':>8}{'淨值/年':>9}{'損平':>8}")
res=[]
for k in (1,2,3,5,10,20):
    for scheme in ("ICpos","IC","EW_sig"):
        s=build(k,scheme)
        r=HZ.evaluate_k(d,s,k)
        if "error" in r: print(f"{k:>3}{scheme:<9}{r['error']}"); continue
        cost_day=r["turn_daily"]/100*0.471
        print(f"{k:>3}{scheme:<9}{r['hit']:>7.1f}%{r['gross_trade']:>+9.4f}%{r['t_nw']:>+7.2f}"
              f"{r['turn_rebal']:>8.1f}%{r['turn_daily']:>8.2f}%{r['gross_day']:>+8.4f}%"
              f"{cost_day:>7.4f}%{r['net_ann']:>+8.2f}%{r['breakeven']:>7.3f}%")
        res.append({"k":k,"scheme":scheme,**r})
pd.DataFrame(res).to_csv(LAB.DIR/"horizon_eval.csv",index=False)
