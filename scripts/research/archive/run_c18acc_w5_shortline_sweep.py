#!/usr/bin/env python3
"""C18acc WMA(5) short-line research · Phase 0–4 sweeps."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_w5_shortline_sweep import (  # noqa: E402
    render_w5_shortline_md,
    run_w5_shortline_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc WMA(5) short-line sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-01-01")
    ap.add_argument("--date-end", default=None, help="default: DB latest")
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument("--oos-h1-start", default="2026-01-01")
    ap.add_argument("--oos-h1-end", default="2026-06-30")
    ap.add_argument("--phase", type=int, choices=(1, 2, 3, 4), default=1)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        payload = run_w5_shortline_sweep(
            conn,
            phase=args.phase,  # type: ignore[arg-type]
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_h1_start=args.oos_h1_start,
            oos_h1_end=args.oos_h1_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    suffix = f"_p{args.phase}" if args.phase != 1 else ""
    out_json = args.out or RESEARCH_RRG / (
        f"{stamp}_c18acc_w20_conditional_accel_sweep.json"
        if args.phase == 4
        else f"{stamp}_c18acc_w5_shortline_sweep{suffix}.json"
    )
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_w5_shortline_md(payload), encoding="utf-8")

    verdict = payload.get("verdict") or {}
    print(verdict.get("summary", ""))
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0 if verdict.get("recommend_adopt") else 1


if __name__ == "__main__":
    raise SystemExit(main())
