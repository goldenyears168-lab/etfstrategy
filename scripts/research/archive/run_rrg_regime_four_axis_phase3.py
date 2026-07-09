#!/usr/bin/env python3
"""RRG × Regime four-axis · Phase 3 POOL1 RH50 entry gate.

用法：
  PYTHONPATH=src python scripts/run_rrg_regime_four_axis_phase3.py
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
from research.backtest.rrg_regime_four_axis_phase3 import (  # noqa: E402
    render_rrg_regime_four_axis_phase3_md,
    run_rrg_regime_four_axis_phase3,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG × Regime four-axis Phase 3 · POOL1 RH50")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default=None, help="default: DB latest")
    parser.add_argument("--is-end", default="2025-12-31")
    parser.add_argument("--oos-start", default="2026-01-01")
    parser.add_argument("--oos-end", default="2026-06-30")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_rrg_regime_four_axis_phase3(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            oos_end=args.oos_end,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_regime_four_axis_phase3.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_rrg_regime_four_axis_phase3_md(payload), encoding="utf-8")

    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    ev = (payload.get("evaluation") or {}).get("P3-H-RH50") or {}
    print(
        f"  P3-H-RH50 pass={ev.get('pass')} capture={ev.get('capture_rate_full_pct')}% "
        f"OOS Δex={ev.get('OOS_delta_excess_pp')}pp n={ev.get('OOS_n_periods')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
