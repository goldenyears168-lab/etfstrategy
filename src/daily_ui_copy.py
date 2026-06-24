"""日報頁靜態殼層文案 SSOT · 同步至 readdy-490731/src/lib/uiCopy.generated.ts"""

from __future__ import annotations

import re

BRIEF_HERO_SUBTITLE_ZH = (
    "把今天市場強弱、值得注意的股票變化，以及各策略有沒有跑出候選股，"
    "整理在同一頁，讓你快速看完今天發生了什麼。"
)
BRIEF_HERO_FIRST_VISIT_ZH = (
    "第一次來看日報，建議先看「今日亮點」和「一句話市場摘要」，"
    "再往下看各個指標與策略區塊。"
)

BRIEF_TOC_NAV_LABEL_ZH = "目錄"
BRIEF_TOC_GUIDE_TITLE_ZH = "今天的日報要怎麼看？"
BRIEF_TOC_GUIDE_BODY_ZH = "如果你只想花 1～2 分鐘抓重點，可以照這個順序看："
BRIEF_TOC_READING_STEPS: list[dict[str, str]] = [
    {"label": "今日亮點", "hint": "先看今天有哪些股票或結構出現明顯變化。"},
    {"label": "一句話市場摘要", "hint": "快速判斷今天市場偏強、偏弱，還是偏熱。"},
    {"label": "策略篩選", "hint": "確認今天各套策略有沒有跑出新的候選股。"},
]

BRIEF_TOC_GROUP_LABELS: dict[str, str] = {
    "今日閱讀": "先看這裡",
    "策略篩選": "策略篩選",
    "事實層": "客觀事實",
}

BRIEF_REGIME_TOC_LABELS: dict[str, str] = {
    "synopsis": "一句話市場摘要",
    "breadth": "市場有多強？",
    "trend": "大盤趨勢階段",
    "rrg": "強勢族群輪動",
    "stage2": "偏強股參與率",
}

BRIEF_LENS_TITLE_ZH = "今日亮點：今天最值得注意的變化"
BRIEF_LENS_BODY_ZH = (
    "這裡只列出和昨天相比有明顯變化的股票或結構，"
    "讓你先抓出「今天值得多看一眼」的標的。"
    "不一定是進出場訊號，而是提醒你這裡有變化。"
)
BRIEF_LENS_EMPTY_BODY_ZH = (
    "今天沒有新增明顯的市場結構變化，"
    "可以搭配市場環境與策略區塊，專注在整體強弱判讀即可。"
)
BRIEF_LENS_SCAN_HINT_ZH = (
    "先看代號與狀態標籤，點開卡片再看圖表與四項符合細節（ETF、環境、輪動、VCP）；不必每檔都展開。"
)

BRIEF_REGIME_TITLE_ZH = "今天的大盤環境怎麼看？"
BRIEF_REGIME_BODY_ZH = (
    "這一區不是直接告訴你買哪一檔，而是先回答："
    "「今天這個市場，適不適合積極操作？」"
    "我們用幾個常見的指標，從不同角度描述整體環境。"
)
BRIEF_SYNOPSIS_TITLE_ZH = "一句話看今天市場"
BRIEF_SYNOPSIS_PLAIN_LABEL_ZH = "白話版摘要"
BRIEF_SYNOPSIS_TECH_LABEL_ZH = "技術版摘要"
BRIEF_REGIME_READING_HINT_ZH = (
    "先看上方一句話摘要與四項指標，再依序展開各軸圖表。"
    "細節區塊預設收合，需要時再點開。"
)

BRIEF_REGIME_SECTION: dict[str, dict[str, str]] = {
    "breadth": {
        "title": "有多少股票一起變強？",
        "annotation": (
            "Market breadth（市場廣度）看有多少股票一起站上重要均線；"
            "廣度高代表行情不是少數權值股在撐。"
        ),
    },
    "trend": {
        "title": "大盤目前還在多頭階段嗎？",
        "annotation": (
            "Weinstein Stage Analysis（威斯坦階段）用四階段描述大盤是築底、走多、築頂還是下跌。"
        ),
    },
    "rrg": {
        "title": "現在強勢族群多不多？",
        "annotation": (
            "Relative Rotation Graph（RRG）看族群落在領先、轉強、轉弱、落後哪個象限。"
        ),
    },
    "stage2": {
        "title": "現在符合偏強條件的股票多嗎？",
        "annotation": (
            "Stage 2 participation（第 2 階段參與率）看有多少股票符合 Minervini 中期強勢條件。"
        ),
    },
}

BRIEF_RRG_QUADRANT_PLAIN: dict[str, str] = {
    "leading": "已經在強勢區",
    "improving": "正在轉強",
    "weakening": "強勢正在降溫",
    "lagging": "目前相對落後",
}

BRIEF_WEINSTEIN_FIELD_LABELS: dict[str, str] = {
    "30-week MA slope": "長期均線方向",
    "偏離 30w MA": "與長期均線距離",
    "Higher lows": "低點仍持續墊高",
}

BRIEF_ETF_TITLE_ZH = "ETF 今天有沒有調整持股？"
BRIEF_ETF_BODY_ZH = (
    "本區資料來自各檔 ETF 公開持股檔案，記錄前後期成分變化，"
    "是純客觀的「誰加碼、誰減碼、誰被移出」紀錄。"
)
BRIEF_ETF_FACTS_NOTE_ZH = (
    "加碼＝持有股數比前一期增加；減碼＝減少；出清＝持倉從有變成 0。"
    "這裡是事實資料整理，不代表買賣建議。"
)

BRIEF_RRG_INTRADAY_TITLE_ZH = "盤中先看：哪些股票可能正在轉強？"
BRIEF_RRG_INTRADAY_BODY_ZH = (
    "盤中預估快照；收盤後數值可能翻轉，最終以收盤掃描為準。"
)
BRIEF_RRG_DAILY_TITLE_ZH = "收盤後確認：RRG 單軌今天有沒有新候選？"
BRIEF_RRG_DAILY_BODY_ZH = (
    "收盤後正式掃描版，依照單軌濾網與 fresh 軌跡規則確認候選。"
)

BRIEF_VCP_TITLE_ZH = "VCP 觀察名單：誰接近突破？"
BRIEF_VCP_BODY_ZH = (
    "整理接近 VCP 條件的股票，幫你看哪些標的正在靠近關鍵突破價位。"
)
BRIEF_VCP_RESEARCH_BODY_ZH = (
    "VCP 漏斗研究對照，觀察哪些股票正在靠近 VCP 條件的「門口」。"
)

BRIEF_VCP_HEADER_PLAIN: dict[str, str] = {
    "composite": "綜合分數",
    "state": "目前階段",
    "pivot": "關鍵突破價",
    "dist%": "距離突破",
    "stop": "風險參考價",
    "symbol": "代號",
    "name": "名稱",
}

BRIEF_VCP_STATE_PLAIN: dict[str, str] = {
    "Early": "早期整理",
    "Pre-breakout": "接近突破",
    "Pre": "接近突破",
    "Breakout": "已確認突破",
    "breakout_close": "已收盤確認突破",
}


def format_rrg_empty_zh(is_intraday: bool) -> str:
    if is_intraday:
        return "盤中預估目前沒有符合 fresh 條件的候選股票。"
    return "今天收盤後，沒有股票符合 RRG 單軌的候選條件。"


def format_rrg_signal_count_zh(n: int, is_intraday: bool) -> str:
    if is_intraday:
        return f"盤中預估顯示 {n} 檔符合「新鮮軌跡」條件，適合作為今天盤中觀察重點之一。"
    return f"今天共有 {n} 檔符合單軌條件，可進一步查看軌跡路徑與位置變化。"


def plain_vcp_header(header: str) -> str:
    key = header.strip().lower()
    return BRIEF_VCP_HEADER_PLAIN.get(key) or BRIEF_VCP_HEADER_PLAIN.get(header) or header


def plain_vcp_state(state: str) -> str:
    for pattern, plain in BRIEF_VCP_STATE_PLAIN.items():
        if pattern in state:
            return plain
    return state
