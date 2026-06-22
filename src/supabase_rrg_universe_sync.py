"""Sync rrg_universe_scores from SQLite to Supabase PostgREST."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from typing import Any, Literal

import requests

from market_benchmark import is_trading_date
from stock_db import load_rrg_universe_scores
from supabase_research_sync import (
    _headers,
    _supabase_url,
    allow_scheduled_supabase_push,
    supabase_configured,
)

ScreenKind = Literal["intraday", "close"]
_TABLE = "rrg_universe_scores"
_SCHEMA = "stock_research"


def _rest_url() -> str:
    base = _supabase_url()
    if not base:
        raise RuntimeError(
            "SUPABASE_URL 或 VITE_PUBLIC_SUPABASE_URL 未設定（見 .env.example）"
        )
    return f"{base}/rest/v1/{_TABLE}"


def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
    def _json_field(val: str | None) -> Any:
        if not val:
            return None
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val

    return {
        "session_date": row["session_date"],
        "screen_kind": row["screen_kind"],
        "data_baseline_date": row["data_baseline_date"],
        "stock_id": row["stock_id"],
        "stock_name": row["stock_name"],
        "rs_ratio": row["rs_ratio"],
        "rs_momentum": row["rs_momentum"],
        "quadrant": row["quadrant"],
        "quadrants_json": _json_field(row["quadrants_json"]),
        "trend": row["trend"],
        "disp": row["disp"],
        "seg_last": row["seg_last"],
        "segs_json": _json_field(row["segs_json"]),
        "tier2": int(row["tier2"] or 0),
        "mono_tier2": int(row["mono_tier2"] or 0),
        "mono_fresh": int(row["mono_fresh"] or 0),
        "daily_pct": row["daily_pct"],
        "tick_ok": row["tick_ok"],
        "synced_at": row["synced_at"],
    }


def sync_rrg_universe_to_supabase(
    conn: sqlite3.Connection,
    session_date: str,
    screen_kind: ScreenKind,
) -> int:
    """同 session_date + screen_kind 先刪後插至 stock_research.rrg_universe_scores。"""
    if not supabase_configured():
        raise RuntimeError("Supabase 未設定")

    rows = load_rrg_universe_scores(conn, session_date, screen_kind)
    url = _rest_url()
    headers = _headers()

    delete_resp = requests.delete(
        url,
        headers=headers,
        params={
            "session_date": f"eq.{session_date}",
            "screen_kind": f"eq.{screen_kind}",
        },
        timeout=120,
    )
    if delete_resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase {_TABLE} delete failed: "
            f"{delete_resp.status_code} {delete_resp.text[:500]}"
        )

    if not rows:
        return 0

    payload = [_row_payload(r) for r in rows]
    insert_resp = requests.post(
        url,
        headers={**headers, "Prefer": "return=minimal"},
        json=payload,
        timeout=120,
    )
    if insert_resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase {_TABLE} insert failed: "
            f"{insert_resp.status_code} {insert_resp.text[:500]}"
        )
    return len(payload)


def maybe_sync_rrg_universe_to_supabase(
    conn: sqlite3.Connection,
    session_date: str,
    screen_kind: ScreenKind,
    *,
    scheduled: bool = True,
) -> int | None:
    """RUN_SUPABASE_RESEARCH_SYNC=1 時同步；否則略過。"""
    flag = os.environ.get("RUN_SUPABASE_RESEARCH_SYNC", "0").strip()
    if flag in ("0", "false", "False", ""):
        return None
    if scheduled and not allow_scheduled_supabase_push(conn):
        return None
    if scheduled and not is_trading_date(conn, date.fromisoformat(session_date)):
        return None
    return sync_rrg_universe_to_supabase(conn, session_date, screen_kind)
