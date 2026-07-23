#!/usr/bin/env python3
"""Poll disposition Live TA → ops.live_ta（mini · 30–60s 安全重入）.

Examples:
  PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py
  PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py --stocks 2492:華新科 --dry-run
  OPS_LIVE_TA_STOCKS=2492:華新科,8046 PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv  # noqa: E402
from ops_console_sync import supabase_ops_configured  # noqa: E402
from ops_live_ta import parse_stock_list, run_live_ta_poll  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upsert ops.live_ta disposition auction state")
    parser.add_argument(
        "--stocks",
        default="",
        help="Comma list stock_id[:name] · default OPS_LIVE_TA_STOCKS or 2492:華新科",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute only · no Supabase write")
    parser.add_argument("--json", action="store_true", help="Print JSON rows")
    args = parser.parse_args(argv)

    load_project_dotenv()
    stocks = parse_stock_list(args.stocks or None)
    if not args.dry_run and not supabase_ops_configured():
        print(
            "錯誤：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定（見 .env.example）",
            file=sys.stderr,
        )
        return 2

    conn = None
    try:
        from stock_db import connect

        conn = connect()
    except Exception:  # noqa: BLE001
        conn = None

    try:
        rows = run_live_ta_poll(stocks, conn=conn, dry_run=args.dry_run)
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(
                f"{r['stock_id']} {r.get('stock_name') or ''} "
                f"px={r.get('last_print')} phase={r.get('phase')} "
                f"action={r.get('action')} next={r.get('next_auction_at')}"
            )
            print(f"  {r.get('note_zh')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
