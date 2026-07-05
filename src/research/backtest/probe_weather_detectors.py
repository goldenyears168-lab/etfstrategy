"""Weather probe detectors · 低門檻寬觸發 · 只供 market-probe-radar 量測當日市場天氣。

與 expert_intraday_standalone 策略級檢測器分離：無量能濾網、無 pivot、無陽線確認。
"""

from __future__ import annotations

from typing import Literal

from research.backtest.rrg_mono_expert_entry import (
    EMA_SLOW,
    NO_TRADE_BEFORE,
    EntryTrigger,
    _in_bone_zone,
    _minute_ge,
    _tradeable_bars,
    enrich_bars,
)
from stock_db.kbar import KbarBar

ORB_START = "09:05"
ORB_END = "09:15"

ProbeWeatherMode = Literal[
    "orb_probe",
    "vwap_reclaim_probe",
    "vwap_bounce_probe",
    "bone_zone_probe",
    "session_momentum_probe",
]

PROBE_WEATHER_MODES: tuple[ProbeWeatherMode, ...] = (
    "orb_probe",
    "vwap_reclaim_probe",
    "vwap_bounce_probe",
    "bone_zone_probe",
    "session_momentum_probe",
)

SESSION_PROBE_MINUTE = "09:35"


def detect_orb_probe(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """OR 高點突破 · 無量能 / 陽線濾網。"""
    enriched = enrich_bars(bars)
    or_rows = [
        r
        for r in enriched
        if _minute_ge(r.bar.minute, ORB_START) and r.bar.minute <= f"{ORB_END}:00"
    ]
    if len(or_rows) < 2:
        return None
    or_high = max(r.bar.high for r in or_rows)
    or_low = min(r.bar.low for r in or_rows)
    after_or = [
        r
        for r in enriched
        if _minute_ge(r.bar.minute, NO_TRADE_BEFORE) and r.bar.minute > f"{ORB_END}:00"
    ]
    for row in after_or:
        if row.bar.close > or_high:
            return EntryTrigger(
                mode="orb_probe",
                entry_minute=row.bar.minute,
                entry_px=row.bar.close,
                stop_px=or_low,
            )
    return None


def detect_vwap_reclaim_probe(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """曾低於 VWAP · 首根收盤站上 VWAP（不要求陽線）。"""
    enriched = _tradeable_bars(enrich_bars(bars))
    was_below = False
    for row in enriched:
        if row.vwap is None:
            continue
        if row.bar.close < row.vwap:
            was_below = True
        if was_below and row.bar.close > row.vwap:
            return EntryTrigger(
                mode="vwap_reclaim_probe",
                entry_minute=row.bar.minute,
                entry_px=row.bar.close,
                stop_px=min(row.bar.low, row.vwap),
            )
    return None


def detect_vwap_bounce_probe(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """觸及 VWAP · 下一根收上 VWAP（不要求全日站上）。"""
    enriched = _tradeable_bars(enrich_bars(bars))
    if len(enriched) < 2:
        return None
    for i in range(len(enriched) - 1):
        touch = enriched[i]
        nxt = enriched[i + 1]
        if touch.vwap is None or nxt.vwap is None:
            continue
        if touch.bar.low > touch.vwap:
            continue
        if nxt.bar.close > nxt.vwap:
            return EntryTrigger(
                mode="vwap_bounce_probe",
                entry_minute=nxt.bar.minute,
                entry_px=nxt.bar.close,
                stop_px=min(touch.bar.low, touch.vwap),
            )
    return None


def detect_bone_zone_probe(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """回踩 9/20 帶 · 收盤站上 9 EMA（不要求陽線）。"""
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
        if in_pullback and row.bar.close > row.ema9:
            return EntryTrigger(
                mode="bone_zone_probe",
                entry_minute=row.bar.minute,
                entry_px=row.bar.close,
                stop_px=min(row.bar.low, row.ema20),
            )
    return None


def detect_session_momentum_probe(bars: tuple[KbarBar, ...]) -> EntryTrigger | None:
    """09:35 固定錨點 · 每檔有 K 線即一筆 · 量測早盤→收盤廣度（高 n）。"""
    enriched = _tradeable_bars(enrich_bars(bars))
    for row in enriched:
        if not _minute_ge(row.bar.minute, SESSION_PROBE_MINUTE):
            continue
        return EntryTrigger(
            mode="session_momentum_probe",
            entry_minute=row.bar.minute,
            entry_px=row.bar.close,
            stop_px=row.bar.low,
        )
    return None


_PROBE_DETECTORS = {
    "orb_probe": detect_orb_probe,
    "vwap_reclaim_probe": detect_vwap_reclaim_probe,
    "vwap_bounce_probe": detect_vwap_bounce_probe,
    "bone_zone_probe": detect_bone_zone_probe,
    "session_momentum_probe": detect_session_momentum_probe,
}


def detect_probe_weather_entry(
    mode: str,
    bars: tuple[KbarBar, ...],
) -> EntryTrigger | None:
    fn = _PROBE_DETECTORS.get(mode)
    if fn is None:
        return None
    return fn(bars)
