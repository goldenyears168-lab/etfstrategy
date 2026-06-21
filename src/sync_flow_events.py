#!/usr/bin/env python3
"""② 收盤後落地 flow_events 快照（防 logic drift · 只寫 DB）。"""

from __future__ import annotations

import sqlite3

from holdings_research import ADD_ACTIONS, REDUCE_ACTIONS
from project_config import FLOW_VERSION
from signal_engine import StockSignal, build_aligned_signals
from stock_db import upsert_flow_events


def _source_etfs_for_signal(sig: StockSignal) -> tuple[str, int]:
    if sig.net_side == "add":
        actions = ADD_ACTIONS
    elif sig.net_side == "reduce":
        actions = REDUCE_ACTIONS
    else:
        actions = ADD_ACTIONS | REDUCE_ACTIONS
    codes = sorted(
        {leg.etf_code for leg in sig.legs if leg.action in actions and leg.etf_code}
    )
    return "|".join(codes), len(codes)


def flow_rows_from_signals(
    *,
    prev_date: str,
    event_date: str,
    signals: list[StockSignal],
    flow_version: str = FLOW_VERSION,
) -> list[dict]:
    rows: list[dict] = []
    for sig in signals:
        if sig.net_side not in ("add", "reduce", "mixed"):
            continue
        source_etfs, etf_count = _source_etfs_for_signal(sig)
        rows.append(
            {
                "event_date": event_date,
                "prev_date": prev_date,
                "stock_id": sig.stock_id,
                "stock_name": sig.stock_name or sig.stock_id,
                "net_side": sig.net_side,
                "consensus": sig.consensus_level or "NONE",
                "intent": sig.position_intent or "WATCH",
                "conviction": float(sig.conviction_score),
                "implied_flow_ntd": sig.flow_ntd_total,
                "consensus_score": float(sig.consensus_score),
                "etf_count": etf_count,
                "source_etfs": source_etfs,
                "flow_version": flow_version,
            }
        )
    return rows


def persist_flow_events(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    flow_version: str = FLOW_VERSION,
) -> int:
    """對齊 cohort 當日寫入 flow_events；無對齊則 0。"""
    result = build_aligned_signals(conn, etf_codes)
    if result is None:
        return 0
    rows = flow_rows_from_signals(
        prev_date=result.prev_date,
        event_date=result.curr_date,
        signals=result.signals,
        flow_version=flow_version,
    )
    return upsert_flow_events(conn, rows)
