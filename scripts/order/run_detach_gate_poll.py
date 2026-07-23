#!/usr/bin/env python3
"""Detach Gate poll · evaluate → notify → optional half-flatten @ bid1.

  PYTHONPATH=src .venv/bin/python scripts/order/run_detach_gate_poll.py
  ORDER_DETACH_GATE_DRY_RUN=1  # default · preview only
  ORDER_DETACH_GATE_DRY_RUN=0 ORDER_DETACH_GATE_AUTO_SUBMIT=1  # live half sell
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from order.detach_gate_order import (  # noqa: E402
    load_ledger,
    mark_order_notified,
    mark_signal_notified,
    notify_orders,
    notify_signal,
    run_half_flatten,
    save_ledger,
    session_already_flattened,
    session_dry_run_done,
    session_live_attempted,
)
from order.us_tw_5m_sell_gate import evaluate_session_poll, write_latest_snapshot  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import PROJECT_ROOT  # noqa: E402


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="Detach Gate 5m poll")
    ap.add_argument("--session-date", default=None)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=PROJECT_ROOT / "reports" / "order" / "snapshots",
    )
    ap.add_argument("--no-order", action="store_true", help="Evaluate + notify only")
    ap.add_argument("--force-order", action="store_true", help="Attempt flatten even if not RED now (latched OK)")
    args = ap.parse_args(argv)

    if not _env_flag("RUN_DETACH_GATE", "1"):
        print("DETACH_GATE_SKIP=RUN_DETACH_GATE=0")
        return 0

    eval_payload = evaluate_session_poll(session_date=args.session_date)
    path = write_latest_snapshot(eval_payload, out_dir=args.out_dir)
    nq_refresh = eval_payload.get("nq_refresh")
    if nq_refresh:
        print(f"DETACH_GATE_NQ_REFRESH={json.dumps(nq_refresh, ensure_ascii=False)}")
    print("DETACH_GATE_EVAL=1")
    summary = {
        k: eval_payload.get(k)
        for k in (
            "ok",
            "level",
            "action_hint",
            "first_red_poll",
            "first_yellow_poll",
            "latest",
            "strategy_id",
            "tw_source",
            "nq_source",
            "error",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {path}")

    if not eval_payload.get("ok"):
        return 2

    day = str(eval_payload.get("session_date"))
    level = str(eval_payload.get("level") or "")
    redish = level in {"RED", "RED_LATCHED"} or bool(eval_payload.get("first_red_poll"))

    ledger = load_ledger()
    first_signal = False
    if redish:
        first_signal = mark_signal_notified(
            ledger,
            day,
            level=level,
            poll=str((eval_payload.get("latest") or {}).get("poll") or ""),
        )
        save_ledger(ledger)
        if first_signal:
            print("DETACH_GATE_SIGNAL=1")
            try:
                notify_signal(eval_payload, first=True)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: signal notify failed: {exc}")

    order_payload = None
    should_order = redish and not args.no_order
    if args.force_order:
        should_order = True
    dry_env = _env_flag("ORDER_DETACH_GATE_DRY_RUN", "1")
    if should_order and session_already_flattened(ledger, day):
        print("DETACH_GATE_ORDER_SKIP=already_done_today")
    elif (
        should_order
        and (not dry_env)
        and session_live_attempted(ledger, day)
        and not args.force_order
    ):
        # Prior live wave already hit Fubon (ok or rejects) · never re-place today.
        print("DETACH_GATE_ORDER_SKIP=already_attempted_today")
    elif (
        should_order
        and dry_env
        and session_dry_run_done(ledger, day)
        and not args.force_order
    ):
        # Prior dry-run already previewed + emailed · do not re-trigger launcher mail.
        print("DETACH_GATE_ORDER_SKIP=dry_run_already_today")
    elif should_order:
        order_payload = run_half_flatten(session_date=day, force=bool(args.force_order))
        # Reload: subprocess dry/live may have updated ledger on disk.
        ledger = load_ledger()
        status = str(order_payload.get("status") or "")
        # already_* means no new attempt · do not arm DETACH_GATE_ORDER=1.
        if status in {"already_done", "already_dry_run", "already_attempted"}:
            print(f"DETACH_GATE_ORDER_SKIP={status}")
        else:
            first_order = mark_order_notified(ledger, day)
            save_ledger(ledger)
            print(
                json.dumps(
                    {
                        k: order_payload.get(k)
                        for k in ("status", "dry_run", "reason", "n_actionable")
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            for row in order_payload.get("orders") or []:
                print(
                    f"  {row.get('symbol')}: {row.get('status')} "
                    f"sell={row.get('sell_qty')}/{row.get('holdings_qty')} bid={row.get('bid_price')}"
                )
            if first_order:
                print("DETACH_GATE_ORDER=1")
                try:
                    notify_orders(order_payload)
                except Exception as exc:  # noqa: BLE001
                    print(f"WARN: order notify failed: {exc}")
            else:
                # Should be rare now that live attempts latch; log only.
                print("DETACH_GATE_ORDER_RETRY=1")
            op_path = args.out_dir / f"detach_gate_orders_{day.replace('-', '')}.json"
            args.out_dir.mkdir(parents=True, exist_ok=True)
            op_path.write_text(
                json.dumps(order_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"Wrote {op_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
