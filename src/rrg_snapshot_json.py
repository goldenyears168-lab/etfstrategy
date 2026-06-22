"""RRG mono daily · JSON snapshot for Supabase / Readdy (rrg-mono-daily-v1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from project_config import DEFAULT_ETF_CODES
from rrg_mono_daily_brief import (
    EXECUTION_DETAIL_ZH,
    EXECUTION_RULE_ZH,
    HOLD_DAYS,
    MAX_SLOTS,
    ScanRow,
    _scan_rows,
    load_slot_state,
)

_TPE = ZoneInfo("Asia/Taipei")
CONTRACT = "rrg-mono-daily-v1"
STRATEGY_ID = "rrg-mono-hold7"

FRESH_HEADERS = ["#", "代號", "名稱", "seg_last", "位移", "三段", "當日", "RV", "MV"]
ALL_HEADERS = ["#", "代號", "名稱", "fr", "seg_last", "位移", "當日", "象限路徑"]


def _fmt_pct(v: float | None) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.1f}%"


def _fresh_row(i: int, r: ScanRow) -> list[str | int | float | None]:
    segs = " / ".join(f"{s:.2f}" for s in r.segs)
    return [
        i,
        r.stock_id,
        r.stock_name,
        round(r.seg_last, 3),
        round(r.disp, 2),
        segs,
        _fmt_pct(r.daily_pct),
        round(r.rs_ratio, 1),
        round(r.rs_momentum, 1),
    ]


def _all_row(i: int, r: ScanRow) -> list[str | int | float | None]:
    qp = " → ".join(r.quadrants)
    return [
        i,
        r.stock_id,
        r.stock_name,
        "★" if r.fresh else "",
        round(r.seg_last, 3),
        round(r.disp, 2),
        _fmt_pct(r.daily_pct),
        qp,
    ]


def build_rrg_mono_snapshot_json(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    intraday: bool = False,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """PIT-safe RRG mono scan payload for Readdy."""
    all_mono, fresh_mono = _scan_rows(conn, as_of, etf_codes=etf_codes)
    state = load_slot_state()
    slots = state.get("slots", [])
    occupied = len(slots)

    return {
        "contract": CONTRACT,
        "layer": "strategy",
        "strategy_id": STRATEGY_ID,
        "as_of": as_of,
        "intraday": intraday,
        "hold_days": HOLD_DAYS,
        "max_slots": MAX_SLOTS,
        "slots_occupied": occupied,
        "slots_label": f"{occupied}/{MAX_SLOTS}",
        "fresh_count": len(fresh_mono),
        "mono_count": len(all_mono),
        "execution_rule_zh": EXECUTION_RULE_ZH,
        "execution_detail_zh": EXECUTION_DETAIL_ZH,
        "strategy_spec_zh": f"單軌濾網 + fresh 訊號 + 依軌跡排序 + 持有 {HOLD_DAYS} 日（{EXECUTION_RULE_ZH}）",
        "tables": {
            "fresh_mono": {
                "title": "mono fresh 候選",
                "headers": FRESH_HEADERS,
                "rows": [_fresh_row(i, r) for i, r in enumerate(fresh_mono, 1)],
            },
            "all_mono": {
                "title": "所有 mono 候選",
                "headers": ALL_HEADERS,
                "rows": [_all_row(i, r) for i, r in enumerate(all_mono, 1)],
            },
        },
        "meta": {"generated_at": datetime.now(_TPE).isoformat()},
    }
