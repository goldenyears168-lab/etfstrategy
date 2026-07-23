#!/usr/bin/env python3
"""ABC v3+F1 · entry gate x TP-only joint sweep (unconstrained universe).

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_entry_gate_tp_sweep.py \
    --date-start 2026-04-01 --date-end 2026-07-07 --hold-days 5 --target 10
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
    render_entry_gate_tp_joint_sweep_md,
    run_entry_gate_tp_joint_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 entry gate x TP-only joint sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2026-04-01")
    ap.add_argument("--date-end", default="2026-07-07")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--target", type=float, default=10.0)
    ap.add_argument("--min-leg-n", type=int, default=15)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_entry_gate_tp_joint_sweep(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            hold_days=args.hold_days,
            target_pct=args.target,
            min_leg_n=args.min_leg_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = (
        args.out
        or RESEARCH_RRG / f"{stamp}_abc_v3_f1_entry_gate_tp_sweep_hold{args.hold_days}d.json"
    )
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_entry_gate_tp_joint_sweep_md(payload), encoding="utf-8")

    all_hold = payload.get("all_hold_only") or {}
    all_tp = payload.get("all_best_tp_only") or {}
    best = payload.get("best_cell") or {}
    print(f"n_raw_legs={payload.get('n_raw_legs')}")
    print(f"all hold-only mean={all_hold.get('mean_all_legs_ret_pct')}%")
    print(f"all best TP-only mean={all_tp.get('mean_all_legs_ret_pct')}% ({all_tp.get('variant_id')})")
    if best:
        print(
            f"best entry gate cell: {best.get('label')} n={best.get('n')} "
            f"mean={best.get('best_tp_only_mean_pct')}% ({best.get('best_tp_only_variant')})"
        )
    hit = payload.get("cells_hitting_target") or []
    print(f"cells hitting target ({args.target:g}%): {len(hit)}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
