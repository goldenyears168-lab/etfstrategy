"""Sync stock_signal_hits to Supabase PostgREST."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from stock_db import DEFAULT_DB_PATH, connect
from stock_signal_index import build_signal_hits_for_date
from supabase_research_sync import (
    BRIEF_CATALOG,
    BriefRecord,
    _headers,
    _rest_url as _brief_rest_url,
    allow_scheduled_supabase_push,
    load_brief,
    supabase_configured,
)

_TPE = ZoneInfo("Asia/Taipei")
_SCHEMA = "stock_research"
_TABLE = "stock_signal_hits"


def _rest_url() -> str:
    base = _brief_rest_url().rsplit("/", 1)[0]
    return f"{base}/{_TABLE}"


def sync_signal_hits_for_date(
    trade_date: date,
    *,
    db_path: str | None = None,
) -> int:
    if not supabase_configured():
        return 0
    conn = connect(db_path or DEFAULT_DB_PATH)
    records: list[BriefRecord] = []
    try:
        for brief_type in BRIEF_CATALOG:
            rec = load_brief(brief_type, trade_date)
            if rec is not None and rec.trade_date == trade_date:
                records.append(rec)
        hits = build_signal_hits_for_date(conn, trade_date.isoformat(), records)
    finally:
        conn.close()

    day = trade_date.isoformat()
    url = _rest_url()
    headers = _headers()
    delete_resp = requests.delete(
        url,
        headers=headers,
        params={"trade_date": f"eq.{day}"},
        timeout=120,
    )
    if delete_resp.status_code >= 400:
        raise RuntimeError(
            f"Supabase {_TABLE} delete failed: "
            f"{delete_resp.status_code} {delete_resp.text[:500]}"
        )
    if not hits:
        return 0

    now = datetime.now(_TPE).isoformat()
    payload: list[dict[str, Any]] = []
    for hit in hits:
        payload.append({**hit, "computed_at": now})

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


def maybe_sync_signal_hits(
    trade_date: date | None = None,
    *,
    scheduled: bool = True,
    db_path: str | None = None,
) -> int:
    if not supabase_configured():
        return 0
    conn = connect(db_path or DEFAULT_DB_PATH)
    try:
        if scheduled and not allow_scheduled_supabase_push(conn):
            return 0
        if trade_date is None:
            from supabase_research_sync import _default_lookup_date

            day = _default_lookup_date(conn)
        else:
            day = trade_date
    finally:
        conn.close()
    return sync_signal_hits_for_date(day, db_path=db_path)


def backfill_signal_hits(dates: list[date], *, db_path: str | None = None) -> dict[str, int]:
    totals: dict[str, int] = {}
    for day in dates:
        totals[day.isoformat()] = sync_signal_hits_for_date(day, db_path=db_path)
    return totals
