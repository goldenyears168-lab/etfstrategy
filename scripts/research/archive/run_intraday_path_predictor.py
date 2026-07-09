#!/usr/bin/env python3
"""1 分 K 轨迹预测 · Phase 0+1 standalone。

用法：
  PYTHONPATH=src python3 scripts/run_intraday_path_predictor.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.intraday_path_predictor import (  # noqa: E402
    render_intraday_path_predictor_md,
    run_intraday_path_predictor,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = ROOT / "reports" / "research" / "intraday"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Intraday 1m path predictor Phase 0+1")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--min-spike", type=float, default=3.0)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_intraday_path_predictor(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            oos_start=args.oos_start,
            min_spike_pct=args.min_spike,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or OUT_DIR / f"{stamp}_intraday_path_predictor.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or OUT_DIR / f"{stamp}_intraday_path_predictor_report.md"
    md_path.write_text(render_intraday_path_predictor_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    rates = payload.get("path_base_rates_pct") or {}
    logit = payload.get("logit_fade_oos") or {}
    print(
        f"Sessions={payload.get('n_sessions')} events={payload.get('n_spike_events')} "
        f"P-B={rates.get('P-B_spike_fade')}% median_ttp={payload.get('time_to_peak_spike_days', {}).get('median_min_opening_spike')}min"
    )
    print(f"Logit OOS AUC multi={logit.get('auc_multivariate')} heat={logit.get('auc_heat_only')} Δ={logit.get('delta_auc')}")
    for hid, r in (payload.get("hypothesis_results") or {}).items():
        print(f"  {hid}: {'PASS' if r.get('pass') else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
