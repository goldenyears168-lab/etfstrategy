"""台股報表與規則引擎用語（DB 存值與對外顯示）。"""

from __future__ import annotations

# --- 價位型態 entry_signal ---
ENTRY_BREAKOUT = "突破"
ENTRY_PULLBACK = "拉回"
ENTRY_WAIT = "觀望"
ENTRY_OVEREXTENDED = "乖離過大"
ENTRY_SKIP = "暫不進場"  # 風控覆寫（ETF 減碼）
ENTRY_TAG_VOLUME = "量價齊揚"
ENTRY_TAG_RETEST = "回測"

ENTRY_SIGNALS = frozenset(
    {
        ENTRY_BREAKOUT,
        ENTRY_PULLBACK,
        ENTRY_WAIT,
        ENTRY_OVEREXTENDED,
        ENTRY_SKIP,
    }
)
ENTRY_TAGS = frozenset({ENTRY_TAG_VOLUME, ENTRY_TAG_RETEST})

# --- 隔日等級 pm_bucket ---
PM_OBSERVE = "觀察"
PM_BREAKOUT = "突破"
PM_AVOID = "回避"

PM_BUCKETS = frozenset({PM_OBSERVE, PM_BREAKOUT, PM_AVOID})
PM_ALLOC_BUCKETS = frozenset({PM_OBSERVE, PM_BREAKOUT})
PM_BUCKET_ORDER = {PM_BREAKOUT: 0, PM_OBSERVE: 1, PM_AVOID: 2}

# --- 觀察名單 watchlist ---
WL_PRIMARY = "首要觀察"
WL_GENERAL = "一般觀察"
WL_CANDIDATE = "候選"
WL_EXCLUDED = "不列入"

WATCHLIST_ON_PM = frozenset({WL_PRIMARY, WL_GENERAL, WL_CANDIDATE})

TIER_BASE_WEIGHT: dict[str, float] = {
    WL_PRIMARY: 0.40,
    WL_GENERAL: 0.20,
    WL_CANDIDATE: 0.10,
    WL_EXCLUDED: 0.0,
}

# --- 籌碼標籤 chip_tag ---
CHIP_SYNC_BUY = "外資、投信同步買超"
CHIP_FOREIGN_BUY = "外資買超"
CHIP_NEUTRAL = "法人中性"
CHIP_FOREIGN_SELL_DIV = "外資賣超背離"
CHIP_DIVERGE = "籌碼背離"
CHIP_SYNC_SELL = "同步賣超"

HIGH_CHIP_RESONANCE_TAGS = frozenset({CHIP_SYNC_BUY, CHIP_FOREIGN_BUY})
EXCLUDE_CHIP_TAGS = frozenset({CHIP_FOREIGN_SELL_DIV})

CHIP_TAG_SCORE: dict[str, float] = {
    CHIP_SYNC_BUY: 92.0,
    CHIP_FOREIGN_BUY: 78.0,
    CHIP_DIVERGE: 62.0,
    CHIP_NEUTRAL: 55.0,
    CHIP_SYNC_SELL: 38.0,
    CHIP_FOREIGN_SELL_DIV: 22.0,
}

# --- 量能 ---
VOL_SURGE = "放量（≥2倍）"
VOL_UP = "量增"
VOL_DOWN = "量縮"
VOL_FLAT = "平量"

# --- 舊 enum（讀 DB / 測試相容）---
LEGACY_ENTRY_SIGNAL: dict[str, str] = {
    "BREAKOUT": ENTRY_BREAKOUT,
    "PULLBACK": ENTRY_PULLBACK,
    "WAIT": ENTRY_WAIT,
    "OVEREXTENDED": ENTRY_OVEREXTENDED,
    "SKIP_ENTRY": ENTRY_SKIP,
}
LEGACY_ENTRY_TAG: dict[str, str] = {
    "STRONG_TREND": ENTRY_TAG_VOLUME,
    "RETEST": ENTRY_TAG_RETEST,
}
LEGACY_PM_BUCKET: dict[str, str] = {
    "RESEARCH": PM_OBSERVE,
    "BREAKOUT": PM_BREAKOUT,
    "AVOID": PM_AVOID,
}
LEGACY_WATCHLIST: dict[str, str] = {
    "A": WL_PRIMARY,
    "B": WL_GENERAL,
    "CANDIDATE": WL_CANDIDATE,
    "SKIP": WL_EXCLUDED,
}
LEGACY_CHIP_TAG: dict[str, str] = {
    "三方共振": CHIP_SYNC_BUY,
    "外資確認": CHIP_FOREIGN_BUY,
    "中性": CHIP_NEUTRAL,
    "接刀警示": CHIP_FOREIGN_SELL_DIV,
    "背離加碼": CHIP_DIVERGE,
    "同步減碼": CHIP_SYNC_SELL,
}


def normalize_entry_signal(value: str) -> str:
    return LEGACY_ENTRY_SIGNAL.get(value, value)


def normalize_entry_tag(value: str) -> str:
    return LEGACY_ENTRY_TAG.get(value, value)


def normalize_pm_bucket(value: str) -> str:
    return LEGACY_PM_BUCKET.get(value, value)


def normalize_watchlist(value: str) -> str:
    return LEGACY_WATCHLIST.get(value, value)


def normalize_chip_tag(value: str) -> str:
    return LEGACY_CHIP_TAG.get(value, value)


def format_entry_display(signal: str, tags: tuple[str, ...]) -> str:
    sig = normalize_entry_signal(signal)
    if not tags:
        return sig
    parts = [normalize_entry_tag(t) for t in tags]
    return sig + "＋" + "＋".join(parts)
