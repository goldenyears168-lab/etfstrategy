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

    from order.expert_pool_staged_gate import load_gate_config, run_staged_gate

    # 今日無任何有效 job（config jobs 全停用或 once_date 已過期）時，不要靜默空轉。
    # once_date 為空 = 每日皆有效；否則必須等於今日才算有效。
    # 印出明顯 marker（launcher 會據此每日寄一次提醒），並跳過（連富邦都不連）。
    cfg = load_gate_config()
    jobs = cfg.get("jobs") or []
    active_today = [
        j
        for j in jobs
        if isinstance(j, dict)
        and j.get("enabled", True)
        and str(j.get("once_date") or "") in ("", day)
    ]
    if jobs and not active_today:
        stale = sorted({str(j.get("once_date")) for j in jobs if isinstance(j, dict)})
        print(
            json.dumps(
                {
                    "status": "no_active_jobs",
                    "session_date": day,
                    "configured_jobs": len(jobs),
                    "stale_once_dates": stale,
                    "hint": (
                        "更新 config/order.yaml 的 expert_pool_staged_gate.jobs"
                        "（symbol／signal_close／once_date=今日）以啟用"
                    ),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        print("EP_STAGED_GATE_NO_ACTIVE_JOBS=1")
        print("EP_STAGED_GATE=1")
        return 0

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
