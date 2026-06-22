"""VCP funnel · JSON snapshot for Supabase / Readdy (vcp-daily-v1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from research.backtest.chunge_funnel_backtest import ChungeCandidate
from vcp_funnel_specs_daily import (
    ENTRY_HINTS,
    SPEC_REGISTRY,
    SPEC_TITLES,
    SPEC_VARIANTS,
    load_spec_candidates,
    resolve_spec_key,
)

_TPE = ZoneInfo("Asia/Taipei")
CONTRACT = "vcp-daily-v1"

BRIEF_TYPE_SPECS: dict[str, tuple[str, ...]] = {
    "vcp_funnel_specs": ("pivot_gate", "coil_close"),
    "vcp_pivot_gate": ("pivot_gate",),
    "vcp_coil_close": ("coil_close",),
}

BRIEF_TYPE_LAYER: dict[str, str] = {
    "vcp_funnel_specs": "research",
    "vcp_pivot_gate": "strategy",
    "vcp_coil_close": "strategy",
}

BRIEF_TYPE_STRATEGY_ID: dict[str, str] = {
    "vcp_pivot_gate": "vcp-pivot-gate",
    "vcp_coil_close": "vcp-coil-close",
    "vcp_funnel_specs": "vcp-pivot-gate",
}

VCP_HEADERS = ["代號", "名稱", "composite", "state", "pivot", "dist%", "stop"]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _candidate_dict(c: ChungeCandidate) -> dict[str, Any]:
    return {
        "stock_id": c.stock_id,
        "stock_name": c.stock_name,
        "composite_score": c.composite_score,
        "execution_state": c.execution_state,
        "entry_ready": c.entry_ready,
        "pivot_price": c.pivot_price,
        "stop_loss": c.stop_loss,
        "distance_from_pivot_pct": c.distance_from_pivot_pct,
    }


def _table_row(c: ChungeCandidate) -> list[str | int | float | None]:
    pivot_s = f"{c.pivot_price:.2f}" if c.pivot_price else "—"
    dist_s = (
        f"{c.distance_from_pivot_pct:.1f}"
        if c.distance_from_pivot_pct is not None
        else "—"
    )
    stop_s = f"{c.stop_loss:.2f}" if c.stop_loss else "—"
    return [
        c.stock_id,
        c.stock_name,
        round(c.composite_score, 1),
        c.execution_state,
        pivot_s,
        dist_s,
        stop_s,
    ]


def _build_variant(
    conn: sqlite3.Connection,
    as_of: str,
    spec_key: str,
    *,
    intraday: bool,
    top_n: int = 15,
) -> dict[str, Any]:
    key = resolve_spec_key(spec_key)
    screen_day, cands, meta = load_spec_candidates(
        conn, as_of, key, top_n=top_n, intraday=intraday
    )
    return {
        "spec_key": key,
        "name": SPEC_TITLES[key],
        "variant_id": SPEC_VARIANTS[key],
        "entry_rules": ENTRY_HINTS[key],
        "screen_as_of": screen_day,
        "intraday": intraday,
        "candidate_count": len(cands),
        "headers": VCP_HEADERS,
        "rows": [_table_row(c) for c in cands],
        "candidates": [_candidate_dict(c) for c in cands],
        "meta": _json_safe(meta) if meta else None,
    }


def build_vcp_snapshot_json(
    conn: sqlite3.Connection,
    as_of: str,
    brief_type: str,
    *,
    schedule_slot: str = "1630",
    top_n: int = 15,
) -> dict[str, Any]:
    """PIT-safe VCP payload for Readdy (no MD parsing)."""
    if brief_type not in BRIEF_TYPE_SPECS:
        raise ValueError(f"unsupported VCP brief_type: {brief_type}")

    intraday = schedule_slot == "1300"
    spec_keys = BRIEF_TYPE_SPECS[brief_type]
    variants = [
        _build_variant(conn, as_of, spec_key, intraday=intraday, top_n=top_n)
        for spec_key in spec_keys
    ]
    total_candidates = sum(v["candidate_count"] for v in variants)

    payload: dict[str, Any] = {
        "contract": CONTRACT,
        "layer": BRIEF_TYPE_LAYER[brief_type],
        "brief_type": brief_type,
        "as_of": as_of,
        "schedule_slot": schedule_slot,
        "intraday": intraday,
        "variant_count": len(variants),
        "candidate_count": total_candidates,
        "variants": variants,
        "meta": {
            "generated_at": datetime.now(_TPE).isoformat(),
            "top_n": top_n,
        },
    }
    if brief_type in BRIEF_TYPE_STRATEGY_ID:
        payload["strategy_id"] = BRIEF_TYPE_STRATEGY_ID[brief_type]
    return payload
