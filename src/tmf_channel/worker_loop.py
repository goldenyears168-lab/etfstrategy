"""Long-lived TMF channel poll worker · single launchd label, reused session.

Replaces cold StartInterval→login every 60s. Still one process path
(``com.jackm4.goldenstocks.tmf-channel-poll``); no nohup side daemon.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from order.tmf_channel_marketdata import in_tmf_trade_window, session_hhmm_now
from order.tmf_channel_order import reconcile_once
from project_dotenv import load_project_dotenv
from tmf_channel.session_pool import get_fubon_session, pool_stats, reset_session_pool

_TZ = ZoneInfo("Asia/Taipei")
_STOP = False


def _on_signal(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    print(f"tmf-worker signal {signum} → stop after tick", flush=True)


def _sleep_sec(*, in_window: bool) -> float:
    # Malformed env must not crash the KeepAlive worker into a restart loop.
    key, default = (
        ("ORDER_TMF_CHANNEL_WORKER_INTERVAL", 20.0)
        if in_window
        else ("ORDER_TMF_CHANNEL_WORKER_IDLE", 60.0)
    )
    try:
        val = float(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def run_forever(*, once: bool = False) -> int:
    global _STOP
    _STOP = False
    load_project_dotenv()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    print(
        json.dumps(
            {
                "event": "worker_start",
                "ts": datetime.now(tz=_TZ).isoformat(),
                "interval": _sleep_sec(in_window=True),
                "idle": _sleep_sec(in_window=False),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    while not _STOP:
        hm = session_hhmm_now()
        win = in_tmf_trade_window(hm)
        if not win:
            if once:
                break
            time.sleep(_sleep_sec(in_window=False))
            continue
        t0 = time.monotonic()
        summary: dict[str, Any]
        try:
            # Warm / refresh pooled session before reconcile (reconcile may
            # still call get_fubon_session via order path after wiring).
            get_fubon_session(realtime=True)
            summary = reconcile_once(force=False, use_session_pool=True)
        except Exception as exc:  # noqa: BLE001
            reset_session_pool()
            summary = {
                "ok": False,
                "reason": "worker_exception",
                "error": str(exc)[:300],
                "trace": traceback.format_exc()[-500:],
            }
        elapsed = round(time.monotonic() - t0, 2)
        summary["worker_elapsed_sec"] = elapsed
        summary["session_pool"] = pool_stats()
        print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
        if once:
            return 0 if summary.get("ok") or summary.get("reason") in (
                "outside_session",
                "ORDER_TMF_CHANNEL_ENABLED=0",
            ) else 1
        # Jitter-free sleep; launchd KeepAlive restarts us if we exit.
        deadline = time.monotonic() + _sleep_sec(in_window=True)
        while not _STOP and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    print(
        json.dumps(
            {"event": "worker_stop", "ts": datetime.now(tz=_TZ).isoformat()},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="TMF channel long-lived poll worker")
    ap.add_argument("--once", action="store_true", help="single tick then exit")
    args = ap.parse_args(argv)
    return run_forever(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
