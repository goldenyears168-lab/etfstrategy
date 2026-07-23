#!/usr/bin/env python3
"""ABC v3+F1 · entry structure sweep (MV gap sweet-band + W3 RV trough rebound).

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_entry_structure_sweep.py \
    --intraday-tail-days 90
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
from research.backtest.abc_v3_f1_entry_structure_sweep import (  # noqa: E402
    render_abc_v3_f1_entry_structure_md,
    run_abc_v3_f1_entry_structure_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 entry structure sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=90)
    ap.add_argument("--min-n", type=int, default=15)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_abc_v3_f1_entry_structure_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            min_n=args.min_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = (
        args.out
        or RESEARCH_RRG / f"{stamp}_abc_v3_f1_entry_structure_{args.intraday_tail_days}d.json"
    )
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_v3_f1_entry_structure_md(payload), encoding="utf-8")

    verdict = payload.get("is_verdict") or {}
    print(verdict.get("summary", ""))
    for r in verdict.get("reasons") or []:
        print(f"  · {r}")
    print("\nIS cells:")
    for row in verdict.get("cells") or []:
        print(
            f"  {row.get('label')}: n={row.get('n')} ret={row.get('mean_ret_3d_net_pct')}% "
            f"win={row.get('win_rate_pct')}% fill={row.get('fill_rate_pct')}% "
            f"Δ={row.get('delta_vs_baseline')}"
        )
    oos = payload.get("oos") or {}
    if oos:
        print(
            f"\nOOS ({oos.get('winner_label')}): n={oos.get('oos_n')} "
            f"meets_min_oos_n={oos.get('meets_min_oos_n')}"
        )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
