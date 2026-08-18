#!/usr/bin/env python3
"""TMF channel · queue-aware tick replay — how much of the edge is queue luck?

Every result behind the live PV16 recipe (cell-tune v2 walk-forward, the
gt5_r5 four-window backtest, the 2026-08-08 audit re-runs) was produced with
``tick_native=False``: a resting limit order is considered filled the moment
a 1-minute bar's high/low *reaches* its price. For a strategy whose entire
alpha is passive limit orders parked 10–42 points away, that is the single
largest untested assumption in the stack — it silently grants front-of-queue
priority on every fill. The project's own CCF second-level study already
measured the opposite in a related contract: 82% of passive limit orders
never reached the front of their queue.

This script re-runs the *unchanged live recipe* over real TAIFEX trade prints
under progressively more honest fill rules and reports the spread:

  bar          — status quo. 1m bars, fill on touch. (the published number)
  tick_touch   — real ticks, fill on touch. Isolates "1m bar vs real tick
                 path" with the queue assumption still fully intact.
  tick_through — real ticks, fill only when a print goes strictly THROUGH the
                 rail. Certain fills only; needs no depth data, so it is not
                 affected by the TX-volume-proxy caveat. A lower bound.
  tick_queue/N — real ticks, fill once N lots have printed at the rail price
                 since it was placed (queue ahead eaten), or on a through
                 print. N sweeps the unknown queue depth.

Read the gap between ``bar`` and ``tick_through`` as the portion of reported
P&L that exists only if you are always first in line.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/research/tmf_queue_aware_replay.py \
        --days 60 --out reports/research/channel_lab/queue_aware_replay.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from copy import deepcopy
from pathlib import Path
from typing import Any

from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import RECIPE_VERSION
from tmf_channel.cache_store import load_day
from tmf_channel.engine import load_vixtwn_delta, simulate
from tmf_channel.tick_index import available_days, build_tick_index, coverage

BAR_SOURCE = "tx_1m_tick_built_582d"

MODELS: list[tuple[str, dict[str, Any]]] = [
    ("bar", {"tick_native": False}),
    ("tick_touch", {"tick_native": True, "fill_model": "touch"}),
    ("tick_through", {"tick_native": True, "fill_model": "through"}),
    ("tick_queue/5", {"tick_native": True, "fill_model": "queue", "queue_ahead_lots": 5}),
    ("tick_queue/20", {"tick_native": True, "fill_model": "queue", "queue_ahead_lots": 20}),
    ("tick_queue/50", {"tick_native": True, "fill_model": "queue", "queue_ahead_lots": 50}),
]


def bars_db() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"


def bar_days() -> list[str]:
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        return [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (BAR_SOURCE,)
            )
        ]
    finally:
        con.close()


def arrays_for(day: str):
    """Full-ISO timestamps, not the cache's bare HH:MM.

    causal_engine._day() slices [:10] off each timestamp to key its VIXTWN /
    gap-bias / session-gate lookups; feeding it "08:45" makes every one of
    those lookups miss silently. The live order layer always passes full ISO,
    so this also keeps the replay on the same code path production uses.
    """
    rows = load_day(day, source=BAR_SOURCE)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{r['cal']}T{r['t']}:00+08:00" for r in rows]
    return O, H, L, C, V, T


def run_model(arrays, tick_idx, overrides: dict[str, Any], vix) -> dict[str, Any]:
    O, H, L, C, V, T = arrays
    recipe = deepcopy(PAPER_RECIPE)
    recipe["hang_anchor"] = "O"
    recipe["eod_flatten"] = True  # research: close the book at session end
    recipe.update(overrides)
    idx = tick_idx if overrides.get("tick_native") else None
    trades, _events, _ws, _wl, _rvol, _regime, _open = simulate(
        O, H, L, C, V, T, recipe, vix_delta=vix, tick_index=idx
    )
    pnls = [float(t["pnl"]) for t in trades]
    return {
        "n": len(pnls),
        "net": round(sum(pnls), 1),
        "wins": sum(1 for x in pnls if x > 0),
        "entries_L": sum(1 for t in trades if t["s"] == "L"),
        "entries_S": sum(1 for t in trades if t["s"] == "S"),
    }


def aggregate(per_day: list[dict[str, Any]], model: str) -> dict[str, Any]:
    rows = [d["models"][model] for d in per_day if model in d.get("models", {})]
    nets = [r["net"] for r in rows]
    n = sum(r["n"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    if not rows:
        return {"days": 0}
    return {
        "days": len(rows),
        "trades": n,
        "net_pts": round(sum(nets), 1),
        "net_twd": round(sum(nets) * 10),  # micro TX = NT$10 / point
        "pts_per_day": round(sum(nets) / len(rows), 2),
        "pts_per_trade": round(sum(nets) / n, 2) if n else None,
        "trade_win_pct": round(100.0 * wins / n, 1) if n else None,
        "day_win_pct": round(100.0 * sum(1 for x in nets if x > 0) / len(nets), 1),
        "median_day": round(st.median(nets), 1),
        "worst_day": round(min(nets), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60, help="most recent N sessions")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD inclusive (overrides --days)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD inclusive")
    ap.add_argument("--out", default=None, help="write JSON result here")
    args = ap.parse_args()

    have_ticks = set(available_days())
    days = [d for d in bar_days() if d in have_ticks]
    if args.start:
        days = [d for d in days if d >= args.start]
    if args.end:
        days = [d for d in days if d <= args.end]
    if not args.start and args.days:
        days = days[-args.days :]
    if not days:
        print("no days with BOTH 1m bars and tick prints")
        return 1

    vix = load_vixtwn_delta() or {}
    per_day: list[dict[str, Any]] = []
    skipped: list[str] = []
    for day in days:
        arrays = arrays_for(day)
        if arrays is None:
            skipped.append(f"{day}:no_bars")
            continue
        T = arrays[5]
        tick_idx = build_tick_index(T)
        if tick_idx is None:
            skipped.append(f"{day}:no_ticks")
            continue
        cov = coverage(T, tick_idx)
        row: dict[str, Any] = {
            "day": day,
            "bars": len(T),
            "ticks": tick_idx.n_tk,
            "tick_coverage": cov,
            "models": {},
        }
        for name, ov in MODELS:
            row["models"][name] = run_model(arrays, tick_idx, ov, vix)
        per_day.append(row)
        print(
            f"{day}  bars={len(T):4d} ticks={tick_idx.n_tk:7d} cov={cov:.2f}  "
            + "  ".join(
                f"{n}={row['models'][n]['net']:+8.1f}({row['models'][n]['n']:2d})"
                for n, _ in MODELS
            ),
            flush=True,
        )

    summary = {name: aggregate(per_day, name) for name, _ in MODELS}
    result = {
        "schema": "tmf-queue-aware-replay-v1",
        "recipe_version": RECIPE_VERSION,
        "bar_source": BAR_SOURCE,
        "tick_source": "finmind_tx_tick_by_day (TX large — price path exact, depth is a proxy)",
        "n_days": len(per_day),
        "day_range": [per_day[0]["day"], per_day[-1]["day"]] if per_day else None,
        "skipped": skipped,
        "mean_tick_coverage": round(
            sum(d["tick_coverage"] for d in per_day) / len(per_day), 4
        )
        if per_day
        else None,
        "summary": summary,
        "per_day": per_day,
    }

    print("\n=== summary ===")
    hdr = f"{'model':<15}{'trades':>8}{'net_pts':>10}{'pts/day':>10}{'pts/trade':>11}{'trade_wr%':>11}{'day_wr%':>9}"
    print(hdr)
    print("-" * len(hdr))
    for name, _ in MODELS:
        s = summary[name]
        if not s.get("days"):
            continue
        print(
            f"{name:<15}{s['trades']:>8}{s['net_pts']:>10}{s['pts_per_day']:>10}"
            f"{str(s['pts_per_trade']):>11}{str(s['trade_win_pct']):>11}{str(s['day_win_pct']):>9}"
        )
    base = summary["bar"]["net_pts"] if summary["bar"].get("days") else None
    thru = summary["tick_through"]["net_pts"] if summary["tick_through"].get("days") else None
    if base is not None and thru is not None:
        print(
            f"\nqueue-dependent share of reported P&L: {base:+.1f} → {thru:+.1f} pts "
            f"({(thru - base):+.1f} pts, {(100.0 * (thru - base) / abs(base)) if base else float('nan'):+.1f}%)"
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
