"""Copytrade L1H9 · JSON snapshot for Supabase / Readdy (copytrade-daily-v1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from copytrade_l1h9_daily import (
    HOLD_DAYS,
    N_SLOTS,
    STRATEGY_ID,
    _consensus_add_set,
    signals_for_date,
)
from project_config import ETF_CODES_HOLDINGS

_TPE = ZoneInfo("Asia/Taipei")
CONTRACT = "copytrade-daily-v1"

ACTION_ZH = {
    "新进": "新進",
    "加码": "加碼",
}

HEADERS = ["代號", "名稱", "動作", "股數差", "權重差", "共識≥2"]


def _table_row(sig, consensus: set[str]) -> list[str | int | float | None]:
    action = ACTION_ZH.get(sig.action, sig.action)
    share_s = f"{int(sig.share_delta):+d}"
    wt_s = f"{sig.weight_delta:+.2f}%" if sig.weight_delta is not None else "—"
    hit = "是" if sig.stock_id in consensus else ""
    return [sig.stock_id, sig.stock_name, action, share_s, wt_s, hit]


def build_copytrade_snapshot_json(
    conn: sqlite3.Connection,
    as_of: str,
    etf_codes: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    codes = etf_codes or ETF_CODES_HOLDINGS
    score_date, outcome_date, signals = signals_for_date(conn, as_of)
    consensus = _consensus_add_set(conn, codes)
    rows = [_table_row(sig, consensus) for sig in signals]
    consensus_hits = [s for s in signals if s.stock_id in consensus]

    return {
        "contract": CONTRACT,
        "layer": "strategy",
        "strategy_id": STRATEGY_ID,
        "as_of": as_of,
        "signal_date": as_of,
        "hold_days": HOLD_DAYS,
        "n_slots": N_SLOTS,
        "hold_range": {
            "score_date": score_date or None,
            "outcome_date": outcome_date or None,
        },
        "signal_count": len(signals),
        "consensus_count": len(consensus_hits),
        "strategy_spec_zh": (
            f"00981A 新進／加碼 → 隔日開盤 · 持 {HOLD_DAYS} 交易日 · {N_SLOTS} 槽"
        ),
        "headers": HEADERS,
        "rows": rows,
        "signals": [
            {
                "stock_id": sig.stock_id,
                "stock_name": sig.stock_name,
                "action": ACTION_ZH.get(sig.action, sig.action),
                "share_delta": int(sig.share_delta),
                "weight_delta": sig.weight_delta,
                "consensus_add": sig.stock_id in consensus,
            }
            for sig in signals
        ],
        "meta": {"generated_at": datetime.now(_TPE).isoformat()},
    }
