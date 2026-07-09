#!/usr/bin/env python3
"""C18acc POOL1+S2 · weekday execution gate sweep (Phase 1–4).

用法：
  PYTHONPATH=src python3 scripts/run_c18acc_weekday_execution_sweep.py
  PYTHONPATH=src python3 scripts/run_c18acc_weekday_execution_sweep.py \\
    --variants W0,E1,E3 --date-start 2020-01-02 --date-end 2026-07-03
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
from research.backtest.c18acc_weekday_execution_sweep import (  # noqa: E402
    ALL_VARIANTS,
    render_c18acc_weekday_execution_sweep_md,
    run_c18acc_weekday_execution_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C18acc weekday execution gate sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2020-01-02")
    parser.add_argument("--date-end", default="auto")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--n-slots", type=int, default=3)
    parser.add_argument(
        "--variants",
        default=",".join(ALL_VARIANTS),
        help="Comma-separated gate ids (default: all Phase 1–3)",
    )
    parser.add_argument("--no-combos", action="store_true", help="Skip Phase 4 combo variants")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    variants = tuple(v.strip() for v in args.variants.split(",") if v.strip())
    if "W0" not in variants:
        variants = ("W0",) + variants

    conn = connect(args.db)
    try:
        date_end = _db_max_date(conn) if args.date_end == "auto" else args.date_end
        payload = run_c18acc_weekday_execution_sweep(
            conn,
            date_start=args.date_start,
            date_end=date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            n_slots=args.n_slots,
            variants=variants,
            include_combos=not args.no_combos,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_weekday_execution_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_c18acc_weekday_execution_sweep_md(payload), encoding="utf-8")

    w0 = (payload.get("w0_baseline") or {}).get("FULL") or {}
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(
        f"  verdict={payload.get('verdict')} W0 excess={w0.get('mean_excess_pct')}% n={w0.get('n_periods')}"
    )
    for vid in ("H1", "F1", "HF1"):
        r = (payload.get("by_gate") or {}).get(vid, {}).get("FULL") or {}
        if not r:
            continue
        d = None
        if r.get("mean_excess_pct") is not None and w0.get("mean_excess_pct") is not None:
            d = round(float(r["mean_excess_pct"]) - float(w0["mean_excess_pct"]), 4)
        print(f"  {vid} FULL Δ={d}pp excess={r.get('mean_excess_pct')}% swaps={r.get('swaps_total')}")
    e1 = (payload.get("by_gate") or {}).get("E1", {}).get("FULL") or {}
    if e1:
        e1_delta = None
        if e1.get("mean_excess_pct") is not None and w0.get("mean_excess_pct") is not None:
            e1_delta = round(float(e1["mean_excess_pct"]) - float(w0["mean_excess_pct"]), 4)
        print(
            f"  E1 FULL Δ={e1_delta}pp capture={e1.get('capture_rate_pct')}% "
            f"best={payload.get('best_variant_full')}"
        )
    for reg in payload.get("regressions") or []:
        print(f"  regression {reg.get('variant_id')}: match W0 {reg.get('match_w0_pct')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
