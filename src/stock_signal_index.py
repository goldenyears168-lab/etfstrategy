"""Build stock_signal_hits rows from daily brief snapshots + lens."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from copytrade_l1h9_daily import signals_for_date
from stock_daily_lens import build_stock_daily_lens_rows
from supabase_research_sync import BriefRecord

BRIEF_META: dict[str, dict[str, str]] = {
    "etf_daily": {
        "source": "etf_daily",
        "tab": "etf",
        "layer_label": "事實層",
        "brief_label": "ETF 持股日報",
    },
    "vcp_funnel_specs": {
        "source": "vcp_funnel",
        "tab": "vcp",
        "layer_label": "研究層",
        "brief_label": "VCP 漏斗研究",
    },
    "vcp_pivot_gate": {
        "source": "vcp_pivot_gate",
        "tab": "vcp",
        "layer_label": "策略層",
        "brief_label": "VCP 突破確認",
    },
    "vcp_coil_close": {
        "source": "vcp_coil_close",
        "tab": "vcp",
        "layer_label": "策略層",
        "brief_label": "VCP 訊號收盤",
    },
    "copytrade_l1h9": {
        "source": "copytrade_l1h9",
        "tab": "copytrade",
        "layer_label": "策略層",
        "brief_label": "ETF00981A 跟單策略",
    },
    "rrg_mono_daily": {
        "source": "rrg_mono_daily",
        "tab": "rrg",
        "layer_label": "策略層",
        "brief_label": "RRG 市場輪動圖選股策略",
    },
    "rrg_mono_intraday": {
        "source": "rrg_mono_intraday",
        "tab": "rrg",
        "layer_label": "策略層",
        "brief_label": "RRG 盤中預估",
    },
    "rrg_mono_swap_accel_daily": {
        "source": "rrg_mono_swap_accel",
        "tab": "rrg",
        "layer_label": "策略層",
        "brief_label": "RRG mono swap-accel（C18acc）",
    },
    "rrg_c18acc_screen": {
        "source": "rrg_mono_swap_accel",
        "tab": "rrg",
        "layer_label": "策略層",
        "brief_label": "RRG mono swap-accel 盤中",
    },
    "lens": {
        "source": "stock_daily_lens",
        "tab": "lens",
        "layer_label": "今日亮點",
        "brief_label": "今日亮點",
    },
}


def _meta(brief_type: str) -> dict[str, str]:
    return BRIEF_META.get(
        brief_type,
        {
            "source": brief_type,
            "tab": brief_type,
            "layer_label": "Other",
            "brief_label": brief_type,
        },
    )


def _hit(
    *,
    trade_date: str,
    stock_id: str,
    brief_type: str,
    schedule_slot: str,
    stock_name: str = "",
    row_json: dict[str, Any] | None = None,
    headline_zh: str = "",
) -> dict[str, Any]:
    m = _meta(brief_type)
    return {
        "trade_date": trade_date,
        "stock_id": stock_id,
        "brief_type": brief_type,
        "schedule_slot": schedule_slot,
        "source": m["source"],
        "stock_name": stock_name or None,
        "tab": m["tab"],
        "layer_label": m["layer_label"],
        "brief_label": m["brief_label"],
        "row_json": row_json or {},
        "headline_zh": headline_zh or None,
    }


def _hits_from_etf_snapshot(record: BriefRecord) -> list[dict[str, Any]]:
    snap = record.snapshot_json or {}
    if snap.get("contract") != "etf-daily-v1":
        return []
    by_stock: dict[str, dict[str, Any]] = {}
    day = record.trade_date.isoformat()
    for section in snap.get("sections") or []:
        if not isinstance(section, dict):
            continue
        etf_code = section.get("etf_code")
        for change in section.get("changes") or []:
            if not isinstance(change, dict):
                continue
            sid = str(change.get("stock_id") or "")
            if not sid:
                continue
            row = dict(change)
            codes = row.pop("etf_codes", None) or []
            if etf_code:
                codes = list(codes) + [etf_code]
            if sid in by_stock:
                prev = by_stock[sid]["row_json"]
                merged_codes = list(
                    dict.fromkeys(
                        (prev.get("etf_codes") or [])
                        + ([prev.get("etf_code")] if prev.get("etf_code") else [])
                        + codes
                    )
                )
                row["etf_codes"] = [c for c in merged_codes if c]
            else:
                row["etf_codes"] = [c for c in codes if c]
            by_stock[sid] = _hit(
                trade_date=day,
                stock_id=sid,
                brief_type=record.brief_type,
                schedule_slot=record.schedule_slot,
                stock_name=str(change.get("stock_name") or ""),
                row_json=row,
            )
    return list(by_stock.values())


def _hits_from_vcp_snapshot(record: BriefRecord) -> list[dict[str, Any]]:
    snap = record.snapshot_json or {}
    if snap.get("contract") != "vcp-daily-v1":
        return []
    out: list[dict[str, Any]] = []
    day = record.trade_date.isoformat()
    for variant in snap.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        spec_key = variant.get("spec_key")
        for cand in variant.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            sid = str(cand.get("stock_id") or "")
            if not sid:
                continue
            row = dict(cand)
            if spec_key:
                row["spec_key"] = spec_key
            out.append(
                _hit(
                    trade_date=day,
                    stock_id=sid,
                    brief_type=record.brief_type,
                    schedule_slot=record.schedule_slot,
                    stock_name=str(cand.get("stock_name") or ""),
                    row_json=row,
                )
            )
    return out


def _hits_from_rrg_snapshot(record: BriefRecord) -> list[dict[str, Any]]:
    snap = record.snapshot_json or {}
    if snap.get("contract") != "rrg-mono-daily-v1":
        return []
    out: list[dict[str, Any]] = []
    day = record.trade_date.isoformat()
    tables = snap.get("tables") or {}
    fresh = tables.get("fresh_mono") if isinstance(tables, dict) else {}
    headers = fresh.get("headers") if isinstance(fresh, dict) else []
    for row in (fresh.get("rows") if isinstance(fresh, dict) else []) or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        sid = str(row[1]).strip()
        if not re.match(r"^\d{4,6}$", sid):
            continue
        name = str(row[2]) if len(row) > 2 else ""
        values = [str(c) for c in row]
        out.append(
            _hit(
                trade_date=day,
                stock_id=sid,
                brief_type=record.brief_type,
                schedule_slot=record.schedule_slot,
                stock_name=name,
                row_json={
                    "headers": headers,
                    "values": values,
                    "fresh": True,
                },
            )
        )
    return out


def _hits_from_rrg_swap_accel_snapshot(record: BriefRecord) -> list[dict[str, Any]]:
    snap = record.snapshot_json or {}
    if snap.get("contract") != "rrg-swap-accel-daily-v1":
        return []
    out: list[dict[str, Any]] = []
    day = record.trade_date.isoformat()
    tables = snap.get("tables") or {}
    pool = tables.get("tomorrow_pool") if isinstance(tables, dict) else {}
    headers = pool.get("headers") if isinstance(pool, dict) else []
    for row in (pool.get("rows") if isinstance(pool, dict) else []) or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        sid = str(row[1]).strip()
        if not re.match(r"^\d{4,6}$", sid):
            continue
        name = str(row[2]) if len(row) > 2 else ""
        values = [str(c) for c in row]
        out.append(
            _hit(
                trade_date=day,
                stock_id=sid,
                brief_type=record.brief_type,
                schedule_slot=record.schedule_slot,
                stock_name=name,
                row_json={
                    "headers": headers,
                    "values": values,
                    "fresh": True,
                },
            )
        )
    return out


def _parse_md_table_hits(record: BriefRecord) -> list[dict[str, Any]]:
    """Fallback: extract | table | rows from content_md."""
    out: list[dict[str, Any]] = []
    lines = record.content_md.splitlines()
    headers: list[str] = []
    in_table = False
    day = record.trade_date.isoformat()

    for line in lines:
        t = line.strip()
        if not t.startswith("|"):
            if in_table:
                in_table = False
                headers = []
            continue
        cells = [c.strip() for c in t.split("|") if c.strip()]
        if not cells or all(re.match(r"^[-:]+$", c) for c in cells):
            continue
        if not in_table:
            headers = cells
            in_table = True
            continue
        if len(cells) != len(headers):
            continue
        sid = cells[0].replace("*", "").strip()
        if not re.match(r"^\d{4,6}$", sid):
            continue
        row = {headers[i]: cells[i] for i in range(len(headers))}
        name = row.get("名稱") or row.get("名称") or ""
        out.append(
            _hit(
                trade_date=day,
                stock_id=sid,
                brief_type=record.brief_type,
                schedule_slot=record.schedule_slot,
                stock_name=str(name),
                row_json={"headers": headers, "values": cells, **row},
            )
        )
    return out


def _hits_from_copytrade(conn: sqlite3.Connection, record: BriefRecord) -> list[dict[str, Any]]:
    day = record.trade_date.isoformat()
    _score, _outcome, signals = signals_for_date(conn, day)
    out: list[dict[str, Any]] = []
    for sig in signals:
        out.append(
            _hit(
                trade_date=day,
                stock_id=sig.stock_id,
                brief_type=record.brief_type,
                schedule_slot=record.schedule_slot,
                stock_name=sig.stock_name,
                row_json={
                    "action": sig.action,
                    "share_delta": sig.share_delta,
                    "weight_delta": sig.weight_delta,
                },
            )
        )
    return out


def hits_from_brief(conn: sqlite3.Connection, record: BriefRecord) -> list[dict[str, Any]]:
    contract = (record.snapshot_json or {}).get("contract")
    if record.brief_type == "etf_daily" and contract == "etf-daily-v1":
        return _hits_from_etf_snapshot(record)
    if record.brief_type in (
        "vcp_funnel_specs",
        "vcp_pivot_gate",
        "vcp_coil_close",
    ) and contract == "vcp-daily-v1":
        return _hits_from_vcp_snapshot(record)
    if record.brief_type == "copytrade_l1h9":
        if contract == "copytrade-daily-v1":
            snap = record.snapshot_json or {}
            out: list[dict[str, Any]] = []
            day = record.trade_date.isoformat()
            for sig in snap.get("signals") or []:
                if not isinstance(sig, dict):
                    continue
                sid = str(sig.get("stock_id") or "")
                if not sid:
                    continue
                out.append(
                    _hit(
                        trade_date=day,
                        stock_id=sid,
                        brief_type=record.brief_type,
                        schedule_slot=record.schedule_slot,
                        stock_name=str(sig.get("stock_name") or ""),
                        row_json=dict(sig),
                    )
                )
            if out:
                return out
        hits = _hits_from_copytrade(conn, record)
        if hits:
            return hits
    if record.brief_type in ("rrg_mono_daily", "rrg_mono_intraday"):
        if contract == "rrg-mono-daily-v1":
            return _hits_from_rrg_snapshot(record)
    if record.brief_type == "rrg_mono_swap_accel_daily":
        if contract == "rrg-swap-accel-daily-v1":
            return _hits_from_rrg_swap_accel_snapshot(record)
    if record.brief_type in ("rrg_mono_daily", "rrg_mono_intraday", "rrg_mono_swap_accel_daily", "rrg_c18acc_screen", "etf_daily", "copytrade_l1h9"):
        return _parse_md_table_hits(record)
    return []


def hits_from_lens(conn: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    rows = build_stock_daily_lens_rows(conn, trade_date)
    out: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row.stock_id)
        out.append(
            _hit(
                trade_date=trade_date,
                stock_id=sid,
                brief_type="lens",
                schedule_slot="1630",
                stock_name=str(row.stock_name or ""),
                row_json={
                    "lens_score": row.lens_score,
                    "signal_convergence": row.signal_convergence,
                    "narrative_zh": row.narrative_zh,
                    "delta_any_signal": row.delta_any_signal,
                    "consensus_add": row.consensus_add,
                },
                headline_zh=str(row.narrative_zh or ""),
            )
        )
    return out


def build_signal_hits_for_date(
    conn: sqlite3.Connection,
    trade_date: str,
    records: list[BriefRecord],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        for hit in hits_from_brief(conn, record):
            key = (hit["trade_date"], hit["stock_id"], hit["brief_type"])
            if key not in merged:
                merged[key] = hit
    for hit in hits_from_lens(conn, trade_date):
        key = (hit["trade_date"], hit["stock_id"], hit["brief_type"])
        if key not in merged:
            merged[key] = hit
    return list(merged.values())
