#!/usr/bin/env python3
"""多頁教學 PDF：下單層流程圖（flowchart）走讀與健康判讀。"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "reports" / "order" / f"{date.today().strftime('%Y%m%d')}_order_layer_teach_deck.pdf"

C_BG = "#f7f5f1"
C_INK = "#1c1917"
C_MUTED = "#57534e"
C_ACCENT = "#0f766e"
C_SOFT = "#ccfbf1"
C_WARN = "#9a3412"
C_WARN_BG = "#ffedd5"
C_CARD = "#ffffff"
C_LINE = "#d6d3d1"
C_BLUE = "#1d4ed8"
C_BLUE_BG = "#dbeafe"
C_PINK = "#9d174d"
C_PINK_BG = "#fce7f3"
C_GREEN = "#166534"
C_GREEN_BG = "#dcfce7"


def _setup_font() -> None:
    plt.rcParams["font.sans-serif"] = [
        "PingFang TC",
        "Heiti TC",
        "Arial Unicode MS",
        "Noto Sans CJK TC",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False


def _new_page(fig_size=(11.0, 8.5)):
    fig, ax = plt.subplots(figsize=fig_size)
    fig.patch.set_facecolor(C_BG)
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 8.5)
    ax.axis("off")
    ax.add_patch(Rectangle((0, 7.85), 11, 0.65, facecolor="#e7e5e4", edgecolor="none", zorder=0))
    return fig, ax


def _footer(ax, page: int, total: int) -> None:
    ax.text(
        0.45,
        0.28,
        "下單層流程圖教學 · 對齊 reports/order/*_order_layer_flowchart.pdf · 非投資建議",
        fontsize=7.2,
        color=C_MUTED,
        ha="left",
        va="center",
    )
    ax.text(10.55, 0.28, f"{page} / {total}", fontsize=8, color=C_MUTED, ha="right", va="center")


def _title(ax, text: str) -> None:
    ax.text(0.5, 8.15, text, fontsize=17, fontweight="bold", color=C_INK, ha="left", va="center")


def _subtitle(ax, text: str) -> None:
    ax.text(0.5, 7.55, text, fontsize=10.5, color=C_ACCENT, ha="left", va="center")


def _body(ax, lines: list[str], *, x: float = 0.5, y0: float = 7.05, size: float = 10.2, gap: float = 0.34) -> float:
    y = y0
    for line in lines:
        ax.text(x, y, line, fontsize=size, color=C_INK, ha="left", va="top")
        y -= gap
    return y


def _callout(ax, xy, wh, title: str, body: str, *, fc=C_SOFT, ec=C_ACCENT) -> None:
    x, y = xy
    w, h = wh
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.08",
            linewidth=1.2,
            edgecolor=ec,
            facecolor=fc,
            zorder=2,
        )
    )
    ax.text(x + 0.16, y + h - 0.26, title, fontsize=10.5, fontweight="bold", color=ec, ha="left", va="top", zorder=3)
    ax.text(x + 0.16, y + h - 0.52, body, fontsize=9.2, color=C_INK, ha="left", va="top", zorder=3, linespacing=1.32)


def _box(ax, xy, text, *, w=2.4, h=0.7, fc=C_CARD, ec=C_MUTED, size=8.2, weight="normal"):
    x, y = xy
    ax.add_patch(
        FancyBboxPatch(
            (x - w / 2, y - h / 2),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.05",
            linewidth=1.0,
            edgecolor=ec,
            facecolor=fc,
            zorder=2,
        )
    )
    ax.text(x, y, text, ha="center", va="center", fontsize=size, fontweight=weight, color=C_INK, zorder=3, linespacing=1.15)


def _arrow(ax, start, end, *, color=C_MUTED):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=1.0,
            color=color,
            shrinkA=3,
            shrinkB=3,
            zorder=1,
        )
    )


# ── pages ──────────────────────────────────────────────────────────


def page_cover(ax, page, total):
    ax.text(0.5, 8.15, "下單層（Order layer）流程圖教學", fontsize=20, fontweight="bold", color=C_INK, ha="left", va="center")
    ax.text(0.5, 7.55, "flowchart 怎麼讀 · 各節點做什麼 · 健康狀態怎麼看", fontsize=12, color=C_ACCENT, ha="left", va="center")
    _callout(
        ax,
        (0.5, 4.85),
        (10.0, 2.25),
        "這份 PDF 教什麼",
        "對齊正式流程圖 reports/order/*_order_layer_flowchart.pdf（與你貼的 mermaid 同構）。\n"
        "不是策略 alpha 課，而是：訊號如何變成富邦委託、中間過哪些閘、壞掉時看哪裡。\n"
        "建議順序：先看總圖（P2）→ 再分節走讀 → 最後用健康檢查表對照 log。",
        fc="#ecfdf5",
    )
    _body(
        ax,
        [
            "對照文件",
            "  · 單頁總圖：reports/order/order_layer_flowchart.pdf",
            "  · 規格：docs/order-layer-prd.md · config/order.yaml",
            "  · 盤中減碼輔助：docs/intraday-exit-playbook.md",
            "",
            "產檔指令",
            "  .venv/bin/python scripts/tools/render_order_layer_teach_deck.py",
            "  .venv/bin/python scripts/tools/render_order_layer_flowchart.py",
        ],
        y0=4.4,
        size=10.0,
        gap=0.32,
    )
    _footer(ax, page, total)


def page_flowchart(ax, page, total):
    """Landscape-ish content on portrait: compact copy of the official flowchart."""
    _title(ax, "01 · 總圖（先建立全貌）")
    _subtitle(ax, "上→下：SSOT → 觸發 → 雙 sleeve 決策 → 共用安檢 → 富邦 → 帳本")

    # SSOT
    _box(ax, (5.5, 6.85), "SSOT · config/order.yaml · .env · account_risk", w=9.2, h=0.48, fc="#e7e5e4", weight="bold", size=9)

    # triggers
    _box(ax, (2.4, 6.15), "launchd\nbuy-signal-radar\n5m · 09:00–13:20", w=2.8, h=0.85, fc=C_BLUE_BG, ec=C_BLUE, size=8)
    _box(ax, (8.6, 6.15), "launchd\nrrg-c18acc-poll\n5m · 09:00–13:30 · NTB≥13:00", w=3.2, h=0.85, fc=C_BLUE_BG, ec=C_BLUE, size=8)
    _arrow(ax, (4.0, 6.62), (2.4, 6.5))
    _arrow(ax, (7.0, 6.62), (8.6, 6.5))

    # ABC
    _box(ax, (2.4, 5.15), "strategy_signal_radar\nabc-v3-f1 observe", w=2.8, h=0.7, size=8)
    _box(ax, (2.4, 4.25), "process_abc_v3_f1_orders\nENTRY chase_ask", w=2.8, h=0.7, fc=C_GREEN_BG, ec=C_GREEN, size=8)
    _box(ax, (2.4, 3.35), "position_orders\nTP SELL / pyramid BUY", w=2.8, h=0.7, fc="#fef9c3", ec="#a16207", size=8)
    _arrow(ax, (2.4, 4.8), (2.4, 4.6))
    _arrow(ax, (2.4, 3.9), (2.4, 3.7))
    ax.text(0.55, 4.25, "ABC", fontsize=10, fontweight="bold", color=C_BLUE, rotation=90, va="center")

    # C18
    _box(ax, (8.6, 5.15), "rrg_mono_swap_accel_screen\nentry · swap · exit", w=3.2, h=0.7, size=8)
    _box(ax, (8.6, 4.25), "c18acc_position_monitor\npyramid_add", w=3.2, h=0.7, fc=C_PINK_BG, ec=C_PINK, size=8)
    _box(ax, (8.6, 3.35), "process_c18acc_orders\nsell_first · budget cap", w=3.2, h=0.7, fc=C_GREEN_BG, ec=C_GREEN, size=8)
    _arrow(ax, (8.6, 4.8), (8.6, 4.6))
    _arrow(ax, (8.6, 3.9), (8.6, 3.7))
    _arrow(ax, (7.0, 5.15), (8.6, 4.5), color=C_PINK)
    ax.text(10.55, 4.25, "C18", fontsize=10, fontweight="bold", color=C_PINK, rotation=90, va="center")

    # guard + broker
    _box(
        ax,
        (5.5, 2.4),
        "live_submit_guard · pytest / FORBIDDEN 拒 · session=今日 · qty≤budget",
        w=7.5,
        h=0.55,
        fc=C_WARN_BG,
        ec=C_WARN,
        weight="bold",
        size=8.5,
    )
    _box(
        ax,
        (5.5, 1.7),
        "account_cap_gate → Fubon · reserved_cash · sell_first · place / chase / poll",
        w=7.5,
        h=0.55,
        fc="#fee2e2",
        ec="#b91c1c",
        weight="bold",
        size=8.5,
    )
    _arrow(ax, (2.4, 3.0), (3.8, 2.55))
    _arrow(ax, (8.6, 3.0), (7.2, 2.55))
    _arrow(ax, (5.5, 2.12), (5.5, 1.98))

    # artifacts
    _box(ax, (1.7, 1.0), "ABC ledger / intents", w=2.6, h=0.5, fc="#f1f5f9", size=8)
    _box(ax, (9.3, 1.0), "C18 ledger / slots", w=2.6, h=0.5, fc="#f1f5f9", size=8)

    ax.text(
        5.5,
        0.55,
        "讀圖口訣：左邊 ABC、右邊 C18；中下共用安檢與富邦；兩側是帳本。",
        ha="center",
        va="center",
        fontsize=9.5,
        color=C_MUTED,
    )
    _footer(ax, page, total)


def page_legend(ax, page, total):
    _title(ax, "02 · 圖例與顏色（怎麼讀）")
    _subtitle(ax, "顏色不是裝飾，是責任邊界")

    rows = [
        (C_BLUE_BG, C_BLUE, "藍 · launchd 觸發", "定時叫醒；本身不算策略、也不送單"),
        ("#f8fafc", C_MUTED, "白／灰 · 決策／觀測", "算訊號、observe；尚未保證送單"),
        (C_GREEN_BG, C_GREEN, "綠 · 下單處理", "組委託、排序、預算；下一步進安檢"),
        ("#fef9c3", "#a16207", "黃 · ABC 持倉後", "進場之後的 TP／加碼，另一條責任"),
        (C_PINK_BG, C_PINK, "粉 · C18 加碼監控", "screen 之外的 pyramid_add 路徑"),
        (C_WARN_BG, C_WARN, "橙 · live_submit_guard", "最後防線：測試／禁 live／日期／張數"),
        ("#fee2e2", "#b91c1c", "紅 · 富邦", "真錢出口；place ≠ 成交"),
    ]
    y = 6.85
    for fc, ec, title, desc in rows:
        _box(ax, (1.35, y), "", w=0.55, h=0.38, fc=fc, ec=ec, size=1)
        ax.text(1.85, y, title, fontsize=10, fontweight="bold", color=ec, ha="left", va="center")
        ax.text(5.0, y, desc, fontsize=9.5, color=C_INK, ha="left", va="center")
        y -= 0.72

    _callout(
        ax,
        (0.5, 0.7),
        (10.0, 1.15),
        "箭頭方向",
        "一律「觸發 → 決策 → 安檢 → 券商 → 帳本」。拒單時也會寫帳本／意圖，但不會進富邦。",
        fc=C_CARD,
        ec=C_MUTED,
    )
    _footer(ax, page, total)


def page_ssot(ax, page, total):
    _title(ax, "03 · SSOT 節點（規則書）")
    _subtitle(ax, "flowchart 最上方那一條：所有開關從這裡來")

    _callout(ax, (0.5, 5.55), (10.0, 1.35), "config/order.yaml", "結構規格：live sleeves、對齊的 research、預設行為。改這裡等於改「設計」。", fc=C_CARD, ec=C_MUTED)
    _callout(
        ax,
        (0.5, 3.95),
        (10.0, 1.35),
        ".env 與 App Support/order.env",
        "運行時開關：DRY_RUN、AUTO_SUBMIT、BUDGET、NO_TRADE_BEFORE、ORDER_LIVE_FORBIDDEN。\n"
        "launchd 讀 Documents/.env 可能被 TCC 擋 → 白名單鏡像到 order.env（非策略需求，是 OS 妥協）。",
        fc=C_WARN_BG,
        ec=C_WARN,
    )
    _callout(
        ax,
        (0.5, 2.35),
        (10.0, 1.35),
        "account_risk（sell_first / reserved_cash）",
        "帳戶級政策：先賣後買騰現金、預留現金不梭哈。這是現金現實，不是 alpha。",
        fc=C_CARD,
        ec=C_MUTED,
    )
    _body(ax, ["優先序：環境變數通常覆蓋 yaml。緊急停火 → ORDER_LIVE_FORBIDDEN=1。"], y0=1.9, size=10.0, gap=0.3)
    _footer(ax, page, total)


def page_trig(ax, page, total):
    _title(ax, "04 · TRIG · launchd 觸發")
    _subtitle(ax, "flowchart 第二層：兩條鬧鐘叫醒兩條產線")

    _callout(
        ax,
        (0.5, 5.35),
        (4.8, 1.95),
        "buy-signal-radar",
        "約 5 分鐘 · 09:00–13:20\n叫醒 ABC sleeve\n→ radar → entry / position\n腳本：Application Support/…/buy-signal-radar.sh",
        fc=C_BLUE_BG,
        ec=C_BLUE,
    )
    _callout(
        ax,
        (5.7, 5.35),
        (4.8, 1.95),
        "rrg-c18acc-poll",
        "約 5 分鐘 · 09:00–13:30\n叫醒 C18acc sleeve\nNTB（常 13:00）前多半只觀察\n腳本：…/rrg-c18acc-poll.sh",
        fc=C_BLUE_BG,
        ec=C_BLUE,
    )
    _body(
        ax,
        [
            "健康（這一層）",
            "  · 健康：tick 標題 → 業務一行（screen / 買入觀測）→ end exit=0",
            "  · 不健康：只有 tick 標題就沒了（常見：source .env TCC + set -e）",
            "  · launchctl list | grep jackm4.etf → 第二欄應為 0",
            "  · log：logs/intraday/launchd_rrg-c18acc-poll.log · launchd_buy-signal-radar.log",
            "",
            "注意：TRIG 健康 ≠ 有訊號；actions=0 / 尚不可進場 仍可算觸發層健康。",
        ],
        y0=4.9,
        size=10.0,
        gap=0.32,
    )
    _footer(ax, page, total)


def page_abc(ax, page, total):
    _title(ax, "05 · ABC sleeve（左欄）")
    _subtitle(ax, "flowchart 左欄：觀測 → 進場下單 → 持倉後處置")

    steps = [
        ("1 radar", "strategy_signal_radar\nabc-v3-f1 observe", "#f8fafc", C_MUTED),
        ("2 ENTRY", "process_abc_v3_f1_orders\nchase_ask BUY", C_GREEN_BG, C_GREEN),
        ("3 持倉後", "position_orders\nTP SELL / pyramid", "#fef9c3", "#a16207"),
        ("4 匯流", "→ live_submit_guard", C_WARN_BG, C_WARN),
    ]
    xs = [1.8, 4.3, 6.8, 9.3]
    for x, (tag, body, fc, ec) in zip(xs, steps):
        _box(ax, (x, 5.6), f"{tag}\n{body}", w=2.2, h=1.35, fc=fc, ec=ec, size=8.2, weight="bold")
    for i in range(3):
        _arrow(ax, (xs[i] + 1.15, 5.6), (xs[i + 1] - 1.15, 5.6))

    _body(
        ax,
        [
            "說明",
            "  · observe ≠ 已下單：左欄上方是「看見」，綠框才是「準備送」",
            "  · ENTRY 與 position_orders 都是下單入口，但責任不同（新倉 vs 既有倉）",
            "  · 策略腳本不 import order；接線在 scripts/run_buy_signal_radar.py",
            "  · 預算約 1 萬/筆（環境可調）；進 GUARD 後仍受 qty cap",
            "",
            "健康／產物",
            "  · log 看 BUY_SIGNAL_RADAR / ABC_V3_F1_SUBMIT",
            "  · 帳本：abc ledger · reports/order/intents/",
        ],
        y0=4.55,
        size=10.0,
        gap=0.32,
    )
    _footer(ax, page, total)


def page_c18(ax, page, total):
    _title(ax, "06 · C18acc sleeve（右欄）")
    _subtitle(ax, "flowchart 右欄：screen →（可選）加碼監控 → 統一下單")

    steps = [
        ("1 screen", "entry / swap / exit\nmax_hold · extension", "#f8fafc", C_MUTED),
        ("2 monitor", "pyramid_add\n30m kinematic", C_PINK_BG, C_PINK),
        ("3 orders", "sell_first · budget\nswap buy retry", C_GREEN_BG, C_GREEN),
        ("4 匯流", "→ live_submit_guard", C_WARN_BG, C_WARN),
    ]
    xs = [1.8, 4.3, 6.8, 9.3]
    for x, (tag, body, fc, ec) in zip(xs, steps):
        _box(ax, (x, 5.6), f"{tag}\n{body}", w=2.2, h=1.35, fc=fc, ec=ec, size=8.2, weight="bold")
    for i in range(3):
        _arrow(ax, (xs[i] + 1.15, 5.6), (xs[i + 1] - 1.15, 5.6))

    _body(
        ax,
        [
            "說明",
            "  · screen 可同時產出進場／換股／出場；粉箭頭表示加碼可旁路進 monitor",
            "  · sell_first 排序：① exit 賣 ② swap 賣 ③ swap 買／進場／加碼",
            "  · NTB（常 13:00）：觸發層可跑，交易窗未開則不應真送",
            "  · 預算約 2 萬/槽；qty = min(預填, shares_for_budget)",
            "",
            "健康／產物",
            "  · log：C18acc screen … actions=N · end exit=0",
            "  · 帳本：c18acc_order_ledger.json · rrg_c18acc_slots.json",
        ],
        y0=4.55,
        size=10.0,
        gap=0.32,
    )
    _footer(ax, page, total)


def page_guard(ax, page, total):
    _title(ax, "07 · GUARD · live_submit_guard")
    _subtitle(ax, "flowchart 中下橙框：兩條 sleeve 的共用安檢門")

    gates = [
        ("G1", "pytest 或\nORDER_LIVE_FORBIDDEN？", "是 → 拒單", C_WARN_BG, C_WARN),
        ("G2", "session_date\n= 今日？", "否 → 拒單", "#ffedd5", "#c2410c"),
        ("G3", "買進張數", "≤ budget 可買張數", C_SOFT, C_ACCENT),
    ]
    for x, (tag, q, a, fc, ec) in zip([2.0, 5.5, 9.0], gates):
        _box(ax, (x, 5.55), f"{tag}\n{q}\n→ {a}", w=2.9, h=1.55, fc=fc, ec=ec, size=9, weight="bold")

    _body(
        ax,
        [
            "為什麼在圖上單獨成層",
            "  · 2026-07-13：pytest + dry_run=False 可繞 mock 走真實 Fubon worker",
            "  · fixture 用歷史 session_date 時，G2 可擋「用昨天的 swap 故事下今天的單」",
            "  · G3 對齊每筆約 1–2 萬授權，避免高價股預填 100 股",
            "",
            "拒單也會進 ART（ledger / intent），但箭頭不會進紅框富邦。",
            "緊急：ORDER_LIVE_FORBIDDEN=1",
        ],
        y0=4.35,
        size=10.0,
        gap=0.32,
    )
    _footer(ax, page, total)


def page_broker(ax, page, total):
    _title(ax, "08 · BROKER + ART（富邦與帳本）")
    _subtitle(ax, "flowchart 底部：真錢出口與可審計痕跡")

    _callout(
        ax,
        (0.5, 5.25),
        (4.8, 2.0),
        "account_cap_gate → Fubon",
        "reserved_cash\nsell_first 騰現金\nplace_order · chase · poll lifecycle\nplace 成功 ≠ 已成交",
        fc="#fee2e2",
        ec="#b91c1c",
    )
    _callout(
        ax,
        (5.7, 5.25),
        (4.8, 2.0),
        "ART 兩側方框",
        "左：ABC ledger / intents/\n右：C18 ledger / slots\n本機真相；需與富邦 App 對帳\n對不上 → 先停火再查",
        fc="#f1f5f9",
        ec=C_MUTED,
    )
    _body(
        ax,
        [
            "健康（這一層）",
            "  · 有送單意圖時：intent JSON 存在、ledger 有對應列、富邦 App 狀態可解釋",
            "  · ambiguous 失敗：下一 tick 應能安全收斂，不應 silently duplicate",
            "  · 無訊號時：ART 安靜是正常，不代表 BROKER 掛了",
        ],
        y0=4.7,
        size=10.0,
        gap=0.34,
    )
    _footer(ax, page, total)


def page_health(ax, page, total):
    _title(ax, "09 · 健康狀態對照表（對 flowchart）")
    _subtitle(ax, "用節點定義 healthy，而不是「有沒有下單」")

    rows = [
        ("TRIG", "tick→業務行→end exit=0；launchctl=0", "只有 tick；Command TCC；腳本 syntax error"),
        ("ABC 決策", "radar 有輸出；時段略過可接受", "traceback；連續無 end"),
        ("C18 決策", "screen 行 + actions=N；NTB 前 0 單可 OK", "11:30 類：tick 後空白"),
        ("GUARD", "誤測／禁 live 出現拒單 log", "pytest 仍進富邦（事故模式）"),
        ("BROKER", "place/poll 可解釋；無 silent dup", "place 成功但 ledger 失真且不收斂"),
        ("SSOT", "order.env 與意圖一致；FORBIDDEN 符合預期", "plist 殘留 DRY_RUN=1 與 live 意圖衝突"),
    ]
    y = 6.85
    ax.text(0.6, y, "節點", fontsize=9, fontweight="bold", color=C_MUTED)
    ax.text(2.2, y, "健康長相", fontsize=9, fontweight="bold", color=C_MUTED)
    ax.text(6.6, y, "不健康長相", fontsize=9, fontweight="bold", color=C_MUTED)
    y -= 0.45
    for node, ok, bad in rows:
        ax.text(0.6, y, node, fontsize=9.5, fontweight="bold", color=C_ACCENT, va="top")
        ax.text(2.2, y, ok, fontsize=9.0, color=C_INK, va="top")
        ax.text(6.6, y, bad, fontsize=9.0, color=C_WARN, va="top")
        y -= 0.78

    _callout(
        ax,
        (0.5, 0.7),
        (10.0, 1.15),
        "快速指令",
        "launchctl list | grep jackm4.etf\n"
        "tail -30 logs/intraday/launchd_rrg-c18acc-poll.log · launchd_buy-signal-radar.log",
        fc=C_CARD,
        ec=C_MUTED,
    )
    _footer(ax, page, total)


def page_env(ax, page, total):
    _title(ax, "10 · 關鍵開關與路徑小抄")
    _subtitle(ax, "讀 flowchart 時可對照的「旋鈕」")

    _body(
        ax,
        [
            "C18acc",
            "  ORDER_C18ACC_ORDER_ENABLED / AUTO_SUBMIT / DRY_RUN / APPLY_STATE",
            "  C18ACC_NO_TRADE_BEFORE（NTB）· C18ACC_BUDGET_TWD_PER_SLOT",
            "",
            "全域安全",
            "  ORDER_LIVE_FORBIDDEN=1 → 所有 live 路徑應拒單",
            "  ORDER_ALLOW_BACKDATED_SESSION（預設關）",
            "",
            "路徑",
            "  正式總圖：reports/order/order_layer_flowchart.pdf",
            "  本教學：reports/order/YYYYMMDD_order_layer_teach_deck.pdf",
            "  launchd 腳本：~/Library/Application Support/com.jackm4.goldenstocks/",
            "  鏡像設定：…/order.env（改 .env 後請 install-launchd.sh）",
            "  log：logs/intraday/launchd_*.log",
        ],
        y0=6.95,
        size=10.0,
        gap=0.30,
    )
    _footer(ax, page, total)


def page_close(ax, page, total):
    _title(ax, "11 · 收束：用 flowchart 說一遍")
    _subtitle(ax, "30 秒口述（對準架構，不是策略）")

    _callout(
        ax,
        (0.5, 4.55),
        (10.0, 2.65),
        "標準說法",
        "「上方 SSOT 定規則；launchd 每五分鐘叫醒 ABC 或 C18。\n"
        "左欄做 abc 觀測與進出場，右欄做 RRG screen／加碼，兩邊匯到 live_submit_guard，\n"
        "通過後才經 account_cap_gate 進富邦，並寫入兩側 ledger。\n"
        "健康要看觸發層是否跑完 tick，以及安檢／帳本是否與富邦一致——\n"
        "不是看今天有沒有 actions。」",
        fc="#ecfdf5",
        ec=C_ACCENT,
    )
    _body(
        ax,
        [
            "相關產物",
            f"  · 本檔：reports/order/{date.today().strftime('%Y%m%d')}_order_layer_teach_deck.pdf",
            "  · 單頁總圖：reports/order/order_layer_flowchart.pdf",
            "  · 規格：docs/order-layer-prd.md",
        ],
        y0=4.1,
        size=10.0,
        gap=0.34,
    )
    _footer(ax, page, total)


def render(out: Path) -> Path:
    _setup_font()
    out.parent.mkdir(parents=True, exist_ok=True)
    pages = [
        page_cover,
        page_flowchart,
        page_legend,
        page_ssot,
        page_trig,
        page_abc,
        page_c18,
        page_guard,
        page_broker,
        page_health,
        page_env,
        page_close,
    ]
    total = len(pages)
    with PdfPages(out) as pdf:
        for i, fn in enumerate(pages, start=1):
            fig, ax = _new_page()
            fn(ax, i, total)
            pdf.savefig(fig, facecolor=fig.get_facecolor())
            plt.close(fig)
    # also keep a stable latest name
    latest = ROOT / "reports" / "order" / "order_layer_teach_deck.pdf"
    latest.write_bytes(out.read_bytes())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Render Order layer flowchart teaching deck")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    path = render(args.out.resolve())
    print(f"wrote {path}")
    print(f"latest {ROOT / 'reports' / 'order' / 'order_layer_teach_deck.pdf'}")


if __name__ == "__main__":
    main()
