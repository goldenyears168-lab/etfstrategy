#!/usr/bin/env python3
"""2026-08-11: standalone, read-only real-time quote collector -- accumulates
second-level TMF front-month price samples so the "5-second acceleration"
exit/re-entry idea (discussed but explicitly NOT buildable/testable without
real second-level history) can eventually be evaluated against real data
instead of guessed at.

2026-08-12 bug fix: switched .quote() -> .trades(), same fix already applied
to unf_second_quote_collector.py for the identical reason. Confirmed live:
this collector's own file went completely flat at exactly 13:45:00 (the
day-session close) -- 5721+ consecutive rows all showing the day's closing
price 45523.0, through the entire 15:00 night-session reopen and beyond
(~2 hours dead by the time this was caught). .quote() simply keeps
returning the day session's last print and never picks up the new night
session's real trades; unf_second_quote_collector.py's docstring already
documented this exact failure mode for .quote() ("found stale, frozen at
day-session close") when it was fixed the same way. .trades() (genuine
tick-level prints, deduplicated on the trade's own `serial` field) does not
have this problem.

Observe-only. No order submission, no session_side_gate/nq_calib touch, no
interaction with the live TMF worker (scripts/order/run_tmf_channel_worker.py)
or its ledger. Safe to run alongside it -- separate Fubon session, separate
process, read-only quote calls only.

Writes one JSON line per NEW trade to
~/goldenstocks-data/cache/tmf_channel/tick_seconds/{YYYY-MM-DD}.jsonl:
  {"ts": "<iso, exchange's own trade time>", "price": <float>, "size": <int>,
   "bid": <float>, "ask": <float>, "symbol": "<front symbol>", "serial": <int>}
Rows written before this fix (up to 2026-08-12 13:45 that day) used the
older {"ts", "price", "symbol"} schema (no size/bid/ask/serial) -- both
schemas share "ts"/"price"/"symbol" so downstream readers keying off those
three fields are unaffected by the mix within one file.

Run: PYTHONPATH=src .venv-fubon/bin/python scripts/research/tmf_second_quote_collector.py
Stop: kill the process (find via `pgrep -f tmf_second_quote_collector`).
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
IDLE_SEC = 30.0  # outside trade window: sleep longer, don't hammer the API for nothing
RESOLVE_RETRY_SEC = 60.0


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl"


def main() -> int:
    session = connect_fubon(realtime=True)
    sym, name, end = resolve_front_symbol(session)
    print(f"collecting {sym} ({name}) every {POLL_SEC}s via trades() -> {OUT_DIR}", flush=True)

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
                new_sym, name, end = resolve_front_symbol(session, max_age_sec=0.0)
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
