from pathlib import Path
import numpy as np, pandas as pd
from scipy import stats as sps
DIR = Path("reports/research/chip-signal-daily-horizon")
m = pd.read_pickle(DIR/"branch_1261_joined.pkl")
m = m[m.dt_sh>0].dropna(subset=["buy_vwap","sell_vwap","spread"]).copy()
m["bl"]=m.buy_vol/1000.; m["sl"]=m.sell_vol/1000.
m["lvl_lot"]=(m.buy_vol+m.sell_vol)/1000./m.n_lvl
def wm(d): return d.dt_pnl.sum()/d.dt_noti.sum()*100 if d.dt_noti.sum() else np.nan
def od(d):
    k=d.groupby("trade_date").size()
    return k.var()/k.mean() if len(k)>10 and k.mean()>0 else np.nan
print(f"樣本 {len(m):,} stock-day / {m.trade_date.nunique()} 日 / {m.stock_id.nunique()} 檔")
print("\n=== 判準3 過離散度 var/mean（各層）===")
subs={"全分點":m,"rt>0.95 純當沖":m[m.rt>0.95],"rt>0.99":m[m.rt>0.99],
      "rt<0.3 純方向":m[m.rt<0.3],"當沖<=2張":m[m.dt_lot<=2],"當沖>20張":m[m.dt_lot>20],
      "名目P90+":m[m.dt_noti>=m.dt_noti.quantile(.9)],
      "rt>0.95&<=2張":m[(m.rt>0.95)&(m.dt_lot<=2)],
      "rt>0.95&名目P90+":m[(m.rt>0.95)&(m.dt_noti>=m[m.rt>0.95].dt_noti.quantile(.9))]}
print(f"{'層':<20}{'n':>8}{'日均':>7}{'var/mean':>10}{'毛邊際%':>10}{'名目億':>9}{'整張%':>8}{'CV(檔內張)':>11}")
for k,d in subs.items():
    if len(d)<50: continue
    per=d.groupby("trade_date").size()
    whole=(np.isclose(d.bl%1,0,atol=1e-6)&np.isclose(d.sl%1,0,atol=1e-6)).mean()*100
    print(f"{k:<20}{len(d):>8,}{per.mean():>7.1f}{od(d):>10.2f}{wm(d):>+10.4f}"
          f"{d.dt_noti.sum()/1e8:>9.1f}{whole:>8.1f}{d.lvl_lot.std()/d.lvl_lot.mean():>11.2f}")
print("\n=== 判準1 單筆規格化 ===")
for k,d in subs.items():
    if len(d)<50: continue
    b=d.loc[d.bl>0,"bl"]
    md=b.mode()
    print(f"  {k:<20} =1張 {(b==1).mean()*100:>5.1f}%  <=2張 {(b<=2).mean()*100:>5.1f}%  "
          f"整張 {(d.buy_vol[d.buy_vol>0]%1000==0).mean()*100:>5.1f}%  "
          f"中位 {b.median():>6.1f} p90 {b.quantile(.9):>7.1f} 眾數 {md.iloc[0]:.2f}×{(b==md.iloc[0]).mean()*100:.1f}%")
print("\n=== 判準2 子群毛邊際 vs 全分點（量加權）===")
base=wm(m)
for k,d in subs.items():
    if len(d)<50: continue
    print(f"  {k:<20}{wm(d):>+9.4f}%　倍數 {wm(d)/base:>6.2f}×　"
          f"{'✓ 更好' if (wm(d)>base) else '✗ 更差'}")
print("\n=== 標的持續性 ===")
for lab,d in [("全分點",m),("rt>0.95",m[m.rt>0.95])]:
    days=sorted(d.trade_date.unique()); ss={k:set(g.stock_id) for k,g in d.groupby("trade_date")}
    ov=[len(ss[days[i]]&ss[days[i+1]])/max(len(ss[days[i]]),1) for i in range(len(days)-1)]
    vc=d.stock_id.value_counts()
    print(f"  {lab}: 次日重疊 {np.mean(ov):.1%}　檔數 {d.stock_id.nunique()}　"
          f"Top10 佔 {vc.head(10).sum()/len(d):.1%}　只1次 {(vc==1).sum()/len(vc):.1%}")
print("\n=== 名目集中度（誰在主導損益）===")
q=m.dt_noti.quantile([.5,.9,.99])
for lab,d in [("P99+",m[m.dt_noti>=q[.99]]),("P90-99",m[(m.dt_noti>=q[.9])&(m.dt_noti<q[.99])]),
              ("P50-90",m[(m.dt_noti>=q[.5])&(m.dt_noti<q[.9])]),("<P50",m[m.dt_noti<q[.5]])]:
    print(f"  {lab:<8} n={len(d):>6,} 名目 {d.dt_noti.sum()/1e8:>6.1f}億 "
          f"({d.dt_noti.sum()/m.dt_noti.sum()*100:>5.1f}%) 毛 {wm(d):+.4f}% "
          f"毛額 {d.dt_pnl.sum()/1e4:>+9.0f} 萬 勝率 {(d.spread>0).mean()*100:.1f}%")
