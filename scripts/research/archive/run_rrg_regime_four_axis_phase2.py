#!/usr/bin/env python3
"""RRG × Regime four-axis · Phase 2 pre-release overlay wiring.

用法：
  PYTHONPATH=src python scripts/run_rrg_regime_four_axis_phase2.py
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
from research.backtest.rrg_regime_four_axis_phase2 import (  # noqa: E402
    render_rrg_regime_four_axis_phase2_md,
    run_rrg_regime_four_axis_phase2,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG × Regime four-axis Phase 2")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_rrg_regime_four_axis_phase2(conn)
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_regime_four_axis_phase2.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_rrg_regime_four_axis_phase2_md(payload), encoding="utf-8")

    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"  passed={payload['n_passed']}/{payload['n_overlays']}")
    for w in payload.get("winners") or []:
        print(
            f"  · {w['overlay_id']} ({w['track']}) capture={w.get('capture_rate_full')} "
            f"OOS Δex={w.get('OOS_delta_excess_pp')}pp"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
