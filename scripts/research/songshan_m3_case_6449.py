#!/usr/bin/env python3
"""M3 · 6449 案例逐筆審查（純研究 · DB 唯讀）.

協調者指定的關鍵反例：9217 於 2026-06-12 單日在 6449 買 1.684 億，其後該股腰斬，
四筆 L1H7 事件全負。檢查 H-C1 的建倉判準會不會把它判成「建倉型、值得跟」。

用法：
    PYTHONPATH=src .venv/bin/python scripts/research/songshan_m3_case_6449.py [股號...]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"
TRADER_ID = "9217"
BFS = ROOT / "reports" / "research" / "branch-footprint-screen"
LABELED = BFS / "songshan_m3_trades_labeled.csv"

COLS = [
    "signal_date", "stock_id", "entry_date", "exit_date", "r_adj_pct",
    "buy_5d", "net_ratio",
    "acc_buy_a", "acc_net_a", "acc_days_a", "grp_a",
    "acc_buy_b", "acc_net_b", "acc_days_b", "grp_b",
    "buy_sh", "sell_sh_next", "flip",
]


def main() -> None:
    targets = sys.argv[1:] or ["6449"]
    df = pd.read_csv(LABELED, dtype={"stock_id": str})
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 60)

    for sid in targets:
        sub = df[df["stock_id"] == sid]
        print(f"\n{'=' * 96}\n{sid} · {len(sub)} 筆事件\n{'=' * 96}")
        if sub.empty:
            continue
        show = sub[[c for c in COLS if c in sub.columns]].copy()
        for c in ("buy_5d", "acc_buy_a", "acc_buy_b"):
            if c in show:
                show[c] = (show[c] / 1e8).round(3)
        for c in ("acc_net_a", "acc_net_b", "net_ratio", "flip"):
            if c in show:
                show[c] = show[c].round(3)
        print(show.to_string(index=False))

    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    for sid in targets:
        tape = pd.read_sql_query(
            """
            SELECT b.trade_date, b.buy AS buy_sh, b.sell AS sell_sh, p.close,
                   b.buy*p.close/1e8 AS buy_yi, b.sell*p.close/1e8 AS sell_yi
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
            WHERE b.source=? AND b.securities_trader_id=? AND b.stock_id=?
            ORDER BY b.trade_date
            """,
            conn,
            params=(SOURCE, SOURCE, TRADER_ID, sid),
        )
        print(f"\n--- 9217 在 {sid} 的完整 tape（{len(tape)} 天有動作）")
        t = tape.round(3)
        print(t.to_string(index=False))
        print(f"    累計買 {tape['buy_yi'].sum():.2f} 億 / 賣 {tape['sell_yi'].sum():.2f} 億")


if __name__ == "__main__":
    main()
