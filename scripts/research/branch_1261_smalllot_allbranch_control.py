from pathlib import Path
import numpy as np, pandas as pd
from stock_db import connect_ro
DIR = Path("reports/research/chip-signal-daily-horizon")
c=connect_ro()
px=pd.read_sql_query("SELECT stock_id,trade_date,source,open,high,low,close,volume/1000.0 vol FROM stock_daily_bars WHERE trade_date>='2025-01-01' AND close>0",c)
px["rk"]=px.source.map({"finmind":0,"twse_mi_index":1,"tpex_daily":2}).fillna(9)
px=px.sort_values("rk").drop_duplicates(["stock_id","trade_date"]).drop(columns=["rk","source"])
def load(tid):
    f=DIR/f"branch_{tid}_pricelevels.pkl"
    if not f.exists(): return None
    d=pd.read_pickle(f).drop_duplicates(["stock_id","trade_date"])
    d=d[(d.trade_date>="2025-01-01")]
    d["buy_vwap"]=d.buy_amt/d.buy_vol.replace(0,np.nan); d["sell_vwap"]=d.sell_amt/d.sell_vol.replace(0,np.nan)
    m=d.merge(px,on=["stock_id","trade_date"],how="inner",validate="one_to_one")
    m["dt_sh"]=m[["buy_vol","sell_vol"]].min(axis=1); m["dt_lot"]=m.dt_sh/1000.
    m["dt_pnl"]=(m.sell_vwap-m.buy_vwap)*m.dt_sh
    m["dt_noti"]=m.dt_sh*(m.buy_vwap+m.sell_vwap)/2
    m["spread"]=(m.sell_vwap/m.buy_vwap-1)*100
    m["rt"]=m.dt_sh/m[["buy_vol","sell_vol"]].max(axis=1).replace(0,np.nan)
    m["rng"]=(m.high-m.low).replace(0,np.nan)
    m["buy_pos"]=(m.buy_vwap-m.low)/m.rng; m["sell_pos"]=(m.sell_vwap-m.low)/m.rng
    return m.dropna(subset=["buy_vwap","sell_vwap"]).query("dt_sh>0")
def wm(d): return d.dt_pnl.sum()/d.dt_noti.sum()*100 if d.dt_noti.sum() else np.nan
def od(d):
    k=d.groupby("trade_date").size(); return k.var()/k.mean() if len(k)>10 else np.nan
TIDS=["1261","9661","8888","9225","9217","884M","981M","9B2Y","9268","9800","1480","1650","5110"]
print("共同窗期 2025-01-01 起。『<=2張』= 當沖張數<=2 的子群 —— 檢定它是否是 1261 特有")
print(f"{'分點':<7}{'全分點毛%':>11}{'rt>.95毛%':>11}{'倍數':>7}{'<=2張毛%':>11}{'rt>.95&<=2張':>13}"
      f"{'<=2張佔名目':>12}{'過離散(rt>.95)':>14}{'日均檔':>8}")
rows=[]
for t in TIDS:
    m=load(t)
    if m is None or len(m)<500: print(f"{t:<7} 無資料"); continue
    s=m[m.rt>0.95]; sm=m[m.dt_lot<=2]; ss=m[(m.rt>0.95)&(m.dt_lot<=2)]
    rows.append({"tid":t,"all":wm(m),"pure":wm(s),"small":wm(sm),"ps":wm(ss),
                 "share":sm.dt_noti.sum()/m.dt_noti.sum()*100,"od":od(s),
                 "perday":m.groupby("trade_date").size().mean(),
                 "buy_pos_s":ss.buy_pos.median(),"sell_pos_s":ss.sell_pos.median(),
                 "n_s":len(ss)})
    r=rows[-1]
    print(f"{t:<7}{r['all']:>+11.4f}{r['pure']:>+11.4f}{r['pure']/r['all']:>7.2f}"
          f"{r['small']:>+11.4f}{r['ps']:>+13.4f}{r['share']:>11.1f}%{r['od']:>14.2f}{r['perday']:>8.0f}")
d=pd.DataFrame(rows)
print(f"\n『當沖<=2張』毛邊際：13 個分點中 {(d.small>0).sum()}/{len(d)} 為正，中位 {d.small.median():+.4f}%")
print(f"『rt>.95&<=2張』：{(d.ps>0).sum()}/{len(d)} 為正，中位 {d.ps.median():+.4f}%")
print(f"1261 在『<=2張』的排名：{(d.small>d[d.tid=='1261'].small.iloc[0]).sum()+1}/{len(d)}")
print(f"\n小單子群買位/賣位（1261 vs 同儕）")
for r in rows: print(f"  {r['tid']:<6} 買位 {r['buy_pos_s']:.3f} 賣位 {r['sell_pos_s']:.3f} n={r['n_s']:,}")
