#!/usr/bin/env python3
"""2026-08-11: finmind_tx_tick_by_day/ (used by every backtest cache-builder
tonight, e.g. tx_channel_build_august_fullnight.py) is a hand-built research
cache with no scheduled job behind it -- it stopped at 2026-08-07 simply
because nobody re-ran a builder script since then. This makes it self-
sustaining: idempotent, safe to run daily (research-only, no order-layer
touch), fetches any trading day missing from the cache up to "yesterday"
(today's tick data may still be forming) via the same FinMind TaiwanFuturesTick
call used throughout scripts/research/*build*.py, using stock_db's real
TAIEX trading calendar (not a naive weekday guess) to know which days should
exist.

Run standalone: PYTHONPATH=src .venv/bin/python scripts/research/tx_tick_daily_accumulate.py
Safe to re-run -- already-cached days are skipped (file existence check).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from finmind_client import fetch_finmind_json  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
LOOKBACK_DAYS = 10  # only look this far back for gaps -- older gaps are handled by one-off backfills, not this job


def missing_trading_days() -> list[str]:
    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT DISTINCT date FROM daily_bars WHERE code='IX0001' ORDER BY date"
    ).fetchall()
    conn.close()
    all_days = [d for (d,) in rows]
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    candidates = [d for d in all_days if cutoff <= d <= yesterday]
    return [d for d in candidates if not (TICK_DIR / f"{d}.json").exists()]


def fetch_and_cache(day: str) -> int:
    raw = fetch_finmind_json(
        {"dataset": "TaiwanFuturesTick", "data_id": "TX", "start_date": day, "end_date": day},
        timeout=180,
    )
    data = list(raw.get("data") or [])
    (TICK_DIR / f"{day}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(data)


def main() -> None:
    TICK_DIR.mkdir(parents=True, exist_ok=True)
    days = missing_trading_days()
    if not days:
        print("no gaps in the last %d days -- cache is current." % LOOKBACK_DAYS)
        return
    print(f"fetching {len(days)} missing day(s): {days}")
    for d in days:
        n = fetch_and_cache(d)
        print(f"  {d}: {n} ticks cached")
        time.sleep(0.35)


if __name__ == "__main__":
    main()
