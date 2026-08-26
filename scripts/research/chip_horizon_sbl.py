import numpy as np, pandas as pd, sys
from importlib.machinery import SourceFileLoader
sys.path.insert(0,"scripts/research")
LAB=SourceFileLoader("lab","scripts/research/chip_lab.py").load_module()
HZ=SourceFileLoader("hz","scripts/research/chip_horizon.py").load_module()
d=pd.read_pickle(LAB.DIR/"chip_horizon_panel.pkl")
K=5; s=LAB.signed(d,"sbl_pct","xs")
dates=np.sort(d.trade_date.unique()); oos=set(dates[250:])
print("【A】市值分層 —— K=1 時它在大型股歸零，K=5 呢？")
d["mq"]=d.groupby("trade_date").mcap.transform(lambda x: pd.qcut(x.rank(method='first'),3,labels=False,duplicates='drop'))
print(f"{'市值層':<10}{'命中率':>8}{'每趟gross':>10}{'NW t':>7}{'日均換手':>9}{'淨值/年':>9}{'損平':>8}")
for q,lab in ((0,"小型 1/3"),(1,"中型 1/3"),(2,"大型 1/3")):
    sub=d[d.mq==q].copy()
    r=HZ.evaluate_k(sub,s.reindex(sub.index),K)
    if "error" in r: print(f"  {lab:<8}{r['error']}"); continue
    print(f"  {lab:<8}{r['hit']:>7.1f}%{r['gross_trade']:>+9.4f}%{r['t_nw']:>+7.2f}"
          f"{r['turn_daily']:>8.2f}%{r['net_ann']:>+8.2f}%{r['breakeven']:>7.3f}%")
print("\n【B】分期穩定性（sbl_pct, K=5）")
x=d.assign(_s=s).dropna(subset=["_s",f"n{K}"])
rows=[];hist=[]
for t,g in x.groupby("trade_date",sort=True):
    if len(g)<120: continue
    n=max(3,int(round(len(g)*0.058))); q=g.sort_values("_s",ascending=False)
    L=set(q.stock_id.head(n)); tk=len(L-hist[-K])/n if len(hist)>=K else np.nan
    hist.append(L)
    if t in oos: rows.append({"t":t,"long":q[f"n{K}"].head(n).mean(),"turn":tk})
r=pd.DataFrame(rows)
for i,seg in enumerate(np.array_split(r,4),1):
    g_=seg.long.mean()*100; tau=seg.turn.mean()/K
    print(f"  第{i}段 {seg.t.iloc[0]}~{seg.t.iloc[-1]}  gross {g_:+.4f}%  NW t {HZ.nw_t(seg.long,K):+.2f}"
          f"  淨值/年 {(g_/K-tau*0.471)*242:+.2f}%")
print("\n【C】名單長相：最常入選的 10 檔與其市值分位")
cnt={}
for t,g in x.groupby("trade_date",sort=True):
    if len(g)<120 or t not in oos: continue
    n=max(3,int(round(len(g)*0.058)))
    for sid in g.sort_values("_s",ascending=False).stock_id.head(n): cnt[sid]=cnt.get(sid,0)+1
top=sorted(cnt.items(),key=lambda z:-z[1])[:10]
nm=LAB.__dict__.get("names",None)
last=d[d.trade_date==d.trade_date.max()].set_index("stock_id")
tot=len([t for t in x.trade_date.unique() if t in oos])
for sid,c in top:
    mc=last.mcap.get(sid,np.nan); mq=last.mq.get(sid,np.nan)
    print(f"    {sid}  入選 {c}/{tot} 日 ({c/tot*100:.0f}%)  市值層 {'小中大'[int(mq)] if pd.notna(mq) else '?'}")
print("\n【D】成本敏感度（sbl_pct, K=5, 全宇宙）")
r=HZ.evaluate_k(d,s,K)
for disc,lab in ((0.6,'6折'),(0.38,'3.8折'),(1.0,'無折扣')):
    for slip in (0.0,0.1,0.2,0.3):
        c=0.1425*disc*2+0.3+slip
        net=(r['gross_day']-r['turn_daily']/100*c)*242
        print(f"  {lab:<6}滑價{slip:.1f}%  合計成本 {c:.3f}%  淨值/年 {net:+.2f}%")
