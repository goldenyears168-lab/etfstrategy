"""Regime daily brief · 作者用語優先之導讀與章節標題（讀者面向）。"""

from __future__ import annotations

# 專案產品名僅在報告開頭出現一次
PRODUCT_LAYER_ONCE = (
    "專案層：**Regime four-axis diagnostic（四軸體制診斷）** · "
    "本 memo 整合下列作者方法之每日快照；非 live gate、非 alpha、非策略績效。"
)

BRIEF_TITLE_PREFIX = "Market structure memo"
BRIEF_TITLE_ZH = "市場結構日報"
BRIEF_LEAD = "Weinstein · Minervini · Kempenaer RRG · Zweig / Deemer breadth"

SEC_SYNOPSIS = "Daily synopsis（每日摘要）"
SEC_BREADTH = "1 · Breadth axis · Level / Rhythm / Impulse"
SEC_BREADTH_LEVEL = "1A · Breadth level · % Above MA"
SEC_BREADTH_RHYTHM = "1B · Zweig EMA rhythm tier"
SEC_BREADTH_IMPULSE = "1C · Breadth impulse · Zweig thrust / Deemer BAM"
SEC_TREND = "2 · Weinstein Stage Analysis · weekly"
SEC_RRG = "3 · Relative Rotation Graphs · Kempenaer RRG"
SEC_MINERVINI_UNIVERSE = "4 · Minervini Trend Template · universe pass rate"

STAGE_ZH: dict[str, str] = {
    "basing": "築底",
    "advancing": "上升",
    "topping": "築頂",
    "declining": "下跌",
    "unknown": "未知",
}

# Kempenaer / StockCharts 象限英文名
QUADRANT_LABEL: dict[str, str] = {
    "leading": "Leading",
    "improving": "Improving",
    "weakening": "Weakening",
    "lagging": "Lagging",
}

TAIL_DIR_ZH: dict[str, str] = {
    "↗ up-right": "↗ up-right（相對走強）",
    "→ up-left": "→ up-left（動量轉弱）",
    "↑ down-left": "↑ down-left（相對改善）",
    "↙ down-left": "↙ down-left（相對走弱）",
    "—": "—",
}

MINERVINI_ROWS: tuple[tuple[str, str, str], ...] = (
    ("c1_price_above_sma150_200", "Price > SMA150 & SMA200", "收盤價高於 150 日與 200 日均線"),
    ("c2_sma150_above_sma200", "SMA150 > SMA200", "150 日均線在 200 日之上"),
    ("c3_sma200_trending_up", "SMA200 trending up (22d)", "200 日均線近 22 日向上"),
    ("c4_sma50_above_sma150_200", "SMA50 > SMA150 & SMA200", "50 日均線高於 150 與 200 日"),
    ("c5_price_above_sma50", "Price > SMA50", "收盤價高於 50 日均線"),
    ("c6_30pct_above_52w_low", "≥30% above 52w low", "距 52 週低點至少漲 30%"),
    ("c7_within_25pct_52w_high", "Within 25% of 52w high", "距 52 週高點不超過 25%"),
    ("c8_rs_rank_above_70", "RS rank > 70", "RS 排名 > 70（指數端略過）"),
)

GUIDE_HEADER = (
    "本 memo 依四條作者／業界標準路徑描述台股加權指數與研究樣本："
    "\n\n"
    "1. **% above MA**（Deemer 系統計）— 50 日／200 日均線上方股票占比"
    "\n2. **Weinstein Stage Analysis** — 週線 Stage 1–4 與 30-week MA"
    "\n3. **Relative Rotation Graphs**（Kempenaer RRG）— RS-Ratio × RS-Momentum 四象限"
    "\n4. **Minervini Trend Template** — universe 內個股八項模板通過率"
    "\n\n"
    "建議：先看 **Daily synopsis** → 逐章讀 **Notes** → 圖表請開 "
    "[`daily_brief.html`](daily_brief.html)（Markdown 預覽不顯示 SVG）。"
)

GUIDE_SYNOPSIS = (
    "**Daily synopsis** 將四路徑各一句合成摘要。"
    "若 % above 200-day MA 偏高但 Minervini pass rate 偏低，常見於指數由少數大型股拉抬。"
)

GUIDE_BREADTH = (
    "**Breadth axis** 分三層（皆為 Regime 診斷 · 非 Strategy overlay）："
    "\n\n"
    "**1A Level · % above MA**（Deemer 系統計）：50-day / 200-day MA 上方占比。"
    "\n\n"
    "**1B Rhythm · Zweig EMA rhythm tier**：adv/decl ratio 的 10-day EMA 分 tier（off / low / mid / high）。"
    "描述市場參與**節奏**，有別於 200MA **水位**。"
    "\n\n"
    "**1C Impulse · Zweig Breadth Thrust / Deemer BAM**：事件層 thrust 與 breakaway momentum。"
    "\n\n"
    "**50 vs 200 spread：** 50-day 廣度減 200-day 廣度。"
    "**Advance/decline divergence：** 指數近 20 日向上而 50-day 廣度走弱。"
)

GUIDE_BREADTH_LEVEL = (
    "**Level** 讀 200-day MA 五區間（oversold → overbought）。"
    "圖表上：加權指數 + 50MA／200MA 廣度 %；背景色為 200-day 分區間。"
)

GUIDE_BREADTH_RHYTHM = (
    "**Zweig EMA rhythm tier**（Zweig / Deemer 廣度傳統）："
    "adv/decl 日線 ratio 的 10-day EMA，依 tier 閾值分 off / low / mid / high。"
    "Research validation 顯示 rhythm tier 具統計增量；Regime 僅報讀 tier，**不含 exposure 仓位**。"
)

GUIDE_BREADTH_IMPULSE = (
    "**Impulse** 偵測 thrust 事件：Zweig 以 adv/decl EMA 穿越偵測 Breadth Thrust；"
    "Deemer 以 10-day adv/decl ratio 偵測 BAM。"
    "Thrust 窗口 active 表示近期曾觸發 thrust／BAM，仍在 hold 期內。"
)

GUIDE_TREND = (
    "**Weinstein Stage Analysis（1988）** 以 **週線** 加權指數判 Stage："
    "1 basing → 2 advancing → 3 topping → 4 declining；基準為 **30-week MA**。"
    "圖底 **Stage ribbon** 為週線 Stage 著色（紫 S1、綠 S2、橙 S3、紅 S4）。"
    "\n\n"
    "**Minervini Trend Template（2013）** 八條日線規則檢驗指數是否處 Stage 2 型上升結構。"
)

GUIDE_RRG = (
    "**Relative Rotation Graphs**（Julius de Kempenaer · StockCharts 實作）："
    "個股相對 benchmark 畫在 RS-Ratio（JdK）× RS-Momentum 平面。"
    "\n\n"
    "- **Leading**：相對強、動量強"
    "\n- **Improving**：相對弱、動量轉強"
    "\n- **Weakening**：相對強、動量轉弱"
    "\n- **Lagging**：相對弱、動量弱"
    "\n\n"
    "**Symbol table** 依象限排序（StockCharts 慣例）；**tail** 為近 4 交易日軌跡。"
    "**Quadrant migration** 為 1 日跨象限檔數。"
)

GUIDE_MINERVINI_UNIVERSE = (
    "**Minervini Trend Template · universe pass rate**："
    "對樣本個股逐日檢查八項模板，≥7/8 計入（RS 項略過）。"
    "可與 % above 200-day MA 對照：廣度高且 pass rate 高 → 廣泛 Stage 2 參與；"
    "廣度高但 pass rate 低 → 可能少數 leadership 拉指數。"
    "\n\n"
    "**圖表：** 近 90 日每日 pass rate。"
)


def stage_display(stage: int | None, name: str | None) -> str:
    n = name or "unknown"
    zh = STAGE_ZH.get(n, n)
    if stage:
        return f"Stage {stage} · {n}（{zh}）"
    return n


def tail_display(label: str | None) -> str:
    if not label:
        return "—"
    return TAIL_DIR_ZH.get(label, label)


def quadrant_display(q: str) -> str:
    return QUADRANT_LABEL.get(q, q)
