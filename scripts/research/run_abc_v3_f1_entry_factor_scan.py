#!/usr/bin/env python3
"""ABC v3+F1 · entry 因子相關性掃描（raw / z-score / percentile / ratio）.

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_entry_factor_scan.py \
    --date-start 2025-09-01 --date-end 2026-07-01 --hold-days 5
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
from research.backtest.abc_v3_f1_entry_factor_scan import (  # noqa: E402
    render_entry_factor_scan_md,
    run_entry_factor_scan_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 entry factor correlation scan")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-09-01")
    ap.add_argument("--date-end", default="2026-07-01")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--baseline-window", type=int, default=60)
    ap.add_argument("--min-trailing-days", type=int, default=10)
    ap.add_argument("--min-factor-n", type=int, default=20)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_entry_factor_scan_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            hold_days=args.hold_days,
            baseline_window=args.baseline_window,
            min_trailing_days=args.min_trailing_days,
            min_factor_n=args.min_factor_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_entry_factor_scan_hold{args.hold_days}d.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_entry_factor_scan_md(payload), encoding="utf-8")

    print(f"n_legs={payload.get('n_legs')} n_stocks={payload.get('n_stocks')}")
    for row in payload.get("factor_scan") or []:
        if row.get("skipped"):
            print(f"  {row.get('factor')}: n={row.get('n')} (skipped, below min_factor_n)")
            continue
        print(
            f"  {row.get('factor')}: n={row.get('n')} pearson={row.get('pearson_r')} "
            f"spearman={row.get('spearman_rho')} lift={row.get('tercile_lift')}pp"
        )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
