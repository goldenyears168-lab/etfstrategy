#!/usr/bin/env python3
"""RRG × Regime four-axis · Phase 1 gate sweep.

用法：
  PYTHONPATH=src python scripts/run_rrg_regime_four_axis_sweep.py
  PYTHONPATH=src python scripts/run_rrg_regime_four_axis_sweep.py \\
    --annot-json reports/research/rrg/20260708_rrg_regime_four_axis_annot.json
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
from research.backtest.archive.rrg_regime_four_axis_sweep import (  # noqa: E402
    load_annot_events,
    render_rrg_regime_four_axis_sweep_md,
    run_rrg_regime_four_axis_sweep,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _db_max_date(conn) -> str:
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn)
    return str(bench.index.max())[:10]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG × Regime four-axis Phase 1 sweep")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--annot-json", type=Path, default=None, help="reuse Phase 0 JSON")
    parser.add_argument("--date-start", default=None)
    parser.add_argument("--date-end", default="auto")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    events = None
    date_start = args.date_start
    date_end = None if args.date_end == "auto" else args.date_end

    if args.annot_json is not None:
        events = load_annot_events(args.annot_json)
        if not date_start and events:
            date_start = events[0]["date"]
        if args.date_end == "auto" and events:
            date_end = events[-1]["date"]
    else:
        conn = connect(args.db)
        try:
            if args.date_end == "auto":
                date_end = _db_max_date(conn)
            payload = run_rrg_regime_four_axis_sweep(
                conn,
                date_start=date_start,
                date_end=date_end,
            )
        finally:
            conn.close()
        stamp = date.today().strftime("%Y%m%d")
        out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_regime_four_axis_sweep.json"
        out_md = out_json.with_suffix(".md")
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out_md.write_text(render_rrg_regime_four_axis_sweep_md(payload), encoding="utf-8")
        print(f"Wrote {out_json}")
        print(f"Wrote {out_md}")
        print(f"  passed={payload['n_passed']}/{payload['n_variants']}")
        for w in payload.get("winners") or [][:5]:
            print(
                f"  · {w['gate_id']}@{w['cohort']} capture={w.get('capture_rate_full')} "
                f"OOS Δex={w.get('OOS_delta_excess_pp')}pp"
            )
        return 0

    payload = run_rrg_regime_four_axis_sweep(
        None,
        events=events,
        date_start=date_start,
        date_end=date_end,
    )
    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_regime_four_axis_sweep.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_rrg_regime_four_axis_sweep_md(payload), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  passed={payload['n_passed']}/{payload['n_variants']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
