"""RRG mono swap-accel（C18acc）· JSON snapshot for Supabase / Readdy."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from project_config import DEFAULT_ETF_CODES
from research.backtest.rrg_mono_score_swap_c import (
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    _trading_days_between,
)
from rrg_mono_daily_brief import MAX_SLOTS, TOP_N, ScanRow
from rrg_mono_swap_accel_daily_brief import build_payload
from snapshot_screen_status import rrg_screen_status

_TPE = ZoneInfo("Asia/Taipei")
CONTRACT = "rrg-swap-accel-daily-v1"
STRATEGY_ID = RRG_MONO_SWAP_ACCEL_SLUG

POOL_HEADERS = ["#", "代號", "名稱", "seg_last", "位移", "四日加速"]
SLOT_HEADERS = ["槽", "代號", "名稱", "進場", "hold", "seg_last", "四日加速"]


def _fmt_accel(v: float | None) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.3f}"


def _pool_row(i: int, r: ScanRow, accel: dict[str, float]) -> list[str | int | float | None]:
    return [
        i,
        r.stock_id,
        r.stock_name,
        round(r.seg_last, 3),
        round(r.disp, 2),
        _fmt_accel(accel.get(r.stock_id)),
    ]


def build_rrg_swap_accel_snapshot_json(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    intraday: bool = False,
    session_date: str | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """PIT-safe C18acc diagnostic payload for Readdy."""
    payload = build_payload(conn, as_of=as_of, etf_codes=etf_codes)
    cfg: ScoreSwapCConfig = payload["config"]
    pool: list[ScanRow] = payload["tomorrow_pool"]
    slots: list[dict[str, Any]] = payload["slots"]
    held_accel: dict[str, float] = payload.get("held_accel") or {}
    chall_accel: dict[str, float] = payload.get("challenger_accel") or {}
    occupied = len(slots)
    scan_label = "盤中" if intraday else "收盤診斷"
    signal_as_of = as_of if not intraday else str(payload["as_of"])

    if pool:
        summary_zh = (
            f"{scan_label} · 隔日候選 {len(pool)} 檔（fresh 池 {payload['pool_fresh_n']} 檔）· "
            f"持倉 {occupied}/{MAX_SLOTS} 槽。"
        )
        empty_reason_zh = None
    elif occupied:
        summary_zh = f"{scan_label} · 暫無 fresh 候選 · 持倉 {occupied}/{MAX_SLOTS} 槽。"
        empty_reason_zh = None
    else:
        summary_zh = f"{scan_label} · 暫無 fresh 候選 · 空槽。"
        empty_reason_zh = "信號日無 fresh mono 候選。"

    slot_rows: list[list[str | int | float | None]] = []
    for p in sorted(slots, key=lambda x: int(x.get("slot", 0))):
        sid = str(p["stock_id"])
        entry = str(p.get("entry_date") or p.get("signal_date") or "")
        hold = (
            _trading_days_between(payload["session_dates"], entry, as_of)
            if entry
            else "—"
        )
        slot_rows.append(
            [
                int(p.get("slot", 0)) + 1,
                sid,
                str(p.get("stock_name") or ""),
                entry,
                hold,
                round(float(p.get("seg_last") or 0), 3),
                _fmt_accel(held_accel.get(sid)),
            ]
        )

    sell = payload.get("hypothetical_swap_sell")
    buy = payload.get("hypothetical_swap_buy")
    swap_hint: str | None = None
    if sell and buy:
        swap_hint = f"假設換倉：{sell['stock_id']} → {buy.stock_id}"

    return {
        "contract": CONTRACT,
        "layer": "strategy",
        "strategy_id": STRATEGY_ID,
        "as_of": as_of,
        "intraday": intraday,
        "session_date": session_date or as_of,
        "data_baseline_date": signal_as_of,
        "brief_kind": payload.get("brief_kind"),
        "variant_id": cfg.variant_id,
        "max_slots": MAX_SLOTS,
        "slots_occupied": occupied,
        "slots_label": f"{occupied}/{MAX_SLOTS}",
        "pool_fresh_n": payload["pool_fresh_n"],
        "pool_count": len(pool),
        "min_hold_days": cfg.min_hold_days,
        "max_hold_days": cfg.max_hold_days,
        "score_margin": cfg.effective_margin,
        "breadth_zone_200": payload.get("breadth_zone"),
        "breadth_zone_zh": payload.get("breadth_zone_zh"),
        "screen_status": rrg_screen_status(
            intraday=intraday,
            mono_count=len(pool),
            fresh_count=payload["pool_fresh_n"],
            slots_label=f"{occupied}/{MAX_SLOTS}",
        ),
        "summary_zh": summary_zh,
        "empty_reason_zh": empty_reason_zh,
        "swap_hint_zh": swap_hint,
        "tables": {
            "tomorrow_pool": {
                "title": f"隔日候選（fresh mono 全池 · 依軌跡排序）",
                "headers": POOL_HEADERS,
                "rows": [_pool_row(i, r, chall_accel) for i, r in enumerate(pool[:TOP_N], 1)],
            },
            "slots": {
                "title": "持倉（C18acc state）",
                "headers": SLOT_HEADERS,
                "rows": slot_rows,
            },
        },
        "meta": {"generated_at": datetime.now(_TPE).isoformat()},
    }
