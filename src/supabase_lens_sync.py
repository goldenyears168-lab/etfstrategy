"""Sync stock_daily_highlight · daily_highlight_alert to Supabase."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from stock_db import load_lens_daily_alert
from supabase_research_sync import (
    _headers,
    _supabase_url,
    allow_scheduled_supabase_push,
    supabase_configured,
)

_TPE = ZoneInfo("Asia/Taipei")

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


def _bool(val: object) -> bool:
    if isinstance(val, bool):
        return val
    return bool(int(val or 0))


def _normalize_highlight_row(row: dict[str, Any]) -> dict[str, Any]:
    """Supabase REST row → builder 可讀的 prev 列（含 lens_score）。"""
    out = dict(row)
    if "lens_score" not in out and "highlight_score" in out:
        out["lens_score"] = out["highlight_score"]
    return out


def load_supabase_highlight_for_date(trade_date: str) -> list[dict[str, Any]]:
    """讀 Supabase 前一日 highlight，供 delta 計算；未設定時回空列。"""
    if not supabase_configured():
        return []
    url = _rest_url(_HIGHLIGHT_TABLE)
    headers = {**_headers(), "Accept-Profile": _SCHEMA}
    resp = requests.get(
        url,
        headers=headers,
        params={
            "trade_date": f"eq.{trade_date}",
            "select": "*",
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase {_HIGHLIGHT_TABLE} read failed: "
            f"{resp.status_code} {resp.text[:500]}"
        )
    return [_normalize_highlight_row(r) for r in (resp.json() or [])]


def highlight_row_to_supabase(row: dict[str, Any]) -> dict[str, Any]:
    codes = row.get("etf_add_codes")
    if codes is None:
        codes_raw = row.get("etf_add_codes_json") or "[]"
        try:
            etf_add_codes = json.loads(codes_raw) if isinstance(codes_raw, str) else list(codes_raw)
        except (json.JSONDecodeError, TypeError):
            etf_add_codes = []
    elif isinstance(codes, str):
        try:
            etf_add_codes = json.loads(codes)
        except json.JSONDecodeError:
            etf_add_codes = []
    else:
        etf_add_codes = list(codes)

    src = row.get("sources_json")
    if isinstance(src, str):
        try:
            sources_json = json.loads(src or "{}")
        except json.JSONDecodeError:
            sources_json = {}
    elif isinstance(src, dict):
        sources_json = src
    else:
        sources_json = {}

    computed_at = row.get("computed_at") or datetime.now(_TPE).isoformat()
    lens_score = row.get("lens_score", row.get("highlight_score", 0))

    return {
        "trade_date": row["trade_date"],
        "stock_id": row["stock_id"],
        "stock_name": row.get("stock_name"),
        "etf_add_codes": etf_add_codes,
        "consensus_add": _bool(row.get("consensus_add")),
        "consensus_streak_days": int(row.get("consensus_streak_days") or 0),
        "breadth_zone_200": row.get("breadth_zone_200"),
        "trend_posture": row.get("trend_posture"),
        "regime_aligned": _bool(row.get("regime_aligned")),
        "rrg_quadrant": row.get("rrg_quadrant"),
        "rrg_quadrant_prev": row.get("rrg_quadrant_prev"),
        "rrg_mono_fresh": _bool(row.get("rrg_mono_fresh")),
        "rrg_tier2": _bool(row.get("rrg_tier2")),
        "rrg_rs_ratio": row.get("rrg_rs_ratio"),
        "rrg_rs_momentum": row.get("rrg_rs_momentum"),
        "rrg_rank": row.get("rrg_rank"),
        "rrg_total": row.get("rrg_total"),
        "vcp_composite": row.get("vcp_composite"),
        "vcp_execution_state": row.get("vcp_execution_state"),
        "vcp_distance_pivot_pct": row.get("vcp_distance_pivot_pct"),
        "copytrade_l1h9_signal": _bool(row.get("copytrade_l1h9_signal")),
        "delta_new_to_watchlist": _bool(row.get("delta_new_to_watchlist")),
        "delta_rrg_quadrant_change": row.get("delta_rrg_quadrant_change"),
        "delta_consensus_new_today": _bool(row.get("delta_consensus_new_today")),
        "delta_any_signal": _bool(row.get("delta_any_signal")),
        "signal_convergence": int(row.get("signal_convergence") or 0),
        "highlight_score": lens_score,
        "featured_rank": row.get("featured_rank"),
        "home_preview_rank": row.get("home_preview_rank"),
        "strategy_group_rank": row.get("strategy_group_rank"),
        "badges_json": row.get("badges_json") or [],
        "narrative_zh": row.get("narrative_zh") or "",
        "highlight_tier": row.get("highlight_tier") or "none",
        "holdings_aligned": _bool(row.get("holdings_aligned", True)),
        "data_baseline_date": row["data_baseline_date"],
        "sources_json": sources_json,
        "computed_at": computed_at,
    }


def _alert_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        items_json = json.loads(row["items_json"] or "[]")
    except json.JSONDecodeError:
        items_json = []
    return {
        "trade_date": row["trade_date"],
        "total_count": int(row.get("total_count") or 0),
        "fire_count": int(row["fire_count"] or 0),
        "delta_new_count": int(row["delta_new_count"] or 0),
        "consensus_add_count": int(row.get("consensus_add_count") or 0),
        "headline_zh": row["headline_zh"],
        "items_json": items_json,
        "computed_at": row["computed_at"],
    }


def sync_stock_daily_highlight_to_supabase(
    trade_date: str,
    rows: list[dict[str, Any]],
) -> int:
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")
    payload = [highlight_row_to_supabase(r) for r in rows]
    return _delete_insert(_HIGHLIGHT_TABLE, trade_date, payload)


def sync_daily_highlight_alert_to_supabase(alert: dict[str, Any]) -> int:
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")
    trade_date = str(alert["trade_date"])
    items = alert.get("items_json") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    payload = {
        "trade_date": trade_date,
        "total_count": int(alert.get("total_count") or 0),
        "fire_count": int(alert.get("fire_count") or 0),
        "delta_new_count": int(alert.get("delta_new_count") or 0),
        "consensus_add_count": int(alert.get("consensus_add_count") or 0),
        "headline_zh": alert["headline_zh"],
        "items_json": items,
        "computed_at": alert.get("computed_at") or datetime.now(_TPE).isoformat(),
    }
    return _delete_insert(_ALERT_TABLE, trade_date, [payload])


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
    trade_date: str,
    highlight_rows: list[dict[str, Any]],
    alert: dict[str, Any],
) -> tuple[int, int]:
    highlight_n = sync_stock_daily_highlight_to_supabase(trade_date, highlight_rows)
    alert_n = sync_daily_highlight_alert_to_supabase(alert)
    return highlight_n, alert_n


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
    highlight_rows: list[dict[str, Any]],
    alert: dict[str, Any],
    *,
    scheduled: bool = True,
) -> tuple[int, int] | None:
    if not _lens_sync_enabled():
        return None
    if scheduled and not allow_scheduled_supabase_push(conn):
        return None
    return sync_lens_bundle_to_supabase(trade_date, highlight_rows, alert)
