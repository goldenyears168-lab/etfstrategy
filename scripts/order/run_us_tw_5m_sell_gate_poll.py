#!/usr/bin/env python3
"""5m sell-gate poll (human SOP · no auto order · no rebuy).

  PYTHONPATH=src .venv/bin/python scripts/order/run_us_tw_5m_sell_gate_poll.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from order.us_tw_5m_sell_gate import evaluate_session_poll, write_latest_snapshot  # noqa: E402
from stock_db import PROJECT_ROOT  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "reports" / "order" / "snapshots",
    )
    args = ap.parse_args(argv)

    payload = evaluate_session_poll(session_date=args.session_date)
    path = write_latest_snapshot(payload, out_dir=args.out_dir)
    print(json.dumps({k: payload.get(k) for k in ("ok", "level", "action_hint", "latest", "first_red_poll", "strategy_id")}, ensure_ascii=False, indent=2))
    print(f"Wrote {path}")
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
