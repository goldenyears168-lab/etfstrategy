#!/usr/bin/env python3
"""Run TW↔NQ 5m state-machine (SYNC/DETACH/REVERSAL/REPAIRING) constrained sweep.

  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_5m_state_machine_study.py
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
from research.backtest.us_tw_5m_state_machine_study import (  # noqa: E402
    registered_grid,
    run_study,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--stamp", default=None, help="report stamp; default today or 20260715_state")
    args = ap.parse_args(argv)

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_us_tw_5m_state_machine"
    # prefer collision-safe stamp if bare date already used elsewhere
    if args.stamp is None:
        stamp = f"{date.today().strftime('%Y%m%d')}_us_tw_5m_state_machine"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    n_grid = len(registered_grid())
    print(f"Registered grid size={n_grid}")
    result = run_study(date_end=args.date_end, out_dir=out_dir)
    payload = result["payload"]
    out_json = RESEARCH_RRG / f"{stamp}.json"
    out_md = RESEARCH_RRG / f"{stamp}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")
    best = payload.get("best_strategy") or {}
    spot = payload.get("spotlight_20260714") or {}
    print(
        f"days={payload['meta']['n_days']} events3={payload['meta']['n_events3']} · "
        f"best={best.get('strategy_id')} net_edge={best.get('net_edge_ntd')} NTD · "
        f"FPR={best.get('false_trigger_rate_on_nonevent')} · pick={payload['meta'].get('pick_rule')}"
    )
    print(
        f"7/14 detach={spot.get('detach_poll')} repair={spot.get('reenter_poll')} "
        f"repair_polls={spot.get('repair_polls')}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Bundle {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
