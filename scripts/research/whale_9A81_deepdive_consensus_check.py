#!/usr/bin/env python3
"""whale_9A81_deepdive: 對 9A81 的 7 檔安靜建倉候選股，查同期間是否有其他分點共識進場。

跑在 mini（有完整分點資料）：
  PYTHONPATH=src python3 scripts/research/whale_9A81_deepdive_consensus_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"

# (stock_id, first_date, last_date) — 建倉期間，取自 quiet_accumulators_candidates.csv
CANDIDATES = [
    ("2379", "2025-10-21", "2026-07-13"),
    ("6531", "2025-10-22", "2026-07-24"),
    ("2049", "2025-10-21", "2026-07-08"),
    ("3034", "2025-10-21", "2026-07-13"),
    ("3406", "2025-10-21", "2026-07-13"),
    ("3702", "2025-10-27", "2026-07-13"),
    ("2451", "2025-10-21", "2026-07-17"),
]


def main() -> None:
    conn = connect(DEFAULT_DB_PATH)
    conn.execute("PRAGMA busy_timeout=30000")

    all_rows = []
    for stock_id, first_date, last_date in CANDIDATES:
        print(f"[INFO] querying stock {stock_id} window {first_date}~{last_date}", flush=True)
        q = """
            SELECT b.securities_trader_id, b.securities_trader,
                   SUM(b.buy) AS total_buy_shares,
                   SUM(b.sell) AS total_sell_shares,
                   SUM(b.net) AS total_net_shares,
                   SUM(b.net * p.close) AS total_net_amt,
                   SUM(b.buy * p.close) AS total_buy_amt,
                   COUNT(*) AS n_days_active,
                   SUM(CASE WHEN b.net > 0 THEN 1 ELSE 0 END) AS n_net_buy_days
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = ?
            WHERE b.source = ? AND b.stock_id = ?
              AND b.trade_date >= ? AND b.trade_date <= ?
            GROUP BY b.securities_trader_id, b.securities_trader
        """
        df = pd.read_sql_query(q, conn, params=(SOURCE, SOURCE, stock_id, first_date, last_date))
        df["stock_id"] = stock_id
        df["window_first"] = first_date
        df["window_last"] = last_date
        all_rows.append(df)
        print(f"  -> {len(df)} distinct traders active", flush=True)

    result = pd.concat(all_rows, ignore_index=True)
    out_path = OUT_DIR / "whale_9A81_deepdive_consensus_raw.csv"
    result.to_csv(out_path, index=False)
    print(f"[INFO] wrote {out_path} ({len(result):,} rows)")


if __name__ == "__main__":
    main()
