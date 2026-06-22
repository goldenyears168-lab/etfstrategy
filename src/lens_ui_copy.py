"""Stock daily lens · user-facing copy SSOT（繁中 · 台灣用語）。

對照總表：docs/terminology.md §10.2
"""

from __future__ import annotations

# Layer 1 區塊標題 · banner · email subject 基底（勿加 Lens · 池）
SECTION_TITLE_ZH = "今日亮點"

# 統計 chip
CHIP_WATCHLIST_ZH = "清單內"  # 後接「N 檔」
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

# 空狀態
EMPTY_TITLE_ZH = "今日無結構變化"
EMPTY_BODY_ZH = (
    "相較昨日，監控清單內尚無新異動。"
    "可切換「全部」查看完整清單。"
)
EMPTY_CTA_ZH = "查看完整清單"
EMPTY_EMAIL_LIST_ZH = "（今日無可列之異動標的）"

# headline 句型（email · banner · lens_daily_alert.headline_zh）
HEADLINE_NO_CHANGE_ZH = "今日無結構變化"


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
