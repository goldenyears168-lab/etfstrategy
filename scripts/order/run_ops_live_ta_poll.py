#!/usr/bin/env python3
"""Poll holdings Live TA → ops.live_ta（mini）.

Universe = order_holdings_snapshot ∪ ops.holdings ∪ OPS_LIVE_TA_STOCKS extras.
Disposition auction clock only for OPS_LIVE_TA_DISPOSITION（default 2492）.

Default one-shot (legacy). Production uses ``--loop``: one Fubon login, then
quote/candles every ``--interval-sec`` until ``--end-time`` so in-process TTL
caches actually work.

Examples:
  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_ops_live_ta_poll.py
  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_ops_live_ta_poll.py --loop
  PYTHONPATH=src .venv-fubon/bin/python scripts/order/run_ops_live_ta_poll.py --stocks 2492:華新科 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

_TPE = ZoneInfo("Asia/Taipei")


def _parse_hm(trade_date: str, hm: str) -> datetime:
    return datetime.strptime(f"{trade_date} {hm}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TPE)


def _print_rows(rows: list[dict], disp: set[str]) -> None:
    print(f"universe n={len(rows)} · disposition={sorted(disp)}")
    for r in rows:
        anchors = r.get("anchors") or {}
        mode = anchors.get("mode")
        qsrc = anchors.get("quote_source") or "?"
        print(
            f"{r['stock_id']} {r.get('stock_name') or ''} "
            f"mode={mode} px={r.get('last_print')} phase={r.get('phase')} "
            f"action={r.get('action')} src={qsrc} next={r.get('next_auction_at')}"
        )
        print(f"  {r.get('note_zh')}")


def _resolve_stocks(conn, args: argparse.Namespace) -> list[tuple[str, str | None]]:
    if args.stocks_only:
        return parse_stock_list(args.stocks or None)
    if args.stocks.strip():
        return resolve_live_ta_universe(conn, extras_raw=args.stocks)
    return resolve_live_ta_universe(conn)


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
    parser.add_argument("--json", action="store_true", help="Print JSON rows (one-shot only)")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Long-running: one Fubon login, poll until --end-time (production)",
    )
    parser.add_argument(
        "--interval-sec",
        type=float,
        default=float(__import__("os").environ.get("OPS_LIVE_TA_INTERVAL_SEC", "15") or "15"),
        help="Loop sleep target seconds (default 15 / OPS_LIVE_TA_INTERVAL_SEC)",
    )
    parser.add_argument(
        "--start-time",
        default=__import__("os").environ.get("OPS_LIVE_TA_START", "08:50:00"),
        help="Loop window start HH:MM:SS (default 08:50:00)",
    )
    parser.add_argument(
        "--end-time",
        default=__import__("os").environ.get("OPS_LIVE_TA_END", "13:35:00"),
        help="Loop window end HH:MM:SS (default 13:35:00)",
    )
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

    disp = parse_disposition_ids()

    if not args.loop:
        stocks = _resolve_stocks(conn, args)
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
            _print_rows(rows, disp)
        return 0

    # --- long-running path ---
    trade_date = datetime.now(_TPE).strftime("%Y-%m-%d")
    try:
        start_dt = _parse_hm(trade_date, args.start_time)
        end_dt = _parse_hm(trade_date, args.end_time)
    except ValueError as exc:
        print(f"錯誤：時間格式 {exc}", file=sys.stderr)
        return 2

    now = datetime.now(_TPE)
    if now > end_dt:
        print(f"[INFO] 已過窗口結束 {args.end_time}，結束。")
        if conn is not None:
            conn.close()
        return 0
    if now < start_dt:
        wait_sec = (start_dt - now).total_seconds()
        print(f"[INFO] 等待開窗 {args.start_time}（{wait_sec:.0f}s）…")
        time.sleep(wait_sec)

    session = None
    try:
        from order.fubon_session import connect_fubon

        print("[INFO] 登入富邦 Neo（realtime）一次，之後迴圈重用 session…", flush=True)
        session = connect_fubon(realtime=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 富邦登入失敗：{exc}", file=sys.stderr, flush=True)
        if conn is not None:
            conn.close()
        return 1

    n_rounds = 0
    try:
        while datetime.now(_TPE) <= end_dt:
            round_start = time.monotonic()
            # Refresh universe each round (holdings may change).
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from stock_db import connect

                    conn = connect()
                except Exception:  # noqa: BLE001
                    conn = None
            stocks = _resolve_stocks(conn, args)
            try:
                rows = run_live_ta_poll(
                    stocks,
                    conn=conn,
                    dry_run=args.dry_run,
                    disposition_ids=disp,
                    fubon_session=session,
                )
                n_rounds += 1
                print(
                    f"[OK] round={n_rounds} n={len(rows)} "
                    f"at {datetime.now(_TPE).strftime('%H:%M:%S')} "
                    f"quote={((rows[0].get('anchors') or {}).get('quote_source') if rows else None)}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 — keep loop alive
                print(f"[WARN] round failed: {exc}", file=sys.stderr, flush=True)
            elapsed = time.monotonic() - round_start
            time.sleep(max(0.0, float(args.interval_sec) - elapsed))
        print(f"[INFO] 窗口結束，共 {n_rounds} 輪。", flush=True)
        return 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
