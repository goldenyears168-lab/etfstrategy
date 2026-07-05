#!/usr/bin/env python3
"""Phase 2b · Extension radar 高點特徵機率分析。

用法：
  PYTHONPATH=src python scripts/analyze_extension_peak_features.py
  PYTHONPATH=src python scripts/analyze_extension_peak_features.py \\
    --date-start 2024-01-01 --date-end 2026-06-26
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.extension_peak_feature_analysis import (  # noqa: E402
    render_peak_analysis_md,
    run_peak_feature_analysis,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extension peak feature probability analysis")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--poll-min", type=int, default=5)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_peak_feature_analysis(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            poll_interval_min=args.poll_min,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_extension_peak_features.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(render_peak_analysis_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    rec = payload.get("recommended_signature") or {}
    alert = rec.get("alert_layer") or {}
    confirm = rec.get("confirm_layer") or {}
    print(
        f"Alert: {alert.get('predicate')} P={alert.get('P_peak_pct')}% lift={alert.get('lift_vs_base')}×"
    )
    print(
        f"Confirm: {confirm.get('predicate')} P={confirm.get('P_confirm_pct')}% "
        f"lift={confirm.get('lift_vs_base')}×"
    )
    print(f"Significant FDR: peak={payload['n_significant_fdr']['peak_spike']} fade={payload['n_significant_fdr']['fade_spike']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
