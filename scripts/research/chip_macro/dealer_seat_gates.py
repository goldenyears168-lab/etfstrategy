"""Final 3 validation gates on the 3 cost-beating 自營 desks (2210/960T/980T).

Gate 1 CONCENTRATION: is the +2.6% broad or a few lottery tickets?
   mean vs median sel10, hit-rate (% sel10>0), trimmed mean (drop top/bottom 5%),
   contribution of the top-decile trades.
Gate 2 REGIME/OOS: split buy-days by market regime (mkt_bull) and by time (IS/OOS halves).
   Does the edge shrink/flip when not in a bull market?
Gate 3 HEDGING PROXY: using aggregate per-stock dealer_self vs dealer_hedge (stock_institutional_side_daily,
   NOT per-seat — best available), split the seat's buy-days into self-dominant vs hedge-dominant and
   compare the edge. Edge in hedge-dominant days => more of a hedging-flow signal.
"""
from __future__ import annotations

import sqlite3
import numpy as np
import pandas as pd

DB = "data/replica/stocks.db"
OUT = "data/research/chip_macro"
DEALERS = {"2210": "群益期貨", "960T": "富邦自營", "980T": "元大自營"}

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
tape = pd.read_sql_query(
    "select securities_trader_id as seat, trade_date as date, stock_id, net "
    f"from stock_broker_branch_daily where securities_trader_id in ({','.join('?'*len(DEALERS))}) and net>0",
    con, params=tuple(DEALERS))
univ = tuple(sorted(tape["stock_id"].unique()))
px = pd.read_sql_query(
    "select stock_id, trade_date as date, open, close, adj_close from stock_daily_bars "
    f"where trade_date>='2024-01-01' and stock_id in ({','.join('?'*len(univ))})", con, params=univ)
# dealer self vs hedge (aggregate per stock-day) — check inst_name values
self_n = ["Dealer_self"]      # domestic dealer proprietary (directional)
hedge_n = ["Dealer_Hedging"]  # domestic dealer hedging
side = pd.read_sql_query(
    "select stock_id, trade_date as date, inst_name, net from stock_institutional_side_daily "
    f"where trade_date>='2024-01-01' and stock_id in ({','.join('?'*len(univ))})", con, params=univ)
con.close()
print("inst_names self=", self_n, " hedge=", hedge_n, flush=True)

px["adj_close"] = px["adj_close"].fillna(px["close"])
px = px.sort_values(["stock_id", "date"])
g = px.groupby("stock_id", group_keys=False)
px["adj_open"] = px["open"] * (px["adj_close"] / px["close"])
px["fwd10"] = g["adj_close"].shift(-10) / g["adj_open"].shift(-1) - 1.0
px["xs10"] = px.groupby("date")["fwd10"].transform("mean")
px["sel10"] = px["fwd10"] - px["xs10"]
# market regime per date (TAIEX MA150 + rising)
tx = pd.read_parquet(f"{OUT}/panel.parquet")[["date", "ix_close"]].sort_values("date")
mma = tx["ix_close"].rolling(150, min_periods=150).mean()
tx["mkt_bull"] = (tx["ix_close"] > mma) & ((mma - mma.shift(25)) > 0)

sside = side[side["inst_name"].isin(self_n)].groupby(["stock_id", "date"])["net"].sum().rename("d_self")
hside = side[side["inst_name"].isin(hedge_n)].groupby(["stock_id", "date"])["net"].sum().rename("d_hedge")

d = (tape.merge(px[["stock_id", "date", "sel10", "fwd10"]], on=["stock_id", "date"])
     .merge(tx[["date", "mkt_bull"]], on="date")
     .merge(sside, on=["stock_id", "date"], how="left").merge(hside, on=["stock_id", "date"], how="left"))
d = d.dropna(subset=["sel10"])
_ds = sorted(d["date"].unique())
med_date = _ds[len(_ds) // 2]

print("\n===== GATE 1: CONCENTRATION (sel10) =====")
for s, nm in DEALERS.items():
    x = d[d.seat == s]["sel10"]
    lo, hi = x.quantile(0.05), x.quantile(0.95)
    trim = x[(x >= lo) & (x <= hi)].mean()
    dec = x.quantile(0.9)
    top_share = x[x >= dec].sum() / x.sum() * 100
    print(f"  {nm}({s}) n={len(x)}: mean {x.mean()*100:+.2f}%  median {x.median()*100:+.2f}%  "
          f"hit%(>0) {(x>0).mean()*100:.0f}  trim5-95 {trim*100:+.2f}%  top-decile contributes {top_share:.0f}% of total")

print("\n===== GATE 2: REGIME / OOS =====")
for s, nm in DEALERS.items():
    x = d[d.seat == s]
    for lab, mask in [("bull", x.mkt_bull == True), ("non-bull", x.mkt_bull == False),
                      ("IS(early)", x.date < med_date), ("OOS(late)", x.date >= med_date)]:
        sub = x[mask]["sel10"]
        flag = " ⚠n<30" if len(sub) < 30 else ""
        print(f"  {nm}({s}) {lab:10s}: sel10 {sub.mean()*100:+.2f}%  n={len(sub)}{flag}")
    print()

print("===== GATE 3: HEDGING PROXY (aggregate dealer self vs hedge on buy-days) =====")
for s, nm in DEALERS.items():
    x = d[d.seat == s].copy()
    x["d_self"] = x["d_self"].fillna(0); x["d_hedge"] = x["d_hedge"].fillna(0)
    self_dom = x[x["d_self"].abs() >= x["d_hedge"].abs()]
    hedge_dom = x[x["d_hedge"].abs() > x["d_self"].abs()]
    print(f"  {nm}({s}): self-dom sel10 {self_dom['sel10'].mean()*100:+.2f}% (n={len(self_dom)})  |  "
          f"hedge-dom sel10 {hedge_dom['sel10'].mean()*100:+.2f}% (n={len(hedge_dom)})  |  "
          f"days self-dom {len(self_dom)/len(x)*100:.0f}%")
print("\ndone.")
