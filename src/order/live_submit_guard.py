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
    """True when a test runner (pytest / unittest) or explicit forbid is active.

    Must never hit the broker under any test harness. The production order poll
    runs via a plain script (run_*_position_poll.py), whose __main__ package is
    not "unittest" and whose argv carries neither "pytest" nor "unittest", so
    these checks stay false in live use — no false-positive block.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if _env_truthy("ORDER_LIVE_FORBIDDEN"):
        return True
    # pytest collection / invocation before PYTEST_CURRENT_TEST is set
    if any("pytest" in str(a) for a in sys.argv):
        return True
    # python -m unittest / coverage run -m unittest (the CI runner)
    main = sys.modules.get("__main__")
    if getattr(main, "__package__", None) == "unittest":
        return True
    if any("unittest" in str(a) for a in sys.argv):
        return True
    return False


def today_session_date() -> str:
    return datetime.now(tz=_TZ).strftime("%Y-%m-%d")


def live_submit_block_reason(*, session_date: str | None = None) -> str | None:
    """Return a short reason to refuse live submit, or None if allowed.

    Last-line policy for every path that reaches ``place_resolved_order``:
    test runners, explicit forbid, master kill-switch, and backdated sessions
    all refuse — including callers that pass ``dry_run=False`` explicitly.
    """
    if under_test_runner():
        return "live_submit_blocked:test_runner_or_ORDER_LIVE_FORBIDDEN"
    # Fail-closed · unset / anything other than truthy means no live broker.
    # Checked here (not only in sleeve auto_submit) so explicit dry_run=False
    # cannot bypass ORDER_MASTER_ENABLED=0 (2026-08-04 unittest→2377 incident).
    if not _env_truthy("ORDER_MASTER_ENABLED"):
        return "live_submit_blocked:ORDER_MASTER_ENABLED=0"
    if session_date and not _env_truthy("ORDER_ALLOW_BACKDATED_SESSION"):
        today = today_session_date()
        if str(session_date).strip() != today:
            return f"live_submit_blocked:session_date={session_date}!=today={today}"
    return None


def assert_live_submit_allowed(*, session_date: str | None = None) -> None:
    reason = live_submit_block_reason(session_date=session_date)
    if reason:
        raise RuntimeError(reason)
