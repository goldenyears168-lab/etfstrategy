"""RRG mono 隔日進場 · 專家確認 K 線觸發（Bone Zone / VWAP reclaim / VWAP bounce）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from stock_db.kbar import KbarBar

ExpertEntryMode = Literal["bone_zone", "vwap_reclaim", "vwap_bounce", "pivot_retest"]
EntryFillMode = Literal["poll_px", "bone_zone", "vwap_reclaim", "vwap_bounce", "pivot_retest"]

NO_TRADE_BEFORE = "09:05"
NO_TRADE_END = "13:30"
EMA_FAST = 9
EMA_SLOW = 20


@dataclass(frozen=True)
class EntryTrigger:
    mode: ExpertEntryMode
    entry_minute: str
    entry_px: float
    stop_px: float


@dataclass
class _EnrichedBar:
    bar: KbarBar
    ema9: float | None
    ema20: float | None
    vwap: float | None


def _norm_minute(minute: str) -> str:
    return minute if len(minute) > 5 else f"{minute}:00"


def _minute_ge(a: str, b: str) -> bool:
    return _norm_minute(a) >= _norm_minute(b)


def bars_at_or_before(bars: tuple[KbarBar, ...], minute: str) -> tuple[KbarBar, ...]:
    """PIT slice · 僅含 minute ≤ 截止時點的 1 分 K。"""
    target = _norm_minute(minute)
    return tuple(b for b in bars if _norm_minute(b.minute) <= target)


def bars_from_minute(bars: tuple[KbarBar, ...], minute: str) -> tuple[KbarBar, ...]:
    """PIT slice · 僅含 minute ≥ 起始時點的 1 分 K。"""
    start = _norm_minute(minute)
    return tuple(b for b in bars if _norm_minute(b.minute) >= start)


def _is_bullish(bar: KbarBar) -> bool:
    return bar.close >= bar.open


def compute_ema(closes: list[float], period: int) -> list[float | None]:
    if not closes or period <= 0:
        return [None] * len(closes)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    mult = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(closes)):
        prev = closes[i] * mult + prev * (1.0 - mult)
        out[i] = prev
    return out


def compute_vwap_series(bars: list[KbarBar]) -> list[float | None]:
    out: list[float | None] = []
    cum_pv = 0.0
    cum_v = 0.0
    for bar in bars:
        tp = (bar.high + bar.low + bar.close) / 3.0
        vol = float(bar.volume) if bar.volume and bar.volume > 0 else 1.0
        cum_pv += tp * vol
        cum_v += vol
        out.append(cum_pv / cum_v if cum_v > 0 else None)
    return out


def enrich_bars(bars: tuple[KbarBar, ...]) -> list[_EnrichedBar]:
    closes = [b.close for b in bars]
    ema9 = compute_ema(closes, EMA_FAST)
    ema20 = compute_ema(closes, EMA_SLOW)
    vwap = compute_vwap_series(list(bars))
    return [
        _EnrichedBar(bar=bars[i], ema9=ema9[i], ema20=ema20[i], vwap=vwap[i])
        for i in range(len(bars))
    ]


def _tradeable_bars(enriched: list[_EnrichedBar]) -> list[_EnrichedBar]:
    return [
        b
        for b in enriched
        if _minute_ge(b.bar.minute, NO_TRADE_BEFORE) and b.bar.minute <= NO_TRADE_END
    ]


def _in_bone_zone(bar: _EnrichedBar) -> bool:
    if bar.ema9 is None or bar.ema20 is None:
        return False
    lo_band = min(bar.ema9, bar.ema20)
    hi_band = max(bar.ema9, bar.ema20)
    return lo_band <= bar.bar.low <= hi_band or (
        bar.bar.low <= hi_band and bar.bar.close >= lo_band and bar.bar.close <= hi_band
    )


def detect_bone_zone_entry(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """Pullback into 9–20 EMA band · confirm = bullish close above 9 EMA."""
    enriched = _tradeable_bars(enrich_bars(bars))
    if len(enriched) < EMA_SLOW:
        return None
    saw_above = False
    in_pullback = False
    for row in enriched:
        if row.ema9 is None or row.ema20 is None:
            continue
        if row.bar.close > row.ema9:
            saw_above = True
        if saw_above and _in_bone_zone(row):
            in_pullback = True
        if in_pullback and _is_bullish(row.bar) and row.bar.close > row.ema9:
            stop = min(row.bar.low, row.ema20)
            return EntryTrigger(
                mode="bone_zone",
                entry_minute=row.bar.minute,
                entry_px=row.bar.close,
                stop_px=stop,
            )
    return None


def detect_vwap_reclaim_entry(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """Was below VWAP · confirm = first bullish close above VWAP."""
    enriched = _tradeable_bars(enrich_bars(bars))
    was_below = False
    for row in enriched:
        if row.vwap is None:
            continue
        if row.bar.close < row.vwap:
            was_below = True
        if was_below and _is_bullish(row.bar) and row.bar.close > row.vwap:
            stop = min(row.bar.low, row.vwap)
            return EntryTrigger(
                mode="vwap_reclaim",
                entry_minute=row.bar.minute,
                entry_px=row.bar.close,
                stop_px=stop,
            )
    return None


def breakout_minute_at_or_above(
    bars: tuple[KbarBar, ...],
    pivot_price: float,
) -> str | None:
    """First tradeable 1m bar with high ≥ pivot (VCP 突破確認 · PIT intraday)."""
    if pivot_price <= 0:
        return None
    for row in _tradeable_bars(enrich_bars(bars)):
        if row.bar.high >= pivot_price:
            return row.bar.minute
    return None


def detect_pivot_retest_entry(
    bars: tuple[KbarBar, ...],
    pivot_price: float,
) -> EntryTrigger | None:
    """VCP pivot retest · 突破後回踩 pivot · 陽線收上 pivot。"""
    if pivot_price <= 0:
        return None
    enriched = _tradeable_bars(enrich_bars(bars))
    saw_breakout = False
    saw_pullback = False
    for row in enriched:
        if row.bar.high >= pivot_price:
            saw_breakout = True
        if not saw_breakout:
            continue
        if row.bar.low <= pivot_price or row.bar.close < pivot_price:
            saw_pullback = True
        if saw_pullback and _is_bullish(row.bar) and row.bar.close > pivot_price:
            stop = min(row.bar.low, pivot_price)
            return EntryTrigger(
                mode="pivot_retest",
                entry_minute=row.bar.minute,
                entry_px=row.bar.close,
                stop_px=stop,
            )
    return None


def detect_vwap_bounce_entry(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """Above VWAP all session · clean touch + next bullish candle."""
    enriched = _tradeable_bars(enrich_bars(bars))
    if len(enriched) < 2:
        return None
    for i in range(len(enriched) - 1):
        prior = enriched[: i + 1]
        touch = enriched[i]
        nxt = enriched[i + 1]
        if touch.vwap is None or nxt.vwap is None:
            continue
        if any(r.bar.close <= r.vwap for r in prior[:-1] if r.vwap is not None):
            continue
        if touch.bar.low > touch.vwap:
            continue
        if touch.bar.close <= touch.vwap:
            continue
        if _is_bullish(nxt.bar):
            stop = min(touch.bar.low, touch.vwap)
            return EntryTrigger(
                mode="vwap_bounce",
                entry_minute=nxt.bar.minute,
                entry_px=nxt.bar.close,
                stop_px=stop,
            )
    return None


DETECTORS: dict[ExpertEntryMode, object] = {
    "bone_zone": detect_bone_zone_entry,
    "vwap_reclaim": detect_vwap_reclaim_entry,
    "vwap_bounce": detect_vwap_bounce_entry,
}

EXPERT_ENTRY_LABELS: dict[ExpertEntryMode, str] = {
    "bone_zone": "Bone Zone 回踩 9–20 EMA · 陽線收上 9 EMA",
    "vwap_reclaim": "VWAP reclaim · 陽線收上 VWAP",
    "vwap_bounce": "VWAP bounce · 全日站上 · 觸線後下一根陽線",
    "pivot_retest": "Pivot retest · 突破後回踩 pivot · 陽線收上",
}


def detect_vcp_expert_entry_after_breakout(
    mode: ExpertEntryMode,
    bars: tuple[KbarBar, ...],
    pivot_price: float,
) -> EntryTrigger | None:
    """VCP 突破當日 · 先確認 high≥pivot，再於 09:05 起掃專家觸發。"""
    bmin = breakout_minute_at_or_above(bars, pivot_price)
    if bmin is None:
        return None
    if mode == "pivot_retest":
        return detect_pivot_retest_entry(bars, pivot_price)
    return detect_expert_entry_after(mode, bars, not_before_minute=bmin)


def detect_expert_entry(mode: ExpertEntryMode, bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    fn = DETECTORS.get(mode)
    if fn is None:
        return None
    return fn(bars)  # type: ignore[operator]


def detect_expert_entry_after(
    mode: ExpertEntryMode,
    bars: tuple[KbarBar, ...],
    *,
    not_before_minute: str,
    at_or_before_minute: str | None = None,
) -> EntryTrigger | None:
    """Rank confirm 後 · 僅在 [not_before, at_or_before] 窗內找第一個專家觸發。"""
    window = bars_from_minute(bars, not_before_minute)
    if at_or_before_minute is not None:
        window = bars_at_or_before(window, at_or_before_minute)
    if not window:
        return None
    trig = detect_expert_entry(mode, window)
    if trig is None:
        return None
    if not _minute_ge(trig.entry_minute, not_before_minute):
        return None
    if at_or_before_minute is not None and not _minute_ge(at_or_before_minute, trig.entry_minute):
        return None
    return trig
