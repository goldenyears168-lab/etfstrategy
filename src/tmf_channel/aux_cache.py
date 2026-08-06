"""TTL caches for VIX / gap-bias aux loads (avoid full-table scan every poll)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_LOCK = threading.Lock()
_STORE: dict[str, tuple[float, Any]] = {}


def get_cached(key: str, ttl_sec: float, loader: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _LOCK:
        hit = _STORE.get(key)
        if hit is not None and now - hit[0] < ttl_sec:
            return hit[1]
    value = loader()
    with _LOCK:
        _STORE[key] = (now, value)
    return value


def clear_aux_cache() -> None:
    with _LOCK:
        _STORE.clear()


def load_vixtwn_1m_cached(*, ttl_sec: float = 300.0):
    from tmf_channel.engine import load_vixtwn_1m

    return get_cached("vixtwn_1m", ttl_sec, load_vixtwn_1m)


def load_vixtwn_delta_cached(*, ttl_sec: float = 600.0):
    from tmf_channel.engine import load_vixtwn_delta

    return get_cached("vixtwn_delta", ttl_sec, load_vixtwn_delta)
