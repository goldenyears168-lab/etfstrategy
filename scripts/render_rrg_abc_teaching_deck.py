"""RRG × ABC v3+f1 教學簡報產生器（多頁 PDF + 每頁 PNG）。

三腿（W20 長線 · W5 中線 · W3 短線）合併在同一張 RRG 四象限圖上，
以回踩軌跡線串接，逐步講解一筆交易從下單到賣出的過程。
內容依據專案回測報告（w3_winner_profile · abc_v3_trade_quality ·
c18acc_s2_dual_wma_annot / exit_save · dual_wma_all_signals_sweep）。

用法::

    .venv/bin/python scripts/render_rrg_abc_teaching_deck.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyArrowPatch, Rectangle

# ── 中文字型 ──────────────────────────────────────────────
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
for _fp in _FONT_CANDIDATES:
    if os.path.exists(_fp):
        fm.fontManager.addfont(_fp)
        plt.rcParams["font.family"] = fm.FontProperties(fname=_fp).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False

# ── 象限底色（淡）與腿色（濃，固定語意）────────────────────
Q_GREEN = "#2E8B57"   # Leading
Q_YELLOW = "#D9A400"  # Weakening
Q_RED = "#C0392B"     # Lagging
Q_BLUE = "#2E6DB4"    # Improving
INK = "#1A1A1A"
GREY = "#8A8A8A"

# 三腿固定顏色（全簡報一致）
C_W20 = "#0B6E4F"  # 長線 深綠
C_W5 = "#E67E22"   # 中線 橘
C_W3 = "#C0392B"   # 短線 紅

AXIS_LO, AXIS_HI = 96.0, 104.0


@dataclass
class Leg:
    """一張 RRG 圖上的點（可含移動箭頭）。"""

    rv: float
    mv: float
    label: str
    color: str = INK
    arrow_to: tuple[float, float] | None = None  # (rv, mv) 持有期移動終點
    ghost: bool = False  # 半透明（表示「之後 / 對照」狀態）


@dataclass
class Panel:
    title: str
    legs: list[Leg]
    banner: str | None = None
    banner_color: str = INK
    connect: bool = False      # 依序連 W20→W5→W3 回踩軌跡虛線
    mv_compare: bool = False   # W3 vs W5 動能高度比較（F 門檻視覺化 · 正向）


@dataclass
class Slide:
    title: str
    subtitle: str
    panels: list[Panel]
    layout: str = "combo"  # combo=單張大圖 · duo=兩張並排 · quad=單張教學
    bullets: list[tuple[str, str]] = field(default_factory=list)  # (符號, 文字)
    footnote: str = ""


def _find(legs: list[Leg], key: str) -> Leg | None:
    for lg in legs:
        if key in lg.label:
            return lg
    return None


def draw_rrg(ax, panel: Panel, *, big: bool = False) -> None:
    """畫單張 RRG 四象限圖（可含多腿 + 軌跡線 + 動能比較）。"""
    lo, hi = AXIS_LO, AXIS_HI
    fs_q = 10 if big else 8         # 象限標籤
    fs_lab = 11 if big else 8.5     # 腿標籤
    dot = 260 if big else 170

    # 四象限底色
    ax.add_patch(Rectangle((100, 100), hi - 100, hi - 100, color=Q_GREEN, alpha=0.10))
    ax.add_patch(Rectangle((100, lo), hi - 100, 100 - lo, color=Q_YELLOW, alpha=0.12))
    ax.add_patch(Rectangle((lo, lo), 100 - lo, 100 - lo, color=Q_RED, alpha=0.10))
    ax.add_patch(Rectangle((lo, 100), 100 - lo, hi - 100, color=Q_BLUE, alpha=0.10))

    ax.text(hi - 0.2, hi - 0.2, "Leading 領先", ha="right", va="top",
            fontsize=fs_q, color=Q_GREEN, fontweight="bold")
    ax.text(hi - 0.2, lo + 0.2, "Weakening 轉弱", ha="right", va="bottom",
            fontsize=fs_q, color=Q_YELLOW, fontweight="bold")
    ax.text(lo + 0.2, lo + 0.2, "Lagging 落後", ha="left", va="bottom",
            fontsize=fs_q, color=Q_RED, fontweight="bold")
    ax.text(lo + 0.2, hi - 0.2, "Improving 改善", ha="left", va="top",
            fontsize=fs_q, color=Q_BLUE, fontweight="bold")

    ax.axhline(100, color=GREY, lw=0.8, ls="--")
    ax.axvline(100, color=GREY, lw=0.8, ls="--")

    # 回踩軌跡線：依 legs 順序（W20→W5→W3）串接
    if panel.connect and len(panel.legs) >= 2:
        xs = [lg.rv for lg in panel.legs]
        ys = [lg.mv for lg in panel.legs]
        ax.plot(xs, ys, color=GREY, lw=1.4, ls="--", alpha=0.7, zorder=3)

    for leg in panel.legs:
        alpha = 0.35 if leg.ghost else 1.0
        if leg.arrow_to is not None:
            arr = FancyArrowPatch(
                (leg.rv, leg.mv), leg.arrow_to,
                arrowstyle="-|>", mutation_scale=18 if big else 14,
                color=leg.color, lw=2.4 if big else 2.0, alpha=0.9, zorder=4,
            )
            ax.add_patch(arr)
        ax.scatter([leg.rv], [leg.mv], s=dot, color=leg.color,
                   edgecolors="white", linewidths=1.6, zorder=5, alpha=alpha)
        ax.text(leg.rv, leg.mv + 0.34, leg.label, ha="center", va="bottom",
                fontsize=fs_lab, fontweight="bold", color=leg.color, zorder=6, alpha=alpha)

    # F 門檻：W3 動能 是否 高於 W5 動能（正向框架）
    if panel.mv_compare:
        w5, w3 = _find(panel.legs, "W5"), _find(panel.legs, "W3")
        if w5 is not None and w3 is not None:
            x_ref = lo + 0.5
            for leg in (w5, w3):
                ax.plot([x_ref, leg.rv], [leg.mv, leg.mv], color=leg.color,
                        lw=1.2, ls=":", alpha=0.8, zorder=3)
            ax.annotate("", xy=(x_ref, w3.mv), xytext=(x_ref, w5.mv),
                        arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.8), zorder=4)
            gap = w3.mv - w5.mv
            ok = gap > -0.01
            ax.text(x_ref + 0.2, (w5.mv + w3.mv) / 2,
                    f"W3−W5 動能\n= {gap:+.1f}",
                    fontsize=9 if big else 8, fontweight="bold", va="center",
                    color=(Q_GREEN if ok else Q_RED), zorder=6)

    if panel.banner:
        ax.text(0.5, -0.135 if big else -0.15, panel.banner, transform=ax.transAxes,
                ha="center", va="top", fontsize=10.5 if big else 9,
                fontweight="bold", color=panel.banner_color)

    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("RS-Ratio（相對強度 · 越右越強）", fontsize=9 if big else 7.5, color=GREY)
    ax.set_ylabel("RS-Momentum（動能 · 越上越加速）", fontsize=9 if big else 7.5, color=GREY)
    ax.set_title(panel.title, fontsize=13 if big else 11,
                 fontweight="bold", color=INK, pad=6)
    ax.set_xticks([98, 100, 102])
    ax.set_yticks([98, 100, 102])
    ax.tick_params(labelsize=8 if big else 7)
    for spine in ax.spines.values():
        spine.set_color(GREY)


def _draw_bullets(fig, bullets, *, x0: float, y0: float, dy: float, fs: float) -> None:
    y = y0
    for symbol, text in bullets:
        color = INK
        if symbol == "賺":
            color = Q_GREEN
        elif symbol == "賠":
            color = Q_RED
        elif symbol == "警":
            color = Q_YELLOW
        elif symbol == "★":
            color = Q_BLUE
        fig.text(x0, y, symbol, fontsize=fs, fontweight="bold", color=color)
        fig.text(x0 + 0.032, y, text, fontsize=fs, color=INK, va="baseline")
        y -= dy


def render_slide(fig, slide: Slide, page_no: int, total: int) -> None:
    fig.clf()
    fig.patch.set_facecolor("white")

    fig.text(0.045, 0.945, slide.title, fontsize=20, fontweight="bold", color=INK)
    fig.text(0.045, 0.905, slide.subtitle, fontsize=11.5, color=Q_BLUE)
    fig.text(0.955, 0.945, f"{page_no} / {total}", fontsize=10, color=GREY, ha="right")
    fig.add_artist(plt.Line2D([0.045, 0.955], [0.888, 0.888],
                              color="#DDDDDD", lw=1.2, transform=fig.transFigure))

    if slide.layout == "combo":
        # 單張大圖（左）＋ 講解（右）
        ax = fig.add_axes([0.05, 0.16, 0.46, 0.68])
        draw_rrg(ax, slide.panels[0], big=True)
        _draw_bullets(fig, slide.bullets, x0=0.565, y0=0.74, dy=0.088, fs=13.5)

    elif slide.layout == "duo":
        # 兩張並排（上）＋ 講解（下）
        ax1 = fig.add_axes([0.06, 0.40, 0.40, 0.44])
        ax2 = fig.add_axes([0.55, 0.40, 0.40, 0.44])
        draw_rrg(ax1, slide.panels[0])
        draw_rrg(ax2, slide.panels[1])
        _draw_bullets(fig, slide.bullets, x0=0.06, y0=0.29, dy=0.052, fs=12.5)

    else:  # quad：單張教學大圖（置中）＋ 講解（右）
        ax = fig.add_axes([0.05, 0.16, 0.46, 0.68])
        draw_rrg(ax, slide.panels[0], big=True)
        _draw_bullets(fig, slide.bullets, x0=0.565, y0=0.74, dy=0.088, fs=13.5)

    if slide.footnote:
        fig.text(0.045, 0.035, slide.footnote, fontsize=8, color=GREY, style="italic")


# ── 常用組合 ──────────────────────────────────────────────
def ideal_combo(title: str = "理想買點：一張圖看三腿", *, connect: bool = True) -> Panel:
    """W20 Leading · W5 Weakening · W3 Lagging（W3 動能 > W5 動能，F 通過）。"""
    return Panel(
        title,
        [
            Leg(103.0, 102.0, "W20 長線", C_W20),
            Leg(100.8, 98.7, "W5 中線", C_W5),
            Leg(99.0, 99.7, "W3 短線", C_W3),
        ],
        banner="W20→W5→W3 順著虛線『往左下回踩』",
        banner_color=GREY,
        connect=connect,
    )


def build_slides() -> list[Slide]:
    slides: list[Slide] = []

    # 1 封面
    slides.append(Slide(
        "RRG 三腿教學：從下單到賣出",
        "W20 長線 · W5 中線 · W3 短線 —— 三腿合併在同一張圖，看懂一筆交易的一生",
        [ideal_combo("同一檔股票 · 三個時間尺度")],
        layout="combo",
        bullets=[
            ("★", "一張圖三個點：W20（長線）· W5（中線）· W3（短線）"),
            ("", "都是『相對大盤』的位置，不是股價本身"),
            ("", "橫軸＝相對強度，縱軸＝動能，交叉點 100 是基準"),
            ("", "本簡報用專案實際回測，講解怎麼賺、怎麼賠"),
            ("★", "重點看『三點相對位置』與『持有期怎麼移動』"),
        ],
        footnote="資料來源：w3_winner_profile · abc_v3_trade_quality · c18acc_s2_dual_wma_annot / exit_save · dual_wma_all_signals_sweep",
    ))

    # 2 座標怎麼讀（單張教學圖）
    slides.append(Slide(
        "第 1 課：一張 RRG 圖怎麼讀",
        "四個象限的意義 —— 先學看懂座標",
        [Panel("四象限意義", [
            Leg(102.5, 102.5, "強且加速", Q_GREEN),
            Leg(102.5, 97.6, "強但減速", Q_YELLOW),
            Leg(97.5, 97.6, "弱且續弱", Q_RED),
            Leg(97.5, 102.5, "轉強中", Q_BLUE),
        ])],
        layout="quad",
        bullets=[
            ("", "右邊＝比大盤強；左邊＝比大盤弱"),
            ("", "上面＝動能加速；下面＝動能減速"),
            ("★", "順時針循環："),
            ("", "改善 → 領先 → 轉弱 → 落後 → 改善…"),
            ("", "同一檔的 W20/W5/W3 會落在不同象限"),
        ],
    ))

    # 3 理想買點（合併圖）
    slides.append(Slide(
        "第 2 課：ABC v3+f1 的『理想買點』",
        "長線撐住、中線剛減速、短線回踩 —— 三腿各就各位",
        [ideal_combo("理想買點 L-Wk-Lg")],
        layout="combo",
        bullets=[
            ("★", "W20 在 Leading：長線結構還強（買它的理由）"),
            ("", "W5 在 Weakening：中線剛開始喘，但仍在 100 右邊"),
            ("", "W3 在 Lagging：短線先蹲（回踩＝便宜的進場點）"),
            ("★", "進場要 W3 動能 > W5 動能（見第 4 課）"),
            ("賺", "回測：ABC v3+f1 完整規格 3 天 +2.85%、勝率 54.7%"),
            ("", "（n=170 · 90d 事件層；val 30d 亦 +1.83% 勝 v3）"),
        ],
        footnote="回測 abc_v3_f1_pipeline_90d：f1 全樣本 2.845% vs v3 2.229% · val 1.826% vs 1.291% passed",
    ))

    # 4 追高接刀（合併圖：三點都擠右上）
    slides.append(Slide(
        "第 3 課：這樣進場最容易賠 —— 追高接刀",
        "三腿『都已經很強』時進場，沒有回踩空間",
        [Panel("危險：三腿全擠右上", [
            Leg(103.4, 102.2, "W20 長線", C_W20),
            Leg(101.6, 101.0, "W5 中線", C_W5),
            Leg(100.5, 100.6, "W3 短線", C_W3),
        ], banner="三點全在 Leading → 沒有回踩、易接刀", banner_color=Q_RED,
           connect=True)],
        layout="combo",
        bullets=[
            ("賠", "W20 + W5 都在 Leading：3 天 −1.08%、勝率僅 41%"),
            ("賠", "W5 還在 Leading（沒減速）：−0.21%（追動能頂點）"),
            ("賠", "W5 動能 > W3 動能：最強負因子 lift −2.27%"),
            ("★", "口訣：三腿都紅通通、都在右上，是刀口不是買點"),
        ],
        footnote="回測 abc_v3_trade_quality n=209 · w3_winner_profile n=795",
    ))

    # 5 F 門檻（正向：進場 W3 MV > W5 MV）· duo
    slides.append(Slide(
        "第 4 課：最可靠的一條線 —— 進場要 W3 動能 > W5 動能",
        "把 W5、W3 畫在同一張圖比高度（縱軸＝動能）：短線動能要贏過中線",
        [
            Panel("✓ 好：W3 動能 > W5", [
                Leg(103.0, 102.0, "W20", C_W20),
                Leg(100.8, 98.7, "W5", C_W5),
                Leg(99.0, 99.7, "W3", C_W3),
            ], banner="短線動能領先 → 回踩後易被拉起", banner_color=Q_GREEN,
               mv_compare=True),
            Panel("✗ 壞：W3 動能 < W5", [
                Leg(103.0, 102.0, "W20", C_W20),
                Leg(100.8, 99.7, "W5", C_W5),
                Leg(99.0, 98.7, "W3", C_W3),
            ], banner="中線動能透支懸空 → 易接刀", banner_color=Q_RED,
               mv_compare=True),
        ],
        layout="duo",
        bullets=[
            ("★", "正向說法：進場時要『W3 動能 > W5 動能』（左圖，箭頭朝上）"),
            ("賺", "W3 高於 W5：短線帶動、回踩後容易反彈"),
            ("賠", "W3 低於 W5（W5 懸空）：中線透支 → 3 天報酬 lift −2.27%"),
            ("★", "這是唯一『進場鑑別 + OOS 驗證』都通過的三腿規則（採納為 F 門檻）"),
        ],
        footnote="回測 abc_v3_trade_quality：W5 MV>W3 MV 為最強負因子 · OOS val 1.83% vs 1.29% passed",
    ))

    # 6 進場分不出賺賠 · duo（贏 vs 輸，幾乎一樣）
    slides.append(Slide(
        "第 5 課：殘酷真相 —— 光看進場圖，分不出賺賠",
        "贏家和輸家『進場當下』的三腿位置，長得幾乎一樣",
        [
            Panel("贏家進場", [
                Leg(103.0, 102.0, "W20", C_W20),
                Leg(100.8, 98.7, "W5", C_W5),
                Leg(99.0, 99.7, "W3", C_W3),
            ], banner="L-Wk-Lg", banner_color=Q_GREEN, connect=True),
            Panel("輸家進場", [
                Leg(102.7, 101.8, "W20", C_W20, ghost=True),
                Leg(100.7, 98.8, "W5", C_W5, ghost=True),
                Leg(99.1, 99.6, "W3", C_W3, ghost=True),
            ], banner="也是 L-Wk-Lg（幾乎重疊）", banner_color=GREY, connect=True),
        ],
        layout="duo",
        bullets=[
            ("", "W20 Leading：贏家 72.2% vs 輸家 69.9%（只差 2.3%）"),
            ("", "L-Wk-Lg 組合：贏家 68.8% vs 輸家 64.8%（只差 4%）"),
            ("★", "結論：進場圖只能當『粗篩門檻』，不能精選贏家"),
            ("賺", "真正決定賺賠的，是進場之後三腿『怎麼動』"),
        ],
        footnote="回測 w3_winner_profile：贏輸象限差異多在 2–4%（Cohen d < 0.2）",
    ))

    # 7 賺錢軌跡（合併圖 + 箭頭）
    slides.append(Slide(
        "第 6 課：賺錢的軌跡 —— 短線往右上『修復』",
        "持有期間 W3、W5 回升去追 W20，代表回踩結束、開始兌現",
        [Panel("賺錢：W3/W5 往右上修復", [
            Leg(103.0, 102.0, "W20 長線", C_W20),
            Leg(100.8, 98.7, "W5 中線", C_W5, arrow_to=(101.8, 101.2)),
            Leg(99.0, 99.7, "W3 短線", C_W3, arrow_to=(101.2, 101.6)),
        ], banner="箭頭朝右上 → 回踩兌現", banner_color=Q_GREEN)],
        layout="combo",
        bullets=[
            ("賺", "W3 從 100 下反彈、W5 追上 W20：均值回歸兌現"),
            ("賺", "最佳出場：等 W5 追過頭（Extension）再走"),
            ("", "→ 超額 +1.14%、勝率 59%"),
            ("", "持有天數：hold 3d 日效率最高、5–8d 總報酬更大"),
        ],
        footnote="回測 dual_wma_all_signals_sweep · abc_v3_hold_sweep",
    ))

    # 8 賠錢軌跡（合併圖 + 箭頭往左下）
    slides.append(Slide(
        "第 7 課：賠錢的軌跡 —— W5 往左下掉（早期警報）",
        "進場對，但持有中 W5 掉進 Lagging、W20 離開 Leading ＝ 結構壞了",
        [Panel("賠錢：三腿往左下崩落", [
            Leg(103.0, 102.0, "W20 長線", C_W20, arrow_to=(101.0, 98.6)),
            Leg(100.8, 98.7, "W5 中線", C_W5, arrow_to=(98.8, 98.0)),
            Leg(99.0, 99.7, "W3 短線", C_W3, arrow_to=(97.6, 98.0)),
        ], banner="箭頭朝左下 → 結構惡化警報", banner_color=Q_RED)],
        layout="combo",
        bullets=[
            ("警", "W5 rotation 警報：W5 掉進 Lagging / W20 離開 Leading"),
            ("警", "回測：警報通常比『被迫停損』早 2 個交易日出現"),
            ("賺", "警報當下賣出，中位數省 +1.37%（57.5% 有救）"),
            ("★", "看到 W5 往左下走，就要提高警覺、收緊停損"),
        ],
        footnote="回測 c18acc_s2_dual_wma_annot（W5 領先中位 2 日）· exit_save（中位 save +1.37%）",
    ))

    # 9 砍太早 · duo（該賣 vs 誤砍）
    slides.append(Slide(
        "第 8 課：小心『砍太早』砍掉大贏家",
        "機械式一轉弱就賣，多數擋住小虧，偶爾錯過大陽線",
        [
            Panel("該賣（結構真壞）", [
                Leg(103.0, 102.0, "W20", C_W20, arrow_to=(100.5, 98.5)),
                Leg(100.8, 98.7, "W5", C_W5, arrow_to=(98.5, 98.0)),
            ], banner="續跌 → 賣對了", banner_color=Q_GREEN),
            Panel("誤砍（其實會噴）", [
                Leg(103.0, 102.0, "W20", C_W20),
                Leg(100.8, 98.7, "W5", C_W5, arrow_to=(102.5, 102.0)),
            ], banner="W5 又拉回 → 賣錯了", banner_color=Q_RED),
        ],
        layout="duo",
        bullets=[
            ("", "反事實回測：中位『提早賣』有救(+)，但平均是負(−1.45%)"),
            ("賠", "案例 2337：抱到底 +54.7%，提早賣只 +25.5% → 少賺 29%"),
            ("★", "正解：W5 警報用來『收緊停損 / 減碼』，不是無腦市價全出"),
            ("", "分工：結構（三腿圖）決定『賣不賣』，價格決定『幾點賣』"),
        ],
        footnote="回測 exit_save：中位 save +1.37% 但 mean excess −1.45pp（右尾被剪）",
    ))

    # 10 總結（合併圖）
    slides.append(Slide(
        "總結：三腿圖的正確用法",
        "進場當粗篩、持有看軌跡、出場靠 W5",
        [ideal_combo("記住這張：理想買點")],
        layout="combo",
        bullets=[
            ("★", "① 進場粗篩：避開『都過熱』，要求 W3 動能 > W5 動能"),
            ("賺", "② 賺錢軌跡：W3/W5 往右上修復追 W20 → 抱住等 Extension"),
            ("警", "③ 賠錢軌跡：W5 往左下掉進 Lagging → 警報（領先約 2 天）"),
            ("★", "④ 出場：W5 警報＝收緊停損，不是無腦全賣"),
            ("", "⑤ 最可靠單一規則：進場 W3 MV > W5 MV（唯一 OOS 通過）"),
        ],
        footnote="本簡報結論全部來自專案既有回測報告（reports/research/rrg/）",
    ))

    return slides


def main() -> None:
    out_dir = os.path.join("reports", "research", "rrg")
    png_dir = os.path.join(out_dir, "rrg_abc_teaching_deck_pages")
    os.makedirs(png_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, "20260708_rrg_abc_teaching_deck.pdf")

    slides = build_slides()
    total = len(slides)
    fig = plt.figure(figsize=(13.33, 7.5))  # 16:9 簡報比例

    with PdfPages(pdf_path) as pdf:
        for i, slide in enumerate(slides, start=1):
            render_slide(fig, slide, i, total)
            pdf.savefig(fig, dpi=150)
            png_path = os.path.join(png_dir, f"page_{i:02d}.png")
            fig.savefig(png_path, dpi=150)
    plt.close(fig)

    print(f"PDF : {pdf_path}")
    print(f"PNG : {png_dir}/page_01..{total:02d}.png（共 {total} 頁）")


if __name__ == "__main__":
    main()
