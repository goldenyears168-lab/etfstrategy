#!/usr/bin/env python3
"""Baseline/placebo check for study_whale_9661_holding_momentum_forward40.py.

Question: the ACCEL vs DECEL transition-signal backtest showed both categories with similarly
negative beta-adjusted forward returns (~-2.3% to -2.6%, first-signal-per-stock, H20/H40). Is this
a universe-wide/period-wide baseline tilt (e.g. beta=1.15 mis-calibration for a small/mid-cap
basket vs IX0001 in this window), or something specific to momentum-transition dates?

Placebo: for the SAME stock universe (the 373 stocks with usable price history), sample one FLAT
(non-signal, cum>0) day per stock at a fixed stride (every 20th eligible day, first one only) and
compute the same L1H20/L1H40 beta-adjusted forward return. Compare distribution to the transition
event results.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
TRADER_ID = "9661"
COST = 0.003
BETA = 1.15
DATA_START = "2024-07-01"
HORIZONS = [20, 40]
MIN_TOTAL_ACTIVE_DAYS = 20
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def find_stock_universe(conn) -> list[str]:
    q = """
        SELECT stock_id, COUNT(*) as n_active
        FROM stock_broker_branch_daily
        WHERE source = ? AND securities_trader_id = ? AND trade_date >= ?
          AND (buy > 0 OR sell > 0)
        GROUP BY stock_id
        HAVING COUNT(*) >= ?
    """
    df = pd.read_sql_query(q, conn, params=(SOURCE, TRADER_ID, DATA_START, MIN_TOTAL_ACTIVE_DAYS))
    return sorted(df["stock_id"].tolist())


def load_ix(conn) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code='IX0001' AND date >= ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (DATA_START,),
    ).fetchall()
    out = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    dates = sorted(out)
    return pd.DataFrame({"date": dates, "open": [out[d][0] for d in dates], "close": [out[d][1] for d in dates]})


def load_stock_bars(conn, stock_ids):
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source='{SOURCE}' AND stock_id IN ({placeholders})
          AND trade_date >= ? AND close > 0
        ORDER BY stock_id, trade_date
    """
    df = pd.read_sql_query(q, conn, params=(*stock_ids, DATA_START))
    df["open"] = df["open"].where(df["open"] > 0, df["close"])
    return df


def load_branch_daily(conn, stock_ids):
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, net
        FROM stock_broker_branch_daily
        WHERE source='{SOURCE}' AND securities_trader_id = ? AND stock_id IN ({placeholders})
          AND trade_date >= ?
    """
    return pd.read_sql_query(q, conn, params=(TRADER_ID, *stock_ids, DATA_START))


def build_hold_lookup(dates, opens, closes, hold):
    n = len(dates)
    rows = []
    for sig_i in range(n - 1):
        entry_i = sig_i + 1
        exit_i = entry_i + hold - 1
        if exit_i >= n:
            continue
        rows.append((dates[sig_i], dates[entry_i], opens[entry_i], dates[exit_i], closes[exit_i]))
    return pd.DataFrame(rows, columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close"])


def main():
    conn = connect(DEFAULT_DB_PATH)
    stock_ids = find_stock_universe(conn)
    bars_all = load_stock_bars(conn, stock_ids)
    branch_all = load_branch_daily(conn, stock_ids)
    ix = load_ix(conn)
    conn.close()

    ix_lookups = {h: build_hold_lookup(ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy(), h).set_index("signal_date") for h in HORIZONS}

    # IX0001 own total return over the window for context
    ix_total_ret = ix["close"].iloc[-1] / ix["close"].iloc[0] - 1
    print(f"[INFO] IX0001 total return {DATA_START}..{ix['date'].iloc[-1]}: {ix_total_ret*100:.1f}%")

    STRIDE = 20
    placebo_rows = []
    n_eligible_stocks = 0
    for sid, g in bars_all.groupby("stock_id"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) < 90:
            continue
        br = branch_all[branch_all["stock_id"] == sid].set_index("trade_date")["net"]
        br = br.reindex(g["trade_date"]).fillna(0.0)
        cum = br.cumsum().to_numpy()
        dates = g["trade_date"].to_numpy()
        opens = g["open"].to_numpy()
        closes = g["close"].to_numpy()
        lookups = {h: build_hold_lookup(dates, opens, closes, h).set_index("signal_date") for h in HORIZONS}
        n_eligible_stocks += 1
        # sample every STRIDE-th day where cum>0, starting at index 60 (skip warmup)
        for i in range(60, len(dates), STRIDE):
            if cum[i] <= 0:
                continue
            sig_date = dates[i]
            rec = {"stock_id": sid, "signal_date": sig_date}
            ok = True
            for h in HORIZONS:
                lk = lookups.get(h)
                ixlk = ix_lookups.get(h)
                if lk is None or ixlk is None or sig_date not in lk.index or sig_date not in ixlk.index:
                    ok = False
                    break
            if not ok:
                continue
            for h in HORIZONS:
                lkr = lookups[h].loc[sig_date]
                ixr = ix_lookups[h].loc[sig_date]
                r_s = lkr["exit_close"] / lkr["entry_open"] - 1 - COST
                r_ix = ixr["exit_close"] / ixr["entry_open"] - 1
                rec[f"h{h}_r_adj_pct"] = (r_s - BETA * r_ix) * 100
            placebo_rows.append(rec)

    placebo = pd.DataFrame(placebo_rows)
    print(f"[INFO] eligible stocks for placebo: {n_eligible_stocks}, placebo obs: {len(placebo)}")
    placebo.to_csv(OUT_DIR / "whale_9661_momentum_placebo_baseline.csv", index=False)

    for h in HORIZONS:
        vals = placebo[f"h{h}_r_adj_pct"].dropna().to_numpy()
        t_stat, p_t = stats.ttest_1samp(vals, 0.0)
        w_stat, p_w = stats.wilcoxon(vals)
        print(
            f"[PLACEBO] H{h}: n={len(vals)} mean={vals.mean():.3f}% median={np.median(vals):.3f}% "
            f"win_rate={ (vals>0).mean()*100:.1f}% t_p={p_t:.4f} wilcoxon_p={p_w:.2e}"
        )
        # also first-per-stock version
        first_only = placebo.sort_values("signal_date").drop_duplicates("stock_id", keep="first")
        vals2 = first_only[f"h{h}_r_adj_pct"].dropna().to_numpy()
        t_stat2, p_t2 = stats.ttest_1samp(vals2, 0.0)
        w_stat2, p_w2 = stats.wilcoxon(vals2)
        print(
            f"[PLACEBO first-per-stock] H{h}: n={len(vals2)} mean={vals2.mean():.3f}% median={np.median(vals2):.3f}% "
            f"win_rate={ (vals2>0).mean()*100:.1f}% t_p={p_t2:.4f} wilcoxon_p={p_w2:.2e}"
        )


if __name__ == "__main__":
    main()
