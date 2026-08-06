"""Desired-state cache · memory + disk (survives worker restart within bar)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

_LOCK = threading.Lock()
_LAST: dict[str, Any] = {}


def _disk_path() -> Path:
    data = os.environ.get("GOLDENSTOCKS_DATA_DIR", "").strip()
    root = Path(data) if data else PROJECT_ROOT
    p = root / "data" / "order"
    p.mkdir(parents=True, exist_ok=True)
    return p / "tmf_desired_cache.json"


def fingerprint_bars(bars: list[dict[str, Any]]) -> str:
    if not bars:
        return "empty"
    last = bars[-1]
    return f"{len(bars)}|{last.get('t')}|{last.get('c')}|{last.get('v')}"


def prefix_fingerprint(bars: list[dict[str, Any]]) -> str:
    """Hash of all-but-last bar (detect append-only growth)."""
    if len(bars) < 2:
        return fingerprint_bars(bars)
    mid = bars[len(bars) // 2]
    first = bars[0]
    prev = bars[-2]
    return (
        f"{len(bars)-1}|{first.get('t')}|{mid.get('t')}|{prev.get('t')}|"
        f"{prev.get('c')}|{prev.get('v')}"
    )


def get_cached_desired(fp: str) -> dict[str, Any] | None:
    with _LOCK:
        hit = _LAST.get("fp")
        if hit == fp:
            payload = _LAST.get("desired")
            if isinstance(payload, dict) and payload.get("ok"):
                out = dict(payload)
                out["desired_cache_hit"] = True
                return out
    # disk fallback (worker restart same bar)
    try:
        raw = json.loads(_disk_path().read_text())
        if raw.get("fp") == fp and isinstance(raw.get("desired"), dict):
            desired = dict(raw["desired"])
            if desired.get("ok"):
                with _LOCK:
                    _LAST["fp"] = fp
                    _LAST["desired"] = desired
                    _LAST["prefix_fp"] = raw.get("prefix_fp")
                desired["desired_cache_hit"] = True
                desired["desired_cache_disk"] = True
                return desired
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return None


def store_desired(
    fp: str,
    desired: dict[str, Any],
    *,
    bars: list[dict[str, Any]] | None = None,
) -> None:
    if not desired.get("ok"):
        return
    payload = dict(desired)
    # Drop heavy lists from disk cache (rails + spot enough for skip).
    disk_desired = {
        k: payload.get(k)
        for k in (
            "ok",
            "want_s",
            "want_l",
            "open_pos",
            "spot",
            "last_t",
            "regime",
            "active_cell",
            "nq_gate",
            "recipe_version",
        )
    }
    # Keep trades/events only in memory for same-process kill/PnL paths.
    prefix_fp = prefix_fingerprint(bars) if bars else None
    with _LOCK:
        _LAST["fp"] = fp
        _LAST["desired"] = payload
        _LAST["prefix_fp"] = prefix_fp
    try:
        _disk_path().write_text(
            json.dumps(
                {"fp": fp, "prefix_fp": prefix_fp, "desired": disk_desired},
                ensure_ascii=False,
                default=str,
            )
        )
    except OSError:
        pass


def clear_desired_cache() -> None:
    with _LOCK:
        _LAST.clear()
    try:
        _disk_path().unlink(missing_ok=True)  # type: ignore[arg-type]
    except TypeError:
        p = _disk_path()
        if p.exists():
            p.unlink()
    except OSError:
        pass
