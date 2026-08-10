"""TMF channel local ledger · day PnL / API budget / last desired state."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_db import DATA_DIR, PROJECT_ROOT

_TZ = ZoneInfo("Asia/Taipei")


def trading_day_str(now: datetime | None = None) -> str:
    """Session-aware trading-day label, not raw calendar date.

    TMF night session runs today 15:00 -> tomorrow 05:00; a plain calendar-date
    compare in roll_day() would flip the ledger (killed flag, api_calls_day,
    broker_pos) at midnight, mid-session — silently re-arming a tripped day-loss
    circuit breaker while the same overnight session is still live. 00:00-04:59
    belongs to the trading day that opened at 15:00 the previous evening.
    """
    now = now or datetime.now(tz=_TZ)
    if now.hour < 5:
        return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.date().strftime("%Y-%m-%d")


def _resolve(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    # prefer state root data/
    cand = DATA_DIR / path.removeprefix("data/")
    if str(path).startswith("data/"):
        return DATA_DIR / path[len("data/") :]
    return PROJECT_ROOT / p


def load_ledger(path: str) -> dict[str, Any]:
    fp = _resolve(path)
    if not fp.exists():
        return _empty()
    try:
        return json.loads(fp.read_text())
    except Exception:
        return _empty()


def save_ledger(path: str, data: dict[str, Any]) -> None:
    fp = _resolve(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(fp)


def _empty() -> dict[str, Any]:
    return {
        "schema": "tmf-channel-ledger-v1",
        "day": None,
        "api_calls_day": 0,
        "day_pnl_pts": 0.0,
        "killed": False,
        "kill_reason": None,
        "last_symbol": None,
        "last_desired": None,
        "actions_tail": [],
        "broker_pos": None,
        # Independent, sim-state-free max-hold safety net (2026-08-07): simulate()'s
        # own max_hold_bars only fires off its internal lots[0]['eb'], which gets
        # bypassed the moment broker_live becomes authoritative for open_pos (see
        # tmf_channel_order.reconcile_once) — a position can then sit for hours with
        # zero max-hold enforcement if the bar-replay ever loses sync with the real
        # broker position. This tracks wall-clock open time independently.
        "position_open_ts": None,
        "position_open_sig": None,
        # Quiet-cancellation hysteresis (2026-08-08): apply_quiet_flat_entry_gate
        # only cancels an already-resting rail once the cell's pv has stayed in
        # the quiet set (e.g. "dry") continuously for quiet_hysteresis_min.
        # Symmetric on the exit side too (quiet_not_quiet_since): a single
        # poll leaving the quiet set does not reset quiet_pv_since, only a
        # sustained exit (quiet_exit_debounce_min) does — otherwise a market
        # drifting briefly in/out of "dry" still matured the streak every
        # ~2-3min and cancel+replaced the identical resting price each time.
        # See that function's docstring for the live incident this fixes.
        "quiet_pv_since": None,
        "quiet_pv_value": None,
        "quiet_not_quiet_since": None,
        # Cancel-rate throttle (2026-08-08): per-side timestamp of the last
        # CANCEL fired for a want-became-None-via-quiet reason. Never touched
        # for block-caused cancels or price-drift cancels — see
        # should_throttle_quiet_cancel() in tmf_channel_order.py. Confirmed
        # via true re-simulation that smoothing the classifier itself (the
        # alternative) is not worth its safety cost, so this throttles the
        # order layer's redundant API round trips instead.
        "cancel_throttle_last": {"S": None, "L": None},
        # Consecutive broker-rejected order actions (2026-08-10): counts
        # api-calling actions (place/cancel/exit) with ok=False in a row,
        # across polls; any ok=True action resets it to 0. Found live: a
        # 財力證明額度 (financial-capacity-proof quota) rejection made the
        # worker retry the same failing SELL every poll indefinitely,
        # burning API calls with no way to succeed until the account-side
        # issue was fixed. See reconcile_once()'s kill_triggers check.
        "consecutive_order_failures": 0,
    }


def roll_day(data: dict[str, Any]) -> dict[str, Any]:
    today = trading_day_str()
    if data.get("day") != today:
        data = _empty()
        data["day"] = today
    return data


def record_actions(data: dict[str, Any], actions: list[dict], *, api_n: int) -> dict[str, Any]:
    data = roll_day(data)
    data["api_calls_day"] = int(data.get("api_calls_day") or 0) + int(api_n)
    tail = list(data.get("actions_tail") or [])
    tail.extend(actions)
    data["actions_tail"] = tail[-80:]
    streak = int(data.get("consecutive_order_failures") or 0)
    for act in actions:
        if not act.get("counts_api"):
            continue
        streak = 0 if act.get("ok") else streak + 1
    data["consecutive_order_failures"] = streak
    return data
