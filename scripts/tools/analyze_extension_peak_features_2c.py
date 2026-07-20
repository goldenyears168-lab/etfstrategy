#!/usr/bin/env python3
"""Phase 2c · Extension radar · 交互 logit · 生存 · OOS · X6/X7/X8 回測。

用法：
  PYTHONPATH=src python scripts/analyze_extension_peak_features_2c.py
  PYTHONPATH=src python scripts/analyze_extension_peak_features_2c.py --skip-backtest
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.archive.extension_peak_feature_analysis import (  # noqa: E402
    render_phase_2c_md,
    run_phase_2c_analysis,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extension peak Phase 2c analysis")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--poll-min", type=int, default=5)
    parser.add_argument("--skip-backtest", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_phase_2c_analysis(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            poll_interval_min=args.poll_min,
            skip_backtest=args.skip_backtest,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_extension_peak_phase2c.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    # backtest payloads are large; keep summaries only in JSON
    slim = {k: v for k, v in payload.items() if k not in ("backtest_full", "backtest_oos")}
    for key in ("backtest_full", "backtest_oos"):
        bt = payload.get(key)
        if bt:
            slim[key] = {
                "date_start": bt.get("date_start"),
                "date_end": bt.get("date_end"),
                "summaries": bt.get("summaries"),
                "best": bt.get("best"),
            }
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(render_phase_2c_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    h = payload.get("hypothesis_results") or {}
    for hid in ("H-2C-1", "H-2C-2", "H-2C-3", "H-2C-4", "H-2C-5"):
        if hid in h:
            print(f"{hid}: {'PASS' if h[hid].get('pass') else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
