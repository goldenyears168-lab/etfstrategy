"""ETF Facts layer · JSON snapshot for Supabase / Readdy (etf-daily-v1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from etf_daily_report import ACTION_LABEL, _holdings_sync_summary
from holdings_research import (
    ConsensusStock,
    build_cross_etf_consensus,
    build_etf_holdings_changes_block,
    fmt_ntd_short,
)
from project_config import ETF_CODES_HOLDINGS, ETF_CODES_LISTED

_TPE = ZoneInfo("Asia/Taipei")
CONTRACT = "etf-daily-v1"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _action_zh(action: str) -> str:
    return ACTION_LABEL.get(action, action)


def _flow_cell(flow_ntd: float | None) -> str:
    return fmt_ntd_short(flow_ntd) or "—"


def _consensus_row(row: ConsensusStock) -> dict[str, Any]:
    return {
        "stock_id": row.stock_id,
        "stock_name": row.stock_name,
        "etf_add": row.etf_add,
        "etf_add_codes": list(row.etf_add_list),
        "etf_reduce": row.etf_reduce,
        "etf_reduce_codes": list(row.etf_reduce_list),
        "share_delta_total": row.share_delta_total,
        "flow_ntd": row.flow_ntd,
        "flow_ntd_short": fmt_ntd_short(row.flow_ntd),
        "growth_pct": row.growth_pct,
    }


def _section_table_row(change: dict[str, Any]) -> list[str | int | float | None]:
    action = str(change.get("action") or "")
    share = change.get("share_delta")
    share_s: str | int = f"{int(share):+d}" if share is not None else "—"
    wt = change.get("weight_delta_pp")
    wt_s = f"{wt:+.2f}" if wt is not None else "—"
    return [
        change["stock_id"],
        change.get("stock_name") or "",
        _action_zh(action),
        share_s,
        wt_s,
        _flow_cell(change.get("flow_ntd")),
    ]


def _build_sections(blocks: list[dict]) -> list[dict[str, Any]]:
    headers = ["代號", "名稱", "動作", "股數差", "權重差", "flow"]
    sections: list[dict[str, Any]] = []
    for block in blocks:
        code = block["etf_code"]
        prev_d = block.get("prev_date")
        curr_d = block.get("curr_date")
        note = block.get("note")
        changes = sorted(
            block.get("changes") or [],
            key=lambda c: abs(float(c.get("flow_ntd") or 0)),
            reverse=True,
        )
        date_range = f"{prev_d} → {curr_d}" if prev_d and curr_d else "—"
        rows = [_section_table_row(ch) for ch in changes]
        add_actions = {"新进", "加码"}
        add_count = sum(1 for ch in changes if ch.get("action") in add_actions)
        section: dict[str, Any] = {
            "etf_code": code,
            "title": code,
            "date_range": date_range,
            "prev_date": prev_d,
            "curr_date": curr_d,
            "headers": headers,
            "rows": rows,
            "changes": [_json_safe(ch) for ch in changes],
        }
        if note:
            section["note"] = note
        if code == "00981A":
            section["981a_add_count"] = add_count
        sections.append(section)
    return sections


def build_etf_snapshot_json(
    conn: sqlite3.Connection,
    as_of: str,
    etf_codes: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """PIT-safe ETF daily payload for Readdy (no MD parsing)."""
    codes = etf_codes or ETF_CODES_HOLDINGS
    sync = _holdings_sync_summary(conn, codes)
    blocks = build_etf_holdings_changes_block(conn, codes, changed_only=True)

    changed_etfs: list[str] = []
    unchanged_etfs: list[str] = []
    skipped_etfs: list[str] = []
    for block in blocks:
        code = block["etf_code"]
        note = block.get("note")
        changes = block.get("changes") or []
        if note:
            skipped_etfs.append(code)
        elif not changes:
            unchanged_etfs.append(code)
        else:
            changed_etfs.append(code)

    consensus_rows = build_cross_etf_consensus(conn, codes)
    consensus_adds = [r for r in consensus_rows if r.etf_add >= 2]
    consensus_adds.sort(key=lambda r: abs(float(r.flow_ntd or 0)), reverse=True)

    sections = _build_sections(blocks)
    s981a = next((s for s in sections if s["etf_code"] == "00981A"), None)

    return {
        "contract": CONTRACT,
        "layer": "facts",
        "as_of": as_of,
        "sync": {
            "listed_synced": sync["listed_synced"],
            "listed_total": sync["listed_total"],
            "listed_parts": sync["listed_parts"],
            "optional_synced": sync["optional_synced"],
            "optional_total": sync["optional_total"],
            "sync_count": f"{sync['listed_synced']}/{sync['listed_total']}",
        },
        "summary": {
            "changed_etfs": changed_etfs,
            "unchanged_etfs": unchanged_etfs,
            "skipped_etfs": skipped_etfs,
            "has_changes": bool(changed_etfs),
        },
        "consensus_add_count": len(consensus_adds),
        "consensus_add_stocks": [r.stock_id for r in consensus_adds],
        "consensus_adds": [_consensus_row(r) for r in consensus_adds],
        "981a_add_count": (s981a or {}).get("981a_add_count", 0),
        "sections": sections,
        "meta": {
            "generated_at": datetime.now(_TPE).isoformat(),
            "etf_codes": list(codes),
            "listed_etf_codes": [c for c in codes if c in ETF_CODES_LISTED],
        },
    }


def etf_snapshot_json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)
