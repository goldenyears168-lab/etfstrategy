#!/usr/bin/env python3
"""Phase 2 · Extension radar 機率校準 · 🟢🟡🔴 歷史 P 填表。

用法：
  PYTHONPATH=src python scripts/calibrate_extension_probs.py
  PYTHONPATH=src python scripts/calibrate_extension_probs.py \\
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

from research.backtest.extension_prob_calibration import (  # noqa: E402
    calibrate_extension_probs,
    render_calibration_md,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extension radar probability calibration")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-26")
    parser.add_argument("--poll-min", type=int, default=5)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = calibrate_extension_probs(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            poll_interval_min=args.poll_min,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_extension_prob_calibration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md_path = args.md or out.with_suffix(".md")
    md_path.write_text(render_calibration_md(payload), encoding="utf-8")
    print(f"Wrote {md_path}")

    for z in ("green", "yellow", "red"):
        s = payload["zones"].get(z) or {}
        print(
            f"{z}: n={s.get('n')} "
            f"P_fade_30m={s.get('P_fade_30m_ge2')}% "
            f"P_below_vwap={s.get('P_close_below_vwap')}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
