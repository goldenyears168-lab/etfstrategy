#!/usr/bin/env python3
"""R5 task 3: query the LIVE production gate right now, exactly as the
running worker would (same module, same get_cached 30-min throttle, no
patching, no wide-window backfill override), and report what it returns
plus the freshness of the underlying NQ/ES 1h bars it used -- to confirm
the 2026-08-08 stale-cache incident (frozen since 2026-08-05, silently
reporting "none" for days) is not recurring in some other form after fix
B+C.

Read-only, makes a real network call to Yahoo Finance via the same code
path nq_gate.py uses (get_cached will reuse a real recent fetch if one
happened in the last 30 minutes within this process's cache namespace --
here it's a fresh process so it will fetch).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import tmf_channel.nq_gate as nq_gate  # noqa: E402

TZ_TW = ZoneInfo("Asia/Taipei")
OUT = ROOT / "reports/research/channel_lab/gt5_r5_live_gate_freshness_check_result.json"


def main() -> None:
    now_tw = datetime.now(tz=TZ_TW)
    today = now_tw.date().isoformat()
    hm = now_tw.strftime("%H:%M")

    side = nq_gate.nq_side_for_day(today, hm=hm)
    err = nq_gate.last_nq_load_error()

    bundle = nq_gate.get_cached("nq_futures_1h", 1800.0, nq_gate._load_futures_bundle)
    result = dict(
        queried_at_tw=now_tw.isoformat(), query_day=today, query_hm=hm,
        nq_side_for_day_result=side, last_nq_load_error=err,
    )
    if bundle is not None:
        nq_1h, es_1h, nq_d, es_d, us_dates = bundle
        result["bundle_latest_us_trade_date"] = max(us_dates) if us_dates else None
        result["bundle_n_us_dates"] = len(us_dates)
        result["nq_1h_latest_bar_start_et"] = str(nq_1h.index.max()) if len(nq_1h) else None
        result["es_1h_latest_bar_start_et"] = str(es_1h.index.max()) if len(es_1h) else None
        result["nq_1h_latest_bar_close_time_et"] = (
            str(nq_1h.index.max() + __import__("datetime").timedelta(hours=1)) if len(nq_1h) else None
        )
        result["nq_1h_n_bars"] = int(len(nq_1h))
        result["es_1h_n_bars"] = int(len(es_1h))
        now_et = now_tw.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
        result["query_time_et"] = str(now_et)
        if len(nq_1h):
            age = now_et - nq_1h.index.max()
            result["nq_1h_latest_bar_age_vs_query_time"] = str(age)
    else:
        result["bundle"] = None
        result["note"] = "bundle unavailable -- gate is fail-safe None, matching live worker behavior"

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
