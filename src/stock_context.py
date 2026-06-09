"""成分股技術位、量能、法人籌碼（讀 stock_daily_bars / stock_institutional_daily）。"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

from market_labels import (
    CHIP_DIVERGE,
    CHIP_FOREIGN_BUY,
    CHIP_FOREIGN_SELL_DIV,
    CHIP_NEUTRAL,
    CHIP_SYNC_BUY,
    CHIP_SYNC_SELL,
    VOL_DOWN,
    VOL_FLAT,
    VOL_SURGE,
    VOL_UP,
)
from signal_engine import StockSignal, build_aligned_signals

TRADING_DAYS_52W = 252
MA20_DAYS = 20
MA60_DAYS = 60
VOL_LOOKBACK = 5
VOL_METRICS_DAYS = 14
ATR_PERIOD = 14


@dataclass(frozen=True)
class TechnicalSnapshot:
    stock_id: str
    trade_date: str | None
    close: float | None
    ma20: float | None
    ma60: float | None
    dist_ma20_pct: float | None
    dist_ma60_pct: float | None
    high_52w: float | None
    low_52w: float | None
    position_52w_pct: float | None
    dist_from_52w_high_pct: float | None
    volume: int | None
    vol_avg_5d: float | None
    vol_ratio_5d: float | None
    vol_label: str
    atr14_pct: float | None = None
    avg_range_pct_14d: float | None = None
    realized_vol_pct_14d: float | None = None


def compute_price_volatility_metrics(
    series: list[sqlite3.Row],
    *,
    close: float,
    window: int = VOL_METRICS_DAYS,
    atr_period: int = ATR_PERIOD,
) -> tuple[float | None, float | None, float | None]:
    """
    從日 K 計算波動指標（%）：
    - ATR14%：14 日平均真實區間 / 收盤
    - avg_range_pct：近 N 日 (high-low)/close 平均
    - realized_vol_pct：近 N 日日報酬率標準差
    """
    if not series or close <= 0 or len(series) < 2:
        return None, None, None

    true_ranges: list[float] = []
    daily_returns_pct: list[float] = []
    range_pcts: list[float] = []

    for i, row in enumerate(series):
        c = float(row["close"])
        h = float(row["high"] if row["high"] is not None else c)
        l = float(row["low"] if row["low"] is not None else c)
        if c > 0:
            range_pcts.append((h - l) / c * 100.0)
        if i == 0:
            continue
        prev_c = float(series[i - 1]["close"])
        if prev_c > 0:
            daily_returns_pct.append((c / prev_c - 1.0) * 100.0)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)

    atr_pct: float | None = None
    if len(true_ranges) >= atr_period:
        atr = sum(true_ranges[-atr_period:]) / atr_period
        atr_pct = round(atr / close * 100.0, 2)

    avg_range: float | None = None
    if len(range_pcts) >= window:
        avg_range = round(sum(range_pcts[-window:]) / window, 2)

    realized: float | None = None
    if len(daily_returns_pct) >= window:
        realized = round(statistics.stdev(daily_returns_pct[-window:]), 2)

    return atr_pct, avg_range, realized


@dataclass(frozen=True)
class InstitutionalSnapshot:
    stock_id: str
    trade_date: str | None
    foreign_net: float | None
    investment_trust_net: float | None
    dealer_self_net: float | None
    foreign_label: str
    trust_label: str
    dealer_label: str


@dataclass(frozen=True)
class ChipResonance:
    stock_id: str
    stock_name: str
    etf_flow: str
    foreign_label: str
    trust_label: str
    dealer_label: str
    tag: str
    note: str


def _inst_label(net: float | None, *, large_threshold: float = 50_000_000) -> str:
    if net is None:
        return "—"
    if net >= large_threshold:
        return "大買超"
    if net > 0:
        return "買超"
    if net <= -large_threshold:
        return "大賣超"
    if net < 0:
        return "賣超"
    return "持平"


def _etf_flow_label(sig: StockSignal | None) -> str:
    if sig is None:
        return "—"
    if sig.net_side == "add":
        return "ETF加碼"
    if sig.net_side == "reduce":
        return "ETF減碼"
    if sig.net_side == "mixed":
        return "ETF分歧調倉"
    return "ETF持平"


def classify_chip_resonance(
    etf_flow: str,
    foreign_net: float | None,
    trust_net: float | None,
    *,
    foreign_sell_threshold: float = -30_000_000,
) -> tuple[str, str]:
    """回傳 (tag, note)。"""
    f_buy = foreign_net is not None and foreign_net > 0
    f_sell = foreign_net is not None and foreign_net <= foreign_sell_threshold
    t_buy = trust_net is not None and trust_net > 0

    if etf_flow == "ETF加碼" and f_buy and t_buy:
        return CHIP_SYNC_BUY, "ETF加碼，外資、投信同步買超"
    if etf_flow == "ETF加碼" and f_sell:
        return CHIP_FOREIGN_SELL_DIV, "ETF加碼，外資大幅賣超"
    if etf_flow == "ETF減碼" and f_buy:
        return CHIP_DIVERGE, "ETF減碼，外資買超（籌碼背離）"
    if etf_flow == "ETF加碼" and f_buy:
        return CHIP_FOREIGN_BUY, "ETF加碼，外資買超"
    if etf_flow == "ETF減碼" and foreign_net is not None and foreign_net < 0:
        return CHIP_SYNC_SELL, "ETF減碼，外資賣超"
    return CHIP_NEUTRAL, "三大法人方向不明顯"


def classify_volume(vol_ratio: float | None) -> str:
    if vol_ratio is None:
        return "—"
    if vol_ratio >= 2.0:
        return VOL_SURGE
    if vol_ratio >= 1.2:
        return VOL_UP
    if vol_ratio < 0.8:
        return VOL_DOWN
    return VOL_FLAT


def load_daily_bars(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    limit: int = 280,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT trade_date, open, high, low, close, volume
            FROM stock_daily_bars
            WHERE stock_id = ? AND source = 'finmind'
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (stock_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def load_tej_daily_bars(
    conn: sqlite3.Connection,
    code: str,
    *,
    limit: int = 280,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT date AS trade_date, open, high, low, close, volume
            FROM daily_bars
            WHERE code = ? AND source = 'tej'
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _compute_technical_from_rows(
    rows: list[sqlite3.Row],
    *,
    entity_id: str,
) -> TechnicalSnapshot | None:
    if not rows:
        return None
    series = list(reversed(rows))
    closes = [float(r["close"]) for r in series if r["close"] is not None]
    if not closes:
        return None
    last = series[-1]
    close = float(last["close"])
    trade_date = str(last["trade_date"])

    def _sma(n: int) -> float | None:
        if len(closes) < n:
            return None
        return sum(closes[-n:]) / n

    ma20 = _sma(MA20_DAYS)
    ma60 = _sma(MA60_DAYS)
    dist_ma20 = ((close / ma20) - 1.0) * 100.0 if ma20 else None
    dist_ma60 = ((close / ma60) - 1.0) * 100.0 if ma60 else None

    window = series[-min(TRADING_DAYS_52W, len(series)) :]
    highs = [float(r["high"] or r["close"]) for r in window]
    lows = [float(r["low"] or r["close"]) for r in window]
    high_52 = max(highs) if highs else None
    low_52 = min(lows) if lows else None
    pos_52 = None
    dist_hi = None
    if high_52 is not None and low_52 is not None and high_52 > low_52:
        pos_52 = (close - low_52) / (high_52 - low_52) * 100.0
        dist_hi = (close / high_52 - 1.0) * 100.0

    vol = int(last["volume"]) if last["volume"] is not None else None
    vols = [
        float(r["volume"])
        for r in series[-(VOL_LOOKBACK + 1) : -1]
        if r["volume"] is not None
    ]
    vol_avg = sum(vols) / len(vols) if vols else None
    vol_ratio = (vol / vol_avg) if vol is not None and vol_avg and vol_avg > 0 else None
    atr14_pct, avg_range_pct_14d, realized_vol_pct_14d = compute_price_volatility_metrics(
        series,
        close=close,
    )

    return TechnicalSnapshot(
        stock_id=entity_id,
        trade_date=trade_date,
        close=close,
        ma20=round(ma20, 2) if ma20 else None,
        ma60=round(ma60, 2) if ma60 else None,
        dist_ma20_pct=round(dist_ma20, 2) if dist_ma20 is not None else None,
        dist_ma60_pct=round(dist_ma60, 2) if dist_ma60 is not None else None,
        high_52w=round(high_52, 2) if high_52 else None,
        low_52w=round(low_52, 2) if low_52 else None,
        position_52w_pct=round(pos_52, 1) if pos_52 is not None else None,
        dist_from_52w_high_pct=round(dist_hi, 2) if dist_hi is not None else None,
        volume=vol,
        vol_avg_5d=round(vol_avg, 0) if vol_avg else None,
        vol_ratio_5d=round(vol_ratio, 2) if vol_ratio is not None else None,
        vol_label=classify_volume(vol_ratio),
        atr14_pct=atr14_pct,
        avg_range_pct_14d=avg_range_pct_14d,
        realized_vol_pct_14d=realized_vol_pct_14d,
    )


def compute_technical(conn: sqlite3.Connection, stock_id: str) -> TechnicalSnapshot | None:
    return _compute_technical_from_rows(
        load_daily_bars(conn, stock_id),
        entity_id=stock_id,
    )


def compute_technical_tej(
    conn: sqlite3.Connection,
    code: str,
) -> TechnicalSnapshot | None:
    """大盤/ADR 等 TEJ daily_bars 技術位（早盤用）。"""
    return _compute_technical_from_rows(
        load_tej_daily_bars(conn, code),
        entity_id=code,
    )


def load_latest_institutional(
    conn: sqlite3.Connection,
    stock_id: str,
) -> InstitutionalSnapshot | None:
    try:
        row = conn.execute(
            """
            SELECT trade_date, foreign_net, investment_trust_net, dealer_self_net
            FROM stock_institutional_daily
            WHERE stock_id = ? AND source = 'finmind'
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (stock_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    fn = row["foreign_net"]
    tn = row["investment_trust_net"]
    dn = row["dealer_self_net"]
    return InstitutionalSnapshot(
        stock_id=stock_id,
        trade_date=str(row["trade_date"]),
        foreign_net=float(fn) if fn is not None else None,
        investment_trust_net=float(tn) if tn is not None else None,
        dealer_self_net=float(dn) if dn is not None else None,
        foreign_label=_inst_label(fn),
        trust_label=_inst_label(tn),
        dealer_label=_inst_label(dn),
    )


def build_chip_resonance(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    stock_ids: list[str],
    name_by_id: dict[str, str],
) -> list[ChipResonance]:
    aligned = build_aligned_signals(conn, etf_codes)
    sig_map: dict[str, StockSignal] = {}
    if aligned is not None:
        sig_map = {s.stock_id: s for s in aligned.signals}

    out: list[ChipResonance] = []
    for sid in stock_ids:
        sig = sig_map.get(sid)
        etf_flow = _etf_flow_label(sig)
        inst = load_latest_institutional(conn, sid)
        fn = inst.foreign_net if inst else None
        tn = inst.investment_trust_net if inst else None
        dn = inst.dealer_self_net if inst else None
        tag, note = classify_chip_resonance(etf_flow, fn, tn)
        out.append(
            ChipResonance(
                stock_id=sid,
                stock_name=name_by_id.get(sid, ""),
                etf_flow=etf_flow,
                foreign_label=inst.foreign_label if inst else "—",
                trust_label=inst.trust_label if inst else "—",
                dealer_label=inst.dealer_label if inst else "—",
                tag=tag,
                note=note,
            )
        )
    return out
