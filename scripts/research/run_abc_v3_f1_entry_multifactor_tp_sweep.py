#!/usr/bin/env python3
"""ABC v3+F1 · gap band × multi-factor overlay × fixed TP-only · hold 5d.

  source .venv/bin/activate && PYTHONPATH=src python3 \\
    scripts/research/run_abc_v3_f1_entry_multifactor_tp_sweep.py \\
    --date-start 2026-04-01 --date-end 2026-07-07 --hold-days 5 --target 8 --min-leg-n 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
assert (ROOT / "src").exists(), f"unexpected ROOT resolution: {ROOT}"

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.abc_v3_f1_entry_multifactor_tp_sweep import (  # noqa: E402
    render_entry_multifactor_tp_sweep_md,
    run_entry_multifactor_tp_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ABC v3+F1 gap band × multi-factor overlay × fixed TP-only sweep"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2026-04-01")
    ap.add_argument("--date-end", default="2026-07-07")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--target", type=float, default=8.0)
    ap.add_argument("--min-leg-n", type=int, default=30)
    ap.add_argument(
        "--out",
        type=Path,
        default=RESEARCH_RRG / "20260710_abc_v3_f1_entry_multifactor_tp_hold5d.json",
    )
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_entry_multifactor_tp_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            hold_days=args.hold_days,
            target_pct=args.target,
            min_leg_n=args.min_leg_n,
        )
    finally:
        conn.close()

    out_json = args.out
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_entry_multifactor_tp_sweep_md(payload), encoding="utf-8")

    base = payload.get("base_gate_cell") or {}
    best = payload.get("best_cell") or {}
    hit = payload.get("cells_hitting_target") or []
    print(f"n_raw_legs={payload.get('n_raw_legs')} n_base_gate={payload.get('n_base_gate_legs')}")
    if base:
        print(
            f"base gate only: n={base.get('n')} hold={base.get('hold_only_mean_pct')}% "
            f"tp={base.get('tp_only_mean_pct')}%"
        )
    if best:
        print(
            f"best overlay: {best.get('overlay_label')} n={best.get('n')} "
            f"tp={best.get('tp_only_mean_pct')}% hold={best.get('hold_only_mean_pct')}%"
        )
    print(f"cells hitting target (≥{args.target:g}%, n≥{args.min_leg_n}): {len(hit)}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
