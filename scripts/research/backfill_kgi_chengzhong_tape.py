#!/usr/bin/env python3
"""Backfill 凱基-城中 per-stock net buy/sell into stock_broker_branch_daily.

Same pipeline as backfill_yuanta_songjiang_tape.py (FinMind
TaiwanStockTradingDailyReport by securities_trader_id).

  PYTHONPATH=src .venv/bin/python scripts/research/backfill_kgi_chengzhong_tape.py
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_kgi_chengzhong_tape.py \\
      --start 2024-07-01 --end 2026-07-17
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from copytrade.branch_signals import (  # noqa: E402
    DEFAULT_TRADER_ID_KGI_CHENGZHONG,
)
from finmind_client import (  # noqa: E402
    fetch_finmind_dataset,
    fetch_taiwan_stock_trading_daily_report,
    finmind_token,
)
from stock_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    upsert_stock_broker_branch_daily,
)

sys.path.insert(0, str(ROOT / "scripts" / "research"))
from backfill_yuanta_songjiang_tape import (  # noqa: E402
    aggregate_day_rows,
    existing_tape_dates,
    list_calendar_dates,
)

SOURCE = "finmind"
REQUEST_DELAY_SEC = 0.35
NAME_HINTS = ("凱基-城中", "凱基城中")
DEFAULT_TRADER_ID = DEFAULT_TRADER_ID_KGI_CHENGZHONG
BRANCH_LABEL = "凱基-城中"


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def resolve_kgi_chengzhong_trader(
    *,
    prefer_id: str = DEFAULT_TRADER_ID,
) -> tuple[str, str]:
    rows = fetch_finmind_dataset("TaiwanSecuritiesTraderInfo")
    matches: list[tuple[str, str]] = []
    for r in rows:
        name = str(r.get("securities_trader") or r.get("name") or "")
        tid = str(r.get("securities_trader_id") or r.get("id") or "")
        if not tid or not name:
            continue
        if any(h in name for h in NAME_HINTS) or (
            "凱基" in name and "城中" in name
        ):
            matches.append((tid, name))
    if not matches:
        raise RuntimeError(
            "TaiwanSecuritiesTraderInfo: no trader matching 凱基+城中"
        )
    for tid, name in matches:
        if tid == prefer_id:
            return tid, name
    for tid, name in matches:
        if "凱基-城中" in name:
            return tid, name
    return matches[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill 凱基-城中 branch tape")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--trader-id", default=DEFAULT_TRADER_ID)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch days that already have rows",
    )
    ap.add_argument(
        "--max-days",
        type=int,
        default=0,
        help="Cap number of API days (0 = no cap); useful for smoke tests",
    )
    args = ap.parse_args()

    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    trader_id, trader_name = resolve_kgi_chengzhong_trader(
        prefer_id=str(args.trader_id)
    )
    print(f"trader: id={trader_id} name={trader_name}")

    start = _parse_day(args.start).isoformat()
    end = _parse_day(args.end).isoformat()
    conn = connect(args.db)
    try:
        calendar = list_calendar_dates(conn, start, end)
        if not calendar:
            print(
                f"ERROR: no stock_daily_bars dates in {start}..{end}",
                file=sys.stderr,
            )
            return 1
        have = (
            set()
            if args.force
            else existing_tape_dates(conn, trader_id, start, end)
        )
        todo = [d for d in calendar if d not in have]
        if args.max_days > 0:
            todo = todo[: args.max_days]
        print(
            f"window {start}..{end} calendar={len(calendar)} "
            f"have={len(have)} fetch={len(todo)}"
        )
        n_rows = 0
        n_days = 0
        errors: list[str] = []
        for i, day in enumerate(todo, 1):
            try:
                raw = fetch_taiwan_stock_trading_daily_report(
                    trade_date=day,
                    securities_trader_id=trader_id,
                )
                rows = aggregate_day_rows(
                    raw,
                    securities_trader_id=trader_id,
                    trade_date=day,
                )
                for row in rows:
                    if not row.get("securities_trader") or row[
                        "securities_trader"
                    ] == "元大-松江":
                        row["securities_trader"] = trader_name or BRANCH_LABEL
                n_rows += upsert_stock_broker_branch_daily(conn, rows)
                n_days += 1
                if i % 20 == 0 or i == len(todo):
                    print(
                        f"  [{i}/{len(todo)}] {day} stocks={len(rows)} "
                        f"cum_rows={n_rows}"
                    )
            except Exception as exc:  # noqa: BLE001 — continue backfill
                errors.append(f"{day}: {exc}")
                print(f"  WARN {day}: {exc}", file=sys.stderr)
            time.sleep(max(0.0, float(args.delay)))
        print(
            f"done days_ok={n_days} rows_upserted={n_rows} errors={len(errors)}"
        )
        if errors[:5]:
            for e in errors[:5]:
                print(f"  err: {e}", file=sys.stderr)
        return 0 if n_days or not todo else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
