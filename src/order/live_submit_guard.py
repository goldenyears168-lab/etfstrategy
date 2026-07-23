"""Hard gates for live Fubon submit · refuse pytest / forbidden / backdated sessions."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")
_TRUE = frozenset({"1", "true", "yes", "on"})


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in _TRUE


def under_test_runner() -> bool:
    """True when pytest (or explicit forbid) must never hit the broker."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if _env_truthy("ORDER_LIVE_FORBIDDEN"):
        return True
    # pytest collection / invocation before PYTEST_CURRENT_TEST is set
    if any("pytest" in str(a) for a in sys.argv):
        return True
    return False


def today_session_date() -> str:
    return datetime.now(tz=_TZ).strftime("%Y-%m-%d")


def live_submit_block_reason(*, session_date: str | None = None) -> str | None:
    """Return a short reason to refuse live submit, or None if allowed."""
    if under_test_runner():
        return "live_submit_blocked:test_runner_or_ORDER_LIVE_FORBIDDEN"
    if session_date and not _env_truthy("ORDER_ALLOW_BACKDATED_SESSION"):
        today = today_session_date()
        if str(session_date).strip() != today:
            return f"live_submit_blocked:session_date={session_date}!=today={today}"
    return None


def assert_live_submit_allowed(*, session_date: str | None = None) -> None:
    reason = live_submit_block_reason(session_date=session_date)
    if reason:
        raise RuntimeError(reason)
