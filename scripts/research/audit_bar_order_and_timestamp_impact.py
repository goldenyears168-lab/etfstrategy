#!/usr/bin/env python3
"""Quantify how much the 2026-08-11 calendar-attribution fixes move existing
TMF channel backtest numbers.

Two independent defects were fixed; this script isolates each one.

DEFECT A — bar ORDER (the bigger one).
  `cache_store.load_day()` read from bars.sqlite with `ORDER BY t`, a plain
  lexicographic sort. For a "session"-convention source (tx_1m_fullnight_cache*)
  the 00:00-04:59 night tail is calendar day+1 and belongs at the END of the
  session, but lexicographic sort hoists it to the FRONT — so simulate() saw the
  last 5 hours of the night session BEFORE the day session of the same key.
  26% of all bars, every run made after bars.sqlite was materialised (2026-08-09).

DEFECT B — bar TIMESTAMP.
  `T = f"{day}T{t}:00.000+08:00"` is 24h early for those same tail bars, so the
  per-bar NQ/ES gate (continuous_gate_for_day) read futures from the wrong day.

Variants compared (same recipe, same days):
  legacy_order  : rows sorted lexicographically by t   (pre-fix behaviour)
  fixed_order   : rows in true chronological order      (post-fix)
  legacy_ts     : T = f"{day}T{t}..."                   (pre-fix)
  fixed_ts      : T = cache_store.bar_timestamps(...)   (post-fix)

Read-only: no DB writes, no order submit, no network unless --with-gate.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/audit_bar_order_and_timestamp_impact.py
  PYTHONPATH=src .venv/bin/python scripts/research/audit_bar_order_and_timestamp_impact.py --limit 10
  PYTHONPATH=src .venv/bin/python scripts/research/audit_bar_order_and_timestamp_impact.py --with-gate
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import (  # noqa: E402
    bar_timestamps,
    list_days,
    load_day,
)
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

SOURCE = "tx_1m_fullnight_cache_full.json"
OUT = Path("reports/research/channel_lab/audit_bar_order_and_timestamp_impact.json")
IS_JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]


def arrays(day: str, *, order: str, ts: str):
    rows = load_day(day, source=SOURCE)  # already chronological post-fix
    if not rows:
        return None
    if order == "legacy":
        rows = sorted(rows, key=lambda r: str(r.get("t") or ""))
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    if ts == "legacy":
        T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    else:
        T = bar_timestamps(day, rows, source=SOURCE)
    return O, H, L, C, V, T


def net_for(day, recipe, vix, *, order, ts, gate_fn=None):
    arr = arrays(day, order=order, ts=ts)
    if arr is None:
        return None
    O, H, L, C, V, T = arr
    rec = deepcopy(recipe)
    if gate_fn is not None:
        rec["session_side_gate"] = gate_fn(day, T)
    trades, *_ = simulate(O, H, L, C, V, T, rec, vix_delta=vix)
    return {
        "n_trades": len(trades),
        "net": round(sum(t["pnl"] for t in trades), 1),
        "n_night_tail_trades": sum(
            1 for t in trades if str(t.get("et", ""))[11:16] < "05:00"
        ),
    }


def summarize(rows, key_a, key_b, label):
    pairs = [(r[key_a]["net"], r[key_b]["net"]) for r in rows
             if r.get(key_a) and r.get(key_b)]
    if not pairs:
        return {}
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    diffs = [y - x for x, y in pairs]
    n = len(diffs)
    mean_d = st.fmean(diffs)
    std_d = st.stdev(diffs) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 else 0.0
    n_changed = sum(1 for d in diffs if abs(d) > 1e-9)
    out = {
        "label": label,
        "n_days": n,
        f"{key_a}_sum": round(sum(a), 1),
        f"{key_b}_sum": round(sum(b), 1),
        "delta_sum": round(sum(diffs), 1),
        "delta_mean_per_day": round(mean_d, 2),
        "delta_std": round(std_d, 2),
        "t": round(t_stat, 3),
        "n_days_changed": n_changed,
        "pct_days_changed": round(100.0 * n_changed / n, 1),
        "max_abs_day_delta": round(max((abs(d) for d in diffs), default=0.0), 1),
    }
    print(f"\n--- {label} ---")
    print(f"  n_days={n}  {key_a}_sum={out[f'{key_a}_sum']}  "
          f"{key_b}_sum={out[f'{key_b}_sum']}  delta={out['delta_sum']}")
    print(f"  per-day delta mean={out['delta_mean_per_day']} t={out['t']}  "
          f"days changed={n_changed}/{n} ({out['pct_days_changed']}%)  "
          f"max |day delta|={out['max_abs_day_delta']}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--with-gate", action="store_true",
                    help="also measure DEFECT B (needs NQ/ES data, may hit network)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    days = list_days(source=SOURCE)
    if args.limit:
        days = days[: args.limit]
    print(f"days={len(days)} [{days[0]} .. {days[-1]}] source={SOURCE}")

    rows = []
    for day in days:
        r = {"day": day}
        r["legacy_order"] = net_for(day, recipe, vix, order="legacy", ts="legacy")
        r["fixed_order"] = net_for(day, recipe, vix, order="fixed", ts="fixed")
        rows.append(r)

    report = {"source": SOURCE, "n_days": len(rows), "per_day": rows, "summaries": {}}
    report["summaries"]["defect_A_bar_order_all_days"] = summarize(
        rows, "legacy_order", "fixed_order",
        "DEFECT A: bar order (legacy lexicographic -> chronological), no gate")

    is_rows = [r for r in rows if r["day"] in set(IS_JULY_DAYS)]
    oos_rows = [r for r in rows if r["day"] < "2026-07-08"]
    if is_rows:
        report["summaries"]["defect_A_IS_july17d"] = summarize(
            is_rows, "legacy_order", "fixed_order", "DEFECT A on the 17 July IS days")
    if oos_rows:
        report["summaries"]["defect_A_OOS_66d"] = summarize(
            oos_rows, "legacy_order", "fixed_order", "DEFECT A on the 66-day OOS window")

    if args.with_gate:
        from tmf_continuous_gate_vs_frozen_anchor import (
            continuous_gate_for_day,
            patch_nq_gate_for_backfill,
        )

        patch_nq_gate_for_backfill(lookback_days=500)

        def gate(day, T):
            # allow_stale: this audit intentionally feeds the pre-fix timestamps
            # through to measure how far off they were.
            return continuous_gate_for_day(day, T, allow_stale=True)

        grows = []
        for day in days:
            g = {"day": day}
            g["legacy_ts"] = net_for(day, recipe, vix, order="fixed", ts="legacy",
                                     gate_fn=gate)
            g["fixed_ts"] = net_for(day, recipe, vix, order="fixed", ts="fixed",
                                    gate_fn=gate)
            grows.append(g)
        report["per_day_gate"] = grows
        report["summaries"]["defect_B_gate_timestamp_all_days"] = summarize(
            grows, "legacy_ts", "fixed_ts",
            "DEFECT B: NQ/ES gate timestamp (24h-stale -> real), chronological order")
        gis = [r for r in grows if r["day"] in set(IS_JULY_DAYS)]
        goos = [r for r in grows if r["day"] < "2026-07-08"]
        if gis:
            report["summaries"]["defect_B_IS_july17d"] = summarize(
                gis, "legacy_ts", "fixed_ts", "DEFECT B on the 17 July IS days")
        if goos:
            report["summaries"]["defect_B_OOS_66d"] = summarize(
                goos, "legacy_ts", "fixed_ts", "DEFECT B on the 66-day OOS window")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
