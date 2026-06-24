"""Stock daily lens · user-facing copy SSOT（繁中 · 台灣用語）。

對照總表：docs/terminology.md §10.2
"""

from __future__ import annotations

# Layer 1 區塊標題 · banner · email subject 基底（勿加 Lens · 池）
SECTION_TITLE_ZH = "今日亮點"

# 統計 chip
CHIP_WATCHLIST_ZH = "監控清單"  # 後接「N 檔」→ format_watchlist_count_zh
CHIP_NEW_OBSERVATION_ZH = "新進觀察"
CHIP_CONVERGENCE_ZH = "四框架收斂"

# 篩選 tab
TAB_DELTA_ZH = "今日異動"
TAB_ALL_ZH = "全部"
TAB_WATCH_ZH = "持續關注"

# 排序
SORT_DELTA_FIRST_ZH = "變化優先"
SORT_CONVERGENCE_ZH = "收斂程度"
SORT_SCORE_ZH = "參考分"
SORT_SCORE_TOOLTIP_ZH = "系統內部排序分數，不代表買賣建議。"

# 說明文
PIT_FOOTNOTE_ZH = "只用當日及以前資料，事後不改寫過去紀錄。"
CONVERGENCE_TOOLTIP_ZH = "ETF 加碼、大盤環境、類股輪動、VCP 四項符合幾項。"
LENS_SUBTITLE_ZH = "和昨天比：ETF 持股、大盤強度、類股輪動、VCP 篩選條件有哪些變化。"
RRG_MIGRATION_LABELS_ZH = {
    "improving_to_leading": "轉強→領先",
    "leading_to_weakening": "領先→轉弱",
    "lagging_to_improving": "落後→轉強",
    "weakening_to_lagging": "轉弱→落後",
}
RRG_FRESH_ZH = "fresh"
RRG_FRESH_SIGNAL_ZH = "fresh 訊號"
PIVOT_DISTANCE_ZH = "距突破價"
RRG_RANK_LABEL_ZH = "排行"

BADGE_PLAIN_ZH: dict[str, str] = {
    "new_observation": "新進觀察",
    "consensus_add": "多檔 ETF 持續加碼",
    "consensus_delta": "多檔 ETF 同步加碼",
    "copytrade": "ETF 持股異動訊號",
    "signal": "策略訊號",
    "watch": "仍值得追蹤，尚未失效",
    "rrg_fresh": "RRG fresh 訊號",
}

RRG_QUADRANT_CHANGE_PLAIN_ZH: dict[str, str] = {
    "improving→leading": "相對輪動由轉強進入領先象限",
    "leading→improving": "由領先回到轉強，仍在強勢帶附近",
    "lagging→improving": "由落後轉為轉強",
    "weakening→lagging": "由轉弱進入落後",
    "leading→weakening": "由領先轉為轉弱",
    "improving→weakening": "由轉強轉為轉弱",
}


def badge_plain_zh(key: str, label_zh: str) -> str:
    if key in BADGE_PLAIN_ZH:
        return BADGE_PLAIN_ZH[key]
    lower = label_zh.lower()
    for pattern, plain in RRG_QUADRANT_CHANGE_PLAIN_ZH.items():
        if pattern.lower() in lower:
            return plain
    if "共識加碼" in label_zh:
        return "多檔 ETF 持續加碼"
    return label_zh


def format_rrg_rank_zh(rank: int | None, total: int | None) -> str | None:
    if total is None or total <= 0:
        return None
    if rank is None or rank <= 0:
        return f"—/{total}"
    return f"{rank}/{total}"

# 空狀態
EMPTY_TITLE_ZH = "今日無結構變化"
EMPTY_BODY_ZH = (
    "相較昨日，監控清單內尚無新異動。"
    "可切換「全部」查看完整清單。"
)
EMPTY_CTA_ZH = "查看完整清單"
EMPTY_EMAIL_LIST_ZH = "（今日無可列之異動標的）"

# 收合區塊 · 閱讀動線（台灣用語）
COLLAPSE_HINT_ZH = "預設收合，點開查看更多"
ADVANCED_READING_ZH = "進階閱讀"
SCAN_SYMBOL_TAGS_HINT_ZH = "先看代號與標籤"
SCAN_SYMBOL_ACTION_HINT_ZH = "先看代號與動作"
SCAN_SYMBOL_COMPOSITE_HINT_ZH = "先看代號與 Composite 分數"
REGIME_CONTEXT_SUMMARY_ZH = "市場環境摘要"
OPEN_TODAY_BRIEF_ZH = "開啟今日日報"
OPEN_FULL_BRIEF_ZH = "開啟完整日報"
STRATEGY_SPEC_DETAILS_ZH = "策略規格與產出資訊"
MORE_TO_DAILY_BRIEF_ZH = "其餘標的請至日報查看"
MORE_TO_DAILY_BRIEF_ZH = "其餘標的請至日報查看"
SESSION_DATE_LABEL_ZH = "場次日"
WATCHLIST_SOURCES_LABEL_ZH = "監控清單"
BRIEF_LIST_INTRO_ZH = (
    "選日期進完整日報；每列附當日市場環境摘要與今日亮點標題。"
)
ETF_SCAN_HINT_ZH = f"{SCAN_SYMBOL_ACTION_HINT_ZH}；多檔 ETF 時其餘區塊預設收合。"
VCP_SCAN_HINT_ZH = f"{SCAN_SYMBOL_COMPOSITE_HINT_ZH}，再展開規則細節。"
LENS_SCAN_HINT_ZH = (
    f"{SCAN_SYMBOL_TAGS_HINT_ZH}，點開再看圖表與四項符合細節（ETF、環境、輪動、VCP）；不必每檔都展開。"
)
RRG_SCAN_HINT_ZH = f"先看 {RRG_FRESH_ZH} 列表；全部候選與輪動圖在下方可展開。"
COPYTRADE_SCAN_HINT_ZH = "先看下方今日異動表；異動檔數＝今日新進或加碼持股檔數。"
RRG_ALL_CANDIDATES_TITLE_ZH = "全部候選"
STRATEGY_SPEC_HINT_ZH = f"執行規則 · 資料日（{COLLAPSE_HINT_ZH}）"
OPEN_TODAY_SCREEN_ZH = "開啟今日日報篩選"
OPS_SCHEDULE_COLLAPSE_ZH = f"排程與資料來源（{COLLAPSE_HINT_ZH}）"

# headline 句型（email · banner · lens_daily_alert.headline_zh）
HEADLINE_NO_CHANGE_ZH = "今日無結構變化"


def format_watchlist_count_zh(count: int) -> str:
    """規模 chip · 例：監控清單 153 檔"""
    return f"{CHIP_WATCHLIST_ZH} {count} 檔"


def format_headline_zh(
    trade_date: str,
    *,
    fire_count: int = 0,
    delta_new_count: int = 0,
) -> str:
    """一句話收盤摘要 · 台灣慣用句型（trade_date 供 DB；headline 不重複日期）。"""
    _ = trade_date
    parts: list[str] = []
    if fire_count:
        parts.append(f"{fire_count} 檔四框架收斂")
    if delta_new_count:
        parts.append(f"{delta_new_count} 檔新進觀察")
    if not parts:
        return f"{SECTION_TITLE_ZH}：{HEADLINE_NO_CHANGE_ZH}"
    return f"{SECTION_TITLE_ZH}：" + " · ".join(parts)
