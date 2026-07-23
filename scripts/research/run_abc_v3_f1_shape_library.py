#!/usr/bin/env python3
"""ABC v3+F1 · multisignal shape library study (RP-5).

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_shape_library.py \
    --date-start 2024-01-02 --date-end 2026-07-01

Collects raw ABC-V3-F1 legs, attaches normalized gate features, runs the RP-6
three-stage shape validation pipeline, and writes JSON + markdown reports.
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
from research.backtest.abc_v3_f1_shape_library_study import (  # noqa: E402
    prepare_annotated_legs,
    render_shape_library_md,
    run_shape_library_study,
)
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 shape library study (RP-5)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-02")
    ap.add_argument("--date-end", default=None, help="default: last trading date in DB")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--target", type=float, default=8.0)
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
            f"shape_library: collecting + annotating legs · {len(sample_dates)} dates "
            f"({sample_dates[0]} .. {sample_dates[-1]}) · hold={args.hold_days}d …",
            flush=True,
        )
        legs = prepare_annotated_legs(conn, dates=sample_dates, hold_days=args.hold_days)
        print(f"shape_library: {len(legs)} legs · running three-stage validation …", flush=True)
        if not legs:
            print("BLOCKER: no raw ABC-V3-F1 legs in window", file=sys.stderr)
            return 1
        payload = run_shape_library_study(
            legs,
            date_start=sample_dates[0],
            date_end=sample_dates[-1],
            hold_days=args.hold_days,
            target_pct=args.target,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_shape_library.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_shape_library_md(payload), encoding="utf-8")

    crit = payload.get("success_criteria") or {}
    print(f"\nverdict: {payload.get('verdict')}")
    print(
        f"  holdout_passed={payload.get('n_holdout_passed')} · "
        f"union_annual_n={payload.get('union', {}).get('annualized_n')} · "
        f"union_mean={payload.get('union', {}).get('mean_ret_pct')}%"
    )
    for k, v in crit.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    if payload.get("holdout_passed_ids"):
        print(f"  passed_ids: {', '.join(payload['holdout_passed_ids'])}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
