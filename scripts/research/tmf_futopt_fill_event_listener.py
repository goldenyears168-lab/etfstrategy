#!/usr/bin/env python3
"""2026-08-13: standalone, read-only observer for Fubon's futopt order/fill
PUSH events (set_on_futopt_filled / set_on_futopt_order / set_on_order_
futopt_changed) -- Phase 1 of a WebSocket/event-driven integration explored
after tonight's live-monitoring finding that market-order (exit_market)
actions never record a confirmed fill price anywhere (only the order
intent + last-quoted spot before send, silently assuming zero slippage
exactly when the safety net fires because price is already moving hard
against the position).

fubon_neo.CoreSDK exposes set_on_futopt_filled/set_on_futopt_order/
set_on_order_futopt_changed (confirmed via introspection -- no docstrings,
compiled extension, so this script's whole purpose is to empirically
observe what payload shape these actually deliver before designing
anything around them). This is NOT the market-data websocket_client
(quotes/candles/trades) already explored elsewhere tonight -- these are
account-level order/execution push callbacks, a separate mechanism,
currently used NOWHERE in this repo (confirmed via grep across src/order/).

Deliberately scoped to Phase 1 only: OBSERVE, do not act. A real
integration into the live worker's reconcile_once() control flow is a much
bigger change (touches the live order-submission path) and is explicitly
NOT what this script does -- it runs a SEPARATE Fubon session, read-only,
alongside the live worker, and just logs whatever these callbacks deliver
for the account's real activity (including fills from the live worker's
own trades, since callbacks are account-scoped push, not tied to which
session placed the order).

Payload objects are serialized via the SAME bounded, safe key-allowlist
pattern already used for order responses (order.fubon_futopt_orders.
_serialize) -- per that module's own warning, NEVER call dir()/vars() on a
live Fubon SDK object (seen to hang / balloon RSS on some handles,
2026-08-05). If the payload isn't a recognized type, only its class name is
logged, never a raw introspection dump.

Writes one JSON line per event to
~/goldenstocks-data/cache/tmf_channel/tick_seconds/futopt_fill_events_{YYYY-MM-DD}.jsonl

Run: PYTHONPATH=src .venv-fubon/bin/python scripts/research/tmf_futopt_fill_event_listener.py
Stop: kill the process (find via `pgrep -f tmf_futopt_fill_event_listener`).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from order.fubon_futopt_orders import _serialize  # noqa: E402
from order.fubon_session import connect_fubon  # noqa: E402
from stock_db import DATA_DIR  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
OUT_DIR = DATA_DIR.parent / "cache" / "tmf_channel" / "tick_seconds"


def _out_path(now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"futopt_fill_events_{now.strftime('%Y-%m-%d')}.jsonl"


def _log_event(kind: str, payload) -> None:
    now = datetime.now(tz=_TZ)
    rec = {
        "ts": now.isoformat(timespec="milliseconds"),
        "kind": kind,
        "payload_type": type(payload).__name__,
        "payload": _serialize(payload),
    }
    try:
        with _out_path(now).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 -- logging must never crash the listener
        print(f"[{now.isoformat(timespec='seconds')}] log write error: {exc}", flush=True)
    print(f"[{now.isoformat(timespec='seconds')}] EVENT {kind}: {json.dumps(rec['payload'], ensure_ascii=False, default=str)[:300]}", flush=True)


def on_futopt_filled(*args) -> None:
    # Unknown real arity/shape until observed live -- log every positional
    # arg the SDK actually calls us with, don't assume a single payload.
    for i, a in enumerate(args):
        _log_event(f"futopt_filled[{i}]", a)


def on_futopt_order(*args) -> None:
    for i, a in enumerate(args):
        _log_event(f"futopt_order[{i}]", a)


def on_order_futopt_changed(*args) -> None:
    for i, a in enumerate(args):
        _log_event(f"order_futopt_changed[{i}]", a)


def main() -> int:
    session = connect_fubon(realtime=False)
    sdk = session.sdk
    print("registering futopt order/fill event callbacks...", flush=True)
    sdk.set_on_futopt_filled(on_futopt_filled)
    sdk.set_on_futopt_order(on_futopt_order)
    sdk.set_on_order_futopt_changed(on_order_futopt_changed)
    print("registered. listening (observe-only, no orders ever placed by this script)...", flush=True)

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
