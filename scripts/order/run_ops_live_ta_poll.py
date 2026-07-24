#!/usr/bin/env python3
"""Poll holdings Live TA → ops.live_ta（mini · ~60s 安全重入）.

Universe = order_holdings_snapshot ∪ ops.holdings ∪ OPS_LIVE_TA_STOCKS extras.
Disposition auction clock only for OPS_LIVE_TA_DISPOSITION（default 2492）.

Examples:
  PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py
  PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py --stocks 2492:華新科 --dry-run
  OPS_LIVE_TA_STOCKS=2492:華新科 PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py
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
from ops_live_ta import (  # noqa: E402
    parse_disposition_ids,
    parse_stock_list,
    resolve_live_ta_universe,
    run_live_ta_poll,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upsert ops.live_ta for holdings (+ optional disposition auction)"
    )
    parser.add_argument(
        "--stocks",
        default="",
        help="Comma list stock_id[:name] extras · default: holdings ∪ OPS_LIVE_TA_STOCKS",
    )
    parser.add_argument(
        "--stocks-only",
        action="store_true",
        help="Ignore holdings; use --stocks / OPS_LIVE_TA_STOCKS only",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute only · no Supabase write")
    parser.add_argument("--json", action="store_true", help="Print JSON rows")
    args = parser.parse_args(argv)

    load_project_dotenv()
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

    if args.stocks_only:
        stocks = parse_stock_list(args.stocks or None)
    elif args.stocks.strip():
        # Explicit CLI extras still merge with holdings
        stocks = resolve_live_ta_universe(conn, extras_raw=args.stocks)
    else:
        stocks = resolve_live_ta_universe(conn)

    disp = parse_disposition_ids()
    try:
        rows = run_live_ta_poll(
            stocks,
            conn=conn,
            dry_run=args.dry_run,
            disposition_ids=disp,
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"universe n={len(rows)} · disposition={sorted(disp)}")
        for r in rows:
            mode = (r.get("anchors") or {}).get("mode")
            print(
                f"{r['stock_id']} {r.get('stock_name') or ''} "
                f"mode={mode} px={r.get('last_print')} phase={r.get('phase')} "
                f"action={r.get('action')} next={r.get('next_auction_at')}"
            )
            print(f"  {r.get('note_zh')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
