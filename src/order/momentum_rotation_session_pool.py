"""Reusable Fubon session for momentum-rotation KeepAlive poll worker.

跟 dayflip_short_session_pool.py 同一套「快取 session、按存活時間過期重登」手法
（見該檔 docstring 沿革），這裡是同構複製，不建議跟其他 sleeve 共用同一個 pool
模組實例——每個 sleeve 各自的 session 生命週期／重試邏輯要能獨立除錯，這是
repo 既有的（刻意的）重複，不是疏漏。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from order.fubon_session import FubonSession, connect_fubon, safe_logout

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
    with _LOCK:
        sess = _STATE.get("session")
        born = float(_STATE.get("born_mono") or 0.0)
        age = time.monotonic() - born if born else 1e9
        cached = sess if (not force_new and sess is not None and age < max_age_sec) else None
    if cached is not None:
        return cached  # type: ignore[return-value]

    session = connect_fubon()
    with _LOCK:
        old_sess = _STATE.get("session")
        _STATE["session"] = session
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
            "age_sec": round(time.monotonic() - born, 1) if born else None,
            "login_count": int(_STATE.get("login_count") or 0),
        }
