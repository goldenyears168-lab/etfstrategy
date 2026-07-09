#!/usr/bin/env python3
"""ABC v3+f1 · embed H-ABC-FILTER-1 · event OOS + 5-slot top-K compare.

  PYTHONPATH=src python3 scripts/run_abc_v3_f1_pipeline.py --intraday-tail-days 90
  PYTHONPATH=src python3 scripts/run_abc_v3_f1_pipeline.py --breadth-strat
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.abc_v3_slot_sweep import (  # noqa: E402
    render_abc_v3_f1_breadth_strat_md,
    render_abc_v3_f1_pipeline_md,
    render_abc_v3_f1_slot_val_diagnostic_md,
    run_abc_v3_f1_breadth_stratification,
    run_abc_v3_f1_pipeline_study,
    run_abc_v3_f1_slot_val_diagnostic,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ABC v3+f1 embedded filter pipeline")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-01")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--intraday-tail-days", type=int, default=90)
    ap.add_argument("--train-days", type=int, default=60)
    ap.add_argument("--val-days", type=int, default=30)
    ap.add_argument("--breadth-strat", action="store_true", help="G3 breadth stratification only")
    ap.add_argument("--slot-val-diagnostic", action="store_true", help="5-slot val failure diagnostic")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    stamp = date.today().strftime("%Y%m%d")
    t0 = time.perf_counter()
    try:
        if args.breadth_strat:
            payload = run_abc_v3_f1_breadth_stratification(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                intraday_tail_days=args.intraday_tail_days,
            )
            out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_breadth_strat.json"
            out_md = out_json.with_suffix(".md")
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            out_md.write_text(render_abc_v3_f1_breadth_strat_md(payload), encoding="utf-8")
            verdict = payload.get("verdict") or {}
            print(
                f"G3 breadth · n={payload.get('n_trades')} · "
                f"{'passed' if verdict.get('passed') else 'failed'} · {verdict.get('reason')}"
            )
            print(f"\nWrote {out_json}\nWrote {out_md}")
            return 0

        if args.slot_val_diagnostic:
            payload = run_abc_v3_f1_slot_val_diagnostic(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                intraday_tail_days=args.intraday_tail_days,
                train_days=args.train_days,
                val_days=args.val_days,
            )
            out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_slot_val_diagnostic.json"
            out_md = out_json.with_suffix(".md")
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            out_md.write_text(render_abc_v3_f1_slot_val_diagnostic_md(payload), encoding="utf-8")
            verdict = payload.get("verdict") or {}
            print(f"Recommendation: {verdict.get('recommendation')} · {verdict.get('reason')}")
            print(f"\nWrote {out_json}\nWrote {out_md}")
            return 0

        payload = run_abc_v3_f1_pipeline_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            intraday_tail_days=args.intraday_tail_days,
            train_days=args.train_days,
            val_days=args.val_days,
        )
    finally:
        conn.close()

    elapsed = time.perf_counter() - t0
    out_json = args.out or RESEARCH_RRG / f"{stamp}_abc_v3_f1_pipeline_{args.intraday_tail_days}d.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_abc_v3_f1_pipeline_md(payload), encoding="utf-8")

    verdict = payload.get("verdict") or {}
    print(verdict.get("summary", ""))
    for r in verdict.get("reasons") or []:
        print(f"  · {r}")
    print(f"\nRuntime: {elapsed / 60:.1f} min")
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
