#!/usr/bin/env python3
"""2026-08-12: real-time NASDAQ-100 collector via TAIFEX's own listed product
(UNF, 美國那斯達克100期貨) instead of Yahoo NQ=F -- confirmed Yahoo runs
~10min behind wall-clock (regularMarketTime), unusable for second-level
work. UNF is a TAIFEX-native contract tracking NASDAQ-100, queryable via
the SAME Fubon session already used for TMF -- no new account, no paid
data vendor.

Uses futopt.intraday.trades() (genuine tick-level prints, newest-first,
millisecond timestamps) -- NOT .quote(), which was found stale (frozen at
day-session close, 42000+ sec lag) for this product's afterhours session
despite candles()/trades() showing real activity only ~1min behind
wall-clock (mostly this script's own fetch/processing time, not exchange
lag). Deduplicates on the trade's own `serial` field so re-polling the
same window doesn't write duplicate rows.

Observe-only, read-only. No order submission, no interaction with the live
TMF worker/ledger -- separate process, separate Fubon session object.

Writes one JSON line per NEW trade to
~/goldenstocks-data/cache/tmf_channel/tick_seconds/unf_{YYYY-MM-DD}.jsonl
  {"ts": "<iso, exchange's own trade time>", "price": <float>, "size": <int>,
   "bid": <float>, "ask": <float>, "symbol": "<front UNF contract>", "serial": <int>}

Run: PYTHONPATH=src .venv-fubon/bin/python scripts/research/unf_second_quote_collector.py
Stop: kill the process (find via `pgrep -f unf_second_quote_collector`).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.fubon_session import connect_fubon  # noqa: E402
from order.tmf_channel_marketdata import in_tmf_trade_window, resolve_front_symbol  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "tmf_channel" / "tick_seconds"
POLL_SEC = 1.0
IDLE_SEC = 30.0
RESOLVE_RETRY_SEC = 60.0


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"unf_{now.strftime('%Y-%m-%d')}.jsonl"


def main() -> int:
    session = connect_fubon(realtime=True)
    sym, name, end = resolve_front_symbol(session, product="UNF")
    print(f"collecting {sym} ({name}, end={end}) every {POLL_SEC}s via trades() -> {OUT_DIR}", flush=True)

    fut = session.sdk.marketdata.rest_client.futopt
    seen_serials: set[int] = set()
    n_written = 0
    last_report = time.monotonic()
    last_resolve = time.monotonic()
    while True:
        now = datetime.now(tz=_TZ)
        hm = now.strftime("%H:%M")
        if not in_tmf_trade_window(hm):
            time.sleep(IDLE_SEC)
            continue
        if time.monotonic() - last_resolve > RESOLVE_RETRY_SEC:
            try:
                new_sym, name, end = resolve_front_symbol(session, product="UNF", max_age_sec=0.0)
                if new_sym != sym:
                    sym = new_sym
                    seen_serials.clear()
                    print(f"[{now.isoformat(timespec='seconds')}] front-month rolled to {sym}", flush=True)
            except Exception as exc:
                print(f"[{now.isoformat(timespec='seconds')}] front-symbol resolve error: {exc}", flush=True)
            last_resolve = time.monotonic()
        try:
            # trades() wants lowercase "afterhours" (unlike tickers()'s
            # uppercase "AFTERHOURS") -- confirmed live, not documented.
            api_session = "REGULAR" if "08:45" <= hm <= "13:45" else "afterhours"
            res = fut.intraday.trades(symbol=sym, session=api_session)
            rows = res.get("data") if isinstance(res, dict) else res
            rows = list(rows or [])
            new_rows = [r for r in rows if int(r.get("serial") or -1) not in seen_serials]
            for r in sorted(new_rows, key=lambda r: int(r.get("serial") or 0)):
                serial = int(r.get("serial") or -1)
                seen_serials.add(serial)
                us = r.get("time")
                trade_ts = (
                    datetime.fromtimestamp(float(us) / 1e6, tz=timezone.utc).astimezone(_TZ)
                    if us
                    else now
                )
                rec = {
                    "ts": trade_ts.isoformat(timespec="milliseconds"),
                    "price": float(r.get("price")),
                    "size": int(r.get("size") or 0),
                    "bid": r.get("bid"),
                    "ask": r.get("ask"),
                    "symbol": sym,
                    "serial": serial,
                }
                with _out_path(trade_ts).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_written += 1
            # keep the dedup set bounded -- old serials never reappear
            if len(seen_serials) > 20000:
                seen_serials = set(sorted(seen_serials)[-10000:])
        except Exception as exc:  # noqa: BLE001 -- best-effort collector, never crash the loop
            print(f"[{now.isoformat(timespec='seconds')}] trades() error: {exc}", flush=True)
        if time.monotonic() - last_report > 300:
            print(f"[{now.isoformat(timespec='seconds')}] {n_written} trades written so far", flush=True)
            last_report = time.monotonic()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
