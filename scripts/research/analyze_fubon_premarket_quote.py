#!/usr/bin/env python3
"""富邦盤前試撮 quote 收集器 · 收盤後準度分析（研究用 · 每天累積一筆歷史對照）。

比較 ``fubon_premarket_quote_snapshot``（08:30-09:00 試撮，見
``collect_fubon_premarket_quote.py``）最後一筆試撮價，跟當天實際開盤/最高/最低/
收盤（FinMind ``TaiwanStockPrice``）的差距，並把結果累積寫入
``reports/research/fubon_premarket_quote_accuracy.csv``（每個交易日+股票一列，
idempotent：同一天重跑會覆蓋舊列，不會重複累加）。

用法（收盤後執行，一般 .venv 即可，不需要 .venv-fubon／不需要富邦登入）：
    PYTHONPATH=src .venv/bin/python scripts/research/analyze_fubon_premarket_quote.py \\
        --date 2026-07-23
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind_dataset  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

_OPEN_CUTOFF = "09:00:00"
_CSV_PATH = ROOT / "reports" / "research" / "fubon_premarket_quote_accuracy.csv"
_CSV_FIELDS = [
    "trade_date",
    "stock_id",
    "last_trial_price",
    "last_trial_ts",
    "last_trial_size",
    "actual_open",
    "open_error",
    "open_error_pct",
    "actual_high",
    "actual_low",
    "actual_close",
    "is_fade_day",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="富邦盤前試撮 quote 收盤後準度分析")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="交易日（預設：今天）")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return p.parse_args()


def _last_trial_before_open(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT stock_id, last_trial_price, last_trial_ts, last_trial_size, poll_ts
        FROM fubon_premarket_quote_snapshot
        WHERE trade_date = ?
          AND substr(poll_ts, 12, 8) < ?
          AND last_trial_price IS NOT NULL
        ORDER BY stock_id, poll_ts
        """,
        (trade_date, _OPEN_CUTOFF),
    )
    latest: dict[str, sqlite3.Row] = {}
    for row in cur.fetchall():
        latest[row["stock_id"]] = row  # 保持最後一筆（依 poll_ts 遞增排序）
    return list(latest.values())


def _actual_ohlc(stock_id: str, trade_date: str) -> dict | None:
    day = datetime.strptime(trade_date, "%Y-%m-%d").date()
    try:
        rows = fetch_finmind_dataset("TaiwanStockPrice", data_id=stock_id, start=day, end=day)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] {stock_id} FinMind 查詢失敗：{exc}")
        return None
    return rows[0] if rows else None


def main() -> int:
    args = _parse_args()
    trade_date = args.date or date_cls.today().strftime("%Y-%m-%d")

    if not args.db.exists():
        print(f"[ERROR] database not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        trials = _last_trial_before_open(conn, trade_date)
    finally:
        conn.close()

    if not trials:
        print(f"[WARN] {trade_date} 沒有任何盤前試撮資料（fubon_premarket_quote_snapshot）。")
        return 0

    results: list[dict] = []
    for row in trials:
        stock_id = row["stock_id"]
        actual = _actual_ohlc(stock_id, trade_date)
        if actual is None:
            print(f"[WARN] {stock_id} 無實際日 K 資料，略過。")
            continue
        trial_price = float(row["last_trial_price"])
        actual_open = float(actual["open"])
        error = actual_open - trial_price
        error_pct = (error / trial_price * 100) if trial_price else None
        actual_close = float(actual["close"])
        results.append(
            {
                "trade_date": trade_date,
                "stock_id": stock_id,
                "last_trial_price": trial_price,
                "last_trial_ts": row["last_trial_ts"],
                "last_trial_size": row["last_trial_size"],
                "actual_open": actual_open,
                "open_error": round(error, 4),
                "open_error_pct": round(error_pct, 3) if error_pct is not None else None,
                "actual_high": actual["max"],
                "actual_low": actual["min"],
                "actual_close": actual_close,
                "is_fade_day": int(actual_close < actual_open),
            }
        )

    if not results:
        print(f"[WARN] {trade_date} 沒有可比對的結果（可能是 FinMind 當天資料還沒到位）。")
        return 0

    print(
        f"{'stock':<8}{'trial':>10}{'trial_ts':>16}{'open':>10}{'誤差':>10}"
        f"{'誤差%':>9}{'高':>10}{'低':>10}{'收':>10}{'開高走低':>10}"
    )
    for r in results:
        print(
            f"{r['stock_id']:<8}{r['last_trial_price']:>10}{(r['last_trial_ts'] or '')[11:19]:>16}"
            f"{r['actual_open']:>10}{r['open_error']:>10}{r['open_error_pct']:>9}"
            f"{r['actual_high']:>10}{r['actual_low']:>10}{r['actual_close']:>10}"
            f"{'是' if r['is_fade_day'] else '否':>10}"
        )

    _CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[tuple[str, str], dict] = {}
    if _CSV_PATH.exists():
        with _CSV_PATH.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[(row["trade_date"], row["stock_id"])] = row
    for r in results:
        existing[(r["trade_date"], r["stock_id"])] = {k: r[k] for k in _CSV_FIELDS}
    ordered = sorted(existing.values(), key=lambda r: (r["trade_date"], r["stock_id"]))
    with _CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(ordered)
    print(f"[OK] 已累積寫入 {_CSV_PATH}（目前共 {len(ordered)} 列）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
