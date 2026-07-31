#!/usr/bin/env python3
"""Leading Dip Order poll · dual-sleeve (main coverage + mid ≥10:00 open-anchor).

  PYTHONPATH=src .venv/bin/python scripts/order/run_leading_dip_poll.py
  PYTHONPATH=src .venv/bin/python scripts/order/run_leading_dip_poll.py --date 2026-07-15 --time 10:35

Launchd: com.jackm4.goldenstocks.leading-dip-poll · Mon–Fri 09:05–13:25 / 5m

Default: DRY_RUN=1 · AUTO_SUBMIT=0. Live:
  ORDER_LEADING_DIP_DRY_RUN=0 ORDER_LEADING_DIP_AUTO_SUBMIT=1
Budget: ORDER_LEADING_DIP_BUDGET_TWD / ORDER_LEADING_DIP_MID_BUDGET_TWD (yaml default 20000).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")
SNAP_DIR = ROOT / "reports" / "order" / "snapshots"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Leading Dip order poll")
    p.add_argument("--date", metavar="YYYY-MM-DD")
    p.add_argument("--time", metavar="HH:MM")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument(
        "--force-dry-run",
        action="store_true",
        help="Override env and force dry_run=1 for this tick",
    )
    return p.parse_args()


def _write_snapshot(payload: dict, day: str) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = day.replace("-", "")
    path = SNAP_DIR / f"leading_dip_{stamp}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    path.write_text(text, encoding="utf-8")
    (SNAP_DIR / "leading_dip_latest.json").write_text(text, encoding="utf-8")
    return path


def main() -> int:
    load_project_dotenv()
    args = _parse_args()
    now = datetime.now(tz=_TZ)
    day = args.date or now.strftime("%Y-%m-%d")
    minute = args.time or now.strftime("%H:%M")

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 1

    from order.leading_dip_order import process_leading_dip_poll
    from order.leading_dip_order_config import load_leading_dip_order_config

    cfg = load_leading_dip_order_config()
    dry = True if args.force_dry_run else cfg.dry_run

    conn = connect(args.db)
    try:
        payload = process_leading_dip_poll(
            session_date=day,
            poll_minute=minute,
            conn=conn,
            dry_run=dry,
            cfg=cfg,
        )
    finally:
        conn.close()

    snap = _write_snapshot(payload, day)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    # Machine-readable markers for launchd email filters
    print("LEADING_DIP_POLL=1")
    entries = payload.get("entries") or []
    mid_entries = payload.get("mid_entries") or []
    exits = payload.get("exits") or []
    mid_exits = payload.get("mid_exits") or []
    live_entry = any(
        str(e.get("status") or "")
        in {"submitted", "filled", "partial", "working", "dry_run", "queued_no_submit"}
        for e in list(entries) + list(mid_entries)
    )
    live_exit = any(
        str(e.get("status") or "")
        not in {"", "skip"}
        for e in list(exits) + list(mid_exits)
    )
    if live_entry:
        print("LEADING_DIP_ENTRY=1")
    if any(
        str(e.get("status") or "")
        in {"submitted", "filled", "partial", "working", "dry_run", "queued_no_submit"}
        for e in mid_entries
    ):
        print("LEADING_DIP_MID_ENTRY=1")
    if live_exit:
        print("LEADING_DIP_EXIT=1")

    print(
        f"status={payload.get('status')} open={payload.get('open_n')} "
        f"room={payload.get('room')} candidates={payload.get('n_candidates_raw')} "
        f"picks={payload.get('n_picks')} mid_active={payload.get('mid_active')} "
        f"mid_cands={payload.get('n_mid_candidates_raw')} mid_picks={payload.get('n_mid_picks')} "
        f"exits={len(exits)} mid_exits={len(mid_exits)} "
        f"entries={len(entries)} mid_entries={len(mid_entries)} "
        f"budget={cfg.budget_twd} mid_budget={cfg.mid_budget_twd} "
        f"dry_run={dry} auto_submit={cfg.auto_submit} snap={snap}"
    )
    return 0 if payload.get("status") not in {"blocked", "error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
