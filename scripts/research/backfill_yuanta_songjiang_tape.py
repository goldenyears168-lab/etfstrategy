#!/usr/bin/env python3
"""Backfill 元大-松江 per-stock net buy/sell into stock_broker_branch_daily.

Uses FinMind TaiwanStockTradingDailyReport queried by securities_trader_id
(one request per trading day).

  PYTHONPATH=src .venv/bin/python scripts/research/backfill_yuanta_songjiang_tape.py
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_yuanta_songjiang_tape.py \\
      --start 2024-07-01 --end 2026-07-16
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

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

SOURCE = "finmind"
REQUEST_DELAY_SEC = 0.35
# Prefer exact FinMind display name; id resolved / asserted at runtime.
YUANTA_SONGJIANG_NAME_HINTS = ("元大-松江", "元大松江")
# Common id seen in public chip sites; verified against TaiwanSecuritiesTraderInfo.
DEFAULT_TRADER_ID = "9801"  # FinMind TaiwanSecuritiesTraderInfo · 元大-松江


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def resolve_yuanta_songjiang_trader(
    *,
    prefer_id: str = DEFAULT_TRADER_ID,
) -> tuple[str, str]:
    """Return (securities_trader_id, securities_trader name)."""
    rows = fetch_finmind_dataset("TaiwanSecuritiesTraderInfo")
    matches: list[tuple[str, str]] = []
    for r in rows:
        name = str(r.get("securities_trader") or r.get("name") or "")
        tid = str(r.get("securities_trader_id") or r.get("id") or "")
        if not tid or not name:
            continue
        if any(h in name for h in YUANTA_SONGJIANG_NAME_HINTS) or (
            "元大" in name and "松江" in name
        ):
            matches.append((tid, name))
    if not matches:
        raise RuntimeError(
            "TaiwanSecuritiesTraderInfo: no trader matching 元大+松江"
        )
    for tid, name in matches:
        if tid == prefer_id:
            return tid, name
    # Prefer hyphenated display name if present.
    for tid, name in matches:
        if "元大-松江" in name:
            return tid, name
    return matches[0]


def aggregate_day_rows(
    raw: list[dict],
    *,
    securities_trader_id: str,
    trade_date: str,
) -> list[dict]:
    """Sum price-level buy/sell → per stock_id net (shares)."""
    by_stock: dict[str, dict[str, float | str]] = {}
    trader_name = ""
    for item in raw:
        sid = str(item.get("stock_id") or "").strip()
        if not sid or not sid.isdigit() or len(sid) != 4:
            continue
        buy = float(item.get("buy") or item.get("Buy") or 0)
        sell = float(item.get("sell") or item.get("Sell") or 0)
        name = str(item.get("securities_trader") or "")
        if name:
            trader_name = name
        slot = by_stock.get(sid)
        if slot is None:
            by_stock[sid] = {"buy": buy, "sell": sell}
        else:
            slot["buy"] = float(slot["buy"]) + buy
            slot["sell"] = float(slot["sell"]) + sell
    out: list[dict] = []
    for sid, agg in by_stock.items():
        buy = float(agg["buy"])
        sell = float(agg["sell"])
        out.append(
            {
                "trade_date": trade_date,
                "securities_trader_id": securities_trader_id,
                "securities_trader": trader_name or "元大-松江",
                "stock_id": sid,
                "buy": buy,
                "sell": sell,
                "net": buy - sell,
                "source": SOURCE,
            }
        )
    return out


def list_calendar_dates(conn, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM stock_daily_bars
        WHERE source = 'finmind' AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def existing_tape_dates(conn, trader_id: str, start: str, end: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM stock_broker_branch_daily
        WHERE securities_trader_id = ? AND source = ?
          AND trade_date >= ? AND trade_date <= ?
        """,
        (trader_id, SOURCE, start, end),
    ).fetchall()
    return {str(r[0]) for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill 元大-松江 branch tape")
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

    trader_id, trader_name = resolve_yuanta_songjiang_trader(
        prefer_id=str(args.trader_id)
    )
    print(f"trader: id={trader_id} name={trader_name}")

    start = _parse_day(args.start).isoformat()
    end = _parse_day(args.end).isoformat()
    conn = connect(args.db)
    try:
        calendar = list_calendar_dates(conn, start, end)
        if not calendar:
            print(f"ERROR: no stock_daily_bars dates in {start}..{end}", file=sys.stderr)
            return 1
        have = set() if args.force else existing_tape_dates(conn, trader_id, start, end)
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
