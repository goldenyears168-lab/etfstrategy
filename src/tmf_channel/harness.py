"""Shared research harness — always evaluates live PAPER_RECIPE.

Usage::

    from tmf_channel.harness import run_days, summarize_days
    from order.tmf_channel_config import PAPER_RECIPE

    rows = run_days(["2026-07-01", "2026-07-02"], recipe=PAPER_RECIPE)
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import RECIPE_VERSION
from tmf_channel.cache_store import load_day
from tmf_channel.engine import load_vixtwn_delta, simulate, summarize


def recipe_hash(recipe: dict[str, Any] | None = None) -> str:
    r = recipe or PAPER_RECIPE
    blob = json.dumps(r, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def engine_id() -> str:
    return "tmf_channel.causal_engine"


def _arrays_from_rows(rows: list[dict[str, Any]]):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [str(r.get("t") or "") for r in rows]
    return O, H, L, C, V, T


def run_day(
    day: str,
    *,
    recipe: dict[str, Any] | None = None,
    cache_name: str = "tx_1m_fullnight_cache_full.json",
    vix_delta: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Simulate one calendar session from a TX 1m cache (research proxy)."""
    recipe = deepcopy(recipe or PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe.setdefault("recipe_version", RECIPE_VERSION)
    rows = load_day(day, source=cache_name)
    if not rows:
        return {"ok": False, "day": day, "reason": "missing_day"}
    O, H, L, C, V, T = _arrays_from_rows(rows)
    vix = vix_delta if vix_delta is not None else (load_vixtwn_delta() or {})
    trades, events, ws, wl, rvol, regime, open_pos = simulate(
        O, H, L, C, V, T, recipe, vix_delta=vix
    )
    s = summarize(trades) if trades else {"n": 0, "net": 0.0}
    return {
        "ok": True,
        "day": day,
        "n_trades": int(s.get("n") or 0),
        "net": float(s.get("net") or 0.0),
        "open_pos": open_pos,
        "recipe_version": recipe.get("recipe_version"),
        "recipe_hash": recipe_hash(recipe),
        "engine": engine_id(),
        "cache": cache_name,
        "n_bars": len(C),
        "n_events": len(events or []),
        "want_s": ws[-1] if ws else None,
        "want_l": wl[-1] if wl else None,
        "regime": regime[-1] if regime else None,
    }


def run_days(
    days: list[str],
    *,
    recipe: dict[str, Any] | None = None,
    cache_name: str = "tx_1m_fullnight_cache_full.json",
) -> list[dict[str, Any]]:
    vix = load_vixtwn_delta() or {}
    return [
        run_day(d, recipe=recipe, cache_name=cache_name, vix_delta=vix) for d in days
    ]


def summarize_days(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("ok")]
    nets = [float(r["net"]) for r in ok]
    n_tr = [int(r["n_trades"]) for r in ok]
    if not nets:
        return {"n_days": 0, "net": 0.0, "mean": 0.0, "wr_days": 0.0}
    pos = sum(1 for x in nets if x > 0)
    return {
        "n_days": len(nets),
        "net": round(sum(nets), 1),
        "mean": round(sum(nets) / len(nets), 1),
        "median": round(sorted(nets)[len(nets) // 2], 1),
        "best": max(nets),
        "worst": min(nets),
        "wr_days": round(100.0 * pos / len(nets), 1),
        "trades_total": sum(n_tr),
        "trades_mean": round(sum(n_tr) / len(n_tr), 1) if n_tr else 0.0,
        "trades_min": min(n_tr) if n_tr else 0,
        "trades_max": max(n_tr) if n_tr else 0,
        "recipe_hash": ok[0].get("recipe_hash"),
        "engine": engine_id(),
    }


def assert_live_recipe(recipe: dict[str, Any]) -> None:
    """Hard fail if a lab hand-copied an obsolete hang band."""
    ver = str(recipe.get("recipe_version") or "")
    if ver and ver != RECIPE_VERSION:
        raise AssertionError(
            f"recipe_version={ver!r} != live {RECIPE_VERSION!r} — "
            "import order.tmf_channel_config.PAPER_RECIPE"
        )
