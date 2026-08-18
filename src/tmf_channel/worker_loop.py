"""Long-lived TMF channel poll worker · single launchd label, reused session.

Replaces cold StartInterval→login every 60s. Still one process path
(``com.jackm4.goldenstocks.tmf-channel-poll``); no nohup side daemon.
"""

from __future__ import annotations

import faulthandler
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from order.tmf_channel_marketdata import in_tmf_trade_window, session_hhmm_now
from order.tmf_channel_order import reconcile_once
from project_dotenv import load_project_dotenv
from tmf_channel.session_pool import get_fubon_session, pool_stats, reset_session_pool

_TZ = ZoneInfo("Asia/Taipei")
_STOP = False

# 2026-08-17 incident: PID 18734 entered reconcile_once() at 09:07 on
# 2026-08-14 and never returned — 3d16h pinned at 100% CPU in a pure-Python
# loop (verified via /usr/bin/sample: main thread spinning in
# _PyEval_EvalFrameDefault, every Fubon SDK thread parked on a semaphore),
# producing zero log lines and zero reconciles for the whole period. Nothing
# in the stack detected it: run_forever() had no timeout around
# reconcile_once(), launchd KeepAlive only restarts on *exit*, and a live
# process burning a core looks identical to a healthy one from the outside.
# The root cause could not be recovered after the fact — py-spy needs root
# and CPython 3.13 has no external attach API (that lands in 3.14 / PEP 768),
# so a hung worker is un-introspectable unless it was armed beforehand.
#
# faulthandler.dump_traceback_later() runs in its own C thread, so it fires
# even when the interpreter is stuck in a Python-level infinite loop that
# never releases to a signal handler. exit=True writes every thread's Python
# stack to the incident file and then _exit()s, which is what lets launchd
# KeepAlive resurrect the worker. Trading-safe by construction: the new
# worker rebuilds all position state from the broker (broker is
# authoritative), so dying mid-reconcile is strictly better than hanging.
_WATCHDOG_ENV = "ORDER_TMF_CHANNEL_RECONCILE_WATCHDOG_SEC"
_WATCHDOG_DEFAULT_SEC = 120.0
# Documented target is ~0.19s/round; observed 2026-08-14 was a steady ~6s
# (never flagged anywhere). Warn so a 30x regression cannot hide again.
_SLOW_POLL_WARN_SEC = 15.0
_incident_fh: Any = None


def _logs_dir() -> Path:
    """``stock_db.LOGS_DIR`` (see CLAUDE.md: never hardcode PROJECT_ROOT/data)."""
    try:
        import stock_db

        return Path(stock_db.LOGS_DIR)
    except Exception:  # noqa: BLE001
        root = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or Path.home() / "goldenstocks-data")
        return root / "logs"


def incident_path() -> Path:
    d = _logs_dir() / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tmf_worker_watchdog.log"


def heartbeat_path() -> Path:
    d = _logs_dir() / "intraday"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tmf_channel_worker_heartbeat.json"


def _watchdog_sec() -> float:
    try:
        val = float(os.environ.get(_WATCHDOG_ENV) or _WATCHDOG_DEFAULT_SEC)
    except (TypeError, ValueError):
        return _WATCHDOG_DEFAULT_SEC
    return val if val > 0 else 0.0


def _arm_watchdog(seconds: float, *, phase: str) -> None:
    """Dump every thread's Python stack then hard-exit if ``phase`` overruns."""
    global _incident_fh
    if seconds <= 0:
        return
    if _incident_fh is None:
        _incident_fh = incident_path().open("a", buffering=1, encoding="utf-8")
        # Banner once per process, NOT once per poll: at a 20s cadence the
        # per-poll version wrote ~4,300 lines/day of "nothing happened" into a
        # file whose entire purpose is to be empty unless something did. If the
        # watchdog fires, faulthandler appends the dump here itself.
        _incident_fh.write(
            f"\n=== watchdog active pid={os.getpid()} timeout={seconds:.0f}s "
            f"phase={phase} since={datetime.now(tz=_TZ).isoformat()} ===\n"
        )
    faulthandler.dump_traceback_later(seconds, repeat=False, file=_incident_fh, exit=True)


def _disarm_watchdog() -> None:
    faulthandler.cancel_dump_traceback_later()


def write_heartbeat(payload: dict[str, Any]) -> None:
    """Externally-checkable liveness marker (mtime is the signal).

    A hung worker keeps its PID and its launchd state, so process existence
    proves nothing — this file's mtime is the only cheap way for an outside
    monitor (or the user) to tell "reconciling" from "wedged".
    """
    try:
        path = heartbeat_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # never let a heartbeat write failure kill the worker


def _prior_incident_note() -> dict[str, Any] | None:
    """Surface a previous watchdog kill / stale heartbeat at startup."""
    try:
        hb = heartbeat_path()
        if not hb.exists():
            return None
        age_sec = time.time() - hb.stat().st_mtime
        prior = json.loads(hb.read_text(encoding="utf-8"))
        if age_sec < 300:
            return None
        return {
            "prior_heartbeat_age_sec": round(age_sec),
            "prior_heartbeat_ts": prior.get("ts"),
            "prior_phase": prior.get("phase"),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return None


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
    watchdog_sec = _watchdog_sec()
    start_evt = {
        "event": "worker_start",
        "ts": datetime.now(tz=_TZ).isoformat(),
        "pid": os.getpid(),
        "interval": _sleep_sec(in_window=True),
        "idle": _sleep_sec(in_window=False),
        "watchdog_sec": watchdog_sec,
        "incident_log": str(incident_path()),
    }
    prior = _prior_incident_note()
    if prior:
        start_evt["prior_incident"] = prior
    print(json.dumps(start_evt, ensure_ascii=False), flush=True)
    while not _STOP:
        hm = session_hhmm_now()
        win = in_tmf_trade_window(hm)
        if not win:
            write_heartbeat(
                {"ts": datetime.now(tz=_TZ).isoformat(), "pid": os.getpid(), "phase": "idle", "hhmm": hm}
            )
            if once:
                break
            time.sleep(_sleep_sec(in_window=False))
            continue
        write_heartbeat(
            {"ts": datetime.now(tz=_TZ).isoformat(), "pid": os.getpid(), "phase": "reconcile_enter", "hhmm": hm}
        )
        t0 = time.monotonic()
        summary: dict[str, Any]
        try:
            # Warm / refresh pooled session before reconcile (reconcile may
            # still call get_fubon_session via order path after wiring).
            # Both calls are inside the watchdog: the 2026-08-14 hang was in
            # this block and there is no evidence pinning it to either half.
            _arm_watchdog(watchdog_sec, phase="reconcile")
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
        finally:
            _disarm_watchdog()
        elapsed = round(time.monotonic() - t0, 2)
        summary["worker_elapsed_sec"] = elapsed
        if elapsed >= _SLOW_POLL_WARN_SEC:
            summary["slow_poll_warn"] = f"{elapsed:.1f}s >= {_SLOW_POLL_WARN_SEC:.0f}s"
        summary["session_pool"] = pool_stats()
        print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
        write_heartbeat(
            {
                "ts": datetime.now(tz=_TZ).isoformat(),
                "pid": os.getpid(),
                "phase": "reconcile_done",
                "hhmm": hm,
                "elapsed_sec": elapsed,
                "reason": summary.get("reason"),
            }
        )
        if once:
            return 0 if summary.get("ok") or summary.get("reason") in (
                "outside_session",
                "ORDER_TMF_CHANNEL_ENABLED=0",
            ) else 1
        # Jitter-free sleep; launchd KeepAlive restarts us if we exit.
        deadline = time.monotonic() + _sleep_sec(in_window=True)
        while not _STOP and time.monotonic() < deadline:
            # max(0.0, ...) — the loop condition and this subtraction are not
            # atomic; a negative remainder would make time.sleep() raise and
            # take the worker down through the KeepAlive restart path.
            time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
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
