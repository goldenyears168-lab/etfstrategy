#!/usr/bin/env python3
"""2026-08-17: one-off backfill of TX raw ticks for the 2021-12~2022-03 window
(TAIEX topped 2022-01-05 at ~18619, then rolled into the 2022 bear leg,
with the Russia-Ukraine shock in late Feb 2022) -- research-only, no
order-layer touch. Companion to tx_tick_daily_accumulate.py's daily-gap
version, but for an explicit historical range instead of "last 10 days".

Purpose: the existing tx_1m_fullnight_cache_full.json / tx_1m_tick_built_fullnight_aug
caches only cover 2026 (a single bull-market regime). This pulls a genuine
down-market window so intraday signals (e.g. night-session / 52-week-high
research) can be cross-checked outside that one regime.

Writes to the same finmind_tx_tick_by_day/ dir as tx_tick_daily_accumulate.py,
same file format, so tx_1m_bars_daily_accumulate.py's resample logic can be
reused as-is once this is done (just point it at this date range).

Run: PYTHONPATH=src .venv/bin/python scripts/research/tx_tick_backfill_2022bear.py
Idempotent -- already-cached days are skipped (file existence check).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from finmind_client import fetch_finmind_json  # noqa: E402
from stock_db import connect  # noqa: E402

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
START = "2021-12-01"
END = "2022-03-31"


def target_trading_days() -> list[str]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_bars WHERE code='IX0001' AND date>=? AND date<=? ORDER BY date",
            (START, END),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


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
    days = [d for d in target_trading_days() if not (TICK_DIR / f"{d}.json").exists()]
    if not days:
        print(f"no gaps in {START}~{END} -- cache is current.")
        return
    print(f"fetching {len(days)} day(s) in {START}~{END}")
    for i, d in enumerate(days):
        try:
            n = fetch_and_cache(d)
            print(f"  [{i+1}/{len(days)}] {d}: {n} ticks cached", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}/{len(days)}] {d}: FETCH_ERR {str(exc)[:120]}", flush=True)
        time.sleep(0.35)
    print("DONE")


if __name__ == "__main__":
    main()
