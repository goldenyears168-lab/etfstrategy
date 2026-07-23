#!/usr/bin/env python3
"""Expert-pool staged gap/05/25 gate.

  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_expert_pool_staged_gate.py
  ORDER_EP_STAGED_GATE_DRY_RUN=1 ORDER_EP_STAGED_GATE_IGNORE_CLOCK=1 \\
    ORDER_EP_STAGED_GATE_STAGE=open \\
    PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_expert_pool_staged_gate.py --date 2026-07-23
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv

_TZ = ZoneInfo("Asia/Taipei")


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument(
        "--stage",
        choices=("open", "e05", "e25", "auto"),
        default="auto",
        help="Force stage; auto uses wall clock windows",
    )
    ap.add_argument("--force-dry-run", action="store_true")
    ap.add_argument(
        "--ignore-clock",
        action="store_true",
        help="Allow --stage outside calendar window",
    )
    args = ap.parse_args()
    if args.ignore_clock:
        os.environ["ORDER_EP_STAGED_GATE_IGNORE_CLOCK"] = "1"
    day = args.date or datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    stage = None if args.stage == "auto" else args.stage
    if stage and args.ignore_clock:
        os.environ["ORDER_EP_STAGED_GATE_STAGE"] = stage

    from order.expert_pool_staged_gate import run_staged_gate

    out = run_staged_gate(
        session_date=day,
        stage=stage,
        dry_run=True if args.force_dry_run else None,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("EP_STAGED_GATE=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
