#!/usr/bin/env python3
"""ABC v3+f1 · kinematic lead-lag study (MV vs price peak/giveback).

  PYTHONPATH=src python3 scripts/research/archive/run_abc_v3_f1_kinematic_leadlag_study.py
  PYTHONPATH=src python3 scripts/research/archive/run_abc_v3_f1_kinematic_leadlag_study.py \\
    --ledger-json reports/research/rrg/20260708_c18acc_abc_v3_f1_comparison.json
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
from research.backtest.archive.abc_v3_f1_kinematic_leadlag_study import (  # noqa: E402
    find_latest_comparison_json,
    render_abc_v3_f1_kinematic_leadlag_study_md,
    run_abc_v3_f1_kinematic_leadlag_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+f1 kinematic lead-lag study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--ledger-json",
        type=Path,
        default=None,
        help="Comparison JSON with abc_f1_trade_ledger (default: latest in reports/research/rrg/)",
    )
    ap.add_argument("--date-start", default=None)
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    ledger_path = args.ledger_json or find_latest_comparison_json()
    if ledger_path is None or not ledger_path.exists():
        print(
            "BLOCKER: no ledger JSON — pass --ledger-json or add "
            "*_c18acc_abc_v3_f1_comparison.json under reports/research/rrg/",
            file=sys.stderr,
        )
        return 1

    t0 = time.perf_counter()
    conn = connect(args.db)
    try:
        payload = run_abc_v3_f1_kinematic_leadlag_study(
            conn,
            ledger_json=ledger_path,
            date_start=args.date_start,
            date_end=args.date_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_kinematic_leadlag_study.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_v3_f1_kinematic_leadlag_study_md(payload), encoding="utf-8")

    for s in payload.get("cohort_summaries") or []:
        print(
            f"  {s.get('cohort')}: n={s.get('n')} · mean_net={s.get('mean_net_pct')}% · "
            f"w3_before_peak={s.get('pct_w3_before_peak')}% · "
            f"w3_lead_peak={s.get('mean_w3_lead_peak_polls')}"
        )
    print(f"\nRuntime: {(time.perf_counter() - t0) / 60:.1f} min")
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
