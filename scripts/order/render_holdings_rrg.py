#!/usr/bin/env python3
"""持倉 RRG 位置圖 · 單頁 HTML · 盤中 WMA20/5/3 三腿 + 評語 + 持倉狀態。

讀 holdings_pulse 匯出的精簡 JSON（帳戶／持倉狀態、現價），以 stock_daily_bars
（finmind 日線）為歷史、以 JSON 內 current_price 合成「今日 provisional close」，
再以 IX0001 基準（盤中 1m K；缺則沿用昨收）計算 JdK RS-Ratio / RS-Momentum。

輸出：
  1) 單檔 HTML（總覽圖 + 持倉狀態 + 每檔 RRG 圖／評語／持倉狀態）。
  2) RRG 數值 md 表（SSOT，可 grep 存查）。

須以主環境 .venv 執行（含 pandas / matplotlib）；富邦 .venv-fubon 無這些套件。

  .venv/bin/python scripts/order/render_holdings_rrg.py --open
  .venv/bin/python scripts/order/render_holdings_rrg.py --date 2026-07-08
  .venv/bin/python scripts/order/render_holdings_rrg.py --json <path> --lengths 20 5 3
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_benchmark import load_benchmark_close  # noqa: E402
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from rrg_mono_intraday_watch import _build_provisional_panels  # noqa: E402
from rrg_rotation import classify_quadrant, compute_rrg_panel  # noqa: E402
from rrg_universe_snapshot import _intraday_benchmark_price  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

# 使用者已出清、略過 RRG 報告
_EXCLUDE_STOCK_IDS = frozenset({"1608"})

_TAG_SORT = {
    "長線轉弱": 0,
    "W5 掉隊警報": 1,
    "回踩觀察": 2,
    "中性觀察": 3,
    "回踩修復": 4,
    "雙腿續強·短線轉弱": 5,
    "三腿續強": 6,
    "資料不足": 99,
}

_QUAD_LABEL = {
    "leading": "Leading 領先",
    "weakening": "Weakening 轉弱",
    "lagging": "Lagging 落後",
    "improving": "Improving 改善",
}
_QUAD_BG = {
    "leading": "#d6f5d6",
    "weakening": "#fff2cc",
    "lagging": "#f9d6d6",
    "improving": "#d6e4ff",
}
# 三腿固定色：長線深藍 → 中線橘 → 短線紅
_LEG_COLOR = {20: "#1a237e", 5: "#ef6c00", 3: "#c62828"}
_LEG_NAME = {20: "W20 長線", 5: "W5 中線", 3: "W3 短線"}
_FALLBACK_COLOR = "#455a64"

# Dark theme kept for optional dark surfaces; email digest uses light.
_THEME_LIGHT = "light"
_THEME_DARK = "dark"
_DARK = {
    "fig_bg": "#1a1b26",
    "ax_bg": "#0f1419",
    "label": "#a9b1d6",
    "title": "#c0caf5",
    "spine": "#3b4261",
    "grid": "#1f2335",
    "cross": "#565f89",
    "caption": "#565f89",
    "structure": "#7aa2f7",
    "marker_edge": "#1a1b26",
    "quad_bg": {
        "leading": "#1a3a2a",
        "weakening": "#3a3218",
        "lagging": "#3a1a1a",
        "improving": "#1a2740",
    },
    "quad_label": {
        "leading": "#3dd68c",
        "weakening": "#e0af68",
        "lagging": "#f07178",
        "improving": "#7aa2f7",
    },
    "leg": {20: "#7aa2f7", 5: "#e0af68", 3: "#f07178"},
    "leg_fallback": "#a9b1d6",
}

# 評語嚴重度色（避免 emoji 在 CJK 字型缺字，改用 ● 前綴 + 文字色）
_TAG_RED = "#c62828"
_TAG_ORANGE = "#ef6c00"
_TAG_GREEN = "#2e7d32"
_TAG_GREY = "#616161"


def _normalize_theme(theme: str | None = None, *, dark: bool = False) -> str:
    if dark or theme in ("dark", "email"):
        return _THEME_DARK
    return _THEME_LIGHT


def _leg_color(length: int, *, theme: str = _THEME_LIGHT) -> str:
    if theme == _THEME_DARK:
        return _DARK["leg"].get(length, _DARK["leg_fallback"])
    return _LEG_COLOR.get(length, _FALLBACK_COLOR)


def _leg_name(length: int) -> str:
    return _LEG_NAME.get(length, f"W{length}")


def _label(sid: str, name: str) -> str:
    """代號 + 名稱；名稱缺或等於代號時只顯示一次（避免「1608 1608」）。"""
    name = (name or "").strip()
    return f"{sid} {name}".strip() if name else sid


def _caliber_desc(caliber: dict) -> str:
    """人類可讀的口徑短語（收盤／盤中 + 基準狀態），供頁面/表格共用。"""
    if caliber.get("mode") == "close":
        return f"收盤口徑（截至 {caliber['panel_last']}）"
    if caliber.get("bench_intraday"):
        return f"盤中 {caliber['poll_minute']}（基準：盤中5mK）"
    return f"盤中 {caliber['poll_minute']}（基準：⚠沿用昨收，相對值可能偏差）"


def _pick_cjk_font() -> str | None:
    candidates = [
        "PingFang HK",
        "PingFang TC",
        "Heiti TC",
        "Arial Unicode MS",
        "Songti TC",
        "Noto Sans CJK TC",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            return name
    # macOS 常見備援字型檔（未被 fontManager 掃到時手動註冊）
    for path in ("/Library/Fonts/Arial Unicode.ttf", "/System/Library/Fonts/PingFang.ttc"):
        if Path(path).is_file():
            try:
                font_manager.fontManager.addfont(path)
                return font_manager.FontProperties(fname=path).get_name()
            except Exception:
                continue
    return None


def _latest_default_json() -> Path | None:
    snap_dir = ROOT / "reports" / "order" / "snapshots"
    files = sorted(snap_dir.glob("holdings_pulse_*.json"))
    return files[-1] if files else None


def _to_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _holding_exit_meta(h: dict) -> dict:
    """從 pulse JSON 取出結構／旗標欄位（md 表用；PDF 不再畫 playbook）。"""
    flags = h.get("structural_flags")
    if flags is None:
        flags = []
    elif not isinstance(flags, list):
        flags = [str(flags)]
    return {
        "tier": str(h.get("structure_tier") or "").strip() or None,
        "hold_days": h.get("hold_days"),
        "gate_2pct": _to_float(h.get("gate_2pct")),
        "trigger_s1b": _to_float(h.get("trigger_s1b")),
        "dist_gate_2pct_pct": _to_float(h.get("dist_gate_2pct_pct")),
        "dist_s1b_pct": _to_float(h.get("dist_s1b_pct")),
        "dist_extension_pct": _to_float(h.get("dist_extension_pct")),
        "structural_flags": [str(x) for x in flags if x],
        "rrg_close_quadrant": str(h.get("rrg_close_quadrant") or "").strip() or None,
        "rrg_intraday_quadrant": str(h.get("rrg_intraday_quadrant") or "").strip() or None,
    }


def _holding_account_fields(h: dict) -> dict:
    """帳戶／持倉狀態欄位（與 fill email 【持倉狀態】對齊）。"""
    cost = _to_float(h.get("cost_price"))
    shares = int(h.get("shares") or 0)
    cost_basis = (cost * shares) if cost is not None and shares else None
    market_value = _to_float(h.get("market_value"))
    if market_value is None:
        px = _to_float(h.get("current_price"))
        if px is not None and shares:
            market_value = px * shares
    return {
        "shares": shares,
        "cost_price": cost,
        "cost_basis": cost_basis,
        "prev_close": _to_float(h.get("prev_close")),
        "market_value": market_value,
        "unrealized_pnl": _to_float(h.get("unrealized_pnl")),
        "price_source": str(h.get("price_source") or "").strip() or None,
    }


def _fmt_pct(val: float | None) -> str:
    return f"{val:+.2f}%" if val is not None else "—"


def _fmt_px(val: float | None) -> str:
    if val is None:
        return "—"
    if abs(val) >= 100:
        return f"{val:.1f}"
    return f"{val:.2f}"


def _fmt_money(val: float | int | None) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):,.0f}"
    except (TypeError, ValueError):
        return str(val)


def _account_summary_line(account: dict | None) -> str | None:
    if not account:
        return None
    bits: list[str] = []
    if account.get("cash_available") is not None:
        bits.append(f"可用現金 NT$ {_fmt_money(account['cash_available'])}")
    if account.get("total_market_value") is not None:
        bits.append(f"持倉市值 NT$ {_fmt_money(account['total_market_value'])}")
    if account.get("total_unrealized_pnl") is not None:
        bits.append(f"未實現 {_fmt_money(account['total_unrealized_pnl'])}")
    return " · ".join(bits) if bits else None


def _holding_status_lines(info: dict) -> list[str]:
    """單檔持倉狀態（PDF 右欄／總覽用）。"""
    return [
        (
            f"持股 {int(info.get('shares') or 0)} 股 · 現價 {_fmt_px(info.get('current_price'))}"
            f" · 今日漲跌 {_fmt_pct(info.get('daily_pct'))}"
        ),
        (
            f"成本均價 {_fmt_px(info.get('cost_price'))}"
            f" · 投資成本 NT$ {_fmt_money(info.get('cost_basis'))}"
            f" · 市值 NT$ {_fmt_money(info.get('market_value'))}"
        ),
        (
            f"損益 NT$ {_fmt_money(info.get('unrealized_pnl'))}"
            f" · 損益率 {_fmt_pct(info.get('pnl_pct'))}"
            f" · 昨收 {_fmt_px(info.get('prev_close'))}"
            f" · 報價來源 {info.get('price_source') or '—'}"
        ),
    ]


def _compute_positions(
    conn,
    holdings: list[dict],
    *,
    session_date: str,
    poll_minute: str,
    lengths: list[int],
    trail: int,
    caliber_mode: str = "auto",
    asof_cap: str | None = None,
) -> tuple[dict, str, dict]:
    """回傳 (positions, panel_last, caliber)。

    positions[sid] = {name, pnl_pct, daily_pct, current_price, intraday,
                      per_length: {L: {ratio, mom, quad, trail}}}

    口徑一致性（A1）：個股現價與基準必須同口徑，否則相對強弱會被大盤位移污染。
    - caliber_mode="auto"：能取到盤中基準（IX0001 1m K）才走盤中，否則退回收盤。
    - caliber_mode="close"：強制收盤（不代入現價、基準停在昨收）。
    - caliber_mode="intraday"：強制盤中（即使基準沿用昨收，頁面會標警告）。
    - asof_cap：若給定，僅用 trade_date ≤ asof 的日線（PIT；研究／訊號日回放用）。
    """
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    if asof_cap:
        close = close.loc[close.index <= asof_cap]
        bench = bench.loc[bench.index <= asof_cap]

    hold_ids = [str(h.get("stock_id") or "").strip() for h in holdings]
    hold_ids = [sid for sid in hold_ids if sid]
    cols = [sid for sid in hold_ids if sid in close.columns]
    close_sub = close[cols] if cols else close.iloc[:, :0]

    raw_last = str(close.index[-1]) if len(close.index) else session_date

    bench_tick = _intraday_benchmark_price(
        conn, session_date, poll_minute, price_mode="kbar"
    )
    bench_ok = bench_tick is not None and bench_tick > 0

    if caliber_mode == "close":
        use_intraday = False
    elif caliber_mode == "intraday":
        use_intraday = True
    else:  # auto：唯有盤中基準可用才走盤中，確保與個股同口徑
        use_intraday = bench_ok

    stock_ticks: dict[str, float] = {}
    if use_intraday:
        for h in holdings:
            sid = str(h.get("stock_id") or "").strip()
            px = _to_float(h.get("current_price"))
            if sid and px is not None and px > 0:
                stock_ticks[sid] = px

    # 收盤口徑：股票與基準都停在最近收盤（bench_tick 不代入）。
    panel_bench_tick = bench_tick if use_intraday else None
    close_prov, bench_prov, _, _ = _build_provisional_panels(
        close_sub, bench, session_date, stock_ticks, panel_bench_tick
    )
    if use_intraday:
        panel_last = str(close_prov.index[-1]) if len(close_prov.index) else session_date
    else:
        panel_last = raw_last

    per_len_panels: dict[int, tuple] = {}
    for length in lengths:
        rs, mom, _ = compute_rrg_panel(close_prov, bench_prov, length=length)
        per_len_panels[length] = (rs, mom)

    positions: dict[str, dict] = {}
    for h in holdings:
        sid = str(h.get("stock_id") or "").strip()
        if not sid:
            continue
        name = str(h.get("stock_name") or "").strip()
        if name == sid:
            name = ""  # 名稱等於代號時視為缺，避免「1608 1608」
        per_length: dict[int, dict | None] = {}
        for length in lengths:
            rs, mom = per_len_panels[length]
            if sid not in rs.columns:
                per_length[length] = None
                continue
            r = rs[sid].dropna()
            m = mom[sid].dropna()
            common = r.index.intersection(m.index)
            if len(common) == 0:
                per_length[length] = None
                continue
            last = common[-1]
            r_last = float(r.loc[last])
            m_last = float(m.loc[last])
            tail_idx = common[-trail:]
            # (trade_date, RS-Ratio, RS-Mom)；末點日期＝當前口徑日
            trail_pts = [
                (str(i), float(r.loc[i]), float(m.loc[i])) for i in tail_idx
            ]
            per_length[length] = {
                "ratio": r_last,
                "mom": m_last,
                "quad": classify_quadrant(r_last, m_last),
                "trail": trail_pts,
            }
        positions[sid] = {
            "name": name,
            "pnl_pct": _to_float(h.get("pnl_pct")),
            "daily_pct": _to_float(h.get("daily_pct")),
            "current_price": _to_float(h.get("current_price")),
            "weight_pct": _to_float(h.get("weight_pct")),
            "intraday": sid in stock_ticks,
            "per_length": per_length,
            **_holding_account_fields(h),
            **_holding_exit_meta(h),
        }

    caliber = {
        "mode": "intraday" if use_intraday else "close",
        "bench_intraday": bench_ok,
        "panel_last": panel_last,
        "poll_minute": poll_minute,
        "session_date": session_date,
        "trail": trail,
    }
    return positions, panel_last, caliber


def _verdict(info: dict, lengths: list[int]) -> tuple[str, str, list[str]]:
    """依三腿象限 + F 門檻 + 損益，產生 (tag, 色, 評語列)（持倉/出場導向）。"""
    pl = info["per_length"]

    def _q(length: int) -> str | None:
        cell = pl.get(length)
        return cell["quad"] if cell else None

    def _m(length: int) -> float | None:
        cell = pl.get(length)
        return cell["mom"] if cell else None

    q20, q5, q3 = _q(20), _q(5), _q(3)
    m5, m3 = _m(5), _m(3)
    lines: list[str] = []

    if not any(pl.get(L) for L in lengths):
        return "資料不足", _TAG_GREY, ["缺 finmind 日線，無法計算三腿 RRG。"]

    if q20 in ("lagging", "weakening"):
        tag, color = "長線轉弱", _TAG_RED
        lines.append(
            f"長線 W20 已離開 Leading（{_QUAD_LABEL.get(q20 or '', q20)}）"
            "→ 趨勢保護優先：收緊停損、分批減碼。"
        )
    elif q5 == "lagging" and q20 == "leading":
        tag, color = "W5 掉隊警報", _TAG_ORANGE
        lines.append(
            "中線 W5 掉進 Lagging，屬領先出場訊號（歷史約領先強制出場 ~2 日）"
            "→ 提高警覺；收緊停損或分批減碼。"
        )
    elif q20 == "leading" and q5 in ("weakening", "lagging"):
        if m3 is not None and m5 is not None and m3 > m5:
            tag, color = "回踩修復", _TAG_GREEN
            lines.append(
                "長強、中線暫弱、短線動能回升（W3 MV > W5 MV）"
                "→ 符合 ABC 理想持有型態，續抱。"
            )
        else:
            tag, color = "回踩觀察", _TAG_GREY
            lines.append(
                "長強、中線轉弱，但短線動能尚未領先（W3 MV ≤ W5 MV）"
                "→ 觀望修復力道，勿追高。"
            )
    elif q20 == "leading" and q5 == "leading":
        if q3 == "leading":
            tag, color = "三腿續強", _TAG_GREEN
            lines.append("W20 / W5 / W3 皆 Leading（三腿同步）→ 趨勢持有，續抱。")
        else:
            tag, color = "雙腿續強·短線轉弱", _TAG_GREEN
            lines.append(
                "長中續強（W20 / W5 皆 Leading），W3 短線動能已轉弱"
                "→ 續抱，留意短線回檔。"
            )
    else:
        tag, color = "中性觀察", _TAG_GREY
        lines.append("三腿型態混合 → 依 RRG 三腿與大盤閘門處置。")

    pnl = info.get("pnl_pct")
    day = info.get("daily_pct")
    if pnl is not None or day is not None:
        parts = []
        if pnl is not None:
            parts.append(f"未實現損益 {pnl:+.2f}%")
        if day is not None:
            parts.append(f"當日 {day:+.2f}%")
        lines.append(" · ".join(parts))

    return tag, color, lines


def _sorted_positions(positions: dict, lengths: list[int]) -> list[tuple[str, dict]]:
    def key(item: tuple[str, dict]) -> tuple:
        sid, info = item
        tag, _, _ = _verdict(info, lengths)
        return (_TAG_SORT.get(tag, 50), -(info.get("weight_pct") or 0), sid)

    return sorted(positions.items(), key=key)


def _axis_bounds(pts_xy: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    if pts_xy:
        span_x = max(3.0, max(abs(x - 100) for x, _ in pts_xy) + 1.5)
        span_y = max(3.0, max(abs(y - 100) for _, y in pts_xy) + 1.5)
    else:
        span_x = span_y = 5.0
    return 100 - span_x, 100 + span_x, 100 - span_y, 100 + span_y


def _draw_quadrants(ax, xlo, xhi, ylo, yhi, *, theme: str = _THEME_LIGHT) -> None:
    if theme == _THEME_DARK:
        qbg = _DARK["quad_bg"]
        qlab = _DARK["quad_label"]
        cross = _DARK["cross"]
        label_c = _DARK["label"]
        grid_c = _DARK["grid"]
        alpha_quad = 0.85
        alpha_lab = 0.85
        grid_lw, grid_a = 0.6, 1.0
    else:
        qbg = _QUAD_BG
        qlab = {
            "leading": "#2e7d32",
            "weakening": "#b8860b",
            "lagging": "#c62828",
            "improving": "#1565c0",
        }
        cross = "#9aa0a6"
        label_c = "#1a1b26"
        grid_c = "#e8eaef"
        alpha_quad = 1.0
        alpha_lab = 0.7
        grid_lw, grid_a = 0.5, 1.0

    ax.axhspan(100, yhi, xmin=0.5, xmax=1.0, color=qbg["leading"], alpha=alpha_quad, zorder=0)
    ax.axhspan(ylo, 100, xmin=0.5, xmax=1.0, color=qbg["weakening"], alpha=alpha_quad, zorder=0)
    ax.axhspan(ylo, 100, xmin=0.0, xmax=0.5, color=qbg["lagging"], alpha=alpha_quad, zorder=0)
    ax.axhspan(100, yhi, xmin=0.0, xmax=0.5, color=qbg["improving"], alpha=alpha_quad, zorder=0)
    ax.axhline(100, color=cross, linewidth=0.8, linestyle="--", zorder=1)
    ax.axvline(100, color=cross, linewidth=0.8, linestyle="--", zorder=1)
    ax.text(xhi, yhi, "Leading 領先", ha="right", va="top", color=qlab["leading"], fontsize=9, alpha=alpha_lab)
    ax.text(xhi, ylo, "Weakening 轉弱", ha="right", va="bottom", color=qlab["weakening"], fontsize=9, alpha=alpha_lab)
    ax.text(xlo, ylo, "Lagging 落後", ha="left", va="bottom", color=qlab["lagging"], fontsize=9, alpha=alpha_lab)
    ax.text(xlo, yhi, "Improving 改善", ha="left", va="top", color=qlab["improving"], fontsize=9, alpha=alpha_lab)
    ax.set_xlabel("RS-Ratio（相對強弱）", color=label_c)
    ax.set_ylabel("RS-Momentum（相對動能）", color=label_c)
    if theme == _THEME_DARK:
        ax.set_facecolor(_DARK["ax_bg"])
        ax.tick_params(colors=_DARK["label"], labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(_DARK["spine"])
        ax.grid(True, color=grid_c, linewidth=grid_lw, alpha=grid_a)
    else:
        ax.set_facecolor("#ffffff")
        ax.tick_params(colors="#565f89", labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#d0d5de")
        ax.grid(True, color=grid_c, linewidth=grid_lw, alpha=grid_a)


def _trail_xy(trail_pts: list) -> list[tuple[float, float]]:
    """trail 點格式：(date, ratio, mom) 或舊版 (ratio, mom)。"""
    out: list[tuple[float, float]] = []
    for t in trail_pts or []:
        if len(t) >= 3:
            out.append((float(t[1]), float(t[2])))
        elif len(t) >= 2:
            out.append((float(t[0]), float(t[1])))
    return out


def _trail_date_span(info: dict, lengths: list[int]) -> tuple[str | None, str | None, int]:
    """取最長可用尾跡的起迄交易日（優先 W20）。回傳 (start, end, n_points)。"""
    best: list = []
    for length in sorted(lengths, reverse=True):
        pl = info["per_length"].get(length) or {}
        trail = pl.get("trail") or []
        if len(trail) > len(best):
            best = trail
    if not best:
        return None, None, 0
    dates = []
    for t in best:
        if len(t) >= 3:
            dates.append(str(t[0]))
    if not dates:
        return None, None, len(best)
    return dates[0], dates[-1], len(dates)


def _trail_span_label(info: dict, caliber: dict, lengths: list[int]) -> str | None:
    """人類可讀：尾跡起點日 → 終點日（盤中含 poll 時刻）。"""
    start, end, n = _trail_date_span(info, lengths)
    if not start or not end:
        return None
    end_label = end
    if caliber.get("mode") == "intraday" and end == str(caliber.get("panel_last") or ""):
        poll = str(caliber.get("poll_minute") or "").strip()
        if poll and poll != "—":
            end_label = f"{end} {poll}"
    return f"{start} → {end_label}（{n} 個交易日）"


def _draw_stock_rrg(ax, info: dict, lengths: list[int], *, theme: str = _THEME_LIGHT) -> None:
    """單檔三腿合併 RRG：長→中→短連線 + 各腿尾跡。"""
    ordered = sorted(lengths, reverse=True)  # 20, 5, 3
    legs = [(L, info["per_length"].get(L)) for L in ordered]
    legs = [(L, pl) for L, pl in legs if pl]

    all_xy: list[tuple[float, float]] = []
    for _, pl in legs:
        all_xy.append((pl["ratio"], pl["mom"]))
        all_xy.extend(_trail_xy(pl["trail"]))
    xlo, xhi, ylo, yhi = _axis_bounds(all_xy)
    _draw_quadrants(ax, xlo, xhi, ylo, yhi, theme=theme)

    edge = _DARK["marker_edge"] if theme == _THEME_DARK else "white"
    structure = _DARK["structure"] if theme == _THEME_DARK else "#607d8b"
    trail_alpha = 0.55 if theme == _THEME_DARK else 0.35

    # 尾跡 + 標記
    for length, pl in legs:
        color = _leg_color(length, theme=theme)
        trail_xy = _trail_xy(pl["trail"])
        if len(trail_xy) >= 2:
            tx = [t[0] for t in trail_xy]
            ty = [t[1] for t in trail_xy]
            ax.plot(tx, ty, color=color, linewidth=1.2, alpha=trail_alpha, zorder=2)
        ax.scatter(
            [pl["ratio"]],
            [pl["mom"]],
            s=110,
            color=color,
            zorder=4,
            edgecolors=edge,
            linewidths=1.0,
        )
        ax.annotate(
            _leg_name(length),
            (pl["ratio"], pl["mom"]),
            textcoords="offset points",
            xytext=(8, 6),
            fontsize=10,
            fontweight="bold",
            color=color,
            zorder=5,
        )

    # 長→短 期限結構連線（同一時刻不同平滑長度，非時間軌跡 → 無箭頭虛線）
    main = [(pl["ratio"], pl["mom"]) for _, pl in legs]
    if len(main) >= 2:
        ax.plot(
            [p[0] for p in main], [p[1] for p in main],
            color=structure, alpha=0.7 if theme == _THEME_DARK else 0.55, linewidth=1.3,
            linestyle=(0, (4, 3)), zorder=3,
        )

    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)


def _fmt_cell(pl: dict | None) -> str:
    if not pl:
        return "—"
    return f"{_QUAD_LABEL.get(pl['quad'] or '', '—')} ({pl['ratio']:.1f},{pl['mom']:.1f})"


def _data_basis_lines(info: dict, caliber: dict, lengths: list[int]) -> list[str]:
    length_s = "/".join(f"W{L}" for L in lengths)
    if caliber.get("mode") == "close":
        caliber_line = f"口徑：{_caliber_desc(caliber)}"
    else:
        px = info.get("current_price")
        px_s = f"{px:.2f}" if px is not None else "—"
        caliber_line = f"口徑：{_caliber_desc(caliber)}（現價 {px_s}）"
    lines = [
        f"session {caliber['session_date']}（面板最後 {caliber['panel_last']}）",
        caliber_line,
        f"三腿：{length_s} · JdK RS-Ratio/RS-Mom（基準 100）",
    ]
    span = _trail_span_label(info, caliber, lengths)
    if span:
        lines.append(f"彩線尾跡（時間）：{span}")
    elif caliber.get("trail"):
        lines.append(f"尾跡＝近{caliber['trail']}日收盤軌跡，末點為當前口徑")
    lines.append("來源：finmind 日線 + 今日 provisional close")
    return lines


def _fig_to_data_uri(fig, *, theme: str = _THEME_LIGHT) -> str:
    buf = io.BytesIO()
    face = _DARK["fig_bg"] if theme == _THEME_DARK else "white"
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor=face)
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _render_overview_chart(positions: dict, lengths: list[int]) -> str:
    """全持倉 W20（或缺則最長腿）位置圖 → data URI。"""
    overview_len = 20 if 20 in lengths else lengths[0]
    fig, ax = plt.subplots(figsize=(7.2, 6.2))
    pts = [
        (sid, info["name"], info["per_length"].get(overview_len))
        for sid, info in positions.items()
        if info["per_length"].get(overview_len)
    ]
    all_xy = [(p[2]["ratio"], p[2]["mom"]) for p in pts]
    xlo, xhi, ylo, yhi = _axis_bounds(all_xy)
    span_x = max(xhi - xlo, 1e-6)
    span_y = max(yhi - ylo, 1e-6)
    _draw_quadrants(ax, xlo, xhi, ylo, yhi)
    placed: list[tuple[float, float]] = []
    for sid, name, pl in sorted(pts, key=lambda p: (p[2]["ratio"], p[2]["mom"])):
        x, yv = pl["ratio"], pl["mom"]
        near = sum(
            1
            for px, py in placed
            if abs(px - x) < span_x * 0.14 and abs(py - yv) < span_y * 0.10
        )
        dy = 4 + near * 12
        ax.scatter([x], [yv], s=70, color="#1a237e", zorder=3)
        ax.annotate(
            _label(sid, name),
            (x, yv),
            textcoords="offset points",
            xytext=(6, dy),
            fontsize=8.5,
            zorder=4,
        )
        placed.append((x, yv))
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)
    ax.set_title(f"全持倉 {_leg_name(overview_len)} 位置", fontsize=12)
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def _render_stock_chart(
    info: dict,
    caliber: dict,
    lengths: list[int],
    *,
    title: str | None = None,
) -> str | None:
    """單檔三腿 RRG → data URI；無資料則 None。"""
    fig = _make_stock_rrg_fig(info, caliber, lengths, title=title)
    if fig is None:
        return None
    return _fig_to_data_uri(fig)


def _make_stock_rrg_fig(
    info: dict,
    caliber: dict,
    lengths: list[int],
    *,
    title: str | None = None,
    theme: str = _THEME_LIGHT,
):
    """單檔三腿 RRG figure；無資料則 None（呼叫端負責 close）。"""
    if not any(info["per_length"].get(L) for L in lengths):
        return None
    fig, ax = plt.subplots(figsize=(7.0, 5.8))
    if theme == _THEME_DARK:
        fig.patch.set_facecolor(_DARK["fig_bg"])
        ax.set_facecolor(_DARK["ax_bg"])
    else:
        fig.patch.set_facecolor("#ffffff")
        ax.set_facecolor("#ffffff")
    _draw_stock_rrg(ax, info, lengths, theme=theme)
    if title:
        title_c = _DARK["title"] if theme == _THEME_DARK else "#1a1b26"
        ax.set_title(title, fontsize=11, pad=8, color=title_c)
    span = _trail_span_label(info, caliber, lengths)
    caption = f"彩線尾跡＝{span}" if span else "彩線尾跡＝時間順序"
    cap_c = _DARK["caption"] if theme == _THEME_DARK else "#607d8b"
    fig.text(
        0.5,
        0.02,
        f"{caption}\n灰虛線＝長→短期限結構（W20→W5→W3），非時間順序",
        ha="center",
        va="bottom",
        fontsize=8,
        color=cap_c,
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    return fig


def compute_close_rrg_positions_asof(
    conn,
    stock_ids: list[str],
    *,
    asof: str,
    names: dict[str, str] | None = None,
    lengths: list[int] | None = None,
    trail: int = 8,
) -> tuple[dict[str, dict], dict]:
    """收盤口徑 · PIT RRG（僅 date≤asof）· 回傳 (positions, caliber)。

    與持倉脈動 HTML 同一套 ``compute_rrg_panel`` / 三腿尾跡邏輯；供研究信／digest 重用。
    """
    lengths = list(lengths or [20, 5, 3])
    names = names or {}
    holdings = [
        {
            "stock_id": sid,
            "stock_name": names.get(sid, ""),
            "current_price": None,
        }
        for sid in stock_ids
        if str(sid).strip()
    ]
    positions, _panel_last, caliber = _compute_positions(
        conn,
        holdings,
        session_date=asof,
        poll_minute="—",
        lengths=lengths,
        trail=trail,
        caliber_mode="close",
        asof_cap=asof,
    )
    return positions, caliber


def save_stock_rrg_png(
    info: dict,
    caliber: dict,
    out_path: Path,
    *,
    lengths: list[int] | None = None,
    title: str | None = None,
    theme: str | None = None,
    dark: bool = False,
) -> bool:
    """寫入單檔 WMA20/5/3 RRG PNG（與持倉脈動視覺語言一致）。無資料回傳 False。

    預設 theme=\"light\"（email digest／持倉脈動）。
    theme=\"dark\" / \"email\" 或 dark=True：深色底（可選）。
    """
    lengths = list(lengths or [20, 5, 3])
    theme_n = _normalize_theme(theme, dark=dark)
    cjk = _pick_cjk_font()
    if cjk:
        plt.rcParams["font.family"] = cjk
        plt.rcParams["axes.unicode_minus"] = False
    fig = _make_stock_rrg_fig(info, caliber, lengths, title=title, theme=theme_n)
    if fig is None:
        return False
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    face = _DARK["fig_bg"] if theme_n == _THEME_DARK else "white"
    fig.savefig(out_path, format="png", dpi=140, bbox_inches="tight", facecolor=face)
    plt.close(fig)
    return True


def _ul(items: list[str], *, cls: str = "") -> str:
    cls_attr = f' class="{cls}"' if cls else ""
    lis = "".join(f"<li>{html.escape(x)}</li>" for x in items if x)
    return f"<ul{cls_attr}>{lis}</ul>" if lis else ""


def _render_holdings_rrg_html(
    positions: dict,
    *,
    lengths: list[int],
    caliber: dict,
    account: dict | None = None,
    meta: dict | None = None,
) -> str:
    """組裝單檔 HTML（總覽 + 每檔區塊）。"""
    meta = meta or {}
    acct = _account_summary_line(account)
    title = (
        f"持倉 RRG · {caliber['session_date']} · {caliber['poll_minute']}"
    )
    overview_uri = _render_overview_chart(positions, lengths)
    sorted_pos = _sorted_positions(positions, lengths)

    nav_bits = []
    status_cards = []
    stock_sections = []
    for sid, info in sorted_pos:
        tag, color, verdict_lines = _verdict(info, lengths)
        label = _label(sid, info["name"])
        anchor = f"s-{sid}"
        nav_bits.append(
            f'<a href="#{html.escape(anchor)}">{html.escape(label)}</a>'
        )
        status_cards.append(
            "<article class='status-card'>"
            f"<h3><a href='#{html.escape(anchor)}'>"
            f"<span class='tag' style='color:{html.escape(color)}'>●</span> "
            f"{html.escape(label)} · {html.escape(tag)}</a></h3>"
            f"{_ul(_holding_status_lines(info), cls='status-lines')}"
            "</article>"
        )

        chart_uri = _render_stock_chart(info, caliber, lengths)
        if chart_uri:
            chart_html = (
                f'<img class="rrg-chart" src="{chart_uri}" '
                f'alt="{html.escape(label)} RRG">'
            )
        else:
            chart_html = '<p class="muted">無 RRG 資料（缺 finmind 日線）</p>'

        m5 = (info["per_length"].get(5) or {}).get("mom")
        m3 = (info["per_length"].get(3) or {}).get("mom")
        filter_note = ""
        if m3 is not None and m5 is not None:
            filter_note = (
                f'<p class="muted">進場濾網參考（非持倉訊號）：'
                f"W3 MV {m3:.1f} vs W5 MV {m5:.1f}</p>"
            )

        stock_sections.append(
            f'<section class="stock" id="{html.escape(anchor)}">'
            f"<h2>{html.escape(label)} "
            f"<span class='tag' style='color:{html.escape(color)}'>"
            f"● {html.escape(tag)}</span></h2>"
            '<div class="stock-grid">'
            f"<div class='chart-wrap'>{chart_html}</div>"
            "<aside>"
            "<h3>評語解說</h3>"
            f"{_ul(verdict_lines)}"
            f"{filter_note}"
            "<h3>持倉狀態</h3>"
            f"{_ul(_holding_status_lines(info), cls='status-lines')}"
            "<h3>數據依據</h3>"
            f"{_ul(_data_basis_lines(info, caliber, lengths), cls='basis')}"
            "</aside>"
            "</div>"
            "</section>"
        )

    sync_err = meta.get("rrg_intraday_sync_error")
    sync_html = (
        f'<p class="warn">⚠ RRG 盤中同步：{html.escape(str(sync_err))}</p>'
        if sync_err
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  --ink: #1c1c1c;
  --muted: #5c6570;
  --line: #d9dee5;
  --bg: #f4f6f8;
  --card: #ffffff;
  --accent: #1a237e;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: "PingFang TC", "Noto Sans TC", "Helvetica Neue", sans-serif;
  color: var(--ink);
  background: var(--bg);
  line-height: 1.5;
}}
header {{
  background: var(--card);
  border-bottom: 1px solid var(--line);
  padding: 1.25rem 1.5rem 1rem;
}}
header h1 {{ margin: 0 0 0.35rem; font-size: 1.45rem; }}
.account {{ margin: 0.2rem 0; font-weight: 650; color: var(--accent); }}
.meta, .muted {{ color: var(--muted); font-size: 0.92rem; }}
.warn {{ color: #b71c1c; }}
nav {{
  margin-top: 0.75rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem 0.85rem;
  font-size: 0.9rem;
}}
nav a {{ color: var(--accent); text-decoration: none; }}
nav a:hover {{ text-decoration: underline; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 1rem 1.25rem 3rem; }}
.overview {{
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
  gap: 1rem;
  margin: 1rem 0 1.5rem;
}}
@media (max-width: 900px) {{
  .overview, .stock-grid {{ grid-template-columns: 1fr; }}
}}
.panel {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 0.9rem 1rem;
}}
.panel h2, .stock h2 {{ margin: 0 0 0.65rem; font-size: 1.15rem; }}
.panel h3, aside h3 {{ margin: 0.7rem 0 0.35rem; font-size: 0.98rem; }}
.rrg-chart {{ width: 100%; height: auto; display: block; }}
.status-card {{
  padding: 0.55rem 0;
  border-bottom: 1px solid var(--line);
}}
.status-card:last-child {{ border-bottom: 0; }}
.status-card h3 {{ margin: 0 0 0.25rem; font-size: 0.98rem; }}
.status-card h3 a {{ color: inherit; text-decoration: none; }}
.status-card h3 a:hover {{ text-decoration: underline; }}
ul {{ margin: 0.15rem 0 0.35rem 1.1rem; padding: 0; }}
ul.status-lines li, ul.basis li {{ margin: 0.12rem 0; }}
.tag {{ font-weight: 700; }}
.stock {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 1rem 1.1rem 1.2rem;
  margin: 1rem 0;
}}
.stock-grid {{
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(260px, 0.85fr);
  gap: 1rem;
  align-items: start;
}}
aside {{ font-size: 0.95rem; }}
.footnote {{
  margin-top: 1.5rem;
  color: var(--muted);
  font-size: 0.85rem;
}}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  {f'<p class="account">{html.escape(acct)}</p>' if acct else ''}
  <p class="meta">口徑：{html.escape(_caliber_desc(caliber))} · 面板最後 {html.escape(str(caliber['panel_last']))}</p>
  <p class="meta">advisory · 人工確認後下單 · 不含出場 playbook／自動賣訊</p>
  {sync_html}
  <nav>{''.join(nav_bits)}</nav>
</header>
<main>
  <section class="overview">
    <div class="panel">
      <h2>全持倉位置</h2>
      <img class="rrg-chart" src="{overview_uri}" alt="全持倉 RRG 總覽">
    </div>
    <div class="panel">
      <h2>持倉狀態</h2>
      {''.join(status_cards)}
    </div>
  </section>
  {''.join(stock_sections)}
  <p class="footnote">圖：matplotlib · 文：與 fill email【持倉狀態】同欄位</p>
</main>
</body>
</html>
"""


def _format_table(
    positions: dict,
    *,
    lengths: list[int],
    caliber: dict,
    meta: dict | None = None,
) -> str:
    meta = meta or {}
    pulse_md = f"holdings_pulse_{caliber['session_date'].replace('-', '')}.md"
    lines: list[str] = []
    lines.append(f"# 持倉 RRG 位置 · session {caliber['session_date']} · {caliber['poll_minute']}")
    lines.append("")
    lines.append("> **性質**：RRG 結構快照 + 評語摘要 · advisory · 人工確認後下單")
    lines.append(
        f"> **搭配閱讀**：同 session 的 `{pulse_md}`（完整【持倉狀態】）"
    )
    lines.append("> **本表含**：三腿 RRG、評語、持股／成本／市值／損益；HTML 含合併圖 + 持倉狀態")
    lines.append("> **本表不含**：出場 playbook、自動賣出指令")
    lines.append("")
    acct = _account_summary_line(
        {
            "cash_available": meta.get("cash_available"),
            "total_market_value": meta.get("total_market_value"),
            "total_unrealized_pnl": meta.get("total_unrealized_pnl"),
        }
    )
    if acct:
        lines.append(f"**持倉狀態**：{acct}")
        lines.append("")
    lines.append(f"口徑：{_caliber_desc(caliber)} · 面板最後 {caliber['panel_last']}")
    sync_err = meta.get("rrg_intraday_sync_error")
    if sync_err:
        lines.append(f"⚠ RRG 盤中同步：{sync_err}")
    lines.append("")
    lines.append("RS-Ratio（X）· RS-Momentum（Y）· baseline 100。>100 相對強／加速。")
    lines.append("評語依三腿象限排序（結構差→好）。")
    lines.append("")
    header = "| 代號 | 名稱 | 評語 | 股數 | 成本 | 現價 | 損益 | 損益% | 當日% | 報價 |"
    sep = "|------|------|------|-----:|-----:|-----:|-----:|------:|------:|------|"
    for length in lengths:
        header += f" WMA{length} RS-Ratio | WMA{length} RS-Mom | WMA{length} 象限 |"
        sep += "------:|------:|------|"
    lines.append(header)
    lines.append(sep)
    for sid, info in _sorted_positions(positions, lengths):
        tag, _, _ = _verdict(info, lengths)
        row = (
            f"| {sid} | {info['name'] or sid} | {tag} | {int(info.get('shares') or 0)} "
            f"| {_fmt_px(info.get('cost_price'))} | {_fmt_px(info.get('current_price'))} "
            f"| {_fmt_money(info.get('unrealized_pnl'))} | {_fmt_pct(info.get('pnl_pct'))} "
            f"| {_fmt_pct(info.get('daily_pct'))} | {info.get('price_source') or '—'} |"
        )
        for length in lengths:
            pl = info["per_length"].get(length)
            if not pl:
                row += " — | — | — |"
            else:
                quad = _QUAD_LABEL.get(pl["quad"] or "", "—")
                row += f" {pl['ratio']:.2f} | {pl['mom']:.2f} | {quad} |"
        lines.append(row)
    lines.append("")
    return "\n".join(lines)


def _open_file(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"（自動開啟失敗：{exc}）", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="持倉 RRG HTML · WMA20/5/3 盤中三腿")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Session date（預設今日／JSON）")
    parser.add_argument("--json", type=Path, help="holdings_pulse JSON 路徑（預設最新）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--outdir", type=Path, help="輸出資料夾（預設 reports/order/snapshots/rrg）")
    parser.add_argument("--lengths", type=int, nargs="+", default=[20, 5, 3])
    parser.add_argument("--trail", type=int, default=8, help="尾跡點數")
    parser.add_argument(
        "--caliber", choices=("auto", "intraday", "close"), default="auto",
        help="口徑：auto=有盤中基準才走盤中否則收盤；close=強制收盤；intraday=強制盤中",
    )
    parser.add_argument("--open", dest="open_html", action="store_true", help="產出後自動開啟 HTML")
    args = parser.parse_args(argv)

    session_date = args.date or date.today().isoformat()
    json_path = args.json or _latest_default_json()
    if json_path is None or not Path(json_path).is_file():
        print(f"✗ 找不到持倉 JSON：{json_path}（先跑 holdings_pulse.py --write）", file=sys.stderr)
        return 2

    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    holdings = [
        h for h in (payload.get("holdings") or [])
        if str(h.get("stock_id") or "").strip() not in _EXCLUDE_STOCK_IDS
    ]
    meta = {
        k: payload.get(k)
        for k in (
            "cash_available",
            "total_market_value",
            "total_unrealized_pnl",
            "portfolio_exit_mode",
            "rrg_intraday_sync_error",
            "generated_at",
        )
    }
    account = {
        "cash_available": meta.get("cash_available"),
        "total_market_value": meta.get("total_market_value"),
        "total_unrealized_pnl": meta.get("total_unrealized_pnl"),
    }
    if not holdings:
        print("✗ JSON 內無持倉", file=sys.stderr)
        return 2
    if not args.date and payload.get("session_date"):
        session_date = str(payload["session_date"])
    poll_minute = str(payload.get("poll_minute") or "—")

    cjk = _pick_cjk_font()
    if cjk:
        plt.rcParams["font.family"] = cjk
        plt.rcParams["axes.unicode_minus"] = False

    conn = connect(args.db)
    try:
        positions, _panel_last, caliber = _compute_positions(
            conn,
            holdings,
            session_date=session_date,
            poll_minute=poll_minute,
            lengths=args.lengths,
            trail=args.trail,
            caliber_mode=args.caliber,
        )
    finally:
        conn.close()

    if not positions:
        print("✗ 無法計算 RRG（缺基準或持倉收盤資料）", file=sys.stderr)
        return 1

    outdir = args.outdir or (ROOT / "reports" / "order" / "snapshots" / "rrg")
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = session_date.replace("-", "")

    html_path = outdir / f"holdings_rrg_{stamp}.html"
    html_path.write_text(
        _render_holdings_rrg_html(
            positions,
            lengths=args.lengths,
            caliber=caliber,
            account=account,
            meta=meta,
        ),
        encoding="utf-8",
    )

    table = _format_table(positions, lengths=args.lengths, caliber=caliber, meta=meta)
    table_path = outdir / f"holdings_rrg_{stamp}.md"
    table_path.write_text(table, encoding="utf-8")

    print(table)
    print(f"\nHTML：{html_path}")
    print(f"數值表：{table_path}")
    missing = [
        _label(sid, info["name"])
        for sid, info in positions.items()
        if not any(info["per_length"].get(length) for length in args.lengths)
    ]
    if missing:
        print(f"\n⚠ 無 RRG 資料（缺 finmind 日線）：{', '.join(missing)}")

    if args.open_html:
        _open_file(html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
