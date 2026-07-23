#!/usr/bin/env python3
"""Run TW↔NQ rolling 30m (6×5m) risk-gate study.

  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_30m_rolling_risk_gate_study.py
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
from research.backtest.us_tw_30m_rolling_risk_gate_study import (  # noqa: E402
    registered_grid,
    run_study,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--stamp", default=None)
    args = ap.parse_args(argv)

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_us_tw_30m_rolling_risk_gate"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    print(f"Registered grid size={len(registered_grid())}")
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
        f"best={best.get('strategy_id')} recall={best.get('event_recall')} "
        f"sell@trough={best.get('sell_near_trough_rate_on_events')} "
        f"buy_pre={best.get('reenter_before_trough_rate_on_events')} "
        f"net_edge={best.get('net_edge_ntd')}"
    )
    print(
        f"7/14 detach={spot.get('detach_poll')} trough~{spot.get('trough_poll_close')} "
        f"rebuy={spot.get('rebuy_poll')} headroom={spot.get('exit_headroom')}"
    )
    l2r = (payload.get("risk_control_standard") or {}).get("L2R_optional_rebuy") or {}
    print(f"L2R={l2r.get('strategy_id')} · {l2r.get('definition')}")
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Bundle {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
