#!/usr/bin/env python3
"""ABC v3+f1 · Gap OR + RV3 · hold 3/4/5d study.

  PYTHONPATH=src python3 scripts/research/archive/run_abc_v3_f1_kinematic_gap_rv3_study.py \\
    --date-start 2026-04-01 --date-end 2026-07-07
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.abc_v3_f1_kinematic_gap_rv3_study import (  # noqa: E402
    render_abc_f1_gap_rv3_hold_study_md,
    run_abc_f1_gap_rv3_hold_study,
)
from research.backtest.archive.abc_v3_f1_kinematic_leadlag_study import (  # noqa: E402
    find_latest_comparison_json,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+f1 gap+RV3 hold study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--ledger-json", type=Path, default=None)
    ap.add_argument("--date-start", default="2026-04-01")
    ap.add_argument("--date-end", default="2026-07-07")
    ap.add_argument("--target", type=float, default=10.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    ledger = args.ledger_json or find_latest_comparison_json()
    if ledger is None or not ledger.exists():
        raise SystemExit("ledger JSON not found")

    t0 = time.perf_counter()
    conn = connect(args.db)
    try:
        payload = run_abc_f1_gap_rv3_hold_study(
            conn,
            ledger_json=ledger,
            date_start=args.date_start,
            date_end=args.date_end,
            target_pct=args.target,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    tag = f"{args.date_start.replace('-', '')}_{args.date_end.replace('-', '')}"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_gap_rv3_hold_{tag}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_f1_gap_rv3_hold_study_md(payload), encoding="utf-8")

    best = 0.0
    for block in (payload.get("by_hold_days") or {}).values():
        v = float((block.get("best_rule") or {}).get("mean_all_legs_ret_pct") or 0)
        best = max(best, v)
    print(f"Wrote {out_json}\nWrote {out_md}")
    print(f"  best_portfolio_mean={best:.3f}% target={args.target}% runtime={(time.perf_counter()-t0)/60:.1f}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
