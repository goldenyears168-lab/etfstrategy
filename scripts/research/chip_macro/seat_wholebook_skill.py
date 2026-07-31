"""全書技能檢定 — does a branch seat's buying beat market & same-day random picks across its WHOLE book?

For seats 9661 (富邦新店) and 9217 (凱基松山): take every (stock, date) net-buy across ALL
stocks they trade (2024-07..2026-07), attach the stock's forward return (enter next open),
and measure three things:
  ABS      = mean forward return on buy-days (absolute)
  MKT-ADJ  = mean(pick_fwd - TAIEX_fwd)                 -> excess over just holding the index
  SEL-ALPHA= mean(pick_fwd - same-date cross-sectional mean of ALL stocks)
             -> pure SELECTION skill: did its picks beat random picks made the SAME days?
Significance on SEL-ALPHA via DATE-CLUSTERED bootstrap (resample trading dates w/ replacement)
so cross-sectionally correlated same-day returns don't inflate the t-stat.
Also a per-date equal-weight portfolio Sharpe of "follow all buys, hold 10d" (overlapping, indicative).
"""
from __future__ import annotations

import sqlite3
import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
DB = "data/replica/stocks.db"
OUT = "data/research/chip_macro"
SEATS = {"9661": "富邦新店", "9217": "凱基松山"}
H = [5, 10]

print("1) extracting seat buy tape (slow, unindexed scan)...", flush=True)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
tape = pd.read_sql_query(
    "select securities_trader_id as seat, trade_date as date, stock_id, net "
    "from stock_broker_branch_daily where securities_trader_id in ('9661','9217') and net>0",
    con)
print(f"   buy-instances: {len(tape):,}  seats={tape['seat'].value_counts().to_dict()}", flush=True)

print("2) loading prices for the traded universe...", flush=True)
univ = tuple(sorted(tape["stock_id"].unique()))
q = ("select stock_id, trade_date as date, open, close, adj_close from stock_daily_bars "
     f"where trade_date>='2024-01-01' and stock_id in ({','.join('?'*len(univ))})")
px = pd.read_sql_query(q, con, params=univ)
con.close()
print(f"   price rows: {len(px):,}  stocks: {px['stock_id'].nunique()}", flush=True)

# forward returns per stock (enter next open, div-adj w/ fallback to raw close)
px["adj_close"] = px["adj_close"].fillna(px["close"])
px = px.sort_values(["stock_id", "date"])
adj = px["adj_close"] / px["close"]
px["adj_open"] = px["open"] * adj
g = px.groupby("stock_id", group_keys=False)
entry = g["adj_open"].shift(-1)
for h in H:
    px[f"fwd{h}"] = g["adj_close"].shift(-h) / entry - 1.0

# market (TAIEX) forward returns, same convention
tx = pd.read_parquet(f"{OUT}/panel.parquet")[["date", "ix_open", "ix_close"]].sort_values("date")
for h in H:
    tx[f"tx_fwd{h}"] = tx["ix_close"].shift(-h) / tx["ix_open"].shift(-1) - 1.0

# same-date cross-sectional mean of ALL traded stocks (breadth baseline)
xs = px.groupby("date")[[f"fwd{h}" for h in H]].mean().rename(
    columns={f"fwd{h}": f"xs_fwd{h}" for h in H}).reset_index()

picks = (tape.merge(px[["stock_id", "date"] + [f"fwd{h}" for h in H]], on=["stock_id", "date"])
         .merge(tx[["date"] + [f"tx_fwd{h}" for h in H]], on="date")
         .merge(xs, on="date"))

def boot_dates(df, col, nb=1000):
    dates = df["date"].unique()
    by = {d: sub[col].values for d, sub in df.groupby("date")}
    means = np.empty(nb)
    for i in range(nb):
        samp = rng.choice(dates, len(dates), replace=True)
        vals = np.concatenate([by[d] for d in samp])
        means[i] = vals.mean()
    return np.quantile(means, [0.025, 0.5, 0.975]), (means <= 0).mean()

print("\n" + "=" * 70)
for seat, nm in SEATS.items():
    d = picks[picks["seat"] == seat].dropna(subset=[f"fwd{h}" for h in H])
    print(f"\n===== {nm} ({seat}) — 全書技能檢定 =====")
    print(f"  buy-instances w/ fwd: {len(d):,}  distinct stocks: {d['stock_id'].nunique()}  dates: {d['date'].nunique()}")
    for h in H:
        abs_m = d[f"fwd{h}"].mean()
        mktadj = (d[f"fwd{h}"] - d[f"tx_fwd{h}"]).mean()
        d[f"sel{h}"] = d[f"fwd{h}"] - d[f"xs_fwd{h}"]
        sel_m = d[f"sel{h}"].mean()
        ci, p = boot_dates(d, f"sel{h}")
        sig = "SIG" if (ci[0] > 0 or ci[2] < 0) else "ns"
        print(f"  fwd{h:<2}: ABS {abs_m*100:+.2f}%  MKT-ADJ {mktadj*100:+.2f}%  "
              f"SEL-ALPHA {sel_m*100:+.2f}% (date-boot 95%CI [{ci[0]*100:+.2f},{ci[2]*100:+.2f}] p={p:.3f} {sig})")
    # indicative portfolio: per-date equal-weight pick fwd10 -> daily series
    port = d.groupby("date")["fwd10"].mean()
    if port.std() > 0:
        print(f"  per-date EW pick fwd10 series: mean {port.mean()*100:+.2f}%  "
              f"Sharpe(ann/√252 on {len(port)} dates) {port.mean()/port.std()*np.sqrt(252/10):+.2f}")
print("\ndone.")
