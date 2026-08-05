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
    return data
