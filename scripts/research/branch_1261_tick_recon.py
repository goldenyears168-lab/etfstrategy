"""1261 逐筆重建 —— 依【名目】分層（與應變數 spread 無關，非循環）+ 每層隨機抽樣。"""
import sys, time
from datetime import date
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0,"scripts/research")
from finmind_client import fetch_finmind, fetch_taiwan_stock_trading_daily_report
from branch_tick_reconstruct import analyse
DIR=Path("reports/research/chip-signal-daily-horizon")
TID="1261"; rng=np.random.default_rng(7)
m=pd.read_pickle(DIR/f"branch_{TID}_joined.pkl")
m=m[(m.dt_sh>0)&(m.trade_date>="2026-01-01")].dropna(subset=["spread"]).copy()
s=m[m.rt>0.95].copy()
s["q"]=pd.qcut(s.dt_noti,3,labels=["名目小","名目中","名目大"])
picks=[]
for k,g in s.groupby("q",observed=True):
    idx=rng.choice(len(g),size=min(70,len(g)),replace=False)
    x=g.iloc[idx].copy(); x["stratum"]=k; picks.append(x)
# 另加全分點的名目最大層（追高殺低嫌疑犯）
big=m[m.dt_noti>=m.dt_noti.quantile(.99)]
idx=rng.choice(len(big),size=min(60,len(big)),replace=False)
x=big.iloc[idx].copy(); x["stratum"]="全分點P99名目"; picks.append(x)
todo=pd.concat(picks,ignore_index=True)
print(f"抽 {len(todo)} 個案例（2026 年，依名目分層＋層內隨機）",flush=True)
rows=[]; t0=time.time()
for i,r in enumerate(todo.itertuples()):
    try:
        lv=pd.DataFrame(fetch_taiwan_stock_trading_daily_report(trade_date=r.trade_date,data_id=r.stock_id))
        lv=lv[lv.securities_trader_id==TID]
        for c in ("price","buy","sell"): lv[c]=pd.to_numeric(lv[c],errors="coerce")
        tk=pd.DataFrame(fetch_finmind("TaiwanStockPriceTick",r.stock_id,
                        date.fromisoformat(r.trade_date),date.fromisoformat(r.trade_date)))
        a=analyse(lv,tk)
        if a: rows.append({"stratum":r.stratum,"stock_id":r.stock_id,"trade_date":r.trade_date,
                           "spread":r.spread,"dt_noti":r.dt_noti,"buy_pos":r.buy_pos,
                           "sell_pos":r.sell_pos,"dt_lot":r.dt_lot,**a})
    except Exception as e: pass
    time.sleep(0.45)
    if i%40==39: print(f"  {i+1}/{len(todo)} {(time.time()-t0)/60:.1f}分",flush=True)
o=pd.DataFrame(rows); o.to_pickle(DIR/f"branch_{TID}_tick_recon.pkl")
print(f"\n成功 {len(o)}\n")
print(f"{'層':<14}{'n':>4}{'價差%':>9}{'買時點':>9}{'賣時點':>9}{'買內盤%':>10}{'賣內盤%':>10}{'市場內盤%':>11}{'買位':>7}{'賣位':>7}{'佔量%':>8}")
for k in ["名目小","名目中","名目大","全分點P99名目"]:
    g=o[o.stratum==k]
    if g.empty: continue
    print(f"{k:<12}{len(g):>4}{g.spread.median():>+8.3f}%{g.buy_t.median():>9.3f}{g.sell_t.median():>9.3f}"
          f"{g.buy_inner.median()*100:>9.1f}%{g.sell_inner.median()*100:>9.1f}%{g.mkt_inner.median()*100:>10.1f}%"
          f"{g.buy_pos.median():>7.3f}{g.sell_pos.median():>7.3f}{g.buy_share.median()*100:>7.2f}%")
print("\n買時點>賣時點 的比例（先賣後買=做空當沖）:",f"{(o.buy_t>o.sell_t).mean():.1%}")
print("時點不確定度中位:",f"{o.buy_t_unc.median():.3f}")
