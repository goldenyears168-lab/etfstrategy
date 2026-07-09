#!/usr/bin/env python3
"""C18acc hybrid RRG sweep · W20 RS-Ratio + WMA5 RS-Momentum.

  PYTHONPATH=src python3 scripts/run_c18acc_hybrid_rrg_mom_sweep.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_hybrid_rrg_mom_sweep import (  # noqa: E402
    render_c18acc_hybrid_rrg_mom_md,
    render_hybrid_y_pause_ab_md,
    run_c18acc_hybrid_rrg_mom_sweep,
    run_hybrid_y_pause_ab,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc hybrid RRG · W20 ratio + WMA5 momentum")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument(
        "--quality-only",
        action="store_true",
        help="Skip heavy backtest · only daily + forward-return signal quality (fast)",
    )
    ap.add_argument(
        "--pause-ab",
        action="store_true",
        help="A/B: B0 vs B0 + hybrid-Y pause on quad force-exit",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        if args.pause_ab:
            payload = run_hybrid_y_pause_ab(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
            )
        else:
            payload = run_c18acc_hybrid_rrg_mom_sweep(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                quality_only=args.quality_only,
            )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = "_pause_ab" if args.pause_ab else ("_quality" if args.quality_only else "")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_hybrid_rrg_mom_sweep{suffix}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    render = render_hybrid_y_pause_ab_md if args.pause_ab else render_c18acc_hybrid_rrg_mom_md
    out_md.write_text(render(payload), encoding="utf-8")

    if args.pause_ab:
        v = payload.get("verdict") or {}
        print(v.get("summary", ""))
        for win in ("IS", "OOS", "FULL"):
            d = (payload.get("deltas") or {}).get(win) or {}
            print(
                f"  · {win}: excess {d.get('excess_pp')}pp · total {d.get('total_return_pp')}pp · "
                f"Δquad_force {d.get('quad_force_exits_delta')} · paused {d.get('paused')}"
            )
    elif args.quality_only:
        sq = payload.get("signal_quality") or {}
        ew = sq.get("exit_warning") or {}
        en = sq.get("entry_early") or {}
        print("Signal quality (quality-only):")
        print(
            f"  · exit-warn: hybrid-weak while W20 leading n={ew.get('n_warn_stock_days')} · "
            f"W20 confirms weak within {sq.get('confirm_within_days')}d = {ew.get('w20_confirms_weak_within_pct')}%"
        )
        print(
            f"  · entry-early: hybrid-accel while W20 not-strong n={en.get('n_early_stock_days')} · "
            f"reaches leading within {sq.get('confirm_within_days')}d = {en.get('w20_reaches_leading_within_pct')}%"
        )
    else:
        verdict = payload.get("verdict") or {}
        print(verdict.get("summary", ""))
        for r in verdict.get("reasons") or []:
            print(f"  · {r}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
