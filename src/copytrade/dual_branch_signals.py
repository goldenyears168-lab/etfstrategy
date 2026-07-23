"""雙分點 Copytrade 訊號 · 張數/金額門檻 × sj|xd|and|or|sum。

金額代理（PIT）：訊號日收盤價 × 淨買超股數。收盤後才知分點，無 look-ahead。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from copytrade.branch_signals import (
    BRANCH_BUY_ACTION,
    DEFAULT_TRADER_ID_FUBON_XINDIAN,
    DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    SHARES_PER_LOT,
    _is_listed_equity_id,
    avg_volume_shares,
)
from copytrade.signals import CopytradeSignal, group_signals_by_date
from stock_db import load_broker_branch_nets, normalize_stock_name

Metric = Literal["lots", "amount"]
Mode = Literal["sj", "xd", "and", "or", "sum"]

MODES: tuple[Mode, ...] = ("sj", "xd", "and", "or", "sum")
METRICS: tuple[Metric, ...] = ("lots", "amount")

DEFAULT_MIN_NET_SHARES = (50_000.0, 100_000.0, 500_000.0)
DEFAULT_MIN_NET_AMOUNTS = (5_000_000.0, 10_000_000.0, 20_000_000.0, 50_000_000.0)
# Denser amount grid · aligned to lots≥500張 density near ~40–80M for 新店.
AMOUNT_FOCUS_MIN_NET_AMOUNTS = (
    10_000_000.0,
    20_000_000.0,
    40_000_000.0,
    50_000_000.0,
    80_000_000.0,
    100_000_000.0,
    150_000_000.0,
)
AMOUNT_FOCUS_MODES: tuple[Mode, ...] = ("sj", "xd", "and", "sum")


@dataclass(frozen=True)
class BranchDayScore:
    stock_id: str
    score: float  # lots: net shares; amount: NTD
    net_shares: float
    sj_score: float
    xd_score: float


def _nets_by_date(
    conn: sqlite3.Connection,
    trader_id: str,
    *,
    window_start: str | None,
    window_end: str | None,
    source: str = "finmind",
) -> dict[str, dict[str, float]]:
    """trade_date → {stock_id: net_shares} for positive nets only."""
    rows = load_broker_branch_nets(
        conn,
        trader_id,
        source=source,
        window_start=window_start,
        window_end=window_end,
        min_net=0.0,
    )
    by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        sid = str(r["stock_id"])
        if not _is_listed_equity_id(sid):
            continue
        net = float(r["net"])
        if net <= 0:
            continue
        by_date[str(r["trade_date"])][sid] = net
    return by_date


def _score_value(
    net_shares: float,
    *,
    metric: Metric,
    close: float | None,
) -> float | None:
    if net_shares <= 0:
        return None
    if metric == "lots":
        return net_shares
    if close is None or close <= 0:
        return None
    return net_shares * close


@dataclass
class DualBranchTapeCache:
    """Preload nets (+ closes for amount) once per sweep window."""

    sj: dict[str, dict[str, float]]
    xd: dict[str, dict[str, float]]
    closes: dict[tuple[str, str], float]

    @classmethod
    def load(
        cls,
        conn: sqlite3.Connection,
        *,
        window_start: str | None,
        window_end: str | None,
        trader_id_sj: str = DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
        trader_id_xd: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
        need_closes: bool = True,
    ) -> DualBranchTapeCache:
        sj = _nets_by_date(
            conn, trader_id_sj, window_start=window_start, window_end=window_end
        )
        xd = _nets_by_date(
            conn, trader_id_xd, window_start=window_start, window_end=window_end
        )
        closes: dict[tuple[str, str], float] = {}
        if need_closes:
            # Only closes for stocks that appear on either tape.
            stock_ids = set()
            for day_map in list(sj.values()) + list(xd.values()):
                stock_ids.update(day_map)
            if stock_ids:
                sql = """
                    SELECT stock_id, trade_date, close
                    FROM stock_daily_bars
                    WHERE source = 'finmind' AND close IS NOT NULL
                      AND stock_id IN ({})
                """.format(",".join("?" for _ in stock_ids))
                params: list[object] = list(stock_ids)
                if window_start:
                    sql += " AND trade_date >= ?"
                    params.append(window_start)
                if window_end:
                    sql += " AND trade_date <= ?"
                    params.append(window_end)
                for r in conn.execute(sql, params):
                    closes[(str(r[0]), str(r[1]))] = float(r[2])
        return cls(sj=sj, xd=xd, closes=closes)


def iter_dual_branch_buy_signals(
    conn: sqlite3.Connection,
    *,
    mode: Mode,
    metric: Metric,
    min_threshold: float,
    top_n: int,
    window_start: str | None = None,
    window_end: str | None = None,
    trader_id_sj: str = DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    trader_id_xd: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
    min_avg_volume_shares: float | None = 200_000.0,
    avg_volume_lookback: int = 20,
    tape: DualBranchTapeCache | None = None,
) -> list[CopytradeSignal]:
    """Build dual-branch signals for one (mode, metric, threshold, top_n)."""
    if mode not in MODES:
        raise ValueError(f"unsupported mode: {mode}")
    if metric not in METRICS:
        raise ValueError(f"unsupported metric: {metric}")
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if min_threshold < 0:
        raise ValueError("min_threshold must be >= 0")

    if tape is None:
        tape = DualBranchTapeCache.load(
            conn,
            window_start=window_start,
            window_end=window_end,
            trader_id_sj=trader_id_sj,
            trader_id_xd=trader_id_xd,
            need_closes=(metric == "amount"),
        )
    sj = tape.sj
    xd = tape.xd
    closes = tape.closes if metric == "amount" else {}
    all_dates = sorted(set(sj) | set(xd))

    out: list[CopytradeSignal] = []
    vol_cache: dict[tuple[str, str], float | None] = {}

    for day in all_dates:
        sj_day = sj.get(day) or {}
        xd_day = xd.get(day) or {}
        stock_ids = set(sj_day) | set(xd_day)
        scored: list[BranchDayScore] = []

        for sid in stock_ids:
            sj_net = float(sj_day.get(sid) or 0.0)
            xd_net = float(xd_day.get(sid) or 0.0)
            close = closes.get((sid, day)) if metric == "amount" else None
            sj_sc = _score_value(sj_net, metric=metric, close=close)
            xd_sc = _score_value(xd_net, metric=metric, close=close)
            sj_ok = sj_sc is not None and sj_sc >= min_threshold
            xd_ok = xd_sc is not None and xd_sc >= min_threshold

            score: float | None = None
            if mode == "sj":
                if sj_ok:
                    score = float(sj_sc)  # type: ignore[arg-type]
            elif mode == "xd":
                if xd_ok:
                    score = float(xd_sc)  # type: ignore[arg-type]
            elif mode == "and":
                if sj_ok and xd_ok:
                    # Rank by min leg (both must clear); display score = sum.
                    score = float(sj_sc) + float(xd_sc)  # type: ignore[arg-type]
            elif mode == "or":
                cands = [s for s in (sj_sc, xd_sc) if s is not None and s >= min_threshold]
                if cands:
                    score = max(cands)
            elif mode == "sum":
                parts = [s for s in (sj_sc, xd_sc) if s is not None]
                if parts:
                    total = sum(parts)
                    if total >= min_threshold:
                        score = total

            if score is None:
                continue
            scored.append(
                BranchDayScore(
                    stock_id=sid,
                    score=score,
                    net_shares=max(sj_net, xd_net),
                    sj_score=float(sj_sc or 0.0),
                    xd_score=float(xd_sc or 0.0),
                )
            )

        scored.sort(key=lambda x: (-x.score, x.stock_id))
        kept = 0
        for item in scored:
            if kept >= top_n:
                break
            if min_avg_volume_shares is not None:
                key = (item.stock_id, day)
                if key not in vol_cache:
                    vol_cache[key] = avg_volume_shares(
                        conn,
                        item.stock_id,
                        day,
                        lookback=avg_volume_lookback,
                    )
                avg_vol = vol_cache[key]
                if avg_vol is None or avg_vol < min_avg_volume_shares:
                    continue
            out.append(
                CopytradeSignal(
                    signal_date=day,
                    stock_id=item.stock_id,
                    stock_name=normalize_stock_name(item.stock_id),
                    action=BRANCH_BUY_ACTION,
                    share_delta=item.net_shares,
                    weight_delta=None,
                    weight_pct_curr=None,
                )
            )
            kept += 1
    return out


def group_dual_branch_signals_by_date(
    signals: list[CopytradeSignal],
) -> dict[str, list[CopytradeSignal]]:
    return group_signals_by_date(signals)


def threshold_label(metric: Metric, min_threshold: float) -> str:
    if metric == "lots":
        return f"{min_threshold / SHARES_PER_LOT:.0f}張"
    if min_threshold >= 1_000_000:
        return f"{min_threshold / 1_000_000:.0f}M"
    return f"{min_threshold:.0f}"
