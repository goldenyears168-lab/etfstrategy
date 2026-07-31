"""Materialize clean analysis panels for the branch-follow interaction study.

For each target stock (景碩 3189, 頎邦 6147) produce:
  <code>_trend.parquet : daily trend context
     date, close, adj_close, fwd1/3/5/10 (close-to-close, ENTER next open),
     stk_stage (S1/2/3/4), rs (stock/TAIEX), rs_up, mkt_bull, mkt_L0
  <code>_seats.parquet : full seat footprint  (date, seat_id, seat_name, net)

Return convention (baked in so agents don't reinvent it):
  signal known after close t -> ENTER open(t+1). fwd_h = adj_close(t+h)/... but
  for a next-open entry we approximate with open(t+1): fwd_h = close(t+h)/open(t+1)-1.
  We store fwd on the SIGNAL row t, so agents just do panel[signal].fwdH.mean().
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
DB = "data/replica/stocks.db"
OUT = ROOT / "data" / "research" / "chip_macro"
TX = pd.read_parquet(OUT / "panel.parquet")[["date", "ix_close", "fut_foreign_oi"]]
TARGETS = {"3189": "景碩", "6147": "頎邦"}


def build(code: str):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    px = pd.read_sql_query(
        "select trade_date as date, open, close, adj_close from stock_daily_bars "
        "where stock_id=? order by trade_date", con, params=(code,))
    seats = pd.read_sql_query(
        "select trade_date as date, securities_trader_id as seat_id, "
        "securities_trader as seat_name, net from stock_broker_branch_daily where stock_id=?",
        con, params=(code,))
    con.close()

    d = px.merge(TX.rename(columns={"ix_close": "tx"}), on="date", how="left").sort_values("date").reset_index(drop=True)
    # FIX: adj_close is null for many stocks (e.g. 6147) -> fall back to raw close.
    # Dividend effect is negligible over 5-10d horizons; trend/stage need a continuous series.
    d["adj_close"] = d["adj_close"].fillna(d["close"])
    # adjust factor for open (adj_close/close ratio) so open is dividend-adjusted too
    adj = d["adj_close"] / d["close"]
    d["adj_open"] = d["open"] * adj
    # forward returns: enter open(t+1), exit close(t+h)  (dividend-adjusted)
    entry = d["adj_open"].shift(-1)
    for h in (1, 3, 5, 10):
        d[f"fwd{h}"] = d["adj_close"].shift(-h) / entry - 1.0
    # stock Weinstein stage (on adj_close)
    ma150 = d["adj_close"].rolling(150, min_periods=150).mean()
    up = (ma150 - ma150.shift(25)) > 0
    above = d["adj_close"] > ma150
    d["stk_stage"] = np.select(
        [above & up, above & ~up, ~above & ~up, ~above & up],
        ["S2_adv", "S3_top", "S4_dec", "S1_base"], default="?")
    # Mansfield-style RS vs TAIEX
    rs = d["adj_close"] / d["tx"]
    d["rs"] = rs
    d["rs_up"] = rs > rs.rolling(50, min_periods=50).mean()
    # market regime (from index panel)
    mma = d["tx"].rolling(150, min_periods=150).mean()
    z60 = (d["fut_foreign_oi"] - d["fut_foreign_oi"].rolling(60, min_periods=60).mean()) \
        / d["fut_foreign_oi"].rolling(60, min_periods=60).std()
    d["mkt_bull"] = (d["tx"] > mma) & ((mma - mma.shift(25)) > 0)
    d["mkt_L0"] = d["mkt_bull"] & (z60 > 0)

    trend = d[["date", "close", "adj_close", "fwd1", "fwd3", "fwd5", "fwd10",
               "stk_stage", "rs", "rs_up", "mkt_bull", "mkt_L0"]]
    trend.to_parquet(OUT / f"{code}_trend.parquet", index=False)
    seats.to_parquet(OUT / f"{code}_seats.parquet", index=False)
    nseats = seats["seat_id"].nunique()
    print(f"{code} {TARGETS[code]}: trend {len(trend)} rows ({trend['date'].min()}..{trend['date'].max()}), "
          f"seats {len(seats):,} rows / {nseats} distinct seats")


if __name__ == "__main__":
    for c in TARGETS:
        build(c)
    print("panels ->", OUT)
