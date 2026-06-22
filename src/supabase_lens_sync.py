"""Sync stock_daily_lens · lens_daily_alert from SQLite to Supabase."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

import requests

from stock_db import load_lens_daily_alert, load_stock_daily_lens_for_date
from supabase_research_sync import (
    _headers,
    _supabase_url,
    allow_scheduled_supabase_push,
    supabase_configured,
)

_SCHEMA = "stock_research"
_HIGHLIGHT_TABLE = "stock_daily_highlight"
_ALERT_TABLE = "daily_highlight_alert"


def _rest_url(table: str) -> str:
    base = _supabase_url()
    if not base:
        raise RuntimeError(
            "SUPABASE_URL 或 VITE_PUBLIC_SUPABASE_URL 未設定（見 .env.example）"
        )
    return f"{base.rstrip('/')}/rest/v1/{table}"


def _delete_insert(
    table: str,
    trade_date: str,
    payload: list[dict[str, Any]],
) -> int:
    url = _rest_url(table)
    headers = _headers()
    delete_resp = requests.delete(
        url,
        headers=headers,
        params={"trade_date": f"eq.{trade_date}"},
        timeout=120,
    )
    if delete_resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase {table} delete failed: "
            f"{delete_resp.status_code} {delete_resp.text[:500]}"
        )
    if not payload:
        return 0
    insert_resp = requests.post(
        url,
        headers={**headers, "Prefer": "return=minimal"},
        json=payload,
        timeout=120,
    )
    if insert_resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase {table} insert failed: "
            f"{insert_resp.status_code} {insert_resp.text[:500]}"
        )
    return len(payload)


def _lens_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    codes_raw = row["etf_add_codes_json"] or "[]"
    try:
        etf_add_codes = json.loads(codes_raw)
    except json.JSONDecodeError:
        etf_add_codes = []
    try:
        sources_json = json.loads(row["sources_json"] or "{}")
    except json.JSONDecodeError:
        sources_json = {}

    def _bool(val: object) -> bool:
        return bool(int(val or 0))

    return {
        "trade_date": row["trade_date"],
        "stock_id": row["stock_id"],
        "stock_name": row["stock_name"],
        "etf_add_count": int(row["etf_add_count"] or 0),
        "etf_reduce_count": int(row["etf_reduce_count"] or 0),
        "etf_add_codes": etf_add_codes,
        "etf_flow_ntd": row["etf_flow_ntd"],
        "share_delta_total": row["share_delta_total"],
        "growth_pct": row["growth_pct"],
        "consensus_add": _bool(row["consensus_add"]),
        "consensus_streak_days": int(row["consensus_streak_days"] or 0),
        "breadth_zone_200": row["breadth_zone_200"],
        "trend_posture": row["trend_posture"],
        "regime_aligned": _bool(row["regime_aligned"]),
        "rrg_quadrant": row["rrg_quadrant"],
        "rrg_quadrant_prev": row["rrg_quadrant_prev"],
        "rrg_mono_fresh": _bool(row["rrg_mono_fresh"]),
        "rrg_tier2": _bool(row["rrg_tier2"]),
        "vcp_composite": row["vcp_composite"],
        "vcp_execution_state": row["vcp_execution_state"],
        "vcp_distance_pivot_pct": row["vcp_distance_pivot_pct"],
        "copytrade_l1h9_signal": _bool(row["copytrade_l1h9_signal"]),
        "delta_new_to_watchlist": _bool(row["delta_new_to_watchlist"]),
        "delta_rrg_quadrant_change": row["delta_rrg_quadrant_change"],
        "delta_consensus_new_today": _bool(row["delta_consensus_new_today"]),
        "delta_score_change": row["delta_score_change"],
        "delta_any_signal": _bool(row["delta_any_signal"]),
        "signal_convergence": int(row["signal_convergence"] or 0),
        "highlight_score": row["lens_score"],
        "narrative_zh": row["narrative_zh"],
        "highlight_tier": row["highlight_tier"],
        "holdings_aligned": _bool(row["holdings_aligned"]),
        "data_baseline_date": row["data_baseline_date"],
        "sources_json": sources_json,
        "computed_at": row["computed_at"],
    }


def _alert_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        items_json = json.loads(row["items_json"] or "[]")
    except json.JSONDecodeError:
        items_json = []
    return {
        "trade_date": row["trade_date"],
        "fire_count": int(row["fire_count"] or 0),
        "delta_new_count": int(row["delta_new_count"] or 0),
        "headline_zh": row["headline_zh"],
        "items_json": items_json,
        "computed_at": row["computed_at"],
    }


def sync_stock_daily_lens_to_supabase(
    conn: sqlite3.Connection,
    trade_date: str,
) -> int:
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")
    rows = load_stock_daily_lens_for_date(conn, trade_date)
    payload = [_lens_row_payload(r) for r in rows]
    return _delete_insert(_HIGHLIGHT_TABLE, trade_date, payload)


def sync_lens_daily_alert_to_supabase(
    conn: sqlite3.Connection,
    trade_date: str,
) -> int:
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")
    row = load_lens_daily_alert(conn, trade_date)
    if row is None:
        return _delete_insert(_ALERT_TABLE, trade_date, [])
    return _delete_insert(_ALERT_TABLE, trade_date, [_alert_row_payload(row)])


def sync_lens_bundle_to_supabase(
    conn: sqlite3.Connection,
    trade_date: str,
) -> tuple[int, int]:
    lens_n = sync_stock_daily_lens_to_supabase(conn, trade_date)
    alert_n = sync_lens_daily_alert_to_supabase(conn, trade_date)
    return lens_n, alert_n


def _lens_sync_enabled() -> bool:
    """16:30 排程用 RUN_SUPABASE_LENS_SYNC=1；亦相容 RUN_SUPABASE_RESEARCH_SYNC=1。"""
    lens_flag = os.environ.get("RUN_SUPABASE_LENS_SYNC", "").strip()
    if lens_flag in ("0", "false", "False"):
        return False
    if lens_flag in ("1", "true", "True"):
        return True
    research_flag = os.environ.get("RUN_SUPABASE_RESEARCH_SYNC", "0").strip()
    return research_flag in ("1", "true", "True")


def maybe_sync_lens_bundle_to_supabase(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    scheduled: bool = True,
) -> tuple[int, int] | None:
    if not _lens_sync_enabled():
        return None
    if scheduled and not allow_scheduled_supabase_push(conn):
        return None
    return sync_lens_bundle_to_supabase(conn, trade_date)
