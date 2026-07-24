"""Private ops console · Supabase ``ops.*`` upsert helpers (service role).

PostgREST currently exposes ``public`` (not ``ops``), so writers target
``public.ops_*`` views; INSTEAD OF triggers forward into ``ops.*``.
Frontend anon reads the same views.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from project_dotenv import load_project_dotenv

_TPE = ZoneInfo("Asia/Taipei")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def ensure_dotenv() -> None:
    load_project_dotenv()


def supabase_ops_configured() -> bool:
    ensure_dotenv()
    return bool((_env("SUPABASE_URL") or _env("VITE_PUBLIC_SUPABASE_URL")) and _env("SUPABASE_SERVICE_ROLE_KEY"))


def _supabase_url() -> str:
    ensure_dotenv()
    base = _env("SUPABASE_URL") or _env("VITE_PUBLIC_SUPABASE_URL")
    if not base:
        raise RuntimeError("SUPABASE_URL 未設定（見 .env.example）")
    return base.rstrip("/")


def _headers(*, prefer: str = "resolution=merge-duplicates,return=minimal") -> dict[str, str]:
    ensure_dotenv()
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY 未設定（見 .env.example）")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def _rest(table: str) -> str:
    # public.ops_* views (PostgREST-exposed)
    return f"{_supabase_url()}/rest/v1/{table}"


def now_tpe_iso() -> str:
    return datetime.now(_TPE).isoformat()


def _post(
    table: str,
    payload: dict[str, Any] | list[dict[str, Any]],
    *,
    on_conflict: str | None = None,
    prefer: str = "resolution=merge-duplicates,return=minimal",
) -> None:
    params: dict[str, str] = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    resp = requests.post(
        _rest(table),
        headers=_headers(prefer=prefer),
        json=payload,
        params=params or None,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"{table} upsert failed: {resp.status_code} {resp.text[:800]}")


def upsert_live_ta(row: dict[str, Any]) -> None:
    """Upsert one Live TA row · PK ``stock_id`` (view trigger)."""
    body = dict(row)
    body.setdefault("asof", now_tpe_iso())
    body.setdefault("anchors", {})
    body["updated_at"] = now_tpe_iso()
    # Plain INSERT · INSTEAD OF trigger does ON CONFLICT
    _post("ops_live_ta", body, prefer="return=minimal")


def delete_live_ta_except(keep_stock_ids: list[str] | set[str]) -> int:
    """Remove ``ops_live_ta`` rows not in keep set (drop former holdings from UI)."""
    keep = sorted({str(s).strip() for s in keep_stock_ids if str(s).strip()})
    if not supabase_ops_configured():
        return 0
    if not keep:
        # Safety: never wipe entire table on empty universe resolve glitch
        return 0
    # PostgREST: delete where stock_id not in keep
    ids = ",".join(keep)
    resp = requests.delete(
        _rest("ops_live_ta"),
        headers=_headers(prefer="return=representation"),
        params={"stock_id": f"not.in.({ids})"},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"ops_live_ta prune failed: {resp.status_code} {resp.text[:800]}")
    try:
        rows = resp.json()
        return len(rows) if isinstance(rows, list) else 0
    except Exception:  # noqa: BLE001
        return 0


def fetch_latest_ops_holdings_payload() -> dict[str, Any] | None:
    """Latest ``ops_holdings`` row payload (service role · for Live TA universe)."""
    if not supabase_ops_configured():
        return None
    resp = requests.get(
        _rest("ops_holdings"),
        headers=_headers(prefer="return=representation"),
        params={"select": "asof,payload", "order": "asof.desc", "limit": "1"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"ops_holdings get failed: {resp.status_code} {resp.text[:400]}")
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        return None
    payload = rows[0].get("payload")
    return payload if isinstance(payload, dict) else None


def insert_holdings(
    payload: dict[str, Any],
    *,
    account_label: str = "fubon",
    asof: str | None = None,
) -> None:
    """Append holdings snapshot (latest-by-asof on frontend)."""
    _post(
        "ops_holdings",
        {
            "asof": asof or now_tpe_iso(),
            "account_label": account_label,
            "payload": payload,
        },
        prefer="return=minimal",
    )


def upsert_digest(
    *,
    digest_key: str,
    body_md: str,
    subject: str | None = None,
    channel: str = "email",
    severity: str = "info",
    asof: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Upsert Inbox wall row · unique ``digest_key`` (view trigger)."""
    _post(
        "ops_digests",
        {
            "digest_key": digest_key,
            "channel": channel,
            "subject": subject,
            "body_md": body_md,
            "severity": severity,
            "asof": asof or now_tpe_iso(),
            "meta": meta or {},
        },
        prefer="return=minimal",
    )


def upsert_sleeve_status(
    *,
    sleeve_id: str,
    enabled: bool,
    dry_run: bool,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
    asof: str | None = None,
) -> None:
    _post(
        "ops_sleeve_status",
        {
            "sleeve_id": sleeve_id,
            "asof": asof or now_tpe_iso(),
            "enabled": bool(enabled),
            "dry_run": bool(dry_run),
            "note": note,
            "payload": payload or {},
            "updated_at": now_tpe_iso(),
        },
        prefer="return=minimal",
    )


def insert_snapshot(
    *,
    kind: str,
    payload: dict[str, Any],
    asof: str | None = None,
) -> None:
    _post(
        "ops_snapshots",
        {
            "kind": kind,
            "asof": asof or now_tpe_iso(),
            "payload": payload,
        },
        prefer="return=minimal",
    )


def upsert_kb_card(
    *,
    card_id: str,
    title: str,
    body_md: str,
    tags: list[str] | None = None,
) -> None:
    """Upsert knowledge-base card · PK ``id``."""
    _post(
        "ops_kb_cards",
        {
            "id": card_id,
            "title": title,
            "body_md": body_md,
            "tags": tags or [],
            "updated_at": now_tpe_iso(),
        },
        prefer="return=minimal",
    )
