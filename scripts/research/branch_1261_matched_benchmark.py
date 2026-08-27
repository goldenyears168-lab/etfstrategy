"""同股同日配對：1261 的 P99+ 名目列，與 8888/9661/9225 在同一 stock-day 的價差比較。"""
from pathlib import Path
import numpy as np, pandas as pd
from scipy import stats
from stock_db import connect_ro
DIR=Path("reports/research/chip-signal-daily-horizon")
c=connect_ro()
px=pd.read_sql_query("SELECT stock_id,trade_date,source,open,high,low,close FROM stock_daily_bars WHERE trade_date>='2025-01-01' AND close>0",c)
px["rk"]=px.source.map({"finmind":0,"twse_mi_index":1,"tpex_daily":2}).fillna(9)
px=px.sort_values("rk").drop_duplicates(["stock_id","trade_date"]).drop(columns=["rk","source"])
def load(t):
    d=pd.read_pickle(DIR/f"branch_{t}_pricelevels.pkl").drop_duplicates(["stock_id","trade_date"])
    d=d[d.trade_date>="2025-01-01"].copy()
    d["bv"]=d.buy_amt/d.buy_vol.replace(0,np.nan); d["sv"]=d.sell_amt/d.sell_vol.replace(0,np.nan)
    d["spread"]=(d.sv/d.bv-1)*100
    d["mn"]=d[["buy_vol","sell_vol"]].min(axis=1)
    d["rt"]=d.mn/d[["buy_vol","sell_vol"]].max(axis=1).replace(0,np.nan)
    return d[d.mn>0].dropna(subset=["spread"])
a=load("1261").merge(px,on=["stock_id","trade_date"])
a["noti"]=a.mn*(a.bv+a.sv)/2
key=a[a.noti>=a.noti.quantile(.99)][["stock_id","trade_date","spread","noti"]]
print(f"1261 名目P99+ {len(key)} 列，中位價差 {key.spread.median():+.3f}%，勝率 {(key.spread>0).mean():.1%}")
for t in ["8888","9661","9225","9217","884M","9800"]:
    b=load(t)[["stock_id","trade_date","spread","rt"]].rename(columns={"spread":"sp_b"})
    j=key.merge(b,on=["stock_id","trade_date"],how="inner")
    if len(j)<50: print(f"  {t}: 配對不足 {len(j)}"); continue
    w=stats.wilcoxon(j.spread,j.sp_b)
    print(f"  {t}: 配對 {len(j)} 列　1261 {j.spread.median():+.3f}% vs {t} {j.sp_b.median():+.3f}%　"
          f"差 {(j.spread-j.sp_b).median():+.3f}pp　Wilcoxon p={w.pvalue:.2g}　"
          f"{t}勝率 {(j.sp_b>0).mean():.1%}")
print("\n=== 同樣配對，但用 1261 全樣本 ===")
allk=a[["stock_id","trade_date","spread"]]
for t in ["8888","9661","9225","884M"]:
    b=load(t)[["stock_id","trade_date","spread"]].rename(columns={"spread":"sp_b"})
    j=allk.merge(b,on=["stock_id","trade_date"],how="inner")
    w=stats.wilcoxon(j.spread,j.sp_b)
    print(f"  {t}: 配對 {len(j):,} 列　1261 {j.spread.median():+.4f}% vs {t} {j.sp_b.median():+.4f}%　"
          f"差 {(j.spread-j.sp_b).median():+.4f}pp　p={w.pvalue:.2g}")
