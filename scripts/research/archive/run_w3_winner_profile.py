#!/usr/bin/env python3
"""W3 RV high/low winner/loser RRG profile + filter hypothesis backtest.

  PYTHONPATH=src python3 scripts/run_w3_winner_profile.py
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
from research.backtest.w3_rv_hl_winner_profile import (  # noqa: E402
    render_w3_winner_profile_md,
    run_w3_winner_profile_study,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="W3 RV HL winner/loser profile")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=40)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_w3_winner_profile_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            min_n=args.min_n,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_w3_winner_profile.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_w3_winner_profile_md(payload), encoding="utf-8")

    verdict = payload.get("verdict") or {}
    print(verdict.get("summary", ""))
    for r in verdict.get("reasons") or []:
        print(f"  · {r}")
    for h in verdict.get("hypotheses") or []:
        print(f"  · {h.get('id')} {h.get('status')}: n={h.get('n')} ret={h.get('mean_ret_3d_net_pct')}%")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
