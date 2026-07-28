#!/usr/bin/env python3
"""針對已知 stock_daily_bars 缺口，補回 FinMind TaiwanStockPrice + TaiwanStockPriceAdj.

缺口清單來自 reports/research/branch-footprint-screen/coverage_gap_audit_full.csv
（此次分點籌碼研究發現的資料品質問題）。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_stock_daily_bars_gaps.py
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_stock_daily_bars_gaps.py --stock-ids 8358,1815
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_daily_bars  # noqa: E402

SOURCE = "finmind"

# (stock_id, start, end) — 依 coverage_gap_audit_full.csv 的缺口窗口，兩端各加5天緩衝
GAPS: list[tuple[str, str, str]] = [
    ("8358", "2019-12-25", "2023-01-05"),
    ("1815", "2019-12-25", "2023-01-05"),
    ("6147", "2019-12-25", "2023-01-05"),
    ("3006", "2019-01-01", "2025-06-10"),
    ("6141", "2019-01-01", "2026-07-20"),
    ("6269", "2019-01-01", "2026-07-20"),
    ("8213", "2019-01-01", "2026-07-20"),
]


def _float_or_none(v):
    if v is None or v == "":
        return None
    return float(v)


def _int_or_none(v):
    if v is None or v == "":
        return None
    return int(float(v))


def backfill_one(conn, stock_id: str, start: date, end: date, delay: float) -> int:
    price_rows = fetch_finmind("TaiwanStockPrice", stock_id, start, end)
    time.sleep(delay)
    try:
        adj_rows = fetch_finmind("TaiwanStockPriceAdj", stock_id, start, end)
    except requests.HTTPError:
        adj_rows = []
    time.sleep(delay)
    adj_by_date = {str(r["date"])[:10]: float(r["close"]) for r in adj_rows if r.get("close") is not None}

    bars = []
    for row in price_rows:
        trade_date = str(row["date"])[:10]
        close = _float_or_none(row.get("close"))
        if close is None:
            continue
        bars.append(
            {
                "stock_id": stock_id,
                "trade_date": trade_date,
                "open": _float_or_none(row.get("open")),
                "high": _float_or_none(row.get("max")),
                "low": _float_or_none(row.get("min")),
                "close": close,
                "adj_close": adj_by_date.get(trade_date),
                "volume": _int_or_none(row.get("Trading_Volume") or row.get("volume")),
                "amount": _float_or_none(row.get("Trading_money")),
                "source": SOURCE,
            }
        )
    if bars:
        upsert_stock_daily_bars(conn, bars)
    return len(bars)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock-ids", default="", help="逗號分隔，預設處理全部已知缺口清單")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    gaps = GAPS
    if args.stock_ids:
        wanted = {s.strip() for s in args.stock_ids.split(",") if s.strip()}
        gaps = [g for g in gaps if g[0] in wanted]

    conn = connect(DEFAULT_DB_PATH)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        for stock_id, start_s, end_s in gaps:
            start = date.fromisoformat(start_s)
            end = date.fromisoformat(end_s)
            print(f"[{stock_id}] 回補 {start}..{end} ...")
            try:
                n = backfill_one(conn, stock_id, start, end, args.delay)
                print(f"  [OK] {stock_id}: {n} 筆寫入")
            except Exception as exc:  # noqa: BLE001
                print(f"  [ERROR] {stock_id}: {exc}", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
