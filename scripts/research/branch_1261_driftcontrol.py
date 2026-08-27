from pathlib import Path
import numpy as np, pandas as pd
from scipy import stats as sps
from stock_db import connect_ro
DIR = Path("reports/research/chip-signal-daily-horizon")
m = pd.read_pickle(DIR/"branch_1261_joined.pkl")
m = m[m.dt_sh>0].dropna(subset=["buy_vwap","sell_vwap","spread"]).copy()
m["ret_oc"]=m.close/m.open-1
c=connect_ro()
b=pd.read_sql_query("SELECT trade_date, open, close FROM stock_daily_bars WHERE stock_id='0050' AND trade_date>='2025-01-01'",c)
b=b.drop_duplicates("trade_date"); b["mkt_oc"]=b.close/b.open-1
m=m.merge(b[["trade_date","mkt_oc"]],on="trade_date",how="left")
print(f"0050 日內 open→close：平均 {m.groupby('trade_date').mkt_oc.first().mean()*100:+.4f}%  "
      f"上漲日 {(m.groupby('trade_date').mkt_oc.first()>0).mean():.1%}")
def wm(d): return d.dt_pnl.sum()/d.dt_noti.sum()*100 if d.dt_noti.sum() else np.nan
SUBS={"全分點":m,"rt>0.95 純當沖":m[m.rt>0.95],"當沖<=2張":m[m.dt_lot<=2],
      "rt>0.95&<=2張":m[(m.rt>0.95)&(m.dt_lot<=2)]}
print("\n=== 判準4a 依 0050 日內方向分層 ===")
for k,d in SUBS.items():
    up=d[d.mkt_oc>0]; dn=d[d.mkt_oc<=0]
    print(f"  {k:<16} 市場漲日 {wm(up):+.4f}% (n={len(up):,})　市場跌日 {wm(dn):+.4f}% (n={len(dn):,})"
          f"　{'✓ 跌日仍正' if wm(dn)>0 else '✗ 跌日轉負/為負'}")
print("\n=== 判準4b 依個股當日 close/open 五分位 ===")
for k,d in SUBS.items():
    d=d.dropna(subset=["ret_oc"]).copy()
    d["q"]=pd.qcut(d.ret_oc,5,labels=["最跌","跌","平","漲","最漲"])
    r=d.groupby("q",observed=True).apply(lambda x: wm(x))
    print(f"  {k:<16} "+"  ".join(f"{a}{v:+.3f}%" for a,v in r.items()))
print("\n=== 判準4c 回歸 spread ~ 個股ret_oc + 0050ret_oc（截距=漂移中性邊際）===")
for k,d in SUBS.items():
    d=d.dropna(subset=["ret_oc","mkt_oc","spread"])
    X=np.column_stack([np.ones(len(d)), d.ret_oc*100, d.mkt_oc*100])
    w=d.dt_noti.values
    W=np.sqrt(w)[:,None]
    coef,*_=np.linalg.lstsq(X*W, d.spread.values*np.sqrt(w), rcond=None)
    print(f"  {k:<16} 截距 {coef[0]:+.4f}%　β(個股) {coef[1]:+.3f}　β(0050) {coef[2]:+.3f}　"
          f"原始量加權毛 {wm(d):+.4f}%　n={len(d):,}")
print("\n=== 判準4d 逐日毛邊際 t 檢定（等權日）===")
for k,d in SUBS.items():
    g=d.groupby("trade_date").apply(lambda x: wm(x),include_groups=False).dropna()
    t=sps.ttest_1samp(g,0)
    print(f"  {k:<16} 日均 {g.mean():+.4f}%  正日 {(g>0).mean():.1%}  t={t.statistic:+.2f} p={t.pvalue:.2g}")
print("\n=== 補：依股價分層（tick size 效應）===")
for k,d in SUBS.items():
    out=[]
    for lo,hi in [(0,50),(50,200),(200,1000),(1000,10**9)]:
        x=d[(d.close>=lo)&(d.close<hi)]
        if len(x)<100: out.append(f"{lo}-{hi}:n/a"); continue
        out.append(f"{lo}-{hi}元 {wm(x):+.3f}%(n={len(x):,},名目{x.dt_noti.sum()/1e8:.0f}億)")
    print(f"  {k:<16} "+"  ".join(out))
print("\n=== 補：兩個獨立半段（穩定度）===")
dates=np.sort(m.trade_date.unique()); mid=dates[len(dates)//2]
for k,d in SUBS.items():
    h1,h2=d[d.trade_date<mid],d[d.trade_date>=mid]
    print(f"  {k:<16} 前半 {wm(h1):+.4f}% (名目{h1.dt_noti.sum()/1e8:.0f}億)　"
          f"後半 {wm(h2):+.4f}% (名目{h2.dt_noti.sum()/1e8:.0f}億)")
