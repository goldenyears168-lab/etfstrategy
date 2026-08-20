#!/usr/bin/env python3
"""tickrev_t3 step 2 — turn the raw inventory into a per-day coverage table and
an explicit `usable_days` list for the sharded full-sample runner.

Applies the SAME rules the engine applies (imported semantics, re-implemented
here only as *counting*, never as a second copy of the trading logic):
  * `_dominant_outright_contract()` = highest-tick-count pure single-month
    contract_date over the WHOLE day file (spread quotes "AAAAAA/BBBBBB" are
    excluded first) -- verbatim rule from slow_cell_tick_latency_lab.py.
  * day session   = that contract's ticks in [08:45:00, 13:45:00] of D
  * night session = that contract's ticks >= 15:00:00 of D  PLUS  the SAME
    contract's ticks <= 05:00:00 of D+1 (midnight stitch)
  * build_bundle drops a session with < 60 ticks; simulate_block_tick returns
    immediately when the session has < 60 one-minute bars.
    => a day is "usable" iff at least one session survives BOTH.

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_coverage.py
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
RAW = LAB / "tickrev_t3_inventory_raw.json"
OUT = LAB / "tickrev_t3_coverage_days.json"

DAY_EXPECTED_MIN = 301   # 08:45..13:45 inclusive
NIGHT_EXPECTED_MIN = 840  # 15:00..05:00
MIN_TICKS = 60
MIN_BARS = 60


def dominant(contracts: dict) -> tuple[str | None, int, int]:
    """(dominant single-month contract_date, its row count, total single-month rows)."""
    single = {cd: c["n"] for cd, c in contracts.items() if "/" not in cd}
    if not single:
        return None, 0, 0
    cd = max(single.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return cd, single[cd], sum(single.values())


def main() -> None:
    raw = json.loads(RAW.read_text())
    dates = sorted(raw)
    days: dict[str, dict] = {}

    for d in dates:
        rec = raw[d]
        if "error" in rec:
            days[d] = {"date": d, "status": "read_error", "detail": rec["error"], "usable": False}
            continue
        contracts = rec.get("contracts", {})
        n_rows = rec.get("n_rows", 0)
        if n_rows == 0 or not contracts:
            days[d] = {"date": d, "status": "empty_file", "n_rows": n_rows, "usable": False,
                       "bytes": rec["bytes"]}
            continue
        ref, ref_n, single_total = dominant(contracts)
        if ref is None:
            days[d] = {"date": d, "status": "spread_only", "n_rows": n_rows, "usable": False}
            continue
        c = contracts[ref]

        # night tail from D+1's file, same contract label
        d1 = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        nxt = raw.get(d1, {})
        nxt_c = (nxt.get("contracts") or {}).get(ref, {}) if "error" not in nxt else {}
        tail_n = nxt_c.get("night_tail_n", 0)
        tail_min = nxt_c.get("night_tail_min", 0)

        # union over all single-month contracts, to measure how much of the real
        # session the dominant-contract filter throws away (roll days)
        u_day_n = sum(x["day_n"] for cd, x in contracts.items() if "/" not in cd)
        u_night_n = sum(x["night_head_n"] for cd, x in contracts.items() if "/" not in cd)

        day_ticks, day_bars = c["day_n"], c["day_min"]
        night_ticks = c["night_head_n"] + tail_n
        night_bars = c["night_head_min"] + tail_min  # head/tail minute labels can collide in principle
        day_ok = day_ticks >= MIN_TICKS and day_bars >= MIN_BARS
        night_ok = night_ticks >= MIN_TICKS and night_bars >= MIN_BARS

        weekday = dt.date.fromisoformat(d).strftime("%a")
        days[d] = {
            "date": d, "weekday": weekday, "status": "ok",
            "bytes": rec["bytes"], "n_rows": n_rows,
            "dominant_contract": ref,
            "dominant_rows": ref_n,
            "dominant_share_of_single_month": round(ref_n / single_total, 4) if single_total else None,
            "n_single_month_contracts": sum(1 for cd in contracts if "/" not in cd),
            "n_spread_series": sum(1 for cd in contracts if "/" in cd),
            "spread_rows": sum(x["n"] for cd, x in contracts.items() if "/" in cd),
            "day_ticks": day_ticks, "day_bars": day_bars,
            "day_bar_coverage_pct": round(100.0 * day_bars / DAY_EXPECTED_MIN, 1),
            "day_tick_share_of_all_outright": round(day_ticks / u_day_n, 4) if u_day_n else None,
            "night_ticks": night_ticks, "night_bars": night_bars,
            "night_head_ticks": c["night_head_n"], "night_tail_ticks": tail_n,
            "night_bar_coverage_pct": round(100.0 * night_bars / NIGHT_EXPECTED_MIN, 1),
            "night_tick_share_of_all_outright": round(c["night_head_n"] / u_night_n, 4) if u_night_n else None,
            "next_file_present": d1 in raw,
            "day_session_ok": day_ok, "night_session_ok": night_ok,
            "usable": day_ok or night_ok,
            "sessions_usable": [s for s, ok in (("day", day_ok), ("night", night_ok)) if ok],
        }

    OUT.write_text(json.dumps(days, indent=1, ensure_ascii=False))
    usable = [d for d in dates if days[d].get("usable")]
    print(f"files={len(dates)}  usable_days={len(usable)}")
    from collections import Counter
    print(Counter(days[d]["status"] for d in dates))
    print(f"day-session usable={sum(1 for d in dates if days[d].get('day_session_ok'))} "
          f"night-session usable={sum(1 for d in dates if days[d].get('night_session_ok'))}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
