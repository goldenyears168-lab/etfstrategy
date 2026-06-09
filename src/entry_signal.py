"""價位型態標籤（規則 · 讀技術快照，不打 API）。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from market_labels import (
    ENTRY_BREAKOUT,
    ENTRY_OVEREXTENDED,
    ENTRY_PULLBACK,
    ENTRY_SIGNALS,
    ENTRY_SKIP,
    ENTRY_TAG_VOLUME,
    ENTRY_TAGS,
    ENTRY_WAIT,
    VOL_DOWN,
    VOL_SURGE,
    VOL_UP,
    format_entry_display,
)
from stock_context import TechnicalSnapshot

OVEREXTENDED_MA_PCT = 18.0
BREAKOUT_POS_52W = 90.0
BREAKOUT_DIST_HIGH_PCT = -3.0
PULLBACK_MA20_BAND_PCT = 5.0
PULLBACK_MAX_POS_52W = 85.0

STRONG_TREND_FLOW_MIN = 65.0
STRONG_TREND_CHIP_MIN = 70.0


@dataclass(frozen=True)
class EntryContext:
    signal: str
    tags: tuple[str, ...]

    @property
    def display(self) -> str:
        return format_entry_display(self.signal, self.tags)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def extension_pct(tech: TechnicalSnapshot | None) -> float | None:
    if tech is None:
        return None
    vals = [v for v in (tech.dist_ma20_pct, tech.dist_ma60_pct) if v is not None]
    return max(vals) if vals else None


def percentile_value(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return OVEREXTENDED_MA_PCT
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def overextended_min_pct(extensions: list[float]) -> float:
    """Universe 內相對分位 + 絕對下限。"""
    abs_min = _env_float("ENTRY_OVEREXTENDED_ABS_MIN", 12.0)
    rel_pct = _env_float("ENTRY_OVEREXTENDED_REL_PCT", 75.0)
    if not extensions:
        return max(abs_min, OVEREXTENDED_MA_PCT)
    pval = percentile_value(sorted(extensions), rel_pct)
    return max(abs_min, pval)


def classify_entry_signal(
    tech: TechnicalSnapshot | None,
    *,
    net_side: str | None = None,
    overextended_min: float | None = None,
) -> str:
    return classify_entry_context(
        tech,
        net_side=net_side,
        overextended_min=overextended_min,
    ).signal


def _is_extended(
    tech: TechnicalSnapshot,
    *,
    overextended_min: float,
) -> bool:
    ext = extension_pct(tech)
    return ext is not None and ext >= overextended_min


def has_strong_trend(
    tech: TechnicalSnapshot | None,
    *,
    flow_score: float | None = None,
    chip_score: float | None = None,
    overextended_min: float | None = None,
) -> bool:
    """主升段：乖離大但資金+籌碼強，且量未縮。"""
    if tech is None or flow_score is None or chip_score is None:
        return False
    if flow_score < STRONG_TREND_FLOW_MIN or chip_score < STRONG_TREND_CHIP_MIN:
        return False
    thresh = overextended_min if overextended_min is not None else OVEREXTENDED_MA_PCT
    if not _is_extended(tech, overextended_min=thresh):
        return False
    if tech.vol_label == VOL_DOWN:
        return False
    if tech.vol_label in (VOL_SURGE, VOL_UP):
        return True
    if tech.position_52w_pct is not None and tech.position_52w_pct >= 85.0:
        return True
    return False


def classify_entry_context(
    tech: TechnicalSnapshot | None,
    *,
    net_side: str | None = None,
    flow_score: float | None = None,
    chip_score: float | None = None,
    overextended_min: float | None = None,
) -> EntryContext:
    """依 52 週位、乖離、ETF 淨方向分類；強勢延伸加量價齊揚標籤。"""
    if net_side == "reduce":
        return EntryContext(ENTRY_SKIP, ())
    if tech is None:
        return EntryContext(ENTRY_WAIT, ())
    ext_thresh = (
        overextended_min if overextended_min is not None else OVEREXTENDED_MA_PCT
    )
    if _is_extended(tech, overextended_min=ext_thresh):
        signal = ENTRY_OVEREXTENDED
    else:
        signal = ENTRY_WAIT
        pos = tech.position_52w_pct
        dist_hi = tech.dist_from_52w_high_pct
        if (
            pos is not None
            and pos > BREAKOUT_POS_52W
            and dist_hi is not None
            and dist_hi > BREAKOUT_DIST_HIGH_PCT
        ):
            signal = ENTRY_BREAKOUT
        elif (
            tech.dist_ma20_pct is not None
            and abs(tech.dist_ma20_pct) <= PULLBACK_MA20_BAND_PCT
            and (pos is None or pos < PULLBACK_MAX_POS_52W)
        ):
            signal = ENTRY_PULLBACK
    tags: list[str] = []
    if signal == ENTRY_OVEREXTENDED and has_strong_trend(
        tech,
        flow_score=flow_score,
        chip_score=chip_score,
        overextended_min=ext_thresh,
    ):
        tags.append(ENTRY_TAG_VOLUME)
    return EntryContext(signal, tuple(tags))


def classify_entry_context_batch(
    items: list[tuple[str, TechnicalSnapshot | None, str | None, float | None, float | None]],
) -> dict[str, EntryContext]:
    """Research Universe 橫截面：相對乖離過大門檻。"""
    extensions: list[float] = []
    for _sid, tech, _net, _flow, _chip in items:
        if tech is None:
            continue
        ext = extension_pct(tech)
        if ext is not None:
            extensions.append(ext)
    thresh = overextended_min_pct(extensions)
    return {
        sid: classify_entry_context(
            tech,
            net_side=net_side,
            flow_score=flow,
            chip_score=chip,
            overextended_min=thresh,
        )
        for sid, tech, net_side, flow, chip in items
    }


def is_overextended_without_strong_trend(ctx: EntryContext) -> bool:
    return ctx.signal == ENTRY_OVEREXTENDED and ENTRY_TAG_VOLUME not in ctx.tags
