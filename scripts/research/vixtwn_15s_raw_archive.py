#!/usr/bin/env python3
"""2026-08-11: archive VIXTWN's raw ~15s TaiwanOptionVix prints (day session
only, 09:00-13:45) instead of throwing away sub-minute resolution by
aggregating straight to 1m bars like sync_vixtwn_1m.py does. Companion to
tmf_second_quote_collector.py / nq_second_quote_collector.py for the
"N-second acceleration" research -- unlike those two, this is NOT a live
poller: FinMind's TaiwanOptionVix is queryable for any past date after the
fact, so this just fetches+archives a given date's full raw series in one
shot. Re-run daily (or after each day session closes) to keep building the
archive; does not need to run continuously.

Writes ~/goldenstocks-data/cache/tmf_channel/tick_seconds/vixtwn_raw_{date}.jsonl
  one line per FinMind tick: {"date":"2026-08-11","time":"09:00:15","vix":18.23}

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/vixtwn_15s_raw_archive.py            # today
  PYTHONPATH=src .venv/bin/python scripts/research/vixtwn_15s_raw_archive.py --date 2026-08-08
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")

from finmind_client import fetch_finmind_json  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

OUT_DIR = DATA_DIR.parent / "cache" / "tmf_channel" / "tick_seconds"


def fetch_raw_ticks(day: str) -> list[dict]:
    js = fetch_finmind_json({"dataset": "TaiwanOptionVix", "start_date": day, "end_date": day})
    return list(js.get("data") or [])


def main() -> int:
    ap = argparse.ArgumentParser(description="Archive raw 15s VIXTWN ticks for one day")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    day = args.date

    ticks = fetch_raw_ticks(day)
    if not ticks:
        print(f"{day}: no VIXTWN ticks (weekend/holiday, or day session hasn't happened/closed yet)")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"vixtwn_raw_{day}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for t in ticks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"{day}: wrote {len(ticks)} raw ticks -> {out_path}")
    if ticks:
        print(f"  span: {ticks[0].get('time')} .. {ticks[-1].get('time')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
