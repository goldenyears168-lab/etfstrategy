#!/usr/bin/env python3
"""ABC v3+F1 · structural pyramid add study (RP-2 · H-ENTRY-PYRAMID-1).

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_structural_pyramid.py \
    --date-start 2025-07-01 --date-end 2026-07-01

Collects raw ABC-V3-F1 first-trigger legs (unconstrained universe) with full
KinematicSnap 30m timelines, then evaluates the preregistered pyramid-add
conditions (IS pick / OOS verify) against Baseline A (no add) and Baseline B
(unconditional averaging-down).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
assert (ROOT / "src").exists(), f"unexpected ROOT resolution: {ROOT}"

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.abc_v3_f1_entry_gate_tp_sweep import (  # noqa: E402
    collect_abc_v3_f1_raw_legs,
)
from research.backtest.abc_v3_f1_structural_pyramid_study import (  # noqa: E402
    render_structural_pyramid_md,
    run_structural_pyramid_study,
)
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 structural pyramid add study (RP-2)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-07-01")
    ap.add_argument("--date-end", default=None, help="default: last trading date in DB")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--min-trigger-n", type=int, default=30)
    ap.add_argument("--min-oos-trigger-n", type=int, default=15)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        close, _, _ = load_price_panels(conn)
        full_dates = close.index.astype(str).tolist()
        date_end = args.date_end or full_dates[-1]
        sample_dates = [d for d in full_dates if args.date_start <= d <= date_end]
        if not sample_dates:
            print(f"BLOCKER: no trading dates in [{args.date_start}, {date_end}]", file=sys.stderr)
            return 1
        print(
            f"pyramid: collecting raw ABC-V3-F1 legs · {len(sample_dates)} dates "
            f"({sample_dates[0]} .. {sample_dates[-1]}) · hold={args.hold_days}d …",
            flush=True,
        )
        legs = collect_abc_v3_f1_raw_legs(
            conn, dates=sample_dates, hold_days=args.hold_days
        )
        print(f"pyramid: {len(legs)} legs collected · evaluating conditions …", flush=True)
        if not legs:
            print("BLOCKER: no raw ABC-V3-F1 legs in window", file=sys.stderr)
            return 1
        payload = run_structural_pyramid_study(
            conn,
            legs,
            date_start=sample_dates[0],
            date_end=sample_dates[-1],
            hold_days=args.hold_days,
            min_trigger_n=args.min_trigger_n,
            min_oos_trigger_n=args.min_oos_trigger_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_structural_pyramid.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_structural_pyramid_md(payload), encoding="utf-8")

    print(f"\nverdict: {payload.get('verdict')} · winner: {payload.get('winner_id')}")
    for c in payload.get("criteria") or []:
        print(f"  [{'PASS' if c.get('passed') else 'FAIL'}] {c.get('criterion')} — {c.get('detail')}")
    print("\nIS conditions:")
    for cid, r in (payload.get("is_results") or {}).items():
        da = r.get("delta_vs_A_pp") or {}
        db = r.get("delta_vs_B_same_legs_pp") or {}
        print(
            f"  {cid}: n={r.get('n_triggered')} ({r.get('trigger_rate_pct')}%) "
            f"blended={r.get('blended_sync_mean_pct')}% legA={r.get('leg1_only_mean_pct')}% "
            f"dA={da.get('mean')}pp(p~{da.get('p_approx')}) dB={db.get('mean')}pp(p~{db.get('p_approx')})"
        )
    print("\nOOS conditions:")
    for cid, r in (payload.get("oos_results") or {}).items():
        da = r.get("delta_vs_A_pp") or {}
        print(
            f"  {cid}: n={r.get('n_triggered')} blended={r.get('blended_sync_mean_pct')}% "
            f"legA={r.get('leg1_only_mean_pct')}% dA={da.get('mean')}pp(p~{da.get('p_approx')})"
        )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
