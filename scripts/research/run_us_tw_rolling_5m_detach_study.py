#!/usr/bin/env python3
"""Run TW↔NQ rolling 5m detach/repair avoided-loss sweep.

  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_rolling_5m_detach_study.py
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
from research.backtest.us_tw_rolling_5m_detach_study import run_study  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    stamp = date.today().strftime("%Y%m%d")
    out_dir = args.out_dir or (RESEARCH_RRG / f"{stamp}_us_tw_rolling_5m_detach")
    result = run_study(date_end=args.date_end, out_dir=out_dir)
    payload = result["payload"]
    out_json = RESEARCH_RRG / f"{stamp}_us_tw_rolling_5m_detach.json"
    out_md = RESEARCH_RRG / f"{stamp}_us_tw_rolling_5m_detach.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")
    best = payload.get("best_strategy") or {}
    print(
        f"days={payload['meta']['n_days']} events3={payload['meta']['n_events3']} · "
        f"best={best.get('strategy_id')} net_edge={best.get('net_edge_ntd')} NTD"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Bundle {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
