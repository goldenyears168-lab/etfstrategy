#!/usr/bin/env python3
"""ABC v3+F1 · gap 截面 vs 时序分解.

  PYTHONPATH=src python3 scripts/research/run_abc_v3_f1_entry_gap_decomposition.py \\
    --date-start 2025-09-01 --date-end 2026-07-01
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
from research.backtest.abc_v3_f1_entry_gap_decomposition import (  # noqa: E402
    render_gap_decomposition_md,
    run_gap_decomposition_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 gap cross-section vs temporal decomposition")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-09-01")
    ap.add_argument("--date-end", default="2026-07-01")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_gap_decomposition_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            hold_days=args.hold_days,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    tag = f"{args.date_start.replace('-', '')}_{args.date_end.replace('-', '')}"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_gap_decomposition_{tag}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_gap_decomposition_md(payload), encoding="utf-8")

    for v in payload.get("variance_decomposition") or []:
        if not v.get("skipped"):
            print(
                f"  {v.get('col')}: between={v.get('icc_between_share')} "
                f"within={v.get('icc_within_share')} (k={v.get('k_stocks')})"
            )
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
