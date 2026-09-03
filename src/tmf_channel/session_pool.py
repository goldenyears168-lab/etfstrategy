"""Reusable Fubon session for long-lived TMF poll worker."""

from __future__ import annotations

import threading
import time
from typing import Any

from order.fubon_session import FubonSession, connect_fubon, safe_logout

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "session": None,
    "realtime_ok": False,
    "born_mono": 0.0,
    "login_count": 0,
}


def reset_session_pool() -> None:
    with _LOCK:
        _STATE["session"] = None
        _STATE["realtime_ok"] = False
        _STATE["born_mono"] = 0.0


def ensure_realtime(session: FubonSession) -> None:
    """Call SDK init_realtime at most once per session object."""
    # Use ``is True`` so MagicMock stand-ins in tests are not treated as ready.
    if getattr(session, "_tmf_realtime_ok", False) is True:
        return
    session.init_realtime()
    setattr(session, "_tmf_realtime_ok", True)
    with _LOCK:
        if _STATE.get("session") is session:
            _STATE["realtime_ok"] = True


def get_fubon_session(
    *,
    realtime: bool = True,
    max_age_sec: float = 3500.0,
    force_new: bool = False,
) -> FubonSession:
    """Return a live Fubon session, refreshing on age / force / failure."""
    with _LOCK:
        sess = _STATE.get("session")
        born = float(_STATE.get("born_mono") or 0.0)
        age = time.monotonic() - born if born else 1e9
        cached = (
            sess
            if (not force_new and sess is not None and age < max_age_sec)
            else None
        )
    if cached is not None:
        if realtime:
            # Cache hit may hold a realtime=False session; upgrade in place.
            ensure_realtime(cached)
        return cached  # type: ignore[return-value]

    session = connect_fubon(realtime=False)
    if realtime:
        session.init_realtime()
        setattr(session, "_tmf_realtime_ok", True)
    with _LOCK:
        old_sess = _STATE.get("session")
        _STATE["session"] = session
        _STATE["realtime_ok"] = bool(realtime)
        _STATE["born_mono"] = time.monotonic()
        _STATE["login_count"] = int(_STATE.get("login_count") or 0) + 1
    if old_sess is not None and old_sess is not session:
        safe_logout(old_sess)          # 新 session 已就位才登出舊的，失敗無害
    return session


def pool_stats() -> dict[str, Any]:
    with _LOCK:
        born = float(_STATE.get("born_mono") or 0.0)
        return {
            "has_session": _STATE.get("session") is not None,
            "realtime_ok": bool(_STATE.get("realtime_ok")),
            "age_sec": round(time.monotonic() - born, 1) if born else None,
            "login_count": int(_STATE.get("login_count") or 0),
        }
