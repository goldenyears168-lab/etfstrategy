#!/usr/bin/env python3
"""C18acc POOL1 · soft / additive RV-MV edge · poll_5m backtest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_pool_rv98_sweep import (  # noqa: E402
    render_pool_rv98_sweep_md,
    run_pool_rv98_sweep_backtest,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="POOL1 Leading vs soft/additive RV-MV edge · poll_5m"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2020-01-02")
    ap.add_argument("--date-end", default=None, help="default: DB latest")
    ap.add_argument("--is-end", default="2025-12-31")
    ap.add_argument(
        "--oos-h2-mode",
        choices=("forward", "historical"),
        default="historical",
        help="historical=2025 H2 proxy · forward=2026 H2 live holdout",
    )
    ap.add_argument("--rv-min", type=float, default=98.0)
    ap.add_argument(
        "--rv-compare",
        choices=("ge", "gt"),
        default="ge",
        help="ge=RV≥rv_min · gt=RV>rv_min",
    )
    ap.add_argument("--mv-min", type=float, default=100.0, help="require MV > this")
    ap.add_argument(
        "--pool-mode",
        choices=("replace", "union_leading"),
        default="replace",
        help="replace=soften whole gate · union_leading=Leading ∪ soft band",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_pool_rv98_sweep_backtest(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_h2_mode=args.oos_h2_mode,
            rv_min=args.rv_min,
            mv_min_exclusive=args.mv_min,
            rv_compare=args.rv_compare,
            pool_mode=args.pool_mode,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    if args.out:
        out_json = args.out
    elif args.pool_mode == "union_leading":
        out_json = RESEARCH_RRG / f"{stamp}_c18acc_pool_rv98_mv102_union.json"
    else:
        out_json = RESEARCH_RRG / f"{stamp}_c18acc_pool_rv98_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_pool_rv98_sweep_md(payload), encoding="utf-8")

    print(payload.get("verdict", {}).get("summary", ""))
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
