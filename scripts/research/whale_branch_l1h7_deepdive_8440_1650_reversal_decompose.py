#!/usr/bin/env python3
"""Decompose the 8440/1650 reversal: gap effect, cost effect, beta effect, holding-period effect."""
import sys
from pathlib import Path
ROOT = Path("/Users/jackm4/Documents/ETF/股票研究")
sys.path.insert(0, str(ROOT / "src"))
import numpy as np
import pandas as pd
from stock_db import connect, DEFAULT_DB_PATH

EVENTS = ROOT / "reports/research/branch-footprint-screen/whale_branch_l1h7_universe_events.csv"
ev = pd.read_csv(EVENTS)
ev["securities_trader_id"] = ev["securities_trader_id"].astype(str)
target = ev[ev["securities_trader_id"].isin(["8440", "1650"])].copy()
print("event counts:", target.groupby("securities_trader_id").size())

conn = connect(DEFAULT_DB_PATH)

stock_ids = sorted(target["stock_id"].astype(str).unique())
placeholders = ",".join("?" * len(stock_ids))
q = f"""
    SELECT stock_id, trade_date, open, close FROM stock_daily_bars
    WHERE source='finmind' AND stock_id IN ({placeholders}) AND trade_date >= '2024-05-01'
    ORDER BY stock_id, trade_date
"""
bars = pd.read_sql_query(q, conn, params=[str(s) for s in stock_ids])
bars["stock_id"] = bars["stock_id"].astype(str)
bars["open"] = bars["open"].where(bars["open"] > 0, bars["close"])

ix = pd.read_sql_query(
    "SELECT date, open, close FROM daily_bars WHERE code='IX0001' AND date>='2024-05-01' AND open>0 AND close>0 ORDER BY date",
    conn,
)
ix = ix.drop_duplicates(subset="date").reset_index(drop=True)
ix_dates = ix["date"].tolist()
ix_idx = {d: i for i, d in enumerate(ix_dates)}
ix_close = ix.set_index("date")["close"]

# Build per-stock date index maps
by_stock = {sid: g.reset_index(drop=True) for sid, g in bars.groupby("stock_id")}
stock_date_idx = {sid: {d: i for i, d in enumerate(g["trade_date"])} for sid, g in by_stock.items()}

target["stock_id"] = target["stock_id"].astype(str)

rows = []
for _, r in target.iterrows():
    sid = str(r["stock_id"])
    sig = r["signal_date"]
    g = by_stock.get(sid)
    idxmap = stock_date_idx.get(sid, {})
    if g is None or sig not in idxmap:
        continue
    i0 = idxmap[sig]
    # signal-day close (old entry price)
    sig_close = g["close"].iloc[i0]
    if i0 + 1 >= len(g):
        continue
    next_open = g["open"].iloc[i0 + 1]
    next_date = g["trade_date"].iloc[i0 + 1]
    # sanity: next_open should equal r['entry_open'] and next_date == r['entry_date']
    # old-style exit: signal-day-close entry + 9 trading days close (i0+9)
    if i0 + 9 >= len(g):
        old9_close = np.nan
        old9_date = None
    else:
        old9_close = g["close"].iloc[i0 + 9]
        old9_date = g["trade_date"].iloc[i0 + 9]
    # gap effect: next_open vs sig_close
    gap_pct = (next_open / sig_close - 1.0) * 100 if sig_close > 0 else np.nan

    # old-style benchmark (IX close-to-close from signal date to signal+9 trading days), 1x beta, no cost
    if sig in ix_idx:
        bi0 = ix_idx[sig]
        if bi0 + 9 < len(ix_dates):
            b0 = ix_close.iloc[bi0]
            b1 = ix_close.iloc[bi0 + 9]
            old_bench_ret = (b1 / b0 - 1.0) * 100
        else:
            old_bench_ret = np.nan
    else:
        old_bench_ret = np.nan

    old_stock_ret = (old9_close / sig_close - 1.0) * 100 if (sig_close > 0 and not pd.isna(old9_close)) else np.nan
    old_excess = old_stock_ret - old_bench_ret if not (pd.isna(old_stock_ret) or pd.isna(old_bench_ret)) else np.nan

    rows.append({
        "trader": r["securities_trader_id"], "stock_id": sid, "signal_date": sig,
        "sig_close": sig_close, "next_open": next_open, "next_date": next_date,
        "entry_open_csv": r["entry_open"], "entry_date_csv": r["entry_date"],
        "gap_pct": gap_pct,
        "old9_close": old9_close, "old9_date": old9_date,
        "old_stock_ret_pct": old_stock_ret, "old_bench_ret_pct": old_bench_ret, "old_excess_pct": old_excess,
        "new_r_pct": r["r_pct"], "new_r_ix_pct": r["r_ix_pct"], "new_r_adj_pct": r["r_adj_pct"],
    })

out = pd.DataFrame(rows)
print(out.shape)
out.to_csv("/private/tmp/claude-501/-Users-jackm4-Documents-ETF-----/f97d9a02-5e0d-4667-abe1-f63712a134cf/scratchpad/decompose_8440_1650_events.csv", index=False)

for trader, g in out.groupby("trader"):
    print("\n===", trader, "n=", len(g))
    print("mean gap_pct (signal close -> next open):", g["gap_pct"].mean().round(3))
    print("median gap_pct:", g["gap_pct"].median().round(3))
    print("pct of events with negative gap (whale bought into a down-gap next day... wait sign):", (g["gap_pct"]<0).mean().round(3))
    print("old-style (close entry, h9, no cost, 1x beta) mean excess:", g["old_excess_pct"].mean().round(3), "median:", g["old_excess_pct"].median().round(3), "win_rate:", (g["old_excess_pct"]>0).mean().round(3))
    print("new L1H7 r_pct (with cost) mean:", g["new_r_pct"].mean().round(3), "median:", g["new_r_pct"].median().round(3))
    print("new L1H7 r_adj_pct (cost+1.15beta) mean:", g["new_r_adj_pct"].mean().round(3), "median:", g["new_r_adj_pct"].median().round(3))
    # decompose new_r_adj vs old_excess
    # cost effect: new_r_pct + 0.3 approx = pre-cost stock return (close-to-close from next-day-open... not same base)
    # beta effect isolated:
    beta_effect = (-0.15 * g["new_r_ix_pct"])
    print("mean beta-only incremental effect vs 1x (should already be in r_adj-r_pct+r_ix):", beta_effect.mean().round(3))
