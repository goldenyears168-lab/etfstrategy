"""券商分點買超 → CopytradeSignal（門檻 × Top-N）。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from copytrade.signals import CopytradeSignal, group_signals_by_date
from stock_db import load_broker_branch_nets, normalize_stock_name

BRANCH_BUY_ACTION = "分點買超"

# FinMind / TWSE volumes are shares; 1 張 = 1000 股.
SHARES_PER_LOT = 1000

DEFAULT_TRADER_ID_YUANTA_SONGJIANG = "9801"  # 元大-松江
DEFAULT_TRADER_ID_FUBON_XINDIAN = "9661"  # 富邦-新店
DEFAULT_TRADER_ID_KGI_CHENGZHONG = "9227"  # 凱基-城中


def _is_listed_equity_id(stock_id: str) -> bool:
    sid = str(stock_id).strip()
    return sid.isdigit() and len(sid) == 4


def avg_volume_shares(
    conn: sqlite3.Connection,
    stock_id: str,
    as_of: str,
    *,
    lookback: int = 20,
) -> float | None:
    rows = conn.execute(
        """
        SELECT volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
          AND volume IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, as_of, lookback),
    ).fetchall()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < max(5, lookback // 2):
        return None
    return sum(vals) / len(vals)


def iter_branch_buy_signals(
    conn: sqlite3.Connection,
    securities_trader_id: str,
    *,
    min_net_shares: float,
    top_n: int,
    window_start: str | None = None,
    window_end: str | None = None,
    source: str = "finmind",
    min_avg_volume_shares: float | None = 200_000.0,
    avg_volume_lookback: int = 20,
) -> list[CopytradeSignal]:
    """Build signals: net ≥ min_net_shares, then Top-N by net per signal date."""
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if min_net_shares < 0:
        raise ValueError("min_net_shares must be >= 0")

    rows = load_broker_branch_nets(
        conn,
        securities_trader_id,
        source=source,
        window_start=window_start,
        window_end=window_end,
        min_net=min_net_shares,
    )
    by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        sid = str(r["stock_id"])
        if not _is_listed_equity_id(sid):
            continue
        by_date[str(r["trade_date"])].append(r)

    out: list[CopytradeSignal] = []
    vol_cache: dict[tuple[str, str], float | None] = {}
    for signal_date in sorted(by_date):
        day_rows = sorted(
            by_date[signal_date],
            key=lambda r: (-float(r["net"]), str(r["stock_id"])),
        )
        kept = 0
        for r in day_rows:
            if kept >= top_n:
                break
            sid = str(r["stock_id"])
            if min_avg_volume_shares is not None:
                key = (sid, signal_date)
                if key not in vol_cache:
                    vol_cache[key] = avg_volume_shares(
                        conn,
                        sid,
                        signal_date,
                        lookback=avg_volume_lookback,
                    )
                avg_vol = vol_cache[key]
                if avg_vol is None or avg_vol < min_avg_volume_shares:
                    continue
            net = float(r["net"])
            out.append(
                CopytradeSignal(
                    signal_date=signal_date,
                    stock_id=sid,
                    stock_name=normalize_stock_name(sid),
                    action=BRANCH_BUY_ACTION,
                    share_delta=net,
                    weight_delta=None,
                    weight_pct_curr=None,
                )
            )
            kept += 1
    return out


def iter_branch_amount_buy_signals(
    conn: sqlite3.Connection,
    securities_trader_id: str,
    *,
    min_net_amount_ntd: float,
    top_n: int,
    window_start: str | None = None,
    window_end: str | None = None,
    source: str = "finmind",
    min_avg_volume_shares: float | None = 200_000.0,
    avg_volume_lookback: int = 20,
) -> list[CopytradeSignal]:
    """Build signals by PIT net amount: positive net shares × signal-day close."""
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if min_net_amount_ntd < 0:
        raise ValueError("min_net_amount_ntd must be >= 0")

    rows = conn.execute(
        """
        SELECT b.trade_date, b.stock_id, b.net, p.close,
               b.net * p.close AS net_amount_ntd
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id
         AND p.trade_date = b.trade_date
         AND p.source = ?
        WHERE b.securities_trader_id = ?
          AND b.source = ?
          AND b.net > 0
          AND p.close IS NOT NULL
          AND p.close > 0
          AND b.net * p.close >= ?
          AND (? IS NULL OR b.trade_date >= ?)
          AND (? IS NULL OR b.trade_date <= ?)
        ORDER BY b.trade_date, net_amount_ntd DESC, b.stock_id
        """,
        (
            source,
            securities_trader_id,
            source,
            min_net_amount_ntd,
            window_start,
            window_start,
            window_end,
            window_end,
        ),
    ).fetchall()
    by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        sid = str(row["stock_id"])
        if _is_listed_equity_id(sid):
            by_date[str(row["trade_date"])].append(row)

    out: list[CopytradeSignal] = []
    vol_cache: dict[tuple[str, str], float | None] = {}
    for signal_date in sorted(by_date):
        kept = 0
        for row in by_date[signal_date]:
            if kept >= top_n:
                break
            sid = str(row["stock_id"])
            if min_avg_volume_shares is not None:
                key = (sid, signal_date)
                if key not in vol_cache:
                    vol_cache[key] = avg_volume_shares(
                        conn,
                        sid,
                        signal_date,
                        lookback=avg_volume_lookback,
                    )
                avg_vol = vol_cache[key]
                if avg_vol is None or avg_vol < min_avg_volume_shares:
                    continue
            out.append(
                CopytradeSignal(
                    signal_date=signal_date,
                    stock_id=sid,
                    stock_name=normalize_stock_name(sid),
                    action=BRANCH_BUY_ACTION,
                    share_delta=float(row["net"]),
                    weight_delta=None,
                    weight_pct_curr=None,
                )
            )
            kept += 1
    return out


def group_branch_signals_by_date(
    signals: list[CopytradeSignal],
) -> dict[str, list[CopytradeSignal]]:
    return group_signals_by_date(signals)


def mean_net(signals: list[CopytradeSignal]) -> float:
    if not signals:
        return 0.0
    return sum(float(s.share_delta) for s in signals) / len(signals)
