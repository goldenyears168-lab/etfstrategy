#!/usr/bin/env python3
"""逐 ETF Flow 事件表：加減碼 leg + 事前/事後報酬 enrichment。"""

from __future__ import annotations

import sqlite3

from flow_returns import flow_tape_regime, post_event_returns, pre_event_return, sector_for_stock
from holdings_research import ADD_ACTIONS, REDUCE_ACTIONS
from project_config import FLOW_HORIZONS, FLOW_VERSION
from signal_engine import ChangeLeg, build_aligned_signals
from stock_db import load_stock_beta_map, upsert_flow_event_legs


def _leg_side(action: str) -> str | None:
    if action in ADD_ACTIONS:
        return "add"
    if action in REDUCE_ACTIONS:
        return "reduce"
    return None


def flow_leg_rows_from_signals(
    *,
    prev_date: str,
    event_date: str,
    legs: list[ChangeLeg],
    flow_version: str = FLOW_VERSION,
) -> list[dict]:
    rows: list[dict] = []
    for leg in legs:
        if _leg_side(leg.action) is None:
            continue
        row: dict = {
            "event_date": event_date,
            "prev_date": prev_date,
            "stock_id": leg.stock_id,
            "etf_id": leg.etf_code,
            "stock_name": leg.stock_name or leg.stock_id,
            "action": leg.action,
            "shares_delta": leg.share_delta,
            "value_delta": leg.flow_ntd,
            "weight_delta": leg.weight_delta_pp,
            "price_before_5d": None,
            "return_before_5d": None,
            "sector": sector_for_stock(leg.stock_id),
            "theme": leg.theme,
            "flow_tape_regime": None,
            "flow_version": flow_version,
        }
        for h in FLOW_HORIZONS:
            row[f"return_after_{h}d"] = None
            row[f"alpha_after_{h}d"] = None
        rows.append(row)
    return rows


def enrich_flow_event_legs(conn: sqlite3.Connection, *, flow_version: str = FLOW_VERSION) -> int:
    beta_map, _ = load_stock_beta_map(conn)
    rows = conn.execute(
        """
        SELECT event_date, stock_id, etf_id
        FROM flow_event_legs
        WHERE flow_version = ?
        """,
        (flow_version,),
    ).fetchall()
    if not rows:
        return 0
    regime_cache: dict[str, str | None] = {}
    updates: list[dict] = []
    for base in rows:
        event_date = str(base["event_date"])
        stock_id = str(base["stock_id"])
        etf_id = str(base["etf_id"])
        if event_date not in regime_cache:
            regime_cache[event_date] = flow_tape_regime(conn, event_date)
        price5, ret5 = pre_event_return(conn, stock_id, event_date)
        post = post_event_returns(
            conn, event_date=event_date, stock_id=stock_id, beta_map=beta_map
        )
        full = conn.execute(
            """
            SELECT * FROM flow_event_legs
            WHERE event_date = ? AND stock_id = ? AND etf_id = ? AND flow_version = ?
            """,
            (event_date, stock_id, etf_id, flow_version),
        ).fetchone()
        if full is None:
            continue
        payload = dict(full)
        payload["price_before_5d"] = price5
        payload["return_before_5d"] = ret5
        payload["flow_tape_regime"] = regime_cache[event_date]
        payload.update(post)
        updates.append(payload)
    return upsert_flow_event_legs(conn, updates)


def persist_flow_event_legs(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    flow_version: str = FLOW_VERSION,
    enrich: bool = True,
) -> int:
    result = build_aligned_signals(conn, etf_codes)
    if result is None:
        return 0
    legs: list[ChangeLeg] = []
    for sig in result.signals:
        legs.extend(sig.legs)
    rows = flow_leg_rows_from_signals(
        prev_date=result.prev_date,
        event_date=result.curr_date,
        legs=legs,
        flow_version=flow_version,
    )
    n = upsert_flow_event_legs(conn, rows)
    if enrich and n:
        enrich_flow_event_legs(conn, flow_version=flow_version)
    return n
