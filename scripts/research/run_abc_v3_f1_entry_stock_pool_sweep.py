#!/usr/bin/env python3
"""ABC v3+F1 · stock pool tier × gap band × TP-only sweep."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.abc_v3_f1_entry_stock_pool_sweep import (  # noqa: E402
    render_entry_stock_pool_sweep_md,
    run_entry_stock_pool_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+F1 stock pool tier sweep")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-09-01")
    ap.add_argument("--date-end", default="2026-07-01")
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--target", type=float, default=8.0)
    ap.add_argument("--min-leg-n", type=int, default=30)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_entry_stock_pool_sweep(
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
    tag = f"{args.date_start.replace('-', '')}_{args.date_end.replace('-', '')}"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_stock_pool_tp_{tag}.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_entry_stock_pool_sweep_md(payload), encoding="utf-8")

    best = payload.get("best_cell") or {}
    print(
        f"best: {best.get('pool_label')} + {best.get('overlay_label')} "
        f"n={best.get('n')} tp={best.get('tp_only_mean_pct')}%"
    )
    print(f"hits target: {len(payload.get('cells_hitting_target') or [])}")
    print(f"\nWrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
