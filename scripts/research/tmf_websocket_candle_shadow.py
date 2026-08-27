#!/usr/bin/env python3
"""2026-08-13: standalone, read-only shadow listener for Fubon's futopt
market-data WEBSOCKET (fubon_neo.adapter.build_websocket_client, backed by
the same Fugle realtime protocol) -- evaluating whether it can replace the
REST intraday.candles() polling in order.tmf_channel_marketdata, which has
been hitting Fugle 429 rate limits intermittently all session (~11% of
polls today, self-recovering but skipping that poll's flatten/exit check).

sdk.init_realtime() defaults to Mode.Speed, and Speed mode explicitly
REJECTS the 'candles'/'aggregates' channels for futopt (confirmed via
fubon_neo.adapter.WebSocketFutOptClientWrapper.subscribe -- raises on those
channels in Speed mode). This script deliberately requests Mode.Normal on
a SEPARATE session so it can try the 'candles' channel; it does NOT touch
FubonSession.init_realtime() (used by the live worker, defaults to Speed),
so the live worker's behavior is completely unaffected by this script.

The exact subscribe param schema / push message shape for futopt candles is
undocumented (no docstrings, no .pyi, no examples in the vendored package --
confirmed via source read of fugle_marketdata/websocket/**). Same posture as
tmf_futopt_fill_event_listener.py: log everything observed, don't assume a
shape, let real traffic tell us the field names before any live integration
is considered.

Phase: evaluation only. Does NOT modify order.tmf_channel_marketdata or any
live control flow. A real swap (REST -> websocket candles) in the live
worker is a separate, bigger step requiring explicit go-ahead once this
confirms candles actually arrive with usable OHLC fields.

Writes one JSON line per received message to
~/goldenstocks-data/cache/tmf_channel/tick_seconds/futopt_ws_candle_shadow_{YYYY-MM-DD}.jsonl

Run: PYTHONPATH=src .venv-fubon/bin/python scripts/research/tmf_websocket_candle_shadow.py
Stop: kill the process (find via `pgrep -f tmf_websocket_candle_shadow`).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.fubon_session import connect_fubon  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "tmf_channel" / "tick_seconds"
SYMBOL = "TMFH6"


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"futopt_ws_candle_shadow_{now.strftime('%Y-%m-%d')}.jsonl"


def _log(kind: str, payload) -> None:
    now = datetime.now(tz=_TZ)
    rec = {"ts": now.isoformat(timespec="milliseconds"), "kind": kind, "payload": payload}
    try:
        with _out_path(now).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never crash the listener
        print(f"[{now.isoformat(timespec='seconds')}] log write error: {exc}", flush=True)
    print(f"[{now.isoformat(timespec='seconds')}] {kind}: {json.dumps(payload, ensure_ascii=False, default=str)[:300]}", flush=True)


def main() -> int:
    from fubon_neo.adapter import Mode

    session = connect_fubon(realtime=False)
    print("init_realtime(mode=Normal) on a separate session (live worker stays on Speed)...", flush=True)
    session.sdk.init_realtime(mode=Mode.Normal)

    ws = session.sdk.marketdata.websocket_client.futopt

    ws.on("connect", lambda: _log("connect", None))
    ws.on("disconnect", lambda code, msg: _log("disconnect", {"code": code, "msg": msg}))
    ws.on("error", lambda err: _log("error", str(err)))
    ws.on("authenticated", lambda msg: _log("authenticated", msg))
    ws.on("message", lambda raw: _log("message", json.loads(raw)))

    print("connecting...", flush=True)
    ws.connect()
    print(f"connected. auth_status={ws.auth_status} error={ws.error}", flush=True)

    for channel in ("candles", "trades"):
        try:
            ws.subscribe({"channel": channel, "symbol": SYMBOL})
            _log("subscribe_sent", {"channel": channel, "symbol": SYMBOL})
        except Exception as exc:  # noqa: BLE001 -- Speed/Normal channel restrictions raise here
            _log("subscribe_error", {"channel": channel, "symbol": SYMBOL, "error": str(exc)})

    print("listening (observe-only, no orders, no live control flow touched)...", flush=True)
    n_report = 0
    last_report = time.monotonic()
    while True:
        time.sleep(1.0)
        if time.monotonic() - last_report > 300:
            print(f"[{datetime.now(tz=_TZ).isoformat(timespec='seconds')}] still listening, {n_report} 5-min reports so far", flush=True)
            last_report = time.monotonic()
            n_report += 1


if __name__ == "__main__":
    raise SystemExit(main())
