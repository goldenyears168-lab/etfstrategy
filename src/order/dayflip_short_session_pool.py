"""Reusable Fubon session for long-lived dayflip-short poll worker.

2026-08-08: dayflip-short-poll 原本是 StartInterval 60s 冷啟動，08:44-13:41 每分鐘
一支全新 process、每次 connect_fubon() 全新登入（~297 次/日）。跟 TMF 之前的舊架構
同一個問題（見 tmf_channel/session_pool.py 的沿革），這裡直接複用同一套「快取 session、
按存活時間過期重登」手法。dayflip-short 不用即時報價串流（fetch_open_price/
fetch_last_price 都是同步 REST 查詢），所以比 TMF 版本簡單——不用 ensure_realtime。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from order.fubon_session import FubonSession, connect_fubon

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "session": None,
    "born_mono": 0.0,
    "login_count": 0,
}


def reset_session_pool() -> None:
    with _LOCK:
        _STATE["session"] = None
        _STATE["born_mono"] = 0.0


def get_fubon_session(*, max_age_sec: float = 3500.0, force_new: bool = False) -> FubonSession:
    """Return a live Fubon session, refreshing on age / force / failure."""
    with _LOCK:
        sess = _STATE.get("session")
        born = float(_STATE.get("born_mono") or 0.0)
        age = time.monotonic() - born if born else 1e9
        cached = sess if (not force_new and sess is not None and age < max_age_sec) else None
    if cached is not None:
        return cached  # type: ignore[return-value]

    session = connect_fubon()
    with _LOCK:
        _STATE["session"] = session
        _STATE["born_mono"] = time.monotonic()
        _STATE["login_count"] = int(_STATE.get("login_count") or 0) + 1
    return session


def pool_stats() -> dict[str, Any]:
    with _LOCK:
        born = float(_STATE.get("born_mono") or 0.0)
        return {
            "has_session": _STATE.get("session") is not None,
            "age_sec": round(time.monotonic() - born, 1) if born else None,
            "login_count": int(_STATE.get("login_count") or 0),
        }
