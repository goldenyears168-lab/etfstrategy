"""ETF 持股變化跟單訊號 · 主線 digest 與回測共用。

不含回測／模擬邏輯；`copytrade_backtest` 與研究腳本皆由此讀取訊號。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from stock_db import (
    compute_etf_holdings_changes,
    list_etf_snapshot_dates,
    normalize_stock_name,
)

ADD_ACTIONS = frozenset({"新进", "加码"})
INITIATION_ACTION = "新进"
REPEAT_ADD_ACTION = "加码"


@dataclass(frozen=True)
class CopytradeSignal:
    signal_date: str
    stock_id: str
    stock_name: str
    action: str
    share_delta: float
    weight_delta: float | None
    weight_pct_curr: float | None = None


def snapshot_pairs(dates: list[str], *, backfill: bool) -> list[tuple[str, str]]:
    """dates DESC → (score_date=較舊, outcome_date=較新) 相鄰快照對。"""
    if len(dates) < 2:
        return []
    pairs: list[tuple[str, str]] = []
    for i in range(len(dates) - 1):
        outcome_date = dates[i]
        score_date = dates[i + 1]
        pairs.append((score_date, outcome_date))
    if backfill:
        return pairs
    return pairs[:1] if pairs else []


def iter_copytrade_signals(
    conn: sqlite3.Connection,
    etf_code: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    actions: frozenset[str] | None = None,
) -> list[CopytradeSignal]:
    dates = list_etf_snapshot_dates(conn, etf_code)
    pairs = snapshot_pairs(dates, backfill=True)
    out: list[CopytradeSignal] = []
    for _score_date, outcome_date in pairs:
        if window_start and outcome_date < window_start:
            continue
        if window_end and outcome_date > window_end:
            continue
        for row in compute_etf_holdings_changes(
            conn, etf_code, outcome_date, _score_date
        ):
            action = str(row["action"] or "")
            allowed = ADD_ACTIONS if actions is None else actions
            if action not in allowed:
                continue
            delta = float(row["share_delta"] or 0)
            if delta <= 0:
                continue
            out.append(
                CopytradeSignal(
                    signal_date=outcome_date,
                    stock_id=str(row["stock_id"]),
                    stock_name=normalize_stock_name(str(row["stock_name"] or "")),
                    action=action,
                    share_delta=delta,
                    weight_delta=(
                        float(row["weight_delta"])
                        if row["weight_delta"] is not None
                        else None
                    ),
                    weight_pct_curr=(
                        float(row["weight_pct_curr"])
                        if row["weight_pct_curr"] is not None
                        else None
                    ),
                )
            )
    return out


def group_signals_by_date(
    signals: list[CopytradeSignal],
) -> dict[str, list[CopytradeSignal]]:
    grouped: dict[str, list[CopytradeSignal]] = {}
    for sig in signals:
        grouped.setdefault(sig.signal_date, []).append(sig)
    return grouped


def filter_grouped_signals(
    grouped: dict[str, list[CopytradeSignal]],
    actions: frozenset[str],
) -> dict[str, list[CopytradeSignal]]:
    """保留含指定 action 的訊號日；當日僅含符合 action 的 leg。"""
    out: dict[str, list[CopytradeSignal]] = {}
    for signal_date, legs in grouped.items():
        kept = [lg for lg in legs if lg.action in actions]
        if kept:
            out[signal_date] = kept
    return out
