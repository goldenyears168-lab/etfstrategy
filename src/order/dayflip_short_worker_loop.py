"""Long-lived dayflip-short poll worker · single launchd label, reused session.

Replaces cold StartInterval→login every 60s (~297 logins/day, 08:44-13:41).
Same rationale as tmf_channel.worker_loop; mirrors its shape closely so the two
KeepAlive workers in this repo stay easy to reason about side by side.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import stock_db
from order.dayflip_short_order import in_dayflip_short_trade_window, reconcile_once
from order.dayflip_short_session_pool import get_fubon_session, pool_stats, reset_session_pool
from project_dotenv import load_project_dotenv

_TZ = ZoneInfo("Asia/Taipei")
_STOP = False

# 2026-08-08：舊 StartInterval launcher 在 shell 層 grep 這些 action 字串觸發寄信
# （見 dayflip-short-poll-launcher.sh.template 舊版）。KeepAlive worker 常駐後
# launcher 只負責啟動 worker 一次、不會逐 tick 檢查輸出，寄信責任因此搬進這裡，
# 觸發條件維持完全一致。
_ALERT_ACTIONS = frozenset(
    {
        "entered", "entry_failed", "force_closed", "entry_exception",
        "force_close_exception", "entry_exception_limit_reached",
    }
)
_FAILURE_ACTIONS = frozenset(
    {"entry_failed", "entry_exception", "force_close_exception", "entry_exception_limit_reached"}
)


def _on_signal(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    print(f"dayflip-short-worker signal {signum} → stop after tick", flush=True)


def _sleep_sec(*, in_window: bool) -> float:
    # Malformed env must not crash the KeepAlive worker into a restart loop.
    key, default = (
        ("ORDER_DAYFLIP_SHORT_WORKER_INTERVAL", 20.0)
        if in_window
        else ("ORDER_DAYFLIP_SHORT_WORKER_IDLE", 60.0)
    )
    try:
        val = float(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _hm_now() -> str:
    return datetime.now(tz=_TZ).strftime("%H:%M")


def _tick_log_path() -> Path:
    stamp = datetime.now(tz=_TZ).strftime("%Y%m%d")
    return stock_db.LOGS_DIR / "intraday" / f"dayflip_short_{stamp}.log"


def _maybe_alert(summary: dict[str, Any], *, ok: bool) -> None:
    """比照舊 launcher 的每日一封 dedup（flag file），觸發條件不變：
    entered/entry_failed/force_closed/entry_exception/force_close_exception，
    現在額外涵蓋 worker 自己的例外（舊架構下這種例外會讓整個 process 崩潰、
    launcher 靠非零 exit code 這條路徑補寄；worker 常駐後同一條路徑改走這裡）。
    """
    action = str(summary.get("action") or "")
    if action not in _ALERT_ACTIONS and ok:
        return
    if os.environ.get("RUN_DAYFLIP_SHORT_EMAIL", "1").strip().lower() in ("0", "false"):
        return
    stamp = datetime.now(tz=_TZ).strftime("%Y%m%d")
    flag = stock_db.LOGS_DIR / "intraday" / f"dayflip_short_mail_{stamp}.flag"
    if flag.exists():
        return
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        success = ok and action not in _FAILURE_ACTIONS
        subprocess.run(
            [
                str(stock_db.PROJECT_ROOT / ".venv" / "bin" / "python"),
                str(stock_db.PROJECT_ROOT / "scripts" / "notify_job_result.py"),
                "--subject-prefix", "隔日沖做空 sleeve",
                "--exit-code", "0" if success else "1",
                "--log-path", str(_tick_log_path()),
                "--extra", json.dumps(summary, ensure_ascii=False, default=str),
                "--env-flag", "RUN_DAYFLIP_SHORT_EMAIL",
            ],
            check=False,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"alert_error: {exc}", flush=True)


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
        hm = _hm_now()
        win = in_dayflip_short_trade_window(hm)
        if not win:
            if once:
                break
            time.sleep(_sleep_sec(in_window=False))
            continue
        t0 = time.monotonic()
        summary: dict[str, Any]
        ok = True
        try:
            get_fubon_session()
            summary = reconcile_once(use_session_pool=True)
        except Exception as exc:  # noqa: BLE001
            reset_session_pool()
            ok = False
            summary = {
                "action": "worker_exception",
                "error": str(exc)[:300],
                "trace": traceback.format_exc()[-500:],
            }
        elapsed = round(time.monotonic() - t0, 2)
        summary["worker_elapsed_sec"] = elapsed
        summary["session_pool"] = pool_stats()
        print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
        _maybe_alert(summary, ok=ok)
        if once:
            return 0 if ok else 1
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

    ap = argparse.ArgumentParser(description="dayflip-short long-lived poll worker")
    ap.add_argument("--once", action="store_true", help="single tick then exit")
    args = ap.parse_args(argv)
    return run_forever(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
