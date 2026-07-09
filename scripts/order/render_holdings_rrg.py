#!/usr/bin/env python3
"""持倉 RRG 位置圖 · 每檔一頁 PDF · 盤中 WMA20/5/3 三腿 + 評語 + 數據依據。

讀 holdings_pulse 匯出的精簡 JSON（持倉清單、下單當下現價），以 stock_daily_bars
（finmind 日線）為歷史、以 JSON 內 current_price 合成「今日 provisional close」，
再以 IX0001 基準（盤中 1m K；缺則沿用昨收）計算 JdK RS-Ratio / RS-Momentum。

輸出：
  1) 每檔一頁的 PDF（第 1 頁全持倉總覽 + 之後每檔一頁的三腿合併 RRG 圖 + 評語）。
  2) RRG 數值 md 表（SSOT，可 grep 存查）。

須以主環境 .venv 執行（含 pandas / matplotlib）；富邦 .venv-fubon 無這些套件。

  .venv/bin/python scripts/order/render_holdings_rrg.py --open
  .venv/bin/python scripts/order/render_holdings_rrg.py --date 2026-07-08
  .venv/bin/python scripts/order/render_holdings_rrg.py --json <path> --lengths 20 5 3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

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

# 評語嚴重度色（避免 emoji 在 CJK 字型缺字，改用 ● 前綴 + 文字色）
_TAG_RED = "#c62828"
_TAG_ORANGE = "#ef6c00"
_TAG_GREEN = "#2e7d32"
_TAG_GREY = "#616161"


def _leg_color(length: int) -> str:
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
        return f"盤中 {caliber['poll_minute']}（基準：盤中1mK）"
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
    """從 pulse JSON 持倉列取出 exit playbook 摘要欄位。"""
    flags = h.get("structural_flags")
    if flags is None:
        flags = []
    elif not isinstance(flags, list):
        flags = [str(flags)]
    return {
        "tier": str(h.get("structure_tier") or "").strip() or None,
        "vcp_composite": _to_float(h.get("vcp_composite")),
        "vcp_state": str(h.get("vcp_state") or "").strip() or None,
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


def _fmt_pct(val: float | None) -> str:
    return f"{val:+.2f}%" if val is not None else "—"


def _compute_positions(
    conn,
    holdings: list[dict],
    *,
    session_date: str,
    poll_minute: str,
    lengths: list[int],
    trail: int,
    caliber_mode: str = "auto",
) -> tuple[dict, str, dict]:
    """回傳 (positions, panel_last, caliber)。

    positions[sid] = {name, pnl_pct, daily_pct, current_price, intraday,
                      per_length: {L: {ratio, mom, quad, trail}}}

    口徑一致性（A1）：個股現價與基準必須同口徑，否則相對強弱會被大盤位移污染。
    - caliber_mode="auto"：能取到盤中基準（IX0001 1m K）才走盤中，否則退回收盤。
    - caliber_mode="close"：強制收盤（不代入現價、基準停在昨收）。
    - caliber_mode="intraday"：強制盤中（即使基準沿用昨收，頁面會標警告）。
    """
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)

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
            trail_pts = [(float(r.loc[i]), float(m.loc[i])) for i in tail_idx]
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
            "中線 W5 掉進 Lagging，屬領先出場訊號（歷史約領先 S2 強制出場 ~2 日）"
            "→ 提高警覺；跌破 VCP 停損即出。"
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
        lines.append("三腿型態混合 → 依 VCP 停損與大盤閘門處置。")

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


def _exit_playbook_lines(info: dict) -> list[str]:
    """Exit playbook 摘要（持倉頁／PDF 右欄）。"""
    lines: list[str] = []
    tier = info.get("tier")
    if tier:
        lines.append(f"tier={tier}")
    vcp_c = info.get("vcp_composite")
    vcp_s = info.get("vcp_state")
    if vcp_c is not None or vcp_s:
        lines.append(f"VCP {vcp_c if vcp_c is not None else '—'} · {vcp_s or '—'}")
    hd = info.get("hold_days")
    if hd is not None:
        lines.append(f"持有 {hd} 日")
    d2 = info.get("dist_gate_2pct_pct")
    if d2 is not None:
        lines.append(f"距 -2% gate {_fmt_pct(d2)}")
    s1b = info.get("dist_s1b_pct")
    if s1b is not None:
        lines.append(f"距 S1b（弱檔 -3%）{_fmt_pct(s1b)}")
    ext = info.get("dist_extension_pct")
    if ext is not None:
        lines.append(f"距 extension spike {_fmt_pct(ext)}")
    flags = info.get("structural_flags") or []
    if flags:
        lines.append("旗標：" + " · ".join(flags))
    rq_close = info.get("rrg_close_quadrant")
    rq_intra = info.get("rrg_intraday_quadrant")
    if rq_close or rq_intra:
        parts = []
        if rq_close:
            parts.append(f"收盤 {rq_close}")
        if rq_intra:
            parts.append(f"盤中 {rq_intra}")
        lines.append("DB RRG：" + " / ".join(parts))
    return lines


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


def _draw_quadrants(ax, xlo, xhi, ylo, yhi) -> None:
    ax.axhspan(100, yhi, xmin=0.5, xmax=1.0, color=_QUAD_BG["leading"], zorder=0)
    ax.axhspan(ylo, 100, xmin=0.5, xmax=1.0, color=_QUAD_BG["weakening"], zorder=0)
    ax.axhspan(ylo, 100, xmin=0.0, xmax=0.5, color=_QUAD_BG["lagging"], zorder=0)
    ax.axhspan(100, yhi, xmin=0.0, xmax=0.5, color=_QUAD_BG["improving"], zorder=0)
    ax.axhline(100, color="#888", linewidth=0.8, linestyle="--", zorder=1)
    ax.axvline(100, color="#888", linewidth=0.8, linestyle="--", zorder=1)
    ax.text(xhi, yhi, "Leading 領先", ha="right", va="top", color="#2e7d32", fontsize=9, alpha=0.7)
    ax.text(xhi, ylo, "Weakening 轉弱", ha="right", va="bottom", color="#b8860b", fontsize=9, alpha=0.7)
    ax.text(xlo, ylo, "Lagging 落後", ha="left", va="bottom", color="#c62828", fontsize=9, alpha=0.7)
    ax.text(xlo, yhi, "Improving 改善", ha="left", va="top", color="#1565c0", fontsize=9, alpha=0.7)
    ax.set_xlabel("RS-Ratio（相對強弱）")
    ax.set_ylabel("RS-Momentum（相對動能）")
    ax.grid(True, linewidth=0.3, alpha=0.4)


def _draw_stock_rrg(ax, info: dict, lengths: list[int]) -> None:
    """單檔三腿合併 RRG：長→中→短連線 + 各腿尾跡。"""
    ordered = sorted(lengths, reverse=True)  # 20, 5, 3
    legs = [(L, info["per_length"].get(L)) for L in ordered]
    legs = [(L, pl) for L, pl in legs if pl]

    all_xy: list[tuple[float, float]] = []
    for _, pl in legs:
        all_xy.append((pl["ratio"], pl["mom"]))
        all_xy.extend(pl["trail"])
    xlo, xhi, ylo, yhi = _axis_bounds(all_xy)
    _draw_quadrants(ax, xlo, xhi, ylo, yhi)

    # 尾跡 + 標記
    for length, pl in legs:
        color = _leg_color(length)
        trail_pts = pl["trail"]
        if len(trail_pts) >= 2:
            tx = [t[0] for t in trail_pts]
            ty = [t[1] for t in trail_pts]
            ax.plot(tx, ty, color=color, linewidth=1.0, alpha=0.35, zorder=2)
        ax.scatter([pl["ratio"]], [pl["mom"]], s=110, color=color, zorder=4,
                   edgecolors="white", linewidths=1.0)
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
            color="#607d8b", alpha=0.55, linewidth=1.3,
            linestyle=(0, (4, 3)), zorder=3,
        )

    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)


def _fmt_cell(pl: dict | None) -> str:
    if not pl:
        return "—"
    return f"{_QUAD_LABEL.get(pl['quad'] or '', '—')} ({pl['ratio']:.1f},{pl['mom']:.1f})"


def _data_basis_text(info: dict, caliber: dict, lengths: list[int]) -> str:
    length_s = "/".join(f"W{L}" for L in lengths)
    if caliber.get("mode") == "close":
        caliber_line = f"· 口徑：{_caliber_desc(caliber)}"
    else:
        px = info.get("current_price")
        px_s = f"{px:.2f}" if px is not None else "—"
        caliber_line = f"· 口徑：{_caliber_desc(caliber)}（現價 {px_s}）"
    trail = caliber.get("trail")
    trail_line = (
        f"· 尾跡＝近{trail}日收盤軌跡，末點為當前口徑\n" if trail else ""
    )
    return (
        "數據依據\n"
        f"· session {caliber['session_date']}（面板最後 {caliber['panel_last']}）\n"
        f"{caliber_line}\n"
        f"· 三腿：{length_s} · JdK RS-Ratio/RS-Mom（基準 100）\n"
        f"{trail_line}"
        "· 來源：finmind 日線 + 今日 provisional close"
    )


def _add_stock_page(pdf: PdfPages, sid: str, info: dict, caliber: dict, lengths: list[int]) -> None:
    fig = plt.figure(figsize=(11.69, 8.27))  # A4 橫
    tag, color, lines = _verdict(info, lengths)

    fig.text(0.06, 0.945, _label(sid, info["name"]), fontsize=20, fontweight="bold", va="top")
    fig.text(0.06, 0.905, f"● {tag}", fontsize=15, va="top", color=color, fontweight="bold")

    has_data = any(info["per_length"].get(L) for L in lengths)
    ax = fig.add_axes([0.06, 0.13, 0.55, 0.74])
    if has_data:
        _draw_stock_rrg(ax, info, lengths)
        # A4：連線語意圖例（同一時刻不同平滑長度，非時間順序）
        fig.text(
            0.335, 0.055,
            "連線＝長→短 期限結構（W20→W5→W3），非時間順序",
            ha="center", va="top", fontsize=8, color="#607d8b",
        )
    else:
        ax.axis("off")
        ax.text(0.5, 0.5, "無 RRG 資料（缺 finmind 日線）", ha="center", va="center", fontsize=13)

    # 右欄：評語（手動換行，避免長句疊到下一條）+ 數據依據
    y = 0.86
    fig.text(0.66, y, "評語解說", fontsize=14, fontweight="bold", va="top")
    y -= 0.048
    line_h = 0.030
    for ln in lines:
        wrapped = textwrap.wrap(ln, width=22) or [""]
        for i, seg in enumerate(wrapped):
            prefix = "• " if i == 0 else "\u3000\u3000"  # 續行以全形空白縮排對齊
            fig.text(0.66, y, f"{prefix}{seg}", fontsize=11.5, va="top")
            y -= line_h
        y -= 0.010  # bullet 間距

    # A2：F 門檻是進場濾網，非持倉訊號 → 中性灰字，不帶佳/差判斷
    m5 = (info["per_length"].get(5) or {}).get("mom")
    m3 = (info["per_length"].get(3) or {}).get("mom")
    if m3 is not None and m5 is not None:
        y -= 0.006
        fig.text(
            0.66, y,
            f"進場濾網參考（非持倉訊號）：W3 MV {m3:.1f} vs W5 MV {m5:.1f}",
            fontsize=9.5, va="top", color=_TAG_GREY,
        )
        y -= line_h

    exit_lines = _exit_playbook_lines(info)
    if exit_lines:
        y -= 0.012
        fig.text(0.66, y, "出場參考（playbook）", fontsize=12, fontweight="bold", va="top")
        y -= 0.040
        for ln in exit_lines:
            wrapped = textwrap.wrap(ln, width=22) or [""]
            for i, seg in enumerate(wrapped):
                prefix = "• " if i == 0 else "\u3000\u3000"
                fig.text(0.66, y, f"{prefix}{seg}", fontsize=10, va="top", color="#37474f")
                y -= line_h * 0.92
            y -= 0.006

    fig.text(
        0.66, max(y - 0.02, 0.10),
        _data_basis_text(info, caliber, lengths),
        fontsize=9.5, va="top", color="#37474f",
    )
    pdf.savefig(fig)
    plt.close(fig)


def _add_overview_page(pdf: PdfPages, positions: dict, caliber: dict, lengths: list[int]) -> None:
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(
        0.06, 0.95,
        f"持倉 RRG 總覽 · session {caliber['session_date']} · {caliber['poll_minute']}",
        fontsize=18, fontweight="bold", va="top",
    )
    caliber_note = f"口徑：{_caliber_desc(caliber)} · 面板最後 {caliber['panel_last']}"
    fig.text(0.06, 0.915, caliber_note, fontsize=10, va="top", color="#37474f")

    # 左：全持倉 W20 位置
    overview_len = 20 if 20 in lengths else lengths[0]
    ax = fig.add_axes([0.05, 0.08, 0.52, 0.78])
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
    # B2：相近點的標籤上下錯開，避免重疊（簡單避讓，非完美防疊）
    placed: list[tuple[float, float]] = []
    for sid, name, pl in sorted(pts, key=lambda p: (p[2]["ratio"], p[2]["mom"])):
        x, yv = pl["ratio"], pl["mom"]
        near = sum(
            1 for px, py in placed
            if abs(px - x) < span_x * 0.14 and abs(py - yv) < span_y * 0.10
        )
        dy = 4 + near * 12  # 每多一個相近點，標籤再往上錯開一格
        ax.scatter([x], [yv], s=70, color="#1a237e", zorder=3)
        ax.annotate(
            _label(sid, name), (x, yv),
            textcoords="offset points", xytext=(6, dy), fontsize=8.5, zorder=4,
        )
        placed.append((x, yv))
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)
    ax.set_title(f"全持倉 {_leg_name(overview_len)} 位置", fontsize=12)

    # 右：摘要表（每檔三腿象限 + 損益 + 標題）
    y = 0.87
    fig.text(0.60, y, "持倉摘要", fontsize=14, fontweight="bold", va="top")
    y -= 0.05
    for sid, info in _sorted_positions(positions, lengths):
        tag, color, _ = _verdict(info, lengths)
        pnl = info.get("pnl_pct")
        pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"
        quads = " / ".join(
            (_QUAD_LABEL.get((info["per_length"].get(L) or {}).get("quad") or "", "—").split()[0]
             if info["per_length"].get(L) else "—")
            for L in sorted(lengths, reverse=True)
        )
        fig.text(0.60, y, f"● {_label(sid, info['name'])}  {tag}", fontsize=11, fontweight="bold",
                 va="top", color=color)
        y -= 0.032
        fig.text(0.62, y, f"W20/5/3：{quads} · 損益 {pnl_s}", fontsize=9.5, va="top", color="#37474f")
        y -= 0.05
        if y < 0.08:
            break
    pdf.savefig(fig)
    plt.close(fig)


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
        f"> **搭配閱讀**：同 session 的 `{pulse_md}`（現價來源、組合閘門、完整 playbook）"
    )
    lines.append("> **本表含**：三腿 RRG、評語、tier、旗標、損益；PDF 含合併圖 + 出場參考")
    lines.append("> **本表不含**：VCP 停損絕對價、自動賣出指令")
    lines.append("")
    lines.append(f"口徑：{_caliber_desc(caliber)} · 面板最後 {caliber['panel_last']}")
    if meta.get("portfolio_exit_mode") is not None:
        mode = "ON" if meta["portfolio_exit_mode"] else "OFF"
        lines.append(f"組合 exit_mode（09:05 gate）：**{mode}**")
    sync_err = meta.get("rrg_intraday_sync_error")
    if sync_err:
        lines.append(f"⚠ RRG 盤中同步：{sync_err}")
    lines.append("")
    lines.append("RS-Ratio（X）· RS-Momentum（Y）· baseline 100。>100 相對強／加速。")
    lines.append("評語依三腿象限排序（結構差→好）。")
    lines.append("")
    header = "| 代號 | 名稱 | 評語 | 損益% | 當日% | tier | 旗標 |"
    sep = "|------|------|------|------:|------:|------|------|"
    for length in lengths:
        header += f" WMA{length} RS-Ratio | WMA{length} RS-Mom | WMA{length} 象限 |"
        sep += "------:|------:|------|"
    lines.append(header)
    lines.append(sep)
    for sid, info in _sorted_positions(positions, lengths):
        tag, _, _ = _verdict(info, lengths)
        flags = info.get("structural_flags") or []
        flag_s = " · ".join(flags) if flags else "—"
        row = (
            f"| {sid} | {info['name'] or sid} | {tag} | {_fmt_pct(info.get('pnl_pct'))} "
            f"| {_fmt_pct(info.get('daily_pct'))} | {info.get('tier') or '—'} | {flag_s} |"
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
    parser = argparse.ArgumentParser(description="持倉 RRG 每檔一頁 PDF · WMA20/5/3 盤中三腿")
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
    parser.add_argument("--open", dest="open_pdf", action="store_true", help="產出後自動開啟 PDF")
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
            "portfolio_exit_mode",
            "rrg_intraday_sync_error",
            "generated_at",
        )
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
        positions, panel_last, caliber = _compute_positions(
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

    pdf_path = outdir / f"holdings_rrg_{stamp}.pdf"
    with PdfPages(pdf_path) as pdf:
        _add_overview_page(pdf, positions, caliber, args.lengths)
        for sid, info in _sorted_positions(positions, args.lengths):
            _add_stock_page(pdf, sid, info, caliber, args.lengths)

    table = _format_table(positions, lengths=args.lengths, caliber=caliber, meta=meta)
    table_path = outdir / f"holdings_rrg_{stamp}.md"
    table_path.write_text(table, encoding="utf-8")

    print(table)
    print(f"\nPDF（每檔一頁）：{pdf_path}")
    print(f"數值表：{table_path}")
    missing = [
        _label(sid, info["name"])
        for sid, info in positions.items()
        if not any(info["per_length"].get(length) for length in args.lengths)
    ]
    if missing:
        print(f"\n⚠ 無 RRG 資料（缺 finmind 日線）：{', '.join(missing)}")

    if args.open_pdf:
        _open_file(pdf_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
