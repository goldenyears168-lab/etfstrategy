#!/usr/bin/env python3
"""US–TW divergence · sell-all gate research study.

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_divergence_sell_gate_study.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_divergence_sell_gate_study.py \\
      --date-start 2024-08-14 --is-end 2025-07-31
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.us_tw_divergence_sell_gate_study import run_study  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="US–TW divergence sell-all gate study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2024-08-14")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-07-31")
    ap.add_argument("--threshold", type=float, default=3.0)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    stamp = date.today().strftime("%Y%m%d")
    out_dir = args.out_dir or (RESEARCH_RRG / f"{stamp}_us_tw_divergence_sell_gate")
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(args.db)
    try:
        result = run_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            threshold=args.threshold,
            out_dir=out_dir,
        )
    finally:
        conn.close()

    payload = result["payload"]
    out_json = RESEARCH_RRG / f"{stamp}_us_tw_divergence_sell_gate.json"
    out_md = RESEARCH_RRG / f"{stamp}_us_tw_divergence_sell_gate.md"
    # also keep copies inside out_dir
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(result["markdown"], encoding="utf-8")
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")

    meta = payload.get("meta") or {}
    tops = payload.get("top_rule_candidates") or []
    print(
        f"events={meta.get('n_events_primary')}/{meta.get('n_days')} · "
        f"top={tops[0]['hypothesis_id'] if tops else None} "
        f"status={(tops[0]['eval'].get('status') if tops else None)}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Bundle {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
