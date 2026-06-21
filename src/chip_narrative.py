"""資金面敘事（只讀 DB · 簡化版）。"""

from __future__ import annotations

import sqlite3

from chip_data import load_chip_snapshot
from project_config import NEUTRAL_SUBSCORE


def build_chip_scores(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    etf_net_side: str | None = None,
    trade_date: str | None = None,
) -> dict:
    del etf_net_side
    snap = load_chip_snapshot(conn, stock_id, trade_date=trade_date)
    neutral = float(NEUTRAL_SUBSCORE)
    detail = "籌碼中性" if snap is None else "籌碼資料已載入"
    return {
        "crowd_score": neutral,
        "crowd_label": "籌碼中性",
        "crowd_detail": detail,
        "short_pressure_score": neutral,
        "short_pressure_label": "籌碼中性",
        "short_pressure_detail": detail,
        "speculation_score": neutral,
        "speculation_label": "籌碼中性",
        "speculation_detail": detail,
    }


def compose_chip_narrative(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    etf_net_side: str | None = None,
    trade_date: str | None = None,
) -> str:
    snap = load_chip_snapshot(conn, stock_id, trade_date=trade_date)
    if snap is None:
        return ""
    if etf_net_side == "add":
        return "ETF加碼"
    if etf_net_side == "reduce":
        return "ETF減碼"
    return ""
