#!/usr/bin/env python3
"""Render RRG scatter (RS-Ratio × RS-Momentum) for ETF constituent universe."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    compute_etf_holdings_changes,
    connect,
    load_etf_constituent_watchlist,
    normalize_stock_name,
)

from report_paths import RESEARCH_RRG, research_html_path

REPORTS = RESEARCH_RRG

QUADRANT_COLORS = {
    "leading": "#1F8A65",
    "weakening": "#E8A040",
    "lagging": "#C04848",
    "improving": "#2E79B5",
}

QUADRANT_LABEL_ZH = {
    "leading": "Leading 領先",
    "weakening": "Weakening 轉弱",
    "lagging": "Lagging 落後",
    "improving": "Improving 改善",
}

DAY_COLORS = ("#6B8CAE", "#52A882", "#D4AF37", "#E87840", "#C75CB8")

# Trajectory explorer: large canvas for static multi-day overlays.
TRAJ_CHART_W = 1600
TRAJ_CHART_H = 1280
TRAJ_CHART_MARGIN = {"l": 64, "r": 32, "t": 44, "b": 56}

# Interactive timeline: compact canvas, bounds from highlight positions.
TIMELINE_CHART_W = 900
TIMELINE_CHART_H = 720
TIMELINE_CHART_MARGIN = {"l": 56, "r": 24, "t": 36, "b": 52}
TIMELINE_QUAD_OPACITY = 0.08

TIMELINE_TAIL_DAYS = 4
TIMELINE_TAIL_MIN_DISP = 3.0
TIMELINE_VISIBLE_DAYS = 10

HOLDINGS_ACTION_COLORS = {
    "新进": "#2E79B5",
    "加码": "#F07070",
    "减码": "#6BCB94",
    "出清": "#999999",
}


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _short_label(stock_id: str, stock_name: str, *, max_name: int = 8) -> str:
    name = (stock_name or "").strip()
    if len(name) > max_name:
        name = name[: max_name - 1] + "…"
    return f"{stock_id} {name}".strip()


def _format_daily_pct(pct: float | None) -> str:
    if pct is None or pct != pct:
        return "—"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def _daily_pct_color(pct: float | None) -> str:
    if pct is None or pct != pct:
        return "#888"
    if pct > 0:
        return "#F07070"
    if pct < 0:
        return "#6BCB94"
    return "#aaa"


def _classify_trend(trajectory: dict) -> str:
    pts = trajectory["points"]
    dr = pts[-1]["rs_ratio"] - pts[0]["rs_ratio"]
    dm = pts[-1]["rs_momentum"] - pts[0]["rs_momentum"]
    if dr > 0 and dm > 0:
        return "up_right"
    if dr < 0 and dm < 0:
        return "down_left"
    return "other"


def _split_trajectories_by_trend(
    trajectories: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    up_right: list[dict] = []
    down_left: list[dict] = []
    other: list[dict] = []
    for t in trajectories:
        bucket = _classify_trend(t)
        if bucket == "up_right":
            up_right.append(t)
        elif bucket == "down_left":
            down_left.append(t)
        else:
            other.append(t)
    up_right.sort(key=lambda t: (-t["displacement"], t["stock_id"]))
    down_left.sort(key=lambda t: (-t["displacement"], t["stock_id"]))
    return up_right, down_left, other


def _timeline_year_tag(date_from: str | None, date_to: str | None) -> str | None:
    if not date_from:
        return None
    y0, y1 = date_from[:4], (date_to or date_from)[:4]
    return y0 if y0 == y1 else f"{y0}_{y1}"


def _load_trading_dates_range(
    conn,
    *,
    date_from: str,
    date_to: str | None = None,
) -> list[str]:
    if date_to:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date AS d
            FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date >= ? AND trade_date <= ?
            ORDER BY d ASC
            """,
            (date_from, date_to),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date AS d
            FROM stock_daily_bars
            WHERE source = 'finmind' AND trade_date >= ?
            ORDER BY d ASC
            """,
            (date_from,),
        ).fetchall()
    dates = [str(r["d"]) for r in rows]
    if not dates:
        raise ValueError(f"找不到 {date_from} 起的 FinMind 交易日")
    return dates


def _load_rrg_points(
    conn,
    *,
    as_of_date: str | None,
    etf_codes: tuple[str, ...],
    length: int,
) -> tuple[str, list[dict]]:
    from research.backtest.finpilot_local_backtest import load_price_panels
    from market_benchmark import load_benchmark_close
    from project_config import parse_etf_codes
    from rrg_rotation import compute_rrg_panel, classify_quadrant

    del parse_etf_codes
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}
    universe_ids = [w["stock_id"] for w in watch]

    close, _, _vol = load_price_panels(conn)
    if as_of_date is None:
        as_of_date = str(close.index[-1])
    if as_of_date not in close.index:
        raise ValueError(f"as_of_date {as_of_date} 不在股價面板")

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _quad = compute_rrg_panel(close, bench, length=length)
    rrow = rs_ratio.loc[as_of_date]
    mrow = rs_mom.loc[as_of_date]

    points: list[dict] = []
    for sid in universe_ids:
        if sid not in rrow.index:
            continue
        rv = float(rrow[sid]) if rrow[sid] == rrow[sid] else None
        mv = float(mrow[sid]) if mrow[sid] == mrow[sid] else None
        if rv is None or mv is None:
            continue
        quad = classify_quadrant(rv, mv)
        points.append(
            {
                "stock_id": sid,
                "stock_name": name_by_id.get(sid, ""),
                "rs_ratio": round(rv, 2),
                "rs_momentum": round(mv, 2),
                "quadrant": quad,
                "etf_hold_count": next(
                    (w["etf_hold_count"] for w in watch if w["stock_id"] == sid), 0
                ),
            }
        )

    points.sort(key=lambda p: (-p["rs_ratio"], -p["rs_momentum"]))
    return as_of_date, points


def _load_rrg_trajectories(
    conn,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...],
    length: int,
    with_close: bool = False,
) -> list[dict]:
    from research.backtest.finpilot_local_backtest import load_price_panels
    from market_benchmark import load_benchmark_close
    from rrg_rotation import classify_quadrant, compute_rrg_panel

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}
    hold_by_id = {w["stock_id"]: w.get("etf_hold_count", 0) for w in watch}
    universe_ids = [w["stock_id"] for w in watch]

    close, _, _ = load_price_panels(conn)
    for d in dates:
        if d not in close.index:
            raise ValueError(f"date {d} 不在股價面板")

    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _quad = compute_rrg_panel(close, bench, length=length)
    daily_pct_panel = close.pct_change(fill_method=None) * 100.0

    trajectories: list[dict] = []
    for sid in universe_ids:
        pts: list[dict] = []
        for d in dates:
            if sid not in rs_ratio.columns:
                continue
            rv = rs_ratio.at[d, sid]
            mv = rs_mom.at[d, sid]
            if rv != rv or mv != mv:
                continue
            rv_f, mv_f = float(rv), float(mv)
            raw_pct = daily_pct_panel.at[d, sid] if sid in daily_pct_panel.columns else float("nan")
            daily_pct = round(float(raw_pct), 2) if raw_pct == raw_pct else None
            pt: dict = {
                "date": d,
                "rs_ratio": round(rv_f, 2),
                "rs_momentum": round(mv_f, 2),
                "quadrant": classify_quadrant(rv_f, mv_f),
                "daily_pct": daily_pct,
            }
            if with_close and sid in close.columns:
                cv = close.at[d, sid]
                if cv == cv and cv:
                    pt["close"] = round(float(cv), 4)
            pts.append(pt)
        if len(pts) < 2:
            continue
        trajectories.append(
            {
                "stock_id": sid,
                "stock_name": name_by_id.get(sid, ""),
                "etf_hold_count": hold_by_id.get(sid, 0),
                "points": pts,
                "start_quadrant": pts[0]["quadrant"],
                "end_quadrant": pts[-1]["quadrant"],
                "displacement": round(
                    math.hypot(
                        pts[-1]["rs_ratio"] - pts[0]["rs_ratio"],
                        pts[-1]["rs_momentum"] - pts[0]["rs_momentum"],
                    ),
                    2,
                ),
            }
        )

    trajectories.sort(key=lambda t: (-t["displacement"], t["stock_id"]))
    return trajectories


def _collect_holdings_change_events(
    conn,
    etf_code: str,
    dates: list[str],
) -> list[dict]:
    events: list[dict] = []
    for i in range(1, len(dates)):
        prev_date, curr_date = dates[i - 1], dates[i]
        for row in compute_etf_holdings_changes(conn, etf_code, curr_date, prev_date):
            if row["action"] == "不变":
                continue
            events.append(
                {
                    "change_date": curr_date,
                    "prev_date": prev_date,
                    "stock_id": row["stock_id"],
                    "stock_name": normalize_stock_name(row["stock_name"] or ""),
                    "action": row["action"],
                    "shares_prev": row["shares_prev"],
                    "shares_curr": row["shares_curr"],
                    "share_delta": row["share_delta"],
                    "weight_pct_prev": row["weight_pct_prev"],
                    "weight_pct_curr": row["weight_pct_curr"],
                    "weight_delta": row["weight_delta"],
                }
            )
    return events


def _fmt_shares(value: float | None) -> str:
    if value is None or value != value:
        return "—"
    return f"{int(value):,}"


def _holdings_change_table_html(events: list[dict]) -> str:
    rows = []
    for i, e in enumerate(events, 1):
        color = HOLDINGS_ACTION_COLORS.get(e["action"], "#888")
        wd = e["weight_delta"]
        wd_txt = f"{wd:+.2f}%" if wd is not None and wd == wd else "—"
        sd = e["share_delta"]
        sd_txt = f"{int(sd):+,}" if sd is not None and sd == sd else "—"
        rows.append(
            f"<tr><td>{i}</td><td>{e['change_date'][5:]}</td>"
            f"<td>{e['stock_id']}</td><td>{_xml_escape(e['stock_name'])}</td>"
            f"<td style='color:{color}'>{e['action']}</td>"
            f"<td>{_fmt_shares(e['shares_prev'])}</td><td>{_fmt_shares(e['shares_curr'])}</td>"
            f"<td>{sd_txt}</td><td>{wd_txt}</td></tr>"
        )
    return f"""
<table id="holdings-change-table">
  <thead><tr>
    <th>#</th><th>變動日</th><th>代號</th><th>名稱</th><th>動作</th>
    <th>股數(前)</th><th>股數(後)</th><th>股數Δ</th><th>權重Δ</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def _axis_bounds(points: list[dict], *, pad: float = 1.5) -> tuple[float, float, float, float]:
    xs = [p["rs_ratio"] for p in points]
    ys = [p["rs_momentum"] for p in points]
    xmin = min(min(xs), 100.0) - pad
    xmax = max(max(xs), 100.0) + pad
    ymin = min(min(ys), 100.0) - pad
    ymax = max(max(ys), 100.0) + pad
    return xmin, xmax, ymin, ymax


def _svg_chart(points: list[dict], *, as_of: str, length: int) -> str:
    w, h = 900, 720
    margin = {"l": 56, "r": 24, "t": 36, "b": 52}
    plot_w = w - margin["l"] - margin["r"]
    plot_h = h - margin["t"] - margin["b"]
    xmin, xmax, ymin, ymax = _axis_bounds(points)

    def sx(v: float) -> float:
        return margin["l"] + (v - xmin) / (xmax - xmin) * plot_w

    def sy(v: float) -> float:
        return margin["t"] + plot_h - (v - ymin) / (ymax - ymin) * plot_h

    x100, y100 = sx(100.0), sy(100.0)

    # JdK RRG（RS-Ratio →，RS-Momentum ↑；sy 將高動能映射到圖上方）::
    #   Improving (x<100, y>100)  |  Leading   (x>100, y>100)
    #   -------------------------+-------------------------  y=100
    #   Lagging   (x<100, y<100)  |  Weakening (x>100, y<100)
    #                             x=100
    quad_rects = [
        ("leading", 100.0, 100.0, xmax, ymax),
        ("improving", xmin, 100.0, 100.0, ymax),
        ("weakening", 100.0, ymin, xmax, 100.0),
        ("lagging", xmin, ymin, 100.0, 100.0),
    ]
    rects = []
    for quad, x0, y0, x1, y1 in quad_rects:
        rx = sx(x0)
        ry = sy(y1)
        rw = sx(x1) - sx(x0)
        rh = sy(y0) - sy(y1)
        color = QUADRANT_COLORS[quad]
        rects.append(
            f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
            f'fill="{color}" fill-opacity="0.12" stroke="none"/>'
        )

    ticks = []
    for v in _nice_ticks(xmin, xmax):
        x = sx(v)
        ticks.append(
            f'<line x1="{x:.1f}" y1="{margin["t"]}" x2="{x:.1f}" y2="{margin["t"] + plot_h}" '
            f'stroke="#333" stroke-dasharray="3,4"/>'
        )
        ticks.append(
            f'<text x="{x:.1f}" y="{h - 14}" text-anchor="middle" fill="#888" font-size="11">{v:.0f}</text>'
        )
    for v in _nice_ticks(ymin, ymax):
        y = sy(v)
        ticks.append(
            f'<line x1="{margin["l"]}" y1="{y:.1f}" x2="{margin["l"] + plot_w}" y2="{y:.1f}" '
            f'stroke="#333" stroke-dasharray="3,4"/>'
        )
        ticks.append(
            f'<text x="{margin["l"] - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#888" font-size="11">{v:.0f}</text>'
        )

    dots = []
    for p in points:
        quad = p["quadrant"] or "lagging"
        cx, cy = sx(p["rs_ratio"]), sy(p["rs_momentum"])
        color = QUADRANT_COLORS.get(quad, "#888")
        label = f"{p['stock_id']} {p['stock_name']}".strip()
        dots.append(
            f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}" stroke="#111" '
            f'stroke-width="1" data-id="{p["stock_id"]}" data-name="{p["stock_name"]}" '
            f'data-ratio="{p["rs_ratio"]}" data-mom="{p["rs_momentum"]}" '
            f'data-quad="{quad}" data-label="{label}"/>'
        )

    labels = []
    top = sorted(points, key=lambda p: p["rs_ratio"], reverse=True)[:12]
    for p in top:
        cx, cy = sx(p["rs_ratio"]), sy(p["rs_momentum"])
        labels.append(
            f'<text x="{cx + 7:.1f}" y="{cy + 3:.1f}" fill="#ccc" font-size="10">{p["stock_id"]}</text>'
        )

    quad_tags = [
        (sx((xmin + 100) / 2), sy((100 + ymax) / 2), "improving", "Improving"),
        (sx((100 + xmax) / 2), sy((100 + ymax) / 2), "leading", "Leading"),
        (sx((100 + xmax) / 2), sy((ymin + 100) / 2), "weakening", "Weakening"),
        (sx((xmin + 100) / 2), sy((ymin + 100) / 2), "lagging", "Lagging"),
    ]
    zone_labels = []
    for x, y, quad, text in quad_tags:
        zone_labels.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" fill="{QUADRANT_COLORS[quad]}" '
            f'font-size="13" opacity="0.55">{text}</text>'
        )

    return f"""
<svg id="rrg-chart" viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;display:block">
  <rect width="{w}" height="{h}" fill="#141414"/>
  {''.join(rects)}
  {''.join(ticks)}
  <line x1="{margin['l']}" y1="{y100:.1f}" x2="{margin['l'] + plot_w}" y2="{y100:.1f}" stroke="#666" stroke-width="1.2"/>
  <line x1="{x100:.1f}" y1="{margin['t']}" x2="{x100:.1f}" y2="{margin['t'] + plot_h}" stroke="#666" stroke-width="1.2"/>
  <circle cx="{x100:.1f}" cy="{y100:.1f}" r="4" fill="#fff" stroke="#666"/>
  <text x="{x100:.1f}" y="{y100 - 10:.1f}" text-anchor="middle" fill="#aaa" font-size="10">IX0001</text>
  {''.join(zone_labels)}
  {''.join(dots)}
  {''.join(labels)}
  <text x="{margin['l'] + plot_w / 2:.1f}" y="{h - 2}" text-anchor="middle" fill="#bbb" font-size="12">JdK RS-Ratio →</text>
  <text x="14" y="{margin['t'] + plot_h / 2:.1f}" text-anchor="middle" fill="#bbb" font-size="12"
        transform="rotate(-90 14 {margin['t'] + plot_h / 2:.1f})">JdK RS-Momentum ↑</text>
  <text x="{margin['l']}" y="22" fill="#ddd" font-size="14" font-weight="600">RRG Universe · {as_of} · WMA({length})</text>
  <text x="{margin['l'] + plot_w}" y="22" text-anchor="end" fill="#888" font-size="12">{len(points)} 檔</text>
</svg>"""


def _chart_projection(
    values: list[tuple[float, float]],
    *,
    w: int = 900,
    h: int = 720,
    pad: float = 1.5,
    margin: dict[str, int] | None = None,
) -> dict:
    margin = margin or {"l": 56, "r": 24, "t": 36, "b": 52}
    plot_w = w - margin["l"] - margin["r"]
    plot_h = h - margin["t"] - margin["b"]
    xs = [v[0] for v in values]
    ys = [v[1] for v in values]
    xmin = min(min(xs), 100.0) - pad
    xmax = max(max(xs), 100.0) + pad
    ymin = min(min(ys), 100.0) - pad
    ymax = max(max(ys), 100.0) + pad

    def sx(v: float) -> float:
        return margin["l"] + (v - xmin) / (xmax - xmin) * plot_w

    def sy(v: float) -> float:
        return margin["t"] + plot_h - (v - ymin) / (ymax - ymin) * plot_h

    return {
        "w": w,
        "h": h,
        "margin": margin,
        "plot_w": plot_w,
        "plot_h": plot_h,
        "sx": sx,
        "sy": sy,
        "x100": sx(100.0),
        "y100": sy(100.0),
        "xmin": xmin,
        "xmax": xmax,
        "ymin": ymin,
        "ymax": ymax,
    }


def _quad_background_svg(proj: dict, *, opacity: float = 0.12) -> str:
    sx, sy = proj["sx"], proj["sy"]
    xmin, xmax, ymin, ymax = proj["xmin"], proj["xmax"], proj["ymin"], proj["ymax"]
    quad_rects = [
        ("leading", 100.0, 100.0, xmax, ymax),
        ("improving", xmin, 100.0, 100.0, ymax),
        ("weakening", 100.0, ymin, xmax, 100.0),
        ("lagging", xmin, ymin, 100.0, 100.0),
    ]
    rects = []
    for quad, x0, y0, x1, y1 in quad_rects:
        rx = sx(x0)
        ry = sy(y1)
        rw = sx(x1) - sx(x0)
        rh = sy(y0) - sy(y1)
        color = QUADRANT_COLORS[quad]
        rects.append(
            f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
            f'fill="{color}" fill-opacity="{opacity}" stroke="none"/>'
        )
    return "".join(rects)


def _axis_ticks_svg(proj: dict) -> str:
    margin, h, plot_w, plot_h = proj["margin"], proj["h"], proj["plot_w"], proj["plot_h"]
    sx, sy = proj["sx"], proj["sy"]
    xmin, xmax, ymin, ymax = proj["xmin"], proj["xmax"], proj["ymin"], proj["ymax"]
    ticks = []
    for v in _nice_ticks(xmin, xmax):
        x = sx(v)
        ticks.append(
            f'<line x1="{x:.1f}" y1="{margin["t"]}" x2="{x:.1f}" y2="{margin["t"] + plot_h}" '
            f'stroke="#333" stroke-dasharray="3,4"/>'
        )
        ticks.append(
            f'<text x="{x:.1f}" y="{h - 14}" text-anchor="middle" fill="#888" font-size="11">{v:.0f}</text>'
        )
    for v in _nice_ticks(ymin, ymax):
        y = sy(v)
        ticks.append(
            f'<line x1="{margin["l"]}" y1="{y:.1f}" x2="{margin["l"] + plot_w}" y2="{y:.1f}" '
            f'stroke="#333" stroke-dasharray="3,4"/>'
        )
        ticks.append(
            f'<text x="{margin["l"] - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#888" font-size="11">{v:.0f}</text>'
        )
    return "".join(ticks)


def _svg_trajectory_chart(
    trajectories: list[dict],
    *,
    dates: list[str],
    length: int,
    chart_id: str = "rrg-traj-chart",
    title: str | None = None,
    show_name_labels: bool = False,
    show_daily_pct: bool = False,
    point_annotations: dict[tuple[str, str], str] | None = None,
    highlight_ids: frozenset[str] | None = None,
    w: int = TRAJ_CHART_W,
    h: int = TRAJ_CHART_H,
    margin: dict[str, int] | None = None,
) -> str:
    flat = [(p["rs_ratio"], p["rs_momentum"]) for t in trajectories for p in t["points"]]
    if not flat:
        return '<p style="color:#888;font-size:13px;padding:12px 0">無符合條件的軌跡</p>'
    proj = _chart_projection(flat, w=w, h=h, margin=margin or TRAJ_CHART_MARGIN)
    w, h = proj["w"], proj["h"]
    margin, plot_w, plot_h = proj["margin"], proj["plot_w"], proj["plot_h"]
    sx, sy, x100, y100 = proj["sx"], proj["sy"], proj["x100"], proj["y100"]
    xmin, xmax, ymin, ymax = proj["xmin"], proj["xmax"], proj["ymin"], proj["ymax"]

    groups = []
    ordered = sorted(
        trajectories,
        key=lambda t: (0 if highlight_ids and t["stock_id"] in highlight_ids else 1, t["stock_id"]),
    )
    for t in ordered:
        sid = t["stock_id"]
        is_bg = highlight_ids is not None and sid not in highlight_ids
        is_hi = highlight_ids is not None and sid in highlight_ids
        end_quad = t["end_quadrant"] or "lagging"
        color = QUADRANT_COLORS.get(end_quad, "#888")
        coords = [(sx(p["rs_ratio"]), sy(p["rs_momentum"])) for p in t["points"]]
        line_opacity = "0.10" if is_bg else "0.26"
        segments = []
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            segments.append(
                f'<line class="traj-seg" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{color}" stroke-width="0.9" opacity="{line_opacity}" stroke-linecap="round"/>'
            )
        markers = []
        label_svg = ""
        if not is_bg:
            for i, (p, (cx, cy)) in enumerate(zip(t["points"], coords)):
                dc = DAY_COLORS[i % len(DAY_COLORS)]
                is_end = i == len(coords) - 1
                is_start = i == 0
                r = 3.0 if is_end else 2.0
                fill = color if is_end else dc
                stroke = "#fff" if is_end else "#111"
                sw = 1.0 if is_end else 0.5
                pct_txt = _format_daily_pct(p.get("daily_pct"))
                label = (
                    f"{p['date'][5:]} · RS {p['rs_ratio']} · Mom {p['rs_momentum']} · {pct_txt}"
                )
                if is_start:
                    markers.append(
                        f'<circle class="traj-dot" cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" '
                        f'fill="none" stroke="{dc}" stroke-width="1.0" data-step="{i}" '
                        f'data-date="{p["date"]}" data-label="{label}"/>'
                    )
                else:
                    markers.append(
                        f'<circle class="traj-dot" cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" '
                        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" data-step="{i}" '
                        f'data-date="{p["date"]}" data-label="{label}"/>'
                    )
                if show_daily_pct:
                    pct_color = _daily_pct_color(p.get("daily_pct"))
                    markers.append(
                        f'<text class="traj-pct" x="{cx:.1f}" y="{cy - 9:.1f}" text-anchor="middle" '
                        f'fill="{pct_color}" font-size="8" opacity="0.92">{_xml_escape(pct_txt)}</text>'
                    )
                if point_annotations:
                    ann = point_annotations.get((sid, p["date"]))
                    if ann:
                        ac = HOLDINGS_ACTION_COLORS.get(ann, "#ccc")
                        markers.append(
                            f'<text class="traj-action" x="{cx:.1f}" y="{cy + 13:.1f}" text-anchor="middle" '
                            f'fill="{ac}" font-size="8" font-weight="600">{_xml_escape(ann)}</text>'
                        )
            if show_name_labels and coords:
                ex, ey = coords[-1]
                lbl = _xml_escape(_short_label(sid, t["stock_name"]))
                label_svg = (
                    f'<text class="traj-label" x="{ex + 5:.1f}" y="{ey - 3:.1f}" '
                    f'fill="#c8c8c8" font-size="9" opacity="0.88">{lbl}</text>'
                )
        traj_cls = "traj bg" if is_bg else ("traj hi" if is_hi else "traj")
        rotated = "1" if t["start_quadrant"] != t["end_quadrant"] else "0"
        groups.append(
            f'<g class="{traj_cls}" data-id="{sid}" data-name="{_xml_escape(t["stock_name"])}" '
            f'data-end-quad="{end_quad}" data-start-quad="{t["start_quadrant"]}" '
            f'data-rotated="{rotated}" data-disp="{t["displacement"]}">'
            f'{"".join(segments)}{"".join(markers)}{label_svg}</g>'
        )

    quad_tags = [
        (sx((xmin + 100) / 2), sy((100 + ymax) / 2), "improving", "Improving"),
        (sx((100 + xmax) / 2), sy((100 + ymax) / 2), "leading", "Leading"),
        (sx((100 + xmax) / 2), sy((ymin + 100) / 2), "weakening", "Weakening"),
        (sx((xmin + 100) / 2), sy((ymin + 100) / 2), "lagging", "Lagging"),
    ]
    zone_labels = []
    for x, y, quad, text in quad_tags:
        zone_labels.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" fill="{QUADRANT_COLORS[quad]}" '
            f'font-size="11" opacity="0.5">{text}</text>'
        )

    date_range = f"{dates[0][5:]} → {dates[-1][5:]}"
    header = title or f"RRG 輪動軌跡 · {date_range} · WMA({length})"
    return f"""
<svg id="{chart_id}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" style="display:block;min-width:{w}px">
  <rect width="{w}" height="{h}" fill="#141414"/>
  {_quad_background_svg(proj)}
  {_axis_ticks_svg(proj)}
  <line x1="{margin['l']}" y1="{y100:.1f}" x2="{margin['l'] + plot_w}" y2="{y100:.1f}" stroke="#666" stroke-width="1"/>
  <line x1="{x100:.1f}" y1="{margin['t']}" x2="{x100:.1f}" y2="{margin['t'] + plot_h}" stroke="#666" stroke-width="1"/>
  <circle cx="{x100:.1f}" cy="{y100:.1f}" r="3" fill="#fff" stroke="#666"/>
  <text x="{x100:.1f}" y="{y100 - 10:.1f}" text-anchor="middle" fill="#aaa" font-size="10">IX0001</text>
  {''.join(zone_labels)}
  {''.join(groups)}
  <text x="{margin['l'] + plot_w / 2:.1f}" y="{h - 2}" text-anchor="middle" fill="#bbb" font-size="11">JdK RS-Ratio →</text>
  <text x="14" y="{margin['t'] + plot_h / 2:.1f}" text-anchor="middle" fill="#bbb" font-size="11"
        transform="rotate(-90 14 {margin['t'] + plot_h / 2:.1f})">JdK RS-Momentum ↑</text>
  <text x="{margin['l']}" y="28" fill="#ddd" font-size="13" font-weight="600">{_xml_escape(header)}</text>
  <text x="{margin['l'] + plot_w}" y="28" text-anchor="end" fill="#888" font-size="11">{len(trajectories)} 檔</text>
</svg>"""


def _nice_ticks(lo: float, hi: float) -> list[float]:
    step = 2.0
    start = math.floor(lo / step) * step
    out = []
    v = start
    while v <= hi + 0.01:
        if lo <= v <= hi or abs(v - 100) < 0.01:
            out.append(v)
        v += step
    if 100.0 not in out and lo <= 100 <= hi:
        out.append(100.0)
    return sorted(set(out))


def _table_html(points: list[dict]) -> str:
    rows = []
    for i, p in enumerate(points, 1):
        quad = p["quadrant"] or "—"
        color = QUADRANT_COLORS.get(quad, "#888")
        rows.append(
            f"<tr data-quad='{quad}'><td>{i}</td><td>{p['stock_id']}</td>"
            f"<td>{p['stock_name']}</td>"
            f"<td style='color:{color}'>{QUADRANT_LABEL_ZH.get(quad, quad)}</td>"
            f"<td>{p['rs_ratio']}</td><td>{p['rs_momentum']}</td>"
            f"<td>{p['etf_hold_count']}</td></tr>"
        )
    return f"""
<table id="rrg-table">
  <thead><tr>
    <th>#</th><th>代號</th><th>名稱</th><th>象限</th>
    <th>RS-Ratio</th><th>RS-Mom</th><th>ETF數</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def _trajectory_table_html(trajectories: list[dict]) -> str:
    rows = []
    for i, t in enumerate(trajectories, 1):
        start_q = t["start_quadrant"] or "—"
        end_q = t["end_quadrant"] or "—"
        sc = QUADRANT_COLORS.get(start_q, "#888")
        ec = QUADRANT_COLORS.get(end_q, "#888")
        path = " → ".join(p["date"][5:] for p in t["points"])
        rows.append(
            f"<tr data-id='{t['stock_id']}' data-end-quad='{end_q}' data-rotated='"
            f"{1 if start_q != end_q else 0}' data-disp='{t['displacement']}'>"
            f"<td>{i}</td><td>{t['stock_id']}</td><td>{t['stock_name']}</td>"
            f"<td style='color:{sc}'>{QUADRANT_LABEL_ZH.get(start_q, start_q)}</td>"
            f"<td style='color:{ec}'>{QUADRANT_LABEL_ZH.get(end_q, end_q)}</td>"
            f"<td>{t['displacement']}</td><td>{path}</td>"
            f"<td>{t['etf_hold_count']}</td></tr>"
        )
    return f"""
<table id="rrg-traj-table">
  <thead><tr>
    <th>#</th><th>代號</th><th>名稱</th><th>起點象限</th><th>終點象限</th>
    <th>位移</th><th>路徑</th><th>ETF數</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def render_html(
    *,
    as_of: str,
    points: list[dict],
    length: int,
    etf_codes: tuple[str, ...],
) -> str:
    by_quad = {q: sum(1 for p in points if p["quadrant"] == q) for q in QUADRANT_COLORS}
    legend = "".join(
        f'<span class="legend-item"><i style="background:{QUADRANT_COLORS[q]}"></i>'
        f'{QUADRANT_LABEL_ZH[q]} ({by_quad[q]})</span>'
        for q in ("leading", "weakening", "lagging", "improving")
    )
    payload = json.dumps(points, ensure_ascii=False)
    svg = _svg_chart(points, as_of=as_of, length=length)
    table = _table_html(points)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>RRG Universe · {as_of}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:980px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; overflow-x:auto; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px 18px; margin:12px 0; font-size:13px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
    #tooltip {{
      position:fixed; display:none; pointer-events:none; background:#222; border:1px solid #444;
      border-radius:6px; padding:8px 10px; font-size:12px; line-height:1.45; z-index:9; max-width:240px;
    }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th, td {{ padding:6px 8px; border-bottom:1px solid #2a2a2a; text-align:left; }}
    th {{ color:#aaa; position:sticky; top:0; background:#181818; }}
    tr:hover {{ background:#222; }}
    .dot {{ cursor:pointer; }}
    .dot:hover {{ stroke:#fff; stroke-width:1.5; }}
    .filters {{ margin:8px 0 12px; display:flex; gap:8px; flex-wrap:wrap; }}
    .filters button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:4px 10px; cursor:pointer; font-size:12px;
    }}
    .filters button.active {{ background:#333; color:#fff; border-color:#666; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>RRG 相對輪動圖 · Universe 全檔</h1>
    <p class="sub">
      訊號日 <b>{as_of}</b> · 基準 <b>IX0001</b> · WMA length <b>{length}</b><br/>
      Universe：ETF 持股聯集（{','.join(etf_codes)}）· 共 <b>{len(points)}</b> 檔有 RRG 座標
    </p>
    <div class="legend">{legend}</div>
    <div class="panel">{svg}</div>
    <p style="font-size:12px;color:#777;margin:-8px 0 12px">
      橫軸 RS-Ratio、縱軸 RS-Momentum；中心 (100,100) = 與大盤同步。象限依 Julius de Kempenaer JdK 定義。
    </p>
    <div class="panel">
      <div class="filters" id="quad-filters">
        <button class="active" data-quad="all">全部</button>
        <button data-quad="leading">Leading</button>
        <button data-quad="weakening">Weakening</button>
        <button data-quad="lagging">Lagging</button>
        <button data-quad="improving">Improving</button>
      </div>
      {table}
    </div>
  </div>
  <div id="tooltip"></div>
  <script>
    const POINTS = {payload};
    const tooltip = document.getElementById('tooltip');
    const dots = document.querySelectorAll('.dot');
    dots.forEach(el => {{
      el.addEventListener('mouseenter', () => {{
        tooltip.style.display = 'block';
        tooltip.innerHTML = `<b>${{el.dataset.id}}</b> ${{el.dataset.name}}<br/>`
          + `RS-Ratio: ${{el.dataset.ratio}}<br/>RS-Mom: ${{el.dataset.mom}}<br/>`
          + `${{el.dataset.quad}}`;
      }});
      el.addEventListener('mousemove', e => {{
        tooltip.style.left = (e.clientX + 12) + 'px';
        tooltip.style.top = (e.clientY + 12) + 'px';
      }});
      el.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      el.addEventListener('click', () => {{
        const row = document.querySelector(`#rrg-table tr td:nth-child(2)`);
        document.querySelectorAll('#rrg-table tbody tr').forEach(tr => {{
          tr.style.outline = tr.children[1].textContent === el.dataset.id ? '1px solid #888' : '';
        }});
      }});
    }});
    document.getElementById('quad-filters').addEventListener('click', e => {{
      const btn = e.target.closest('button');
      if (!btn) return;
      document.querySelectorAll('#quad-filters button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const q = btn.dataset.quad;
      document.querySelectorAll('#rrg-table tbody tr').forEach(tr => {{
        tr.style.display = (q === 'all' || tr.dataset.quad === q) ? '' : 'none';
      }});
      dots.forEach(d => {{
        d.style.opacity = (q === 'all' || d.dataset.quad === q) ? '1' : '0.12';
      }});
    }});
  </script>
</body>
</html>"""


def render_trajectory_html(
    *,
    dates: list[str],
    trajectories: list[dict],
    length: int,
    etf_codes: tuple[str, ...],
) -> str:
    end_counts = {q: sum(1 for t in trajectories if t["end_quadrant"] == q) for q in QUADRANT_COLORS}
    rotated = sum(1 for t in trajectories if t["start_quadrant"] != t["end_quadrant"])
    legend = "".join(
        f'<span class="legend-item"><i style="background:{QUADRANT_COLORS[q]}"></i>'
        f'終點 {QUADRANT_LABEL_ZH[q]} ({end_counts[q]})</span>'
        for q in ("leading", "weakening", "lagging", "improving")
    )
    day_legend = "".join(
        f'<span class="legend-item"><i style="background:{DAY_COLORS[i % len(DAY_COLORS)]}"></i>'
        f'{d[5:]}</span>'
        for i, d in enumerate(dates)
    )
    payload = json.dumps(trajectories, ensure_ascii=False)
    dates_json = json.dumps(dates, ensure_ascii=False)
    svg = _svg_trajectory_chart(trajectories, dates=dates, length=length)
    table = _trajectory_table_html(trajectories)
    date_label = f"{dates[0]} → {dates[-1]}"

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>RRG 輪動軌跡 · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{TRAJ_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .panel.chart-panel {{ overflow:auto; padding:8px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px 18px; margin:12px 0; font-size:13px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
    #tooltip {{
      position:fixed; display:none; pointer-events:none; background:#222; border:1px solid #444;
      border-radius:6px; padding:8px 10px; font-size:12px; line-height:1.45; z-index:9; max-width:280px;
    }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th, td {{ padding:6px 8px; border-bottom:1px solid #2a2a2a; text-align:left; }}
    th {{ color:#aaa; position:sticky; top:0; background:#181818; }}
    tr {{ cursor:pointer; }}
    tr:hover {{ background:#222; }}
    tr.active {{ outline:1px solid #888; background:#252525; }}
    .filters {{ margin:8px 0 12px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    .filters button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:4px 10px; cursor:pointer; font-size:12px;
    }}
    .filters button.active {{ background:#333; color:#fff; border-color:#666; }}
    .filters input {{
      background:#222; color:#eee; border:1px solid #444; border-radius:4px; padding:4px 8px; font-size:12px; width:88px;
    }}
    .traj {{ cursor:pointer; }}
    .traj.dim {{ opacity:0.06; }}
    .traj.dim .traj-seg {{ stroke-width:1; }}
    .traj.active {{ opacity:1; }}
    .traj.active .traj-seg {{ stroke-width:2.0; opacity:0.92; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>RRG 輪動軌跡 · Universe 疊圖</h1>
    <p class="sub">
      訊號日 <b>{date_label}</b>（{len(dates)} 交易日）· 基準 <b>IX0001</b> · WMA length <b>{length}</b><br/>
      Universe：ETF 持股聯集（{','.join(etf_codes)}）· <b>{len(trajectories)}</b> 檔有完整軌跡 ·
      跨象限 <b>{rotated}</b> 檔 · 圖表 {TRAJ_CHART_W}×{TRAJ_CHART_H}px（可橫向捲動放大檢視）
    </p>
    <div class="legend">{legend}</div>
    <div class="legend" style="margin-top:-4px">日序：{day_legend} · 空心圓=起點 · 實心=終點（終點象限色）</div>
    <div class="panel chart-panel">{svg}</div>
    <p style="font-size:12px;color:#777;margin:-8px 0 12px">
      每檔依時間順序連線；線色＝終點象限。滑鼠移入表格列或軌跡可高亮單檔路徑。
    </p>
    <div class="panel">
      <div class="filters" id="traj-filters">
        <button class="active" data-filter="all">全部</button>
        <button data-filter="rotated">跨象限</button>
        <button data-filter="leading">終點 Leading</button>
        <button data-filter="weakening">終點 Weakening</button>
        <button data-filter="lagging">終點 Lagging</button>
        <button data-filter="improving">終點 Improving</button>
        <button data-filter="movers">位移 Top30</button>
        <input id="stock-search" placeholder="代號…"/>
      </div>
      {table}
    </div>
  </div>
  <div id="tooltip"></div>
  <script>
    const TRAJECTORIES = {payload};
    const tooltip = document.getElementById('tooltip');
    const groups = Array.from(document.querySelectorAll('.traj'));
    const rows = Array.from(document.querySelectorAll('#rrg-traj-table tbody tr'));
    let activeId = null;
    let currentFilter = 'all';

    function matchFilter(g, filter) {{
      if (filter === 'all') return true;
      if (filter === 'rotated') return g.dataset.rotated === '1';
      if (filter === 'movers') {{
        const top = TRAJECTORIES.slice(0, 30).map(t => t.stock_id);
        return top.includes(g.dataset.id);
      }}
      return g.dataset.endQuad === filter;
    }}

    function applyView() {{
      const q = document.getElementById('stock-search').value.trim();
      groups.forEach(g => {{
        const id = g.dataset.id;
        const filterOk = matchFilter(g, currentFilter);
        const searchOk = !q || id.includes(q);
        const active = activeId && id === activeId;
        g.classList.toggle('dim', !filterOk || !searchOk || (activeId && !active));
        g.classList.toggle('active', !!active);
      }});
      rows.forEach(tr => {{
        const id = tr.dataset.id;
        const g = document.querySelector(`.traj[data-id="${{id}}"]`);
        const filterOk = g ? matchFilter(g, currentFilter) : false;
        const searchOk = !q || id.includes(q);
        tr.style.display = (filterOk && searchOk) ? '' : 'none';
        tr.classList.toggle('active', activeId === id);
      }});
    }}

    function setActive(id) {{
      activeId = activeId === id ? null : id;
      applyView();
    }}

    groups.forEach(g => {{
      g.addEventListener('mouseenter', () => {{
        if (activeId) return;
        tooltip.style.display = 'block';
        const t = TRAJECTORIES.find(x => x.stock_id === g.dataset.id);
        if (!t) return;
        const pts = t.points.map(p => `${{p.date.slice(5)}} (${{p.rs_ratio}}, ${{p.rs_momentum}})`).join('<br/>');
        tooltip.innerHTML = `<b>${{t.stock_id}}</b> ${{t.stock_name}}<br/>位移 ${{t.displacement}}<br/>${{pts}}`;
        g.classList.add('active');
        groups.filter(x => x !== g).forEach(x => x.classList.add('dim'));
      }});
      g.addEventListener('mousemove', e => {{
        tooltip.style.left = (e.clientX + 12) + 'px';
        tooltip.style.top = (e.clientY + 12) + 'px';
      }});
      g.addEventListener('mouseleave', () => {{
        tooltip.style.display = 'none';
        if (!activeId) {{
          g.classList.remove('active');
          groups.forEach(x => x.classList.remove('dim'));
        }}
      }});
      g.addEventListener('click', () => setActive(g.dataset.id));
    }});

    rows.forEach(tr => {{
      tr.addEventListener('mouseenter', () => {{
        if (activeId) return;
        const id = tr.dataset.id;
        groups.forEach(g => {{
          const on = g.dataset.id === id;
          g.classList.toggle('active', on);
          g.classList.toggle('dim', !on);
        }});
      }});
      tr.addEventListener('mouseleave', () => {{
        if (!activeId) groups.forEach(g => {{ g.classList.remove('active', 'dim'); }});
      }});
      tr.addEventListener('click', () => setActive(tr.dataset.id));
    }});

    document.getElementById('traj-filters').addEventListener('click', e => {{
      const btn = e.target.closest('button');
      if (!btn) return;
      document.querySelectorAll('#traj-filters button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentFilter = btn.dataset.filter;
      activeId = null;
      applyView();
    }});
    document.getElementById('stock-search').addEventListener('input', applyView);
    applyView();
  </script>
</body>
</html>"""


def render_trajectory_split_html(
    *,
    dates: list[str],
    trajectories: list[dict],
    length: int,
    etf_codes: tuple[str, ...],
) -> str:
    up_right, down_left, other = _split_trajectories_by_trend(trajectories)
    date_label = f"{dates[0]} → {dates[-1]}"
    date_short = f"{dates[0][5:]} → {dates[-1][5:]}"
    day_legend = "".join(
        f'<span class="legend-item"><i style="background:{DAY_COLORS[i % len(DAY_COLORS)]}"></i>'
        f'{d[5:]}</span>'
        for i, d in enumerate(dates)
    )
    svg_up = _svg_trajectory_chart(
        up_right,
        dates=dates,
        length=length,
        chart_id="rrg-traj-up-right",
        title=f"往右上 · RS-Ratio↑ RS-Momentum↑ · {date_short}",
        show_name_labels=True,
        show_daily_pct=True,
    )
    svg_down = _svg_trajectory_chart(
        down_left,
        dates=dates,
        length=length,
        chart_id="rrg-traj-down-left",
        title=f"往左下 · RS-Ratio↓ RS-Momentum↓ · {date_short}",
        show_name_labels=True,
        show_daily_pct=True,
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>RRG 輪動軌跡 · 右上 / 左下 · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{TRAJ_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    h2 {{ font-size:15px; margin:0 0 8px; color:#ddd; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:20px; }}
    .panel.chart-panel {{ overflow:auto; padding:8px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px 18px; margin:8px 0 12px; font-size:13px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
    .traj:hover .traj-seg {{ stroke-width:1.6; opacity:0.55; }}
    .traj:hover .traj-label {{ fill:#fff; opacity:1; }}
    .traj:hover .traj-pct {{ opacity:1; font-size:9px; }}
    .note {{ font-size:12px; color:#777; margin:4px 0 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>RRG 輪動軌跡 · 趨勢分面</h1>
    <p class="sub">
      訊號日 <b>{date_label}</b>（{len(dates)} 交易日）· 基準 <b>IX0001</b> · WMA length <b>{length}</b><br/>
      Universe：ETF 持股聯集（{','.join(etf_codes)}）·
      往右上 <b>{len(up_right)}</b> 檔 · 往左下 <b>{len(down_left)}</b> 檔 ·
      其他方向 <b>{len(other)}</b> 檔（未列入下圖）
    </p>
    <div class="legend">日序：{day_legend} · 空心圓=起點 · 實心=終點 · 小字=代號+名稱 · 點上方=當日漲跌幅（紅漲綠跌）</div>

    <h2>↗ 趨勢往右上（RS-Ratio ↑ 且 RS-Momentum ↑）</h2>
    <div class="panel chart-panel">{svg_up}</div>
    <p class="note">{len(up_right)} 檔 · 終點標籤顯示於軌跡末端</p>

    <h2>↙ 趨勢往左下（RS-Ratio ↓ 且 RS-Momentum ↓）</h2>
    <div class="panel chart-panel">{svg_down}</div>
    <p class="note">{len(down_left)} 檔 · 終點標籤顯示於軌跡末端</p>
  </div>
</body>
</html>"""


def render_holdings_change_html(
    *,
    etf_code: str,
    dates: list[str],
    events: list[dict],
    all_trajectories: list[dict],
    highlight_ids: set[str],
    length: int,
) -> str:
    date_label = f"{dates[0]} → {dates[-1]}"
    date_short = f"{dates[0][5:]} → {dates[-1][5:]}"
    stock_ids = sorted(highlight_ids)
    point_annotations = {(e["stock_id"], e["change_date"]): e["action"] for e in events}
    action_legend = "".join(
        f'<span class="legend-item"><i style="background:{HOLDINGS_ACTION_COLORS[a]}"></i>{a}</span>'
        for a in ("加码", "减码", "新进", "出清")
        if any(e["action"] == a for e in events)
    )
    day_legend = "".join(
        f'<span class="legend-item"><i style="background:{DAY_COLORS[i % len(DAY_COLORS)]}"></i>'
        f'{d[5:]}</span>'
        for i, d in enumerate(dates)
    )
    svg = _svg_trajectory_chart(
        all_trajectories,
        dates=dates,
        length=length,
        chart_id="rrg-holdings-change",
        title=(
            f"{etf_code} 持股變動 · Universe {len(all_trajectories)} 檔 · "
            f"高亮 {len(highlight_ids)} 檔 · {date_short}"
        ),
        show_name_labels=True,
        show_daily_pct=True,
        point_annotations=point_annotations,
        highlight_ids=frozenset(highlight_ids),
    )
    table = _holdings_change_table_html(events)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>{etf_code} 持股變動 RRG · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{TIMELINE_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    h2 {{ font-size:15px; margin:18px 0 8px; color:#ddd; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .panel.chart-panel {{ overflow:visible; padding:8px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px 18px; margin:8px 0 12px; font-size:13px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th, td {{ padding:6px 8px; border-bottom:1px solid #2a2a2a; text-align:left; }}
    th {{ color:#aaa; position:sticky; top:0; background:#181818; }}
    .note {{ font-size:12px; color:#777; margin:4px 0 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{etf_code} 持股變動 · Universe 全檔定位</h1>
    <p class="sub">
      訊號日 <b>{date_label}</b> · 基準 <b>IX0001</b> · WMA length <b>{length}</b><br/>
      背景 <b>{len(all_trajectories)}</b> 檔 Universe 軌跡（淡色）·
      <b>{etf_code}</b> 期間持股變動 <b>{len(events)}</b> 筆 · 高亮 <b>{len(stock_ids)}</b> 檔
      （{', '.join(stock_ids)}）
    </p>
    <div class="legend">日序：{day_legend} · 動作：{action_legend}</div>
    <div class="legend" style="margin-top:-4px">
      淡色線=Universe 全檔 · 高亮=00981A 變動標的 · 點上方=漲跌幅 · 點下方=持股動作 · 線旁=名稱
    </div>
    <div class="panel chart-panel">{svg}</div>
    <p class="note">與 Universe 全檔 RRG 軌跡同一座標系；高亮 {len(stock_ids)} 檔可對照其在 {len(all_trajectories)} 檔中的相對位置</p>

    <h2>持股變動明細</h2>
    <div class="panel">{table}</div>
  </div>
</body>
</html>"""


def _timeline_projection(
    all_trajectories: list[dict],
    highlight_ids: set[str],
) -> dict:
    """Axis bounds from highlight/active stocks so trajectories fill the plot."""
    hi = [t for t in all_trajectories if t["stock_id"] in highlight_ids]
    source = hi if hi else all_trajectories
    flat = [(p["rs_ratio"], p["rs_momentum"]) for t in source for p in t["points"]]
    if not flat:
        flat = [(100.0, 100.0)]
    return _chart_projection(
        flat,
        w=TIMELINE_CHART_W,
        h=TIMELINE_CHART_H,
        margin=TIMELINE_CHART_MARGIN,
    )


def _projection_meta(proj: dict) -> dict:
    margin = proj["margin"]
    return {
        "w": proj["w"],
        "h": proj["h"],
        "margin": margin,
        "plot_w": proj["plot_w"],
        "plot_h": proj["plot_h"],
        "xmin": proj["xmin"],
        "xmax": proj["xmax"],
        "ymin": proj["ymin"],
        "ymax": proj["ymax"],
        "x100": proj["x100"],
        "y100": proj["y100"],
    }


def _svg_timeline_background(
    proj: dict,
    *,
    title: str,
    subtitle: str,
    quad_opacity: float = TIMELINE_QUAD_OPACITY,
) -> str:
    w, h = proj["w"], proj["h"]
    margin = proj["margin"]
    plot_w, plot_h = proj["plot_w"], proj["plot_h"]
    x100, y100 = proj["x100"], proj["y100"]
    sx, sy = proj["sx"], proj["sy"]
    xmin, xmax, ymin, ymax = proj["xmin"], proj["xmax"], proj["ymin"], proj["ymax"]

    quad_tags = [
        (sx((xmin + 100) / 2), sy((100 + ymax) / 2), "improving", "Improving"),
        (sx((100 + xmax) / 2), sy((100 + ymax) / 2), "leading", "Leading"),
        (sx((100 + xmax) / 2), sy((ymin + 100) / 2), "weakening", "Weakening"),
        (sx((xmin + 100) / 2), sy((ymin + 100) / 2), "lagging", "Lagging"),
    ]
    zone_labels = []
    for x, y, quad, text in quad_tags:
        zone_labels.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" fill="{QUADRANT_COLORS[quad]}" '
            f'font-size="11" opacity="0.5">{text}</text>'
        )

    return f"""
<svg id="rrg-timeline-chart" viewBox="0 0 {w} {h}" width="{w}" height="{h}" style="display:block;width:100%;max-width:{w}px;height:auto">
  <rect width="{w}" height="{h}" fill="#141414"/>
  <g id="chart-bg">
  {_quad_background_svg(proj, opacity=quad_opacity)}
  {_axis_ticks_svg(proj)}
  <line x1="{margin['l']}" y1="{y100:.1f}" x2="{margin['l'] + plot_w}" y2="{y100:.1f}" stroke="#666" stroke-width="1"/>
  <line x1="{x100:.1f}" y1="{margin['t']}" x2="{x100:.1f}" y2="{margin['t'] + plot_h}" stroke="#666" stroke-width="1"/>
  <circle cx="{x100:.1f}" cy="{y100:.1f}" r="3" fill="#fff" stroke="#666"/>
  <text x="{x100:.1f}" y="{y100 - 10:.1f}" text-anchor="middle" fill="#aaa" font-size="10">IX0001</text>
  {''.join(zone_labels)}
  </g>
  <g id="dynamic-layer"></g>
  <text x="{margin['l'] + plot_w / 2:.1f}" y="{h - 2}" text-anchor="middle" fill="#bbb" font-size="11">JdK RS-Ratio →</text>
  <text x="14" y="{margin['t'] + plot_h / 2:.1f}" text-anchor="middle" fill="#bbb" font-size="11"
        transform="rotate(-90 14 {margin['t'] + plot_h / 2:.1f})">JdK RS-Momentum ↑</text>
  <text id="chart-title" x="{margin['l']}" y="28" fill="#ddd" font-size="13" font-weight="600">{_xml_escape(title)}</text>
  <text id="frame-label" x="{margin['l'] + plot_w}" y="28" text-anchor="end" fill="#888" font-size="11">{_xml_escape(subtitle)}</text>
</svg>"""


def render_holdings_change_timeline_html(
    *,
    etf_code: str,
    dates: list[str],
    events: list[dict],
    all_trajectories: list[dict],
    highlight_ids: set[str],
    length: int,
    tail_days: int | None = None,
) -> str:
    date_label = f"{dates[0]} → {dates[-1]}"
    date_short = f"{dates[0][5:]} → {dates[-1][5:]}"
    stock_ids = sorted(highlight_ids)
    point_annotations = {(e["stock_id"], e["change_date"]): e["action"] for e in events}
    action_legend = "".join(
        f'<span class="legend-item"><i style="background:{HOLDINGS_ACTION_COLORS[a]}"></i>{a}</span>'
        for a in ("加码", "减码", "新进", "出清")
        if any(e["action"] == a for e in events)
    )
    if len(dates) <= 12:
        day_legend = "".join(
            f'<span class="legend-item"><i style="background:{DAY_COLORS[i % len(DAY_COLORS)]}"></i>'
            f'{d[5:]}</span>'
            for i, d in enumerate(dates)
        )
    else:
        day_legend = (
            f'<span class="legend-item">{len(dates)} 交易日 · '
            f'{dates[0][5:]} → {dates[-1][5:]} · 日序色 5 色循環</span>'
        )

    flat = [(p["rs_ratio"], p["rs_momentum"]) for t in all_trajectories for p in t["points"]]
    proj = _timeline_projection(all_trajectories, highlight_ids)
    title = (
        f"{etf_code} 持股變動 · 互動時間軸 · Universe {len(all_trajectories)} 檔 · "
        f"高亮 {len(highlight_ids)} 檔 · {date_short}"
    )
    svg = _svg_timeline_background(proj, title=title, subtitle=dates[0])
    table = _holdings_change_table_html(events)

    annotations_json = json.dumps(
        {f"{sid}|{d}": a for (sid, d), a in point_annotations.items()},
        ensure_ascii=False,
    )
    payload = json.dumps(all_trajectories, ensure_ascii=False)
    dates_json = json.dumps(dates, ensure_ascii=False)
    highlight_json = json.dumps(sorted(highlight_ids), ensure_ascii=False)
    proj_json = json.dumps(_projection_meta(proj))
    quad_colors_json = json.dumps(QUADRANT_COLORS)
    day_colors_json = json.dumps(list(DAY_COLORS))
    action_colors_json = json.dumps(HOLDINGS_ACTION_COLORS, ensure_ascii=False)
    change_events_json = json.dumps(
        [
            {
                "stock_id": e["stock_id"],
                "change_date": e["change_date"],
                "action": e["action"],
                "stock_name": e.get("stock_name") or "",
            }
            for e in events
        ],
        ensure_ascii=False,
    )
    quad_labels_json = json.dumps(QUADRANT_LABEL_ZH, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>{etf_code} 持股變動 RRG 時間軸 · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{TIMELINE_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    h2 {{ font-size:15px; margin:18px 0 8px; color:#ddd; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .panel.chart-panel {{ overflow:visible; padding:8px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px 18px; margin:8px 0 12px; font-size:13px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
    .timeline-controls {{
      display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; margin:12px 0 8px; font-size:13px;
    }}
    .timeline-controls input[type=range] {{ flex:1; min-width:180px; accent-color:#888; }}
    .timeline-controls button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:5px 12px; cursor:pointer; font-size:12px;
    }}
    .timeline-controls button:hover {{ background:#2a2a2a; color:#fff; }}
    .timeline-controls label {{ color:#aaa; display:flex; align-items:center; gap:6px; }}
    .timeline-controls select {{
      background:#222; color:#eee; border:1px solid #444; border-radius:4px; padding:4px 8px; font-size:12px;
    }}
    #frame-date {{ font-weight:600; color:#ddd; min-width:88px; }}
    .chart-layout {{ display:grid; grid-template-columns:1fr 280px; gap:12px; align-items:start; }}
    @media (max-width:1200px) {{ .chart-layout {{ grid-template-columns:1fr; }} }}
    .frame-insight {{
      background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:12px; font-size:12px; line-height:1.5;
    }}
    .frame-insight h3 {{ margin:0 0 8px; font-size:13px; color:#ddd; }}
    .insight-stat {{ color:#999; margin-bottom:10px; }}
    .insight-stat b {{ color:#e4e4e4; }}
    .insight-chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; min-height:24px; }}
    .chip {{
      display:inline-flex; align-items:center; gap:4px; padding:3px 8px; border-radius:4px;
      background:#252525; border:1px solid #444; font-size:11px; cursor:pointer;
    }}
    .chip:hover {{ border-color:#666; background:#2a2a2a; }}
    .chip .act {{ font-weight:600; }}
    .quad-bars {{ margin-top:8px; }}
    .quad-bar-row {{ display:flex; align-items:center; gap:6px; margin:3px 0; font-size:11px; color:#aaa; }}
    .quad-bar-row span:first-child {{ width:72px; }}
    .quad-bar {{ flex:1; height:6px; background:#2a2a2a; border-radius:3px; overflow:hidden; }}
    .quad-bar i {{ display:block; height:100%; border-radius:3px; }}
    .insight-returns {{ margin-top:4px; font-size:11px; max-height:160px; overflow-y:auto; }}
    .ret-row {{
      display:flex; justify-content:space-between; gap:8px; padding:4px 0; border-bottom:1px solid #252525;
      cursor:pointer; color:#bbb;
    }}
    .ret-row:hover {{ color:#eee; background:#222; }}
    .ret-row .sid {{ color:#ddd; min-width:52px; }}
    .ret-row .nums {{ text-align:right; white-space:nowrap; }}
    details.read-guide {{
      margin-bottom:14px; background:#181818; border:1px solid #333; border-radius:8px; padding:10px 14px;
      font-size:13px; color:#aaa; line-height:1.55;
    }}
    details.read-guide summary {{ cursor:pointer; color:#ccc; font-weight:600; }}
    details.read-guide ul {{ margin:8px 0 0; padding-left:18px; }}
    details.read-guide b {{ color:#ddd; }}
    .slider-wrap {{ flex:1; min-width:180px; position:relative; padding-top:14px; }}
    #slider-marks {{
      position:absolute; top:0; left:0; right:0; height:10px; pointer-events:none;
    }}
    #slider-marks i {{
      position:absolute; width:4px; height:4px; border-radius:50%; background:#D4AF37;
      transform:translateX(-50%); opacity:0.85;
    }}
    #slider-marks i.dense {{ width:3px; height:3px; opacity:0.45; }}
    .panel.chart-panel {{ position:relative; transition:box-shadow 0.2s; }}
    .panel.chart-panel.flash-day {{
      animation: frame-pulse 0.65s ease-out;
    }}
    @keyframes frame-pulse {{
      0% {{ box-shadow: inset 0 0 0 0 rgba(255,200,80,0); }}
      40% {{ box-shadow: inset 0 0 0 2px rgba(255,200,80,0.28); }}
      100% {{ box-shadow: inset 0 0 0 0 rgba(255,200,80,0); }}
    }}
    @keyframes dot-change-pulse {{
      0%, 100% {{ opacity: 0.25; }}
      50% {{ opacity: 0.7; }}
    }}
    circle.change-pulse {{
      animation: dot-change-pulse 0.5s ease-in-out 2;
    }}
    tr.hi-row.on-frame {{ background:#2a2520; }}
    tr.hi-row.on-frame td:nth-child(2) {{ color:#E8A040; font-weight:600; }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th, td {{ padding:6px 8px; border-bottom:1px solid #2a2a2a; text-align:left; }}
    th {{ color:#aaa; position:sticky; top:0; background:#181818; }}
    tr.hi-row {{ cursor:pointer; }}
    tr.hi-row:hover {{ background:#222; }}
    tr.hi-row.active {{ outline:1px solid #666; background:#252525; }}
    .note {{ font-size:12px; color:#777; margin:4px 0 0; }}
    #tooltip {{
      position:fixed; display:none; pointer-events:none; background:#222; border:1px solid #444;
      border-radius:6px; padding:8px 10px; font-size:12px; line-height:1.45; z-index:9; max-width:280px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{etf_code} 持股變動 · RRG 互動時間軸</h1>
    <p class="sub">
      訊號日 <b>{date_label}</b>（{len(dates)} 交易日 · daily bar）· 基準 <b>IX0001</b> · WMA length <b>{length}</b><br/>
      背景 <b>{len(all_trajectories)}</b> 檔 Universe（當日位置淡色）·
      期間持股變動 <b>{len(events)}</b> 筆 · 涉及 <b>{len(stock_ids)}</b> 檔<br/>
      高亮規則：變動當日起 <b>{TIMELINE_VISIBLE_DAYS}</b> 交易日 · tail 前 <b>{TIMELINE_TAIL_DAYS}</b> 日 ·
      位移 ≤{TIMELINE_TAIL_MIN_DISP:g} 不畫 tail
    </p>
    <div class="legend">日序：{day_legend} · 動作：{action_legend}</div>
    <details class="read-guide">
      <summary>如何閱讀這張圖（30 秒）</summary>
      <ul>
        <li><b>四象限</b>：相對大盤 IX0001 的強弱與動能；順時針 Leading→Weakening→Lagging→Improving 是常見輪動方向。</li>
        <li><b>高亮標的</b>：僅在<b>持股變動當日</b>起 {TIMELINE_VISIBLE_DAYS} 個交易日內出現；變動日會<b>閃爍</b>標記。</li>
        <li><b>Tail</b>：最多前 {TIMELINE_TAIL_DAYS} 日軌跡；位移 ≤{TIMELINE_TAIL_MIN_DISP:g} 不畫線（只留當日點）。</li>
        <li><b>報酬</b>：點上方為<b>當日</b>漲跌幅；標籤 / 右側 <b>Σ</b> 為<b>自本次變動日</b>累計；「期初」為時間軸起日累計。</li>
        <li><b>操作</b>：滑桿上<b>金點</b>=有變動的交易日 · 「⏭ 下一筆變動」跳事件 · 點 chip/列可聚焦 · ←→ 換日 · [ ] 跳變動。</li>
      </ul>
    </details>
    <div class="panel">
      <div class="timeline-controls">
        <button type="button" id="btn-prev" title="上一交易日 (←)">◀</button>
        <button type="button" id="btn-prev-change" title="上一筆持股變動 ([)">⏮ 變動</button>
        <button type="button" id="btn-play" title="逐步播放">▶ 逐步</button>
        <button type="button" id="btn-next-change" title="下一筆持股變動 (])">變動 ⏭</button>
        <button type="button" id="btn-next" title="下一交易日 (→)">▶</button>
        <div class="slider-wrap">
          <div id="slider-marks"></div>
          <input type="range" id="frame-slider" min="0" max="{len(dates) - 1}" value="0" step="1"/>
        </div>
        <span id="frame-date">{dates[0][5:]}</span>
        <label><input type="checkbox" id="show-bg"/> Universe 背景</label>
      </div>
      <div class="chart-layout">
        <div class="panel chart-panel flash-target" id="chart-panel" style="margin:0;padding:8px;border:none">{svg}</div>
        <aside class="frame-insight" id="frame-insight">
          <h3>當日摘要</h3>
          <div class="insight-stat" id="insight-stats">—</div>
          <div class="insight-chips" id="insight-chips"></div>
          <h3 style="margin-top:12px">可見標的象限</h3>
          <div class="quad-bars" id="insight-quads"></div>
          <h3 style="margin-top:12px">累計報酬（可見標的）</h3>
          <div class="insight-returns" id="insight-returns"></div>
        </aside>
      </div>
      <p class="note">有持股變動的交易日：圖表邊框與變動標的會短暫閃爍。預設隱藏 Universe 背景以降低雜訊。</p>
    </div>

    <h2>持股變動明細</h2>
    <div class="panel">{table}</div>
  </div>
  <div id="tooltip"></div>
  <script>
    const DATES = {dates_json};
    const TRAJECTORIES = {payload};
    const HIGHLIGHT_IDS = new Set({highlight_json});
    const ANNOTATIONS = {annotations_json};
    const PROJ = {proj_json};
    const QUAD_COLORS = {quad_colors_json};
    const DAY_COLORS = {day_colors_json};
    const ACTION_COLORS = {action_colors_json};
    const CHANGE_EVENTS = {change_events_json};
    const QUAD_LABEL_ZH = {quad_labels_json};
    const TAIL_DAYS = {TIMELINE_TAIL_DAYS};
    const TAIL_MIN_DISP = {TIMELINE_TAIL_MIN_DISP};
    const VISIBLE_DAYS = {TIMELINE_VISIBLE_DAYS};

    const layer = document.getElementById('dynamic-layer');
    const chartPanel = document.getElementById('chart-panel');
    const slider = document.getElementById('frame-slider');
    const frameDate = document.getElementById('frame-date');
    const frameLabel = document.getElementById('frame-label');
    const showBg = document.getElementById('show-bg');
    const tooltip = document.getElementById('tooltip');
    let frameIdx = 0;
    let playing = false;
    let playTimer = null;
    let focusId = null;

    const DATE_INDEX = Object.fromEntries(DATES.map((d, i) => [d, i]));
    const CHANGES_BY_DATE = {{}};
    const CHANGE_IDX_BY_STOCK = {{}};
    for (const ev of CHANGE_EVENTS) {{
      const i = DATE_INDEX[ev.change_date];
      if (i === undefined) continue;
      if (!CHANGES_BY_DATE[i]) CHANGES_BY_DATE[i] = [];
      CHANGES_BY_DATE[i].push(ev);
      if (!CHANGE_IDX_BY_STOCK[ev.stock_id]) CHANGE_IDX_BY_STOCK[ev.stock_id] = [];
      CHANGE_IDX_BY_STOCK[ev.stock_id].push(i);
    }}
    const CHANGE_DAY_INDICES = Object.keys(CHANGES_BY_DATE).map(Number).sort((a, b) => a - b);

    function sx(v) {{
      const {{ margin, plot_w, xmin, xmax }} = PROJ;
      return margin.l + (v - xmin) / (xmax - xmin) * plot_w;
    }}
    function sy(v) {{
      const {{ margin, plot_h, ymin, ymax }} = PROJ;
      return margin.t + plot_h - (v - ymin) / (ymax - ymin) * plot_h;
    }}

    function fmtPct(pct) {{
      if (pct == null || pct !== pct) return '—';
      const sign = pct >= 0 ? '+' : '';
      return sign + pct.toFixed(1) + '%';
    }}
    function pctColor(pct) {{
      if (pct == null || pct !== pct) return '#888';
      if (pct > 0) return '#F07070';
      if (pct < 0) return '#6BCB94';
      return '#aaa';
    }}
    function shortLabel(id, name) {{
      let n = (name || '').trim();
      if (n.length > 8) n = n.slice(0, 7) + '…';
      return (id + ' ' + n).trim();
    }}

    function activeChangeIdx(stockId, idx) {{
      const starts = CHANGE_IDX_BY_STOCK[stockId];
      if (!starts) return null;
      let best = null;
      for (const s of starts) {{
        if (idx >= s && idx < s + VISIBLE_DAYS && (best === null || s > best)) best = s;
      }}
      return best;
    }}

    function cumReturnPct(points, fromIdx, toIdx) {{
      if (fromIdx == null || toIdx == null || fromIdx < 0 || toIdx >= points.length || toIdx < fromIdx) {{
        return null;
      }}
      const c0 = points[fromIdx].close;
      const c1 = points[toIdx].close;
      if (c0 == null || c1 == null || !c0) return null;
      return ((c1 / c0) - 1) * 100;
    }}

    function tailStart(idx, tailLen) {{
      return Math.max(0, idx - tailLen + 1);
    }}

    function isChangeDay(stockId, idx) {{
      const starts = CHANGE_IDX_BY_STOCK[stockId];
      return starts ? starts.includes(idx) : false;
    }}

    function flashChartIfNeeded(idx) {{
      if (!CHANGES_BY_DATE[idx] || CHANGES_BY_DATE[idx].length === 0) return;
      chartPanel.classList.remove('flash-day');
      void chartPanel.offsetWidth;
      chartPanel.classList.add('flash-day');
    }}

    function updateInsight(idx) {{
      const d = DATES[idx];
      const today = CHANGES_BY_DATE[idx] || [];
      let visible = 0;
      const quadCounts = {{ leading:0, weakening:0, lagging:0, improving:0 }};
      for (const sid of HIGHLIGHT_IDS) {{
        if (!isStockVisible(sid, idx)) continue;
        visible += 1;
        const t = TRAJECTORIES.find(x => x.stock_id === sid);
        if (t && t.points[idx]) {{
          const q = t.points[idx].quadrant;
          if (q && quadCounts[q] !== undefined) quadCounts[q] += 1;
        }}
      }}
      const stats = document.getElementById('insight-stats');
      stats.innerHTML =
        `<b>${{d}}</b> · 第 ${{idx + 1}}/${{DATES.length}} 日<br/>` +
        `可見高亮 <b>${{visible}}</b> 檔` +
        (today.length ? ` · 今日變動 <b style="color:#E8A040">${{today.length}}</b> 筆` : ' · 今日無變動');
      const chips = document.getElementById('insight-chips');
      if (!today.length) {{
        chips.innerHTML = '<span style="color:#666">—</span>';
      }} else {{
        chips.innerHTML = today.map(ev => {{
          const ac = ACTION_COLORS[ev.action] || '#ccc';
          const t = TRAJECTORIES.find(x => x.stock_id === ev.stock_id);
          let cumHtml = '';
          if (t) {{
            const pi = DATE_INDEX[ev.change_date];
            const cum = cumReturnPct(t.points, pi, idx);
            if (cum != null) {{
              cumHtml = ` <span style="color:${{pctColor(cum)}}">Σ${{fmtPct(cum)}}</span>`;
            }}
          }}
          return `<span class="chip" data-id="${{ev.stock_id}}" data-idx="${{idx}}">` +
            `<span class="act" style="color:${{ac}}">${{ev.action}}</span> ${{ev.stock_id}}${{cumHtml}}</span>`;
        }}).join('');
        chips.querySelectorAll('.chip').forEach(el => {{
          el.addEventListener('click', () => {{
            focusId = el.dataset.id;
            document.querySelectorAll('#holdings-change-table tbody tr').forEach(r => r.classList.remove('active'));
            renderFrame(idx);
          }});
        }});
      }}
      const maxQ = Math.max(1, ...Object.values(quadCounts));
      const quads = document.getElementById('insight-quads');
      quads.innerHTML = ['leading','weakening','lagging','improving'].map(q => {{
        const n = quadCounts[q];
        const w = (100 * n / maxQ).toFixed(0);
        const c = QUAD_COLORS[q];
        const label = (QUAD_LABEL_ZH[q] || q).split(' ')[0];
        return `<div class="quad-bar-row"><span>${{label}}</span>` +
          `<div class="quad-bar"><i style="width:${{w}}%;background:${{c}}"></i></div>` +
          `<span>${{n}}</span></div>`;
      }}).join('');

      const retRows = [];
      for (const sid of HIGHLIGHT_IDS) {{
        if (!isStockVisible(sid, idx)) continue;
        const t = TRAJECTORIES.find(x => x.stock_id === sid);
        if (!t || !t.points[idx]) continue;
        const chStart = activeChangeIdx(sid, idx);
        const cumCh = cumReturnPct(t.points, chStart, idx);
        const cumYtd = cumReturnPct(t.points, 0, idx);
        retRows.push({{ sid, cumCh, cumYtd, t }});
      }}
      retRows.sort((a, b) => (b.cumCh ?? -999) - (a.cumCh ?? -999));
      const retEl = document.getElementById('insight-returns');
      if (!retRows.length) {{
        retEl.innerHTML = '<span style="color:#666">—</span>';
      }} else {{
        retEl.innerHTML = retRows.map(r => {{
          const chTxt = r.cumCh != null ? fmtPct(r.cumCh) : '—';
          const ytdTxt = r.cumYtd != null ? fmtPct(r.cumYtd) : '—';
          const chCol = r.cumCh != null ? pctColor(r.cumCh) : '#888';
          const ytdCol = r.cumYtd != null ? pctColor(r.cumYtd) : '#888';
          return `<div class="ret-row" data-id="${{r.sid}}">` +
            `<span class="sid">${{r.sid}}</span>` +
            `<span class="nums"><span style="color:${{chCol}}">Σ${{chTxt}}</span> · ` +
            `<span style="color:${{ytdCol}}">期初${{ytdTxt}}</span></span></div>`;
        }}).join('');
        retEl.querySelectorAll('.ret-row').forEach(el => {{
          el.addEventListener('click', () => {{
            focusId = el.dataset.id;
            renderFrame(idx);
          }});
        }});
      }}
    }}

    function syncTableHighlight(idx) {{
      const d = DATES[idx].slice(5);
      document.querySelectorAll('#holdings-change-table tbody tr').forEach(tr => {{
        const cell = tr.cells[1]?.textContent?.trim();
        tr.classList.toggle('on-frame', cell === d);
      }});
    }}

    function initSliderMarks() {{
      const marks = document.getElementById('slider-marks');
      const dense = CHANGE_DAY_INDICES.length > 40;
      marks.innerHTML = CHANGE_DAY_INDICES.map(i => {{
        const pct = (100 * i / (DATES.length - 1)).toFixed(2);
        return `<i class="${{dense ? 'dense' : ''}}" style="left:${{pct}}%" title="${{DATES[i].slice(5)}}"></i>`;
      }}).join('');
    }}

    function jumpChange(delta) {{
      if (!CHANGE_DAY_INDICES.length) return;
      stopPlay();
      let target;
      if (delta < 0) {{
        target = CHANGE_DAY_INDICES.filter(i => i < frameIdx).pop();
        if (target === undefined) target = CHANGE_DAY_INDICES[0];
      }} else {{
        target = CHANGE_DAY_INDICES.find(i => i > frameIdx);
        if (target === undefined) target = CHANGE_DAY_INDICES[CHANGE_DAY_INDICES.length - 1];
      }}
      renderFrame(target);
    }}

    function isStockVisible(stockId, idx) {{
      if (focusId === stockId) return true;
      const starts = CHANGE_IDX_BY_STOCK[stockId];
      if (!starts) return false;
      return starts.some((s) => idx >= s && idx < s + VISIBLE_DAYS);
    }}

    function tailDisplacement(pts) {{
      if (pts.length < 2) return 0;
      const p0 = pts[0], p1 = pts[pts.length - 1];
      const dr = p1.rs_ratio - p0.rs_ratio;
      const dm = p1.rs_momentum - p0.rs_momentum;
      return Math.hypot(dr, dm);
    }}

    function renderFrame(idx) {{
      frameIdx = idx;
      slider.value = String(idx);
      const d = DATES[idx];
      frameDate.textContent = d.slice(5);
      frameLabel.textContent = d + ' · frame ' + (idx + 1) + '/' + DATES.length;
      const t0 = tailStart(idx, TAIL_DAYS);
      const parts = [];
      flashChartIfNeeded(idx);
      updateInsight(idx);
      syncTableHighlight(idx);

      const ordered = TRAJECTORIES.slice().sort((a, b) => {{
        const ah = HIGHLIGHT_IDS.has(a.stock_id) ? 0 : 1;
        const bh = HIGHLIGHT_IDS.has(b.stock_id) ? 0 : 1;
        if (focusId) {{
          if (a.stock_id === focusId) return -1;
          if (b.stock_id === focusId) return 1;
        }}
        return ah - bh || a.stock_id.localeCompare(b.stock_id);
      }});

      for (const t of ordered) {{
        if (idx >= t.points.length) continue;
        const isHi = HIGHLIGHT_IDS.has(t.stock_id);
        if (!isHi && !showBg.checked) continue;
        if (focusId && t.stock_id !== focusId) continue;
        if (isHi && !isStockVisible(t.stock_id, idx)) continue;

        const pts = t.points.slice(t0, idx + 1);
        const endQuad = t.points[idx].quadrant || 'lagging';
        const color = QUAD_COLORS[endQuad] || '#888';
        const showTail = pts.length >= 2 && tailDisplacement(pts) > TAIL_MIN_DISP;

        if (isHi) {{
          if (showTail) {{
            for (let i = 0; i < pts.length - 1; i++) {{
              const p1 = pts[i], p2 = pts[i + 1];
              const opacity = 0.18 + 0.12 * (i / Math.max(pts.length - 1, 1));
              parts.push(
                `<line x1="${{sx(p1.rs_ratio).toFixed(1)}}" y1="${{sy(p1.rs_momentum).toFixed(1)}}" ` +
                `x2="${{sx(p2.rs_ratio).toFixed(1)}}" y2="${{sy(p2.rs_momentum).toFixed(1)}}" ` +
                `stroke="${{color}}" stroke-width="1.8" opacity="${{opacity.toFixed(2)}}" stroke-linecap="round"/>`
              );
            }}
          }}
          pts.forEach((p, i) => {{
            const globalStep = t0 + i;
            const isCurrent = globalStep === idx;
            if (!isCurrent && !showTail) return;
            const cx = sx(p.rs_ratio), cy = sy(p.rs_momentum);
            const dc = DAY_COLORS[globalStep % DAY_COLORS.length];
            const r = isCurrent ? 5.5 : 3.0;
            const fill = isCurrent ? color : dc;
            const stroke = isCurrent ? '#fff' : '#111';
            const sw = isCurrent ? 1.4 : 0.6;
            const label = `${{p.date.slice(5)}} · RS ${{p.rs_ratio}} · Mom ${{p.rs_momentum}} · 日 ${{fmtPct(p.daily_pct)}}`;
            const changePulse = isCurrent && isChangeDay(t.stock_id, idx);
            let cumTip = '';
            if (isCurrent) {{
              const chStart = activeChangeIdx(t.stock_id, idx);
              const cumCh = cumReturnPct(t.points, chStart, idx);
              const cumYtd = cumReturnPct(t.points, 0, idx);
              if (cumCh != null || cumYtd != null) {{
                cumTip = `自變動 ${{fmtPct(cumCh)}} · 期初 ${{fmtPct(cumYtd)}}`;
              }}
            }}
            if (!isCurrent && showTail && globalStep === t0 && pts.length > 1) {{
              parts.push(
                `<circle class="hi-dot" cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{r}}" ` +
                `fill="none" stroke="${{dc}}" stroke-width="1" data-label="${{label}}" data-id="${{t.stock_id}}"/>`
              );
            }} else {{
              parts.push(
                `<circle class="hi-dot" cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{r}}" ` +
                `fill="${{fill}}" stroke="${{stroke}}" stroke-width="${{sw}}" ` +
                `data-label="${{label}}" data-id="${{t.stock_id}}"` +
                `${{cumTip ? ` data-cum="${{cumTip}}"` : ''}}/>`
              );
            }}
            if (changePulse) {{
              parts.push(
                `<circle cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{(r + 1.8).toFixed(1)}}" fill="none" stroke="#FFD878" ` +
                `stroke-width="0.8" opacity="0.35" class="change-pulse" pointer-events="none"/>`
              );
            }}
            const pct = fmtPct(p.daily_pct);
            parts.push(
              `<text x="${{cx.toFixed(1)}}" y="${{(cy - 10).toFixed(1)}}" text-anchor="middle" ` +
              `fill="${{pctColor(p.daily_pct)}}" font-size="8" opacity="0.92">${{pct}}</text>`
            );
            if (isCurrent) {{
              const chStart = activeChangeIdx(t.stock_id, idx);
              const cumCh = cumReturnPct(t.points, chStart, idx);
              const cumYtd = cumReturnPct(t.points, 0, idx);
              if (cumCh != null) {{
                parts.push(
                  `<text x="${{cx.toFixed(1)}}" y="${{(cy - 20).toFixed(1)}}" text-anchor="middle" ` +
                  `fill="${{pctColor(cumCh)}}" font-size="8" font-weight="600" opacity="0.95">Σ${{fmtPct(cumCh)}}</text>`
                );
              }}
              if (cumYtd != null && chStart !== 0) {{
                parts.push(
                  `<text x="${{cx.toFixed(1)}}" y="${{(cy - 29).toFixed(1)}}" text-anchor="middle" ` +
                  `fill="${{pctColor(cumYtd)}}" font-size="7" opacity="0.75">初${{fmtPct(cumYtd)}}</text>`
                );
              }}
            }}
            const ann = ANNOTATIONS[t.stock_id + '|' + p.date];
            if (ann) {{
              const ac = ACTION_COLORS[ann] || '#ccc';
              parts.push(
                `<text x="${{cx.toFixed(1)}}" y="${{(cy + 14).toFixed(1)}}" text-anchor="middle" ` +
                `fill="${{ac}}" font-size="8" font-weight="600">${{ann}}</text>`
              );
            }}
          }});
          const cur = t.points[idx];
          const ex = sx(cur.rs_ratio), ey = sy(cur.rs_momentum);
          const chStart = activeChangeIdx(t.stock_id, idx);
          const cumCh = cumReturnPct(t.points, chStart, idx);
          const cumSuffix = cumCh != null ? ` · Σ${{fmtPct(cumCh)}}` : '';
          parts.push(
            `<text x="${{(ex + 5).toFixed(1)}}" y="${{(ey - 3).toFixed(1)}}" fill="#c8c8c8" ` +
            `font-size="9" opacity="0.92">${{shortLabel(t.stock_id, t.stock_name)}}${{cumSuffix}}</text>`
          );
        }} else {{
          const p = t.points[idx];
          const cx = sx(p.rs_ratio), cy = sy(p.rs_momentum);
          parts.push(
            `<circle class="bg-dot" cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="1.1" ` +
            `fill="${{color}}" opacity="0.07" data-id="${{t.stock_id}}" ` +
            `data-label="${{t.stock_id}} · RS ${{p.rs_ratio}} · Mom ${{p.rs_momentum}}"/>`
          );
        }}
      }}

      layer.innerHTML = parts.join('');
      bindDotTooltips();
    }}

    function bindDotTooltips() {{
      layer.querySelectorAll('circle').forEach(el => {{
        el.addEventListener('mouseenter', (ev) => {{
          const lbl = el.getAttribute('data-label') || '';
          const id = el.getAttribute('data-id') || '';
          const cumExtra = el.getAttribute('data-cum') || '';
          tooltip.innerHTML = id
            ? `<b>${{id}}</b><br/>${{lbl}}${{cumExtra ? '<br/>' + cumExtra : ''}}`
            : lbl;
          tooltip.style.display = 'block';
          tooltip.style.left = (ev.clientX + 12) + 'px';
          tooltip.style.top = (ev.clientY + 12) + 'px';
        }});
        el.addEventListener('mousemove', (ev) => {{
          tooltip.style.left = (ev.clientX + 12) + 'px';
          tooltip.style.top = (ev.clientY + 12) + 'px';
        }});
        el.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
    }}

    function stopPlay() {{
      playing = false;
      if (playTimer) {{ clearInterval(playTimer); playTimer = null; }}
      document.getElementById('btn-play').textContent = '▶ 逐步';
    }}

    function stepPlay() {{
      if (frameIdx >= DATES.length - 1) {{ stopPlay(); return; }}
      renderFrame(frameIdx + 1);
    }}

    slider.addEventListener('input', () => {{
      stopPlay();
      renderFrame(parseInt(slider.value, 10));
    }});
    document.getElementById('btn-prev').addEventListener('click', () => {{
      stopPlay();
      renderFrame(Math.max(0, frameIdx - 1));
    }});
    document.getElementById('btn-next').addEventListener('click', () => {{
      stopPlay();
      renderFrame(Math.min(DATES.length - 1, frameIdx + 1));
    }});
    document.getElementById('btn-prev-change').addEventListener('click', () => jumpChange(-1));
    document.getElementById('btn-next-change').addEventListener('click', () => jumpChange(1));
    document.addEventListener('keydown', (ev) => {{
      if (ev.target.tagName === 'INPUT') return;
      if (ev.key === 'ArrowLeft') {{ stopPlay(); renderFrame(Math.max(0, frameIdx - 1)); }}
      if (ev.key === 'ArrowRight') {{ stopPlay(); renderFrame(Math.min(DATES.length - 1, frameIdx + 1)); }}
      if (ev.key === '[') jumpChange(-1);
      if (ev.key === ']') jumpChange(1);
      if (ev.key === 'Escape') {{ focusId = null; renderFrame(frameIdx); }}
    }});
    document.getElementById('btn-play').addEventListener('click', () => {{
      if (playing) {{ stopPlay(); return; }}
      if (frameIdx >= DATES.length - 1) renderFrame(0);
      playing = true;
      document.getElementById('btn-play').textContent = '⏸ 暫停';
      playTimer = setInterval(stepPlay, 900);
    }});
    showBg.addEventListener('change', () => renderFrame(frameIdx));

    document.querySelectorAll('#holdings-change-table tbody tr').forEach(tr => {{
      tr.classList.add('hi-row');
      tr.addEventListener('click', () => {{
        const id = tr.cells[2]?.textContent?.trim();
        const dateCell = tr.cells[1]?.textContent?.trim();
        if (!id) return;
        document.querySelectorAll('#holdings-change-table tbody tr').forEach(r => r.classList.remove('active'));
        tr.classList.add('active');
        focusId = focusId === id ? null : id;
        if (dateCell) {{
          const full = DATES.find(d => d.slice(5) === dateCell);
          if (full && DATE_INDEX[full] !== undefined) {{
            renderFrame(DATE_INDEX[full]);
            return;
          }}
        }}
        renderFrame(frameIdx);
      }});
    }});

    initSliderMarks();
    renderFrame(0);
  </script>
</body>
</html>"""


def _build_l1h9_executed_legs(
    conn,
    etf_code: str,
    dates: list[str],
    *,
    n_slots: int = 9,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    hold_trading_days: int = 9,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    from research.backtest.copytrade_backtest import (
        compute_signal_day,
        group_signals_by_date,
        iter_copytrade_signals,
        load_stock_beta_map,
    )

    watch = load_etf_constituent_watchlist(conn, (etf_code,))
    name_by_id = {w["stock_id"]: w.get("stock_name") or "" for w in watch}

    def resolve_stock_name(stock_id: str, raw: str) -> str:
        fixed = normalize_stock_name(raw)
        if any("\u4e00" <= ch <= "\u9fff" for ch in fixed):
            return fixed
        return name_by_id.get(stock_id) or fixed

    window_start, window_end = dates[0], dates[-1]
    signals = iter_copytrade_signals(
        conn, etf_code, window_start=window_start, window_end=window_end
    )
    grouped = group_signals_by_date(signals)
    beta_map, _ = load_stock_beta_map(conn)

    complete_days: list = []
    for signal_date in sorted(grouped):
        dr = compute_signal_day(
            conn,
            signal_date,
            grouped[signal_date],
            capital_ntd=capital_ntd,
            entry_lag_days=0,
            hold_trading_days=hold_trading_days,
            cost_bps=cost_bps,
            entry_price_mode="open",
            beta_map=beta_map,
        )
        if dr.status == "complete":
            complete_days.append(dr)

    slots_state: list[str | None] = [None] * n_slots
    executed_signals: list[dict] = []
    skipped_signals: list[dict] = []
    peak = 0

    for dr in complete_days:
        entry = str(dr.entry_date)
        exit_d = str(dr.exit_date)
        for i in range(n_slots):
            ex = slots_state[i]
            if ex is not None and ex < entry:
                slots_state[i] = None
        free = [i for i, ex in enumerate(slots_state) if ex is None]
        if not free:
            skipped_signals.append(
                {
                    "signal_date": dr.signal_date,
                    "entry_date": entry,
                    "exit_date": exit_d,
                    "n_legs": dr.n_legs,
                    "return_pct": dr.return_pct,
                    "reason": "slots_full",
                }
            )
            continue
        slot_idx = free[0]
        slots_state[slot_idx] = exit_d
        peak = max(peak, n_slots - len(free) + 1)
        leg_stocks = [
            {
                "stock_id": leg.stock_id,
                "stock_name": resolve_stock_name(leg.stock_id, leg.stock_name),
                "action": leg.action,
            }
            for leg in (dr.legs or [])
            if leg.status == "complete"
        ]
        executed_signals.append(
            {
                "signal_date": dr.signal_date,
                "entry_date": entry,
                "exit_date": exit_d,
                "slot_id": slot_idx,
                "n_legs": dr.n_legs,
                "deployed_ntd": dr.deployed_ntd,
                "pnl_ntd": dr.pnl_ntd,
                "return_pct": dr.return_pct,
                "bench_return_pct": dr.bench_return_pct,
                "alpha_ntd": dr.alpha_ntd,
                "leg_stocks": leg_stocks,
                "_legs": dr.legs or [],
            }
        )

    legs_out: list[dict] = []
    for sig in executed_signals:
        for leg in sig["_legs"]:
            if leg.status != "complete":
                continue
            legs_out.append(
                {
                    "leg_id": f"{sig['signal_date']}|{leg.stock_id}",
                    "signal_date": sig["signal_date"],
                    "stock_id": leg.stock_id,
                    "stock_name": resolve_stock_name(leg.stock_id, leg.stock_name),
                    "action": leg.action,
                    "entry_date": leg.entry_date,
                    "exit_date": leg.exit_date,
                    "entry_px": leg.entry_px,
                    "exit_px": leg.exit_px,
                    "allocated_ntd": leg.allocated_ntd,
                    "return_pct": leg.return_pct,
                    "pnl_ntd": leg.pnl_ntd,
                    "slot_id": sig["slot_id"],
                }
            )

    _enrich_l1h9_legs_bench(conn, legs_out)

    n_signals = len(complete_days)
    n_executed = len(executed_signals)
    capture = round(100.0 * n_executed / n_signals, 2) if n_signals else None
    total_capital = n_slots * capital_ntd
    meta = {
        "n_slots": n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": cost_bps,
        "hold_trading_days": hold_trading_days,
        "n_signals": n_signals,
        "n_executed": n_executed,
        "n_skipped": len(skipped_signals),
        "signal_capture_pct": capture,
        "peak_concurrent_slots": peak,
        "strategy_id": "l1h9-copytrade",
        "strategy_title": f"{etf_code} L1H9 多槽跟單",
        "strategy_rule": f"T+1 開盤買入 · 持有 {hold_trading_days} 交易日 · 收盤賣出",
        "strategy_filter": f"{etf_code} 新进/加码",
        "display_code": etf_code,
        "table_mode": "copytrade",
        "entry_price_mode": "open",
    }
    return legs_out, executed_signals, skipped_signals, meta


def _build_rrg_mono_executed_legs(
    conn,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    hold_days: int = 7,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    from research.backtest.finpilot_local_backtest import load_price_panels
    from research.backtest.rrg_mono_backtest import _close_trade, build_fresh_mono_calendar
    from rrg_mono_daily_brief import TOP_N, _backfill_exit_dates, _exit_date_from_entry, _expire_slots

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    fresh_by_date = build_fresh_mono_calendar(conn, dates)

    state: dict = {"slots": [], "history": []}
    executed_signals: list[dict] = []
    skipped_signals: list[dict] = []
    legs_out: list[dict] = []
    peak = 0
    n_skip = 0

    for as_of in dates:
        _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)

        held = {p["stock_id"] for p in state.get("slots", [])}
        used = {int(p["slot"]) for p in state.get("slots", [])}
        free = [i for i in range(n_slots) if i not in used]
        fresh = fresh_by_date.get(as_of, [])

        for row in fresh[:TOP_N]:
            if row.stock_id in held:
                continue
            if not free:
                exit_guess = _exit_date_from_entry(conn, full_dates, as_of, hold_days) or ""
                skipped_signals.append(
                    {
                        "signal_date": as_of,
                        "entry_date": as_of,
                        "exit_date": exit_guess,
                        "stock_id": row.stock_id,
                        "stock_name": row.stock_name,
                        "seg_last": row.seg_last,
                        "n_legs": 1,
                        "return_pct": None,
                        "reason": "slots_full",
                    }
                )
                n_skip += 1
                continue
            exit_d = _exit_date_from_entry(conn, full_dates, as_of, hold_days) or ""
            slot = free.pop(0)
            pos = {
                "slot": slot,
                "stock_id": row.stock_id,
                "stock_name": row.stock_name,
                "entry_date": as_of,
                "exit_date": exit_d,
                "seg_last": round(row.seg_last, 4),
                "disp": round(row.disp, 4),
            }
            if not exit_d:
                pos["exit_pending"] = True
            state.setdefault("slots", []).append(pos)
            held.add(row.stock_id)

            entry_px = (
                float(close.at[as_of, row.stock_id])
                if row.stock_id in close.columns
                else None
            )
            exit_px = None
            trade = _close_trade(conn, close, pos) if exit_d and exit_d <= dates[-1] else None
            if trade:
                ret_pct = trade["return_pct"]
                bench_ret = trade["bench_return_pct"]
                excess = trade["excess_pct"]
            elif exit_d and entry_px:
                last_d = dates[-1]
                if last_d in close.index and row.stock_id in close.columns:
                    c1 = float(close.at[last_d, row.stock_id])
                    ret_pct = (c1 / entry_px - 1.0) * 100.0 if entry_px > 0 else 0.0
                    exit_px = c1 if last_d == exit_d else None
                else:
                    ret_pct = 0.0
                bench_ret = None
                excess = None
            else:
                ret_pct = 0.0
                bench_ret = None
                excess = None

            pnl = capital_ntd * ret_pct / 100.0
            alpha_ntd = capital_ntd * excess / 100.0 if excess is not None else None

            executed_signals.append(
                {
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "exit_date": exit_d,
                    "slot_id": slot,
                    "n_legs": 1,
                    "stock_id": row.stock_id,
                    "stock_name": row.stock_name,
                    "seg_last": row.seg_last,
                    "deployed_ntd": capital_ntd,
                    "pnl_ntd": pnl,
                    "return_pct": ret_pct,
                    "bench_return_pct": bench_ret,
                    "alpha_ntd": alpha_ntd,
                }
            )
            legs_out.append(
                {
                    "leg_id": f"{as_of}|{row.stock_id}",
                    "signal_date": as_of,
                    "stock_id": row.stock_id,
                    "stock_name": row.stock_name,
                    "action": "mono",
                    "entry_date": as_of,
                    "exit_date": exit_d,
                    "entry_px": entry_px,
                    "exit_px": exit_px,
                    "allocated_ntd": capital_ntd,
                    "return_pct": ret_pct,
                    "pnl_ntd": pnl,
                    "slot_id": slot,
                    "seg_last": row.seg_last,
                }
            )
            peak = max(peak, len(state["slots"]))

    _enrich_legs_bench(conn, legs_out, entry_price_mode="close")

    n_executed = len(executed_signals)
    n_signals = n_executed + n_skip
    capture = round(100.0 * n_executed / n_signals, 2) if n_signals else None
    total_capital = n_slots * capital_ntd
    meta = {
        "n_slots": n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": 0.0,
        "hold_trading_days": hold_days,
        "n_signals": n_signals,
        "n_executed": n_executed,
        "n_skipped": n_skip,
        "signal_capture_pct": capture,
        "peak_concurrent_slots": peak,
        "strategy_id": "rrg-mono-hold7",
        "strategy_title": f"RRG mono · seg_last · {n_slots}槽 hold{hold_days}",
        "strategy_rule": "D4 收盤進場 / D11 收盤出場（hold7）",
        "strategy_filter": "mono 濾網 + seg_last 排序 · fresh 訊號",
        "display_code": "RRG mono",
        "table_mode": "mono",
        "entry_price_mode": "close",
    }
    return legs_out, executed_signals, skipped_signals, meta


def _load_bench_closes_for_dates(conn, dates: list[str]) -> list[float | None]:
    from research.backtest.copytrade_backtest import _bench_close

    return [
        float(px) if (px := _bench_close(conn, d)) is not None else None for d in dates
    ]


def _enrich_legs_bench(
    conn, legs: list[dict], *, entry_price_mode: str = "open"
) -> None:
    from research.backtest.copytrade_backtest import _bench_close, _bench_open, bench_return_entry_to_exit

    for leg in legs:
        entry = str(leg["entry_date"])
        exit_d = str(leg["exit_date"])
        if entry_price_mode == "close":
            b0 = _bench_close(conn, entry)
        else:
            b0 = _bench_open(conn, entry)
        bench_ret = bench_return_entry_to_exit(
            conn, entry, exit_d, entry_price_mode=entry_price_mode
        )
        leg["bench_entry_px"] = round(b0, 4) if b0 is not None else None
        leg["bench_return_pct"] = round(bench_ret, 4) if bench_ret is not None else None


def _enrich_l1h9_legs_bench(conn, legs: list[dict]) -> None:
    _enrich_legs_bench(conn, legs, entry_price_mode="open")


def _l1h9_signals_table_html(
    executed: list[dict],
    skipped: list[dict],
    *,
    show_leg_stocks: bool = False,
) -> str:
    rows: list[str] = []
    idx = 1
    for sig in executed:
        ret = sig["return_pct"]
        ret_txt = _format_daily_pct(ret)
        ret_col = _daily_pct_color(ret)
        alpha = sig.get("alpha_ntd")
        alpha_txt = f"{alpha:+,.0f}" if alpha is not None and alpha == alpha else "—"
        alpha_col = _daily_pct_color(alpha) if alpha is not None else "#888"
        rows.append(
            f"<tr class='hi-row exec-row' data-signal='{sig['signal_date']}' "
            f"data-entry='{sig['entry_date']}'>"
            f"<td>{idx}</td><td>{sig['signal_date'][5:]}</td>"
            f"<td>{sig['entry_date'][5:]}</td><td>{sig['exit_date'][5:]}</td>"
            f"<td>槽{sig['slot_id'] + 1}</td><td>{sig['n_legs']}</td>"
            f"<td style='color:{ret_col}'>{ret_txt}</td>"
            f"<td style='color:{alpha_col}'>{alpha_txt}</td>"
            f"<td style='color:#6BCB94'>執行</td></tr>"
        )
        if show_leg_stocks and sig.get("leg_stocks"):
            tags = "".join(
                f"<span class='leg-stock-tag'>{s['stock_id']} "
                f"{_xml_escape(s['stock_name'])}</span>"
                for s in sig["leg_stocks"]
            )
            rows.append(
                f"<tr class='leg-stocks-row' data-signal='{sig['signal_date']}'>"
                f"<td colspan='9'><span class='leg-stocks-label'>股票清單</span>{tags}</td></tr>"
            )
        idx += 1
    for sig in skipped:
        ret = sig.get("return_pct")
        ret_txt = _format_daily_pct(ret)
        ret_col = _daily_pct_color(ret)
        rows.append(
            f"<tr class='skip-row' data-signal='{sig['signal_date']}'>"
            f"<td>{idx}</td><td>{sig['signal_date'][5:]}</td>"
            f"<td>{sig['entry_date'][5:]}</td><td>{sig['exit_date'][5:]}</td>"
            f"<td>—</td><td>{sig['n_legs']}</td>"
            f"<td style='color:{ret_col}'>{ret_txt}</td>"
            f"<td>—</td>"
            f"<td style='color:#888'>略過</td></tr>"
        )
        idx += 1
    return f"""
<table id="l1h9-signals-table">
  <thead><tr>
    <th>#</th><th>訊號日</th><th>進場</th><th>出場</th><th>槽位</th>
    <th>異動檔數</th><th>H9 報酬</th><th>α NTD</th><th>狀態</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def _mono_signals_table_html(
    executed: list[dict],
    skipped: list[dict],
) -> str:
    rows: list[str] = []
    idx = 1
    for sig in executed:
        ret = sig["return_pct"]
        ret_txt = _format_daily_pct(ret)
        ret_col = _daily_pct_color(ret)
        alpha = sig.get("alpha_ntd")
        alpha_txt = f"{alpha:+,.0f}" if alpha is not None and alpha == alpha else "—"
        alpha_col = _daily_pct_color(alpha) if alpha is not None else "#888"
        seg = sig.get("seg_last")
        seg_txt = f"{seg:.3f}" if seg is not None and seg == seg else "—"
        exit_d = sig.get("exit_date") or ""
        rows.append(
            f"<tr class='hi-row exec-row' data-signal='{sig['signal_date']}' "
            f"data-entry='{sig['entry_date']}'>"
            f"<td>{idx}</td><td>{sig['signal_date'][5:]}</td>"
            f"<td>{sig['stock_id']}</td><td>{_xml_escape(sig.get('stock_name') or '')}</td>"
            f"<td>{seg_txt}</td>"
            f"<td>{sig['entry_date'][5:]}</td>"
            f"<td>{exit_d[5:] if exit_d else '—'}</td>"
            f"<td>槽{sig['slot_id'] + 1}</td>"
            f"<td style='color:{ret_col}'>{ret_txt}</td>"
            f"<td style='color:{alpha_col}'>{alpha_txt}</td>"
            f"<td style='color:#6BCB94'>執行</td></tr>"
        )
        idx += 1
    for sig in skipped:
        seg = sig.get("seg_last")
        seg_txt = f"{seg:.3f}" if seg is not None and seg == seg else "—"
        exit_d = sig.get("exit_date") or ""
        rows.append(
            f"<tr class='skip-row' data-signal='{sig['signal_date']}'>"
            f"<td>{idx}</td><td>{sig['signal_date'][5:]}</td>"
            f"<td>{sig.get('stock_id', '—')}</td><td>{_xml_escape(sig.get('stock_name') or '')}</td>"
            f"<td>{seg_txt}</td>"
            f"<td>{sig['entry_date'][5:]}</td>"
            f"<td>{exit_d[5:] if exit_d else '—'}</td>"
            f"<td>—</td><td>—</td><td>—</td>"
            f"<td style='color:#888'>略過</td></tr>"
        )
        idx += 1
    return f"""
<table id="l1h9-signals-table">
  <thead><tr>
    <th>#</th><th>訊號日</th><th>代號</th><th>名稱</th><th>seg_last</th>
    <th>進場</th><th>出場</th><th>槽位</th><th>H7 報酬</th><th>α NTD</th><th>狀態</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""


def _slots_timeline_annotations(
    *,
    table_mode: str,
    n_slots: int,
    capital_ntd: float,
    hold_h: int,
) -> dict[str, str]:
    cap = f"{capital_ntd:,.0f}"
    if table_mode == "mono":
        return {
            "slot_help": (
                f"{n_slots} 個獨立資金池，每則 fresh mono 訊號占 1 槽"
                f"（1 檔 1 leg，每槽 {cap} NTD）。出場日收盤後釋放。"
            ),
            "hold_help": (
                "leg=單一標的一檔持倉（本策略 1 訊號 = 1 檔 = 1 leg）；"
                "槽=資金池。出場日仍算持倉中（收盤才賣）。"
            ),
            "skip_reason": "（槽滿／重複標的）",
            "read_guide": f"""
        <li><b>高亮標的</b>：僅在<b>已執行訊號</b>的進場日～出場日（H{hold_h}）內顯示軌跡。</li>
        <li><b>Σ / Δ</b>：Σ=自進場<b>收盤價</b>累計；<b>Δ</b>=較前一交易日的 leg 損益變化。</li>
        <li><b>今日報酬變動</b>：組合總 PnL 的日環差（已實現結算 + 持倉 MTM 變動）。</li>
        <li><b>vs IX0001 超額 α</b>：各 leg 同期部署資金若買加權指數之損益差；口徑=<b>D4 收盤進、D11 收盤出</b>（hold{hold_h}）。</li>
        <li><b>迷你曲線</b>：金=策略累計 · 灰=IX0001 同期基準 · 看是否跑贏大盤。</li>
        <li><b>槽位占用 N / {n_slots}</b>：目前有 N 則 mono 訊號占用資金槽（每槽 {cap} NTD、<b>1 檔標的</b>）。灰格=空閒 · 藍格=占用中。槽滿時新訊號會<b>略過</b>。</li>
        <li><b>leg</b>：單一標的從進場到出場的一檔持倉；本策略<b>1 訊號 = 1 檔 = 1 leg</b>（非 ETF 跟單籃子）。</li>
        <li><b>出場日</b>：當日仍顯示在持倉中（收盤賣出後才釋放槽位）；圖上仍畫 RRG 軌跡。</li>
        <li><b>槽位</b>：最多 <b>{n_slots}</b> 槽並行；exit 日收盤釋放。滑桿<b>藍點</b>=進場 · <b>金點</b>=出場。</li>
        <li><b>操作</b>：←→ 換日 · [ ] 跳進場 · {{}} 跳出場 · Esc 取消聚焦 · 點表格列跳進場日。</li>""",
        }
    return {
        "slot_help": (
            f"{n_slots} 個獨立資金池，每批跟單訊號占 1 槽（每槽 {cap} NTD）。"
            "出場日收盤後釋放。"
        ),
        "hold_help": (
            "批=一個訊號日跟單籃子；leg=籃子內每檔股票一檔持倉；"
            "檔=不重複股票數。出場日仍算持倉中（收盤才賣）。"
        ),
        "skip_reason": "（槽滿/已持有）",
        "read_guide": f"""
        <li><b>高亮標的</b>：僅在<b>已執行訊號</b>的進場日～出場日（H{hold_h}）內顯示軌跡。</li>
        <li><b>Σ / Δ</b>：Σ=自進場開盤價累計；<b>Δ</b>=較前一交易日的 leg 損益變化。</li>
        <li><b>今日報酬變動</b>：組合總 PnL 的日環差（已實現結算 + 持倉 MTM 變動）。</li>
        <li><b>vs IX0001 超額 α</b>：各 leg 同期部署資金若買加權指數之損益差；口徑=L1 開盤進、同 exit 收盤出。</li>
        <li><b>迷你曲線</b>：金=策略累計 · 灰=IX0001 同期基準 · 看是否跑贏大盤。</li>
        <li><b>槽位占用 N / {n_slots}</b>：目前有 N 批訊號正在占用資金槽（每槽 {cap} NTD）。灰格=空閒 · 藍格=占用中。槽滿時新訊號會<b>略過</b>。</li>
        <li><b>批</b>：一個訊號日執行的一次跟單（整籃 {cap} NTD 等權拆成多 leg）。</li>
        <li><b>leg</b>：批內單一股票的一檔持倉（例：一批 5 檔 = 5 leg）。</li>
        <li><b>檔</b>：不重複股票代號數（同批內每檔各算 1 leg，故 leg 數 ≥ 檔數）。</li>
        <li><b>出場日</b>：當日仍顯示在持倉中（收盤賣出後才釋放槽位）；圖上仍畫 RRG 軌跡。</li>
        <li><b>槽位</b>：exit 日收盤釋放。滑桿<b>藍點</b>=進場 · <b>金點</b>=出場。</li>
        <li><b>操作</b>：←→ 換日 · [ ] 跳進場 · {{}} 跳出場 · Esc 取消聚焦 · 點表格列跳進場日。</li>""",
    }


def render_l1h9_slots_timeline_html(
    *,
    etf_code: str,
    dates: list[str],
    legs: list[dict],
    executed_signals: list[dict],
    skipped_signals: list[dict],
    all_trajectories: list[dict],
    meta: dict,
    bench_closes: list[float | None],
    length: int,
) -> str:
    date_label = f"{dates[0]} → {dates[-1]}"
    date_short = f"{dates[0][5:]} → {dates[-1][5:]}"
    n_slots = int(meta["n_slots"])
    capital_ntd = float(meta["capital_ntd"])
    total_capital = float(meta["total_capital_ntd"])
    hold_h = int(meta["hold_trading_days"])
    highlight_ids = {lg["stock_id"] for lg in legs}
    stock_ids = sorted(highlight_ids)
    display_code = str(meta.get("display_code") or etf_code)
    strategy_title = str(meta.get("strategy_title") or f"{etf_code} L1H9 多槽跟單")
    strategy_rule = str(meta.get("strategy_rule") or "")
    strategy_filter = str(meta.get("strategy_filter") or "")
    table_mode = str(meta.get("table_mode") or "copytrade")
    ann = _slots_timeline_annotations(
        table_mode=table_mode,
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        hold_h=hold_h,
    )

    flat = [(p["rs_ratio"], p["rs_momentum"]) for t in all_trajectories for p in t["points"]]
    proj = _timeline_projection(all_trajectories, highlight_ids)
    title = (
        f"{strategy_title} · RRG 時間軸 · {len(legs)} 檔 · "
        f"{meta['n_executed']}/{meta['n_signals']} 訊號 · {date_short}"
    )
    svg = _svg_timeline_background(proj, title=title, subtitle=dates[0])
    if table_mode == "mono":
        table = _mono_signals_table_html(executed_signals, skipped_signals)
    else:
        table = _l1h9_signals_table_html(
            executed_signals, skipped_signals, show_leg_stocks=True
        )

    legs_json = json.dumps(legs, ensure_ascii=False)
    names_by_id = {lg["stock_id"]: lg["stock_name"] for lg in legs}
    names_json = json.dumps(names_by_id, ensure_ascii=False)
    payload = json.dumps(all_trajectories, ensure_ascii=False)
    dates_json = json.dumps(dates, ensure_ascii=False)
    stock_ids_json = json.dumps(stock_ids, ensure_ascii=False)
    proj_json = json.dumps(_projection_meta(proj))
    quad_colors_json = json.dumps(QUADRANT_COLORS)
    day_colors_json = json.dumps(list(DAY_COLORS))
    action_colors_json = json.dumps(HOLDINGS_ACTION_COLORS, ensure_ascii=False)
    entry_events_json = json.dumps(
        [
            {
                "signal_date": s["signal_date"],
                "entry_date": s["entry_date"],
                "exit_date": s["exit_date"],
                "slot_id": s["slot_id"],
                "n_legs": s["n_legs"],
                "return_pct": s["return_pct"],
                "bench_return_pct": s.get("bench_return_pct"),
                "alpha_ntd": s.get("alpha_ntd"),
                "stock_id": s.get("stock_id"),
                "seg_last": s.get("seg_last"),
            }
            for s in executed_signals
        ],
        ensure_ascii=False,
    )
    bench_close_json = json.dumps(bench_closes)
    quad_labels_json = json.dumps(QUADRANT_LABEL_ZH, ensure_ascii=False)
    meta_json = json.dumps(meta, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>{display_code} 多槽 RRG 時間軸 · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{TIMELINE_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    h2 {{ font-size:15px; margin:18px 0 8px; color:#ddd; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .panel.chart-panel {{ overflow:visible; padding:8px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px 18px; margin:8px 0 12px; font-size:13px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
    .kpi-banner {{
      background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:12px 14px;
      margin-bottom:12px; display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
      gap:12px 16px; align-items:end;
    }}
    .kpi-block .label {{ color:#888; font-size:11px; text-transform:uppercase; letter-spacing:0.04em; }}
    .kpi-block .value {{ font-size:20px; font-weight:700; line-height:1.2; margin-top:2px; }}
    .kpi-block .sub {{ font-size:11px; color:#777; margin-top:2px; }}
    .kpi-block .kpi-hint {{ font-size:10px; color:#666; line-height:1.45; margin-top:4px; max-width:220px; }}
    .kpi-label-row {{ display:flex; align-items:center; gap:4px; }}
    .kpi-help {{
      display:inline-flex; align-items:center; justify-content:center;
      width:14px; height:14px; border-radius:50%; background:#333; color:#999;
      font-size:10px; font-weight:600; cursor:help; flex-shrink:0;
    }}
    .kpi-block.highlight {{ background:#1f1a12; border:1px solid #4a3a20; border-radius:6px; padding:8px 10px; }}
    .kpi-daily .delta {{ font-size:13px; font-weight:600; }}
    .kpi-row2 {{
      grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center;
      padding-top:8px; border-top:1px solid #2a2a2a; font-size:12px; color:#888;
    }}
    .slot-meter {{ display:flex; gap:3px; align-items:center; }}
    .slot-meter i {{
      display:block; width:10px; height:10px; border-radius:2px; background:#2a2a2a; border:1px solid #444;
    }}
    .slot-meter i.on {{ background:#6B8CAE; border-color:#8aaccc; }}
    .sparkline-wrap {{ flex:1; min-width:160px; max-width:320px; }}
    .sparkline-wrap svg {{ display:block; width:100%; height:36px; }}
    .event-pills {{ display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }}
    .event-pill {{
      font-size:10px; padding:2px 7px; border-radius:10px; border:1px solid #444; color:#aaa;
    }}
    .event-pill.entry {{ border-color:#4a6a8a; color:#8ab4d4; }}
    .event-pill.exit {{ border-color:#8a6a4a; color:#d4b48a; }}
    .leg-daily {{ font-size:10px; opacity:0.85; }}
    #slider-marks i.exit-mark {{ background:#E8A040; }}
    .timeline-controls {{
      display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; margin:12px 0 8px; font-size:13px;
    }}
    .timeline-controls input[type=range] {{ flex:1; min-width:180px; accent-color:#888; }}
    .timeline-controls button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:5px 12px; cursor:pointer; font-size:12px;
    }}
    .timeline-controls button:hover {{ background:#2a2a2a; color:#fff; }}
    .timeline-controls label {{ color:#aaa; display:flex; align-items:center; gap:6px; }}
    #frame-date {{ font-weight:600; color:#ddd; min-width:88px; }}
    .chart-layout {{ display:grid; grid-template-columns:1fr 300px; gap:12px; align-items:start; }}
    @media (max-width:1200px) {{ .chart-layout {{ grid-template-columns:1fr; }} }}
    .frame-insight {{
      background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:12px; font-size:12px; line-height:1.5;
    }}
    .frame-insight h3 {{ margin:0 0 8px; font-size:13px; color:#ddd; }}
    .insight-stat {{ color:#999; margin-bottom:10px; }}
    .insight-stat b {{ color:#e4e4e4; }}
    .insight-chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; min-height:24px; }}
    .chip {{
      display:inline-flex; align-items:center; gap:4px; padding:3px 8px; border-radius:4px;
      background:#252525; border:1px solid #444; font-size:11px; cursor:pointer;
    }}
    .chip:hover {{ border-color:#666; background:#2a2a2a; }}
    .quad-bars {{ margin-top:8px; }}
    .quad-bar-row {{ display:flex; align-items:center; gap:6px; margin:3px 0; font-size:11px; color:#aaa; }}
    .quad-bar-row span:first-child {{ width:72px; }}
    .quad-bar {{ flex:1; height:6px; background:#2a2a2a; border-radius:3px; overflow:hidden; }}
    .quad-bar i {{ display:block; height:100%; border-radius:3px; }}
    .insight-returns {{ margin-top:4px; font-size:11px; max-height:200px; overflow-y:auto; }}
    .ret-row {{
      display:flex; justify-content:space-between; gap:8px; padding:4px 0; border-bottom:1px solid #252525;
      cursor:pointer; color:#bbb;
    }}
    .ret-row:hover {{ color:#eee; background:#222; }}
    .ret-row .sid {{ color:#ddd; min-width:52px; }}
    .ret-row .nums {{ text-align:right; white-space:nowrap; }}
    details.read-guide {{
      margin-bottom:14px; background:#181818; border:1px solid #333; border-radius:8px; padding:10px 14px;
      font-size:13px; color:#aaa; line-height:1.55;
    }}
    details.read-guide summary {{ cursor:pointer; color:#ccc; font-weight:600; }}
    details.read-guide ul {{ margin:8px 0 0; padding-left:18px; }}
    .slider-wrap {{ flex:1; min-width:180px; position:relative; padding-top:14px; }}
    #slider-marks {{ position:absolute; top:0; left:0; right:0; height:10px; pointer-events:none; }}
    #slider-marks i {{
      position:absolute; width:4px; height:4px; border-radius:50%; background:#6B8CAE;
      transform:translateX(-50%); opacity:0.85;
    }}
    .panel.chart-panel.flash-day {{ animation: frame-pulse 0.65s ease-out; }}
    @keyframes frame-pulse {{
      0% {{ box-shadow: inset 0 0 0 0 rgba(100,180,255,0); }}
      40% {{ box-shadow: inset 0 0 0 2px rgba(100,180,255,0.28); }}
      100% {{ box-shadow: inset 0 0 0 0 rgba(100,180,255,0); }}
    }}
    @keyframes dot-entry-pulse {{
      0%, 100% {{ opacity: 0.25; }}
      50% {{ opacity: 0.7; }}
    }}
    circle.entry-pulse {{ animation: dot-entry-pulse 0.5s ease-in-out 2; }}
    tr.hi-row {{ cursor:pointer; }}
    tr.hi-row:hover {{ background:#222; }}
    tr.hi-row.active {{ outline:1px solid #666; background:#252525; }}
    tr.hi-row.on-frame {{ background:#1a2228; }}
    tr.skip-row {{ color:#666; }}
    tr.leg-stocks-row td {{
      font-size:11px; color:#999; padding:4px 8px 8px 28px; line-height:1.55;
      border-bottom:1px solid #2a2a2a;
    }}
    .leg-stocks-label {{ color:#777; margin-right:8px; }}
    .leg-stock-tag {{
      display:inline-block; margin:2px 6px 2px 0; padding:2px 7px;
      background:#252525; border:1px solid #333; border-radius:3px; color:#ccc;
    }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th, td {{ padding:6px 8px; border-bottom:1px solid #2a2a2a; text-align:left; }}
    th {{ color:#aaa; position:sticky; top:0; background:#181818; }}
    .note {{ font-size:12px; color:#777; margin:4px 0 0; }}
    #tooltip {{
      position:fixed; display:none; pointer-events:none; background:#222; border:1px solid #444;
      border-radius:6px; padding:8px 10px; font-size:12px; line-height:1.45; z-index:9; max-width:300px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{strategy_title} · RRG 互動時間軸</h1>
    <p class="sub">
      策略 <b>{strategy_filter}</b> · {strategy_rule}<br/>
      <b>{n_slots}</b> 槽 × {capital_ntd:,.0f} NTD = {total_capital:,.0f} NTD 總本金 · 持有 <b>{hold_h}</b> 交易日<br/>
      期間 <b>{date_label}</b>（{len(dates)} 交易日）· 基準 <b>IX0001</b> · WMA length <b>{length}</b><br/>
      訊號 <b>{meta['n_signals']}</b> · 執行 <b>{meta['n_executed']}</b> · 略過 <b>{meta['n_skipped']}</b>
      {ann['skip_reason']} · 捕捉率 <b>{meta.get('signal_capture_pct') or '—'}%</b>
    </p>
    <div class="kpi-banner" id="kpi-banner">
      <div class="kpi-block highlight">
        <div class="label">組合總累計報酬</div>
        <div class="value" id="total-return-pct">—</div>
        <div class="sub" id="total-return-ntd">—</div>
      </div>
      <div class="kpi-block kpi-daily">
        <div class="label">今日報酬變動</div>
        <div class="value" id="daily-return-pct">—</div>
        <div class="sub delta" id="daily-return-ntd">—</div>
      </div>
      <div class="kpi-block highlight" style="background:#121a1f;border-color:#2a4a5a">
        <div class="label">vs IX0001 超額 α</div>
        <div class="value" id="excess-return-pct">—</div>
        <div class="sub" id="excess-return-ntd">—</div>
        <div class="sub" id="bench-return-pct">大盤 —</div>
      </div>
      <div class="kpi-block kpi-daily">
        <div class="label">今日超額變動</div>
        <div class="value" id="daily-excess-pct">—</div>
        <div class="sub delta" id="daily-excess-ntd">—</div>
      </div>
      <div class="kpi-block">
        <div class="kpi-label-row">
          <div class="label">槽位占用</div>
          <span class="kpi-help" title="{ann['slot_help']}">?</span>
        </div>
        <div class="value" id="slot-count">—</div>
        <div class="slot-meter" id="slot-meter"></div>
        <div class="kpi-hint" id="slot-hint">—</div>
      </div>
      <div class="kpi-block">
        <div class="kpi-label-row">
          <div class="label">持倉 leg · 標的 · 批</div>
          <span class="kpi-help" title="{ann['hold_help']}">?</span>
        </div>
        <div class="value" id="hold-count">—</div>
        <div class="sub" id="hold-batches">—</div>
        <div class="kpi-hint" id="hold-hint">—</div>
      </div>
      <div class="kpi-row2">
        <div class="sparkline-wrap" title="策略累計 vs IX0001（同本金基準）">
          <svg id="equity-sparkline" viewBox="0 0 300 36" preserveAspectRatio="none"></svg>
        </div>
        <span id="period-stats">—</span>
        <span id="today-events-summary">—</span>
      </div>
    </div>
    <details class="read-guide">
      <summary>如何閱讀（{strategy_title}）</summary>
      <ul>{ann['read_guide']}
      </ul>
    </details>
    <div class="panel">
      <div class="timeline-controls">
        <button type="button" id="btn-prev">◀</button>
        <button type="button" id="btn-prev-entry" title="上一批進場 ([)">⏮ 進場</button>
        <button type="button" id="btn-prev-exit" title="上一批出場 ({{)">⏮ 出場</button>
        <button type="button" id="btn-play">▶ 逐步</button>
        <button type="button" id="btn-next-exit" title="下一批出場 (}})">出場 ⏭</button>
        <button type="button" id="btn-next-entry" title="下一批進場 (])">進場 ⏭</button>
        <button type="button" id="btn-next">▶</button>
        <div class="slider-wrap">
          <div id="slider-marks"></div>
          <input type="range" id="frame-slider" min="0" max="{len(dates) - 1}" value="0" step="1"/>
        </div>
        <span id="frame-date">{dates[0][5:]}</span>
        <label><input type="checkbox" id="show-bg"/> Universe 背景</label>
      </div>
      <div class="chart-layout">
        <div class="panel chart-panel flash-target" id="chart-panel" style="margin:0;padding:8px;border:none">{svg}</div>
        <aside class="frame-insight" id="frame-insight">
          <h3>當日摘要</h3>
          <div class="insight-stat" id="insight-stats">—</div>
          <div class="event-pills" id="event-pills"></div>
          <div class="insight-chips" id="insight-chips"></div>
          <div class="insight-stat" id="insight-extremes" style="font-size:11px;margin-top:6px">—</div>
          <h3 style="margin-top:12px">持倉標的象限</h3>
          <div class="quad-bars" id="insight-quads"></div>
          <h3 style="margin-top:12px">Leg 報酬（持倉中 · Σ / Δ日）</h3>
          <div class="insight-returns" id="insight-returns"></div>
        </aside>
      </div>
      <p class="note">進場／出場日圖表邊框短暫閃爍（藍／金）。預設隱藏 Universe 背景。</p>
    </div>
    <h2>訊號執行明細（{display_code}）</h2>
    <div class="panel">{table}</div>
  </div>
  <div id="tooltip"></div>
  <script>
    const DATES = {dates_json};
    const TRAJECTORIES = {payload};
    const LEGS = {legs_json};
    const NAME_BY_ID = {names_json};
    const STOCK_IDS = new Set({stock_ids_json});
    let PROJ = {proj_json};
    const QUAD_COLORS = {quad_colors_json};
    const DAY_COLORS = {day_colors_json};
    const ACTION_COLORS = {action_colors_json};
    const ENTRY_EVENTS = {entry_events_json};
    const QUAD_LABEL_ZH = {quad_labels_json};
    const META = {meta_json};
    const BENCH_CLOSE = {bench_close_json};
    const TOTAL_CAPITAL = {total_capital};
    const N_SLOTS = {n_slots};
    const HOLD_DAYS = {hold_h};
    const TAIL_MIN_DISP = {TIMELINE_TAIL_MIN_DISP};
    const QUAD_OPACITY = {TIMELINE_QUAD_OPACITY};

    const TRAJ_BY_ID = Object.fromEntries(TRAJECTORIES.map(t => [t.stock_id, t]));
    const layer = document.getElementById('dynamic-layer');
    const chartPanel = document.getElementById('chart-panel');
    const slider = document.getElementById('frame-slider');
    const frameDate = document.getElementById('frame-date');
    const frameLabel = document.getElementById('frame-label');
    const showBg = document.getElementById('show-bg');
    const tooltip = document.getElementById('tooltip');
    let frameIdx = 0;
    let playing = false;
    let playTimer = null;
    let focusId = null;

    const DATE_INDEX = Object.fromEntries(DATES.map((d, i) => [d, i]));
    const LEGS_BY_STOCK = {{}};
    for (const lg of LEGS) {{
      if (!LEGS_BY_STOCK[lg.stock_id]) LEGS_BY_STOCK[lg.stock_id] = [];
      LEGS_BY_STOCK[lg.stock_id].push(lg);
    }}
    const ENTRY_IDX_BY_DATE = {{}};
    const EXIT_IDX_BY_DATE = {{}};
    for (const ev of ENTRY_EVENTS) {{
      const ei = DATE_INDEX[ev.entry_date];
      const xi = DATE_INDEX[ev.exit_date];
      if (ei !== undefined) {{
        if (!ENTRY_IDX_BY_DATE[ei]) ENTRY_IDX_BY_DATE[ei] = [];
        ENTRY_IDX_BY_DATE[ei].push(ev);
      }}
      if (xi !== undefined) {{
        if (!EXIT_IDX_BY_DATE[xi]) EXIT_IDX_BY_DATE[xi] = [];
        EXIT_IDX_BY_DATE[xi].push(ev);
      }}
    }}
    const ENTRY_DAY_INDICES = Object.keys(ENTRY_IDX_BY_DATE).map(Number).sort((a, b) => a - b);
    const EXIT_DAY_INDICES = Object.keys(EXIT_IDX_BY_DATE).map(Number).sort((a, b) => a - b);

    function sx(v) {{
      const {{ margin, plot_w, xmin, xmax }} = PROJ;
      return margin.l + (v - xmin) / (xmax - xmin) * plot_w;
    }}
    function sy(v) {{
      const {{ margin, plot_h, ymin, ymax }} = PROJ;
      return margin.t + plot_h - (v - ymin) / (ymax - ymin) * plot_h;
    }}
    function niceTicks(lo, hi) {{
      const step = 2;
      const start = Math.floor(lo / step) * step;
      const out = [];
      for (let v = start; v <= hi + 0.01; v += step) {{
        if ((v >= lo && v <= hi) || Math.abs(v - 100) < 0.01) out.push(v);
      }}
      if (!out.some(v => Math.abs(v - 100) < 0.01) && lo <= 100 && hi >= 100) out.push(100);
      return [...new Set(out)].sort((a, b) => a - b);
    }}
    function computeFrameProjection(pairs) {{
      const pad = 1.5;
      const {{ margin, plot_w, plot_h, w, h }} = PROJ;
      const xs = pairs.map(p => p[0]);
      const ys = pairs.map(p => p[1]);
      const xmin = Math.min(Math.min(...xs), 100) - pad;
      const xmax = Math.max(Math.max(...xs), 100) + pad;
      const ymin = Math.min(Math.min(...ys), 100) - pad;
      const ymax = Math.max(Math.max(...ys), 100) + pad;
      const projSx = v => margin.l + (v - xmin) / (xmax - xmin) * plot_w;
      const projSy = v => margin.t + plot_h - (v - ymin) / (ymax - ymin) * plot_h;
      return {{
        w, h, margin, plot_w, plot_h, xmin, xmax, ymin, ymax,
        x100: projSx(100), y100: projSy(100),
      }};
    }}
    function collectFramePoints(idx) {{
      const pairs = [];
      const addTraj = (t, ei) => {{
        for (let i = ei; i <= idx && i < t.points.length; i++) {{
          const p = t.points[i];
          if (p.rs_ratio != null && p.rs_momentum != null) pairs.push([p.rs_ratio, p.rs_momentum]);
        }}
      }};
      if (focusId) {{
        const t = TRAJ_BY_ID[focusId];
        if (t && isStockVisible(focusId, idx)) {{
          const plg = primaryLegForStock(focusId, idx);
          addTraj(t, plg ? entryIdxForLeg(plg) : 0);
        }}
      }} else {{
        for (const t of TRAJECTORIES) {{
          if (!STOCK_IDS.has(t.stock_id) || !isStockVisible(t.stock_id, idx)) continue;
          const plg = primaryLegForStock(t.stock_id, idx);
          if (!plg) continue;
          addTraj(t, entryIdxForLeg(plg));
        }}
      }}
      return pairs.length ? pairs : [[98, 98], [102, 102]];
    }}
    function renderChartBackground(proj) {{
      const bg = document.getElementById('chart-bg');
      if (!bg) return;
      const {{ margin, plot_w, plot_h, h, xmin, xmax, ymin, ymax, x100, y100 }} = proj;
      const projSx = v => margin.l + (v - xmin) / (xmax - xmin) * plot_w;
      const projSy = v => margin.t + plot_h - (v - ymin) / (ymax - ymin) * plot_h;
      const parts = [];
      const quadRects = [
        ['leading', 100, 100, xmax, ymax],
        ['improving', xmin, 100, 100, ymax],
        ['weakening', 100, ymin, xmax, 100],
        ['lagging', xmin, ymin, 100, 100],
      ];
      for (const [quad, x0, y0, x1, y1] of quadRects) {{
        const rx = projSx(x0);
        const ry = projSy(y1);
        const rw = projSx(x1) - projSx(x0);
        const rh = projSy(y0) - projSy(y1);
        parts.push(
          `<rect x="${{rx.toFixed(1)}}" y="${{ry.toFixed(1)}}" width="${{rw.toFixed(1)}}" height="${{rh.toFixed(1)}}" ` +
          `fill="${{QUAD_COLORS[quad]}}" fill-opacity="${{QUAD_OPACITY}}" stroke="none"/>`
        );
      }}
      for (const v of niceTicks(xmin, xmax)) {{
        const x = projSx(v);
        parts.push(
          `<line x1="${{x.toFixed(1)}}" y1="${{margin.t}}" x2="${{x.toFixed(1)}}" y2="${{margin.t + plot_h}}" ` +
          `stroke="#333" stroke-dasharray="3,4"/>`
        );
        parts.push(
          `<text x="${{x.toFixed(1)}}" y="${{h - 14}}" text-anchor="middle" fill="#888" font-size="11">${{v.toFixed(0)}}</text>`
        );
      }}
      for (const v of niceTicks(ymin, ymax)) {{
        const y = projSy(v);
        parts.push(
          `<line x1="${{margin.l}}" y1="${{y.toFixed(1)}}" x2="${{margin.l + plot_w}}" y2="${{y.toFixed(1)}}" ` +
          `stroke="#333" stroke-dasharray="3,4"/>`
        );
        parts.push(
          `<text x="${{margin.l - 8}}" y="${{(y + 4).toFixed(1)}}" text-anchor="end" fill="#888" font-size="11">${{v.toFixed(0)}}</text>`
        );
      }}
      parts.push(
        `<line x1="${{margin.l}}" y1="${{y100.toFixed(1)}}" x2="${{margin.l + plot_w}}" y2="${{y100.toFixed(1)}}" stroke="#666" stroke-width="1"/>`
      );
      parts.push(
        `<line x1="${{x100.toFixed(1)}}" y1="${{margin.t}}" x2="${{x100.toFixed(1)}}" y2="${{margin.t + plot_h}}" stroke="#666" stroke-width="1"/>`
      );
      parts.push(
        `<circle cx="${{x100.toFixed(1)}}" cy="${{y100.toFixed(1)}}" r="3" fill="#fff" stroke="#666"/>`
      );
      parts.push(
        `<text x="${{x100.toFixed(1)}}" y="${{(y100 - 10).toFixed(1)}}" text-anchor="middle" fill="#aaa" font-size="10">IX0001</text>`
      );
      const zoneLabels = [
        [projSx((xmin + 100) / 2), projSy((100 + ymax) / 2), 'improving', 'Improving'],
        [projSx((100 + xmax) / 2), projSy((100 + ymax) / 2), 'leading', 'Leading'],
        [projSx((100 + xmax) / 2), projSy((ymin + 100) / 2), 'weakening', 'Weakening'],
        [projSx((xmin + 100) / 2), projSy((ymin + 100) / 2), 'lagging', 'Lagging'],
      ];
      for (const [x, y, quad, text] of zoneLabels) {{
        parts.push(
          `<text x="${{x.toFixed(1)}}" y="${{y.toFixed(1)}}" text-anchor="middle" fill="${{QUAD_COLORS[quad]}}" ` +
          `font-size="11" opacity="0.5">${{text}}</text>`
        );
      }}
      bg.innerHTML = parts.join('');
    }}
    function updateFrameProjection(idx) {{
      PROJ = computeFrameProjection(collectFramePoints(idx));
      renderChartBackground(PROJ);
    }}
    function fmtPct(pct) {{
      if (pct == null || pct !== pct) return '—';
      const sign = pct >= 0 ? '+' : '';
      return sign + pct.toFixed(1) + '%';
    }}
    function fmtNtd(n) {{
      if (n == null || n !== n) return '—';
      const sign = n >= 0 ? '+' : '';
      return sign + Math.round(n).toLocaleString();
    }}
    function pctColor(pct) {{
      if (pct == null || pct !== pct) return '#888';
      if (pct > 0) return '#F07070';
      if (pct < 0) return '#6BCB94';
      return '#aaa';
    }}
    function shortLabel(id, name) {{
      let n = (name || '').trim();
      if (n.length > 8) n = n.slice(0, 7) + '…';
      return (id + ' ' + n).trim();
    }}
    function stockLabel(id, fallback) {{
      const name = (TRAJ_BY_ID[id]?.stock_name || NAME_BY_ID[id] || fallback || '').trim();
      return shortLabel(id, name);
    }}

    function isLegActive(leg, idx) {{
      const ei = DATE_INDEX[leg.entry_date];
      const xi = DATE_INDEX[leg.exit_date];
      if (ei === undefined || xi === undefined) return false;
      return idx >= ei && idx <= xi;
    }}

    function legCumPct(leg, idx) {{
      const ei = DATE_INDEX[leg.entry_date];
      const xi = DATE_INDEX[leg.exit_date];
      if (ei === undefined || idx < ei) return null;
      if (idx >= xi) return leg.return_pct;
      const t = TRAJ_BY_ID[leg.stock_id];
      if (!t || !t.points[idx] || !t.points[idx].close || !leg.entry_px) return null;
      return ((t.points[idx].close / leg.entry_px) - 1) * 100;
    }}

    function legPnlNtd(leg, idx) {{
      const pct = legCumPct(leg, idx);
      if (pct == null) return 0;
      return leg.allocated_ntd * pct / 100;
    }}

    function legBenchCumPct(leg, idx) {{
      if (leg.bench_entry_px == null || !leg.bench_entry_px) return null;
      const ei = DATE_INDEX[leg.entry_date];
      const xi = DATE_INDEX[leg.exit_date];
      if (ei === undefined || idx < ei) return null;
      if (idx >= xi) return leg.bench_return_pct ?? null;
      const bc = BENCH_CLOSE[idx];
      if (bc == null) return null;
      return ((bc / leg.bench_entry_px) - 1) * 100;
    }}

    function legBenchPnlNtd(leg, idx) {{
      const pct = legBenchCumPct(leg, idx);
      if (pct == null) return 0;
      return leg.allocated_ntd * pct / 100;
    }}

    function legDailyPnl(leg, idx) {{
      if (idx <= 0) return null;
      const ei = DATE_INDEX[leg.entry_date];
      if (ei === undefined || idx < ei) return null;
      return legPnlNtd(leg, idx) - legPnlNtd(leg, idx - 1);
    }}

    function portfolioDaily(idx) {{
      if (idx <= 0) return {{ dailyPnl: 0, dailyPct: 0, dailyBenchPnl: 0, dailyBenchPct: 0, dailyExcessPnl: 0, dailyExcessPct: 0 }};
      const today = PORTFOLIO_CURVE[idx];
      const prev = PORTFOLIO_CURVE[idx - 1];
      const dailyPnl = today.totalPnl - prev.totalPnl;
      const dailyPct = (today.totalPct ?? 0) - (prev.totalPct ?? 0);
      const dailyBenchPnl = today.benchPnl - prev.benchPnl;
      const dailyBenchPct = (today.benchPct ?? 0) - (prev.benchPct ?? 0);
      return {{
        dailyPnl, dailyPct, dailyBenchPnl, dailyBenchPct,
        dailyExcessPnl: dailyPnl - dailyBenchPnl,
        dailyExcessPct: dailyPct - dailyBenchPct,
      }};
    }}

    function activeBatchesAt(idx) {{
      return ENTRY_EVENTS.filter(ev => {{
        const ei = DATE_INDEX[ev.entry_date];
        const xi = DATE_INDEX[ev.exit_date];
        return ei !== undefined && xi !== undefined && idx >= ei && idx <= xi;
      }});
    }}

    function holdDayLabel(ev, idx) {{
      const ei = DATE_INDEX[ev.entry_date];
      if (ei === undefined) return '';
      const d = idx - ei + 1;
      return `D+${{d}}/${{HOLD_DAYS}}`;
    }}

    function portfolioAtFrame(idx) {{
      let totalPnl = 0;
      let benchPnl = 0;
      let activeLegs = 0;
      let realizedLegs = 0;
      for (const lg of LEGS) {{
        const ei = DATE_INDEX[lg.entry_date];
        const xi = DATE_INDEX[lg.exit_date];
        if (ei === undefined || idx < ei) continue;
        if (idx >= xi) realizedLegs += 1;
        else activeLegs += 1;
        totalPnl += legPnlNtd(lg, idx);
        benchPnl += legBenchPnlNtd(lg, idx);
      }}
      const totalPct = TOTAL_CAPITAL > 0 ? (totalPnl / TOTAL_CAPITAL) * 100 : null;
      const benchPct = TOTAL_CAPITAL > 0 ? (benchPnl / TOTAL_CAPITAL) * 100 : null;
      const excessPnl = totalPnl - benchPnl;
      const excessPct = totalPct != null && benchPct != null ? totalPct - benchPct : null;
      return {{ totalPnl, totalPct, benchPnl, benchPct, excessPnl, excessPct, activeLegs, realizedLegs }};
    }}

    const PORTFOLIO_CURVE = DATES.map((_, i) => portfolioAtFrame(i));
    let PERIOD_MAX_DD = 0;
    let PERIOD_PEAK_PCT = null;
    let PERIOD_FINAL_ALPHA_NTD = 0;
    (function computePeriodStats() {{
      let peak = -Infinity;
      for (const p of PORTFOLIO_CURVE) {{
        if (p.totalPct == null) continue;
        if (p.totalPct > peak) peak = p.totalPct;
        PERIOD_MAX_DD = Math.max(PERIOD_MAX_DD, peak - p.totalPct);
      }}
      PERIOD_PEAK_PCT = peak > -Infinity ? peak : null;
      const last = PORTFOLIO_CURVE[PORTFOLIO_CURVE.length - 1];
      PERIOD_FINAL_ALPHA_NTD = last ? last.excessPnl : 0;
    }})();

    function activeLegsAt(idx) {{
      return LEGS.filter(lg => isLegActive(lg, idx));
    }}

    function isStockVisible(stockId, idx) {{
      if (focusId === stockId) return true;
      const legs = LEGS_BY_STOCK[stockId];
      if (!legs) return false;
      return legs.some(lg => isLegActive(lg, idx));
    }}

    function entryIdxForLeg(leg) {{
      return DATE_INDEX[leg.entry_date];
    }}

    function isEntryDay(stockId, idx) {{
      const legs = LEGS_BY_STOCK[stockId];
      if (!legs) return false;
      return legs.some(lg => DATE_INDEX[lg.entry_date] === idx);
    }}

    function tailDisplacement(pts) {{
      if (pts.length < 2) return 0;
      const p0 = pts[0], p1 = pts[pts.length - 1];
      return Math.hypot(p1.rs_ratio - p0.rs_ratio, p1.rs_momentum - p0.rs_momentum);
    }}

    function flashChartIfNeeded(idx) {{
      if (ENTRY_IDX_BY_DATE[idx] || EXIT_IDX_BY_DATE[idx]) {{
        chartPanel.classList.remove('flash-day');
        void chartPanel.offsetWidth;
        chartPanel.classList.add('flash-day');
      }}
    }}

    function renderSparkline(currentIdx) {{
      const svg = document.getElementById('equity-sparkline');
      if (!svg) return;
      const stratVals = PORTFOLIO_CURVE.map(p => p.totalPct ?? 0);
      const benchVals = PORTFOLIO_CURVE.map(p => p.benchPct ?? 0);
      const allVals = stratVals.concat(benchVals);
      const min = Math.min(...allVals);
      const max = Math.max(...allVals);
      const span = max - min || 1;
      const w = 300;
      const h = 36;
      const pad = 2;
      function toPts(vals) {{
        return vals.map((v, i) => {{
          const x = (i / Math.max(vals.length - 1, 1)) * w;
          const y = pad + (h - 2 * pad) * (1 - (v - min) / span);
          return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
        }}).join(' ');
      }}
      const benchPts = toPts(benchVals);
      const stratPts = toPts(stratVals);
      const curX = (currentIdx / Math.max(stratVals.length - 1, 1)) * w;
      const curY = pad + (h - 2 * pad) * (1 - ((stratVals[currentIdx] ?? 0) - min) / span);
      svg.innerHTML =
        `<polyline points="${{benchPts}}" fill="none" stroke="#555" stroke-width="1.1" opacity="0.85"/>` +
        `<polyline points="${{stratPts.split(' ').slice(0, currentIdx + 1).join(' ')}}" fill="none" stroke="#D4AF37" stroke-width="1.5"/>` +
        `<circle cx="${{curX.toFixed(1)}}" cy="${{curY.toFixed(1)}}" r="3" fill="#D4AF37" stroke="#fff" stroke-width="0.8"/>` +
        `<line x1="${{curX.toFixed(1)}}" y1="0" x2="${{curX.toFixed(1)}}" y2="${{h}}" stroke="#666" stroke-width="0.5" stroke-dasharray="2,2" opacity="0.6"/>`;
    }}

    function updateKpiBanner(idx) {{
      const pf = PORTFOLIO_CURVE[idx];
      const daily = portfolioDaily(idx);
      const batches = activeBatchesAt(idx);
      const active = activeLegsAt(idx);
      const seenStock = new Set(active.map(lg => lg.stock_id));
      const exitsToday = EXIT_IDX_BY_DATE[idx] || [];
      const occupiedSlots = new Set(batches.map(b => b.slot_id));

      document.getElementById('total-return-pct').textContent = fmtPct(pf.totalPct);
      document.getElementById('total-return-pct').style.color = pctColor(pf.totalPct);
      document.getElementById('total-return-ntd').textContent =
        fmtNtd(pf.totalPnl) + ' NTD · 本金 ' + TOTAL_CAPITAL.toLocaleString();

      const dPctEl = document.getElementById('daily-return-pct');
      const dNtdEl = document.getElementById('daily-return-ntd');
      dPctEl.textContent = idx === 0 ? '—' : fmtPct(daily.dailyPct);
      dPctEl.style.color = pctColor(daily.dailyPct);
      dNtdEl.textContent = idx === 0 ? '（首交易日無前日可比）' : fmtNtd(daily.dailyPnl) + ' NTD';
      dNtdEl.style.color = pctColor(daily.dailyPnl);

      const exPctEl = document.getElementById('excess-return-pct');
      const exNtdEl = document.getElementById('excess-return-ntd');
      exPctEl.textContent = fmtPct(pf.excessPct);
      exPctEl.style.color = pctColor(pf.excessPct);
      exNtdEl.textContent = fmtNtd(pf.excessPnl) + ' NTD · α';
      exNtdEl.style.color = pctColor(pf.excessPnl);
      document.getElementById('bench-return-pct').textContent =
        'IX0001 ' + fmtPct(pf.benchPct) + ' · ' + fmtNtd(pf.benchPnl) + ' NTD';

      const dxPctEl = document.getElementById('daily-excess-pct');
      const dxNtdEl = document.getElementById('daily-excess-ntd');
      dxPctEl.textContent = idx === 0 ? '—' : fmtPct(daily.dailyExcessPct);
      dxPctEl.style.color = pctColor(daily.dailyExcessPct);
      dxNtdEl.textContent = idx === 0 ? '—' : fmtNtd(daily.dailyExcessPnl) + ' NTD';
      dxNtdEl.style.color = pctColor(daily.dailyExcessPnl);

      document.getElementById('slot-count').textContent = batches.length + ' / ' + N_SLOTS;
      document.getElementById('slot-count').style.color = batches.length >= N_SLOTS ? '#E8A040' : '#e4e4e4';
      document.getElementById('slot-meter').innerHTML = Array.from({{ length: N_SLOTS }}, (_, i) =>
        `<i class="${{occupiedSlots.has(i) ? 'on' : ''}}" title="槽${{i + 1}}：${{occupiedSlots.has(i) ? '占用中' : '空閒'}}"></i>`
      ).join('');
      const slotHint = document.getElementById('slot-hint');
      if (!batches.length) {{
        slotHint.textContent = '目前無占用 · 全部 ' + N_SLOTS + ' 槽可接新訊號';
      }} else if (batches.length >= N_SLOTS) {{
        slotHint.textContent = '槽位已滿 · 新訊號需等出場釋放';
      }} else {{
        slotHint.textContent = '占用槽 ' + [...occupiedSlots].sort((a,b)=>a-b).map(s => s + 1).join('、') +
          ' · 尚可接 ' + (N_SLOTS - batches.length) + ' 批';
      }}

      document.getElementById('hold-count').textContent = active.length + ' leg · ' + seenStock.size + ' 檔';
      document.getElementById('hold-batches').textContent = batches.length + ' 批持倉中';
      const holdHint = document.getElementById('hold-hint');
      if (!active.length) {{
        holdHint.textContent = '無持倉 leg · 圖上無高亮軌跡';
      }} else if (exitsToday.length) {{
        holdHint.textContent =
          '含今日出場 ' + exitsToday.length + ' 批（收盤賣出 · 盤中仍計入持倉）';
      }} else {{
        holdHint.textContent =
          '每批最多 ' + HOLD_DAYS + ' 交易日 · 本日持倉 leg 平均 D+' +
          Math.round(active.reduce((s, lg) => {{
            const ei = DATE_INDEX[lg.entry_date];
            return s + (ei !== undefined ? idx - ei + 1 : 0);
          }}, 0) / active.length) + ' 日';
      }}

      document.getElementById('period-stats').textContent =
        `區間峰值 ${{fmtPct(PERIOD_PEAK_PCT)}} · 最大回撤 ${{fmtPct(-PERIOD_MAX_DD)}} · ` +
        `期末 α ${{fmtNtd(PERIOD_FINAL_ALPHA_NTD)}} · 已結束 ${{pf.realizedLegs}}/${{LEGS.length}} leg`;

      const entries = ENTRY_IDX_BY_DATE[idx] || [];
      const exits = EXIT_IDX_BY_DATE[idx] || [];
      let evSummary = '';
      if (entries.length) evSummary += `進場 ${{entries.length}} 批 `;
      if (exits.length) evSummary += `出場 ${{exits.length}} 批 `;
      if (!evSummary) evSummary = '今日無進出場';
      document.getElementById('today-events-summary').textContent = evSummary.trim();

      renderSparkline(idx);
    }}

    function updateInsight(idx) {{
      const d = DATES[idx];
      const pf = PORTFOLIO_CURVE[idx];
      const todayEntries = ENTRY_IDX_BY_DATE[idx] || [];
      const todayExits = EXIT_IDX_BY_DATE[idx] || [];
      const active = activeLegsAt(idx);
      const batches = activeBatchesAt(idx);
      const quadCounts = {{ leading:0, weakening:0, lagging:0, improving:0 }};
      const seenStock = new Set();
      for (const lg of active) {{
        if (seenStock.has(lg.stock_id)) continue;
        seenStock.add(lg.stock_id);
        const t = TRAJ_BY_ID[lg.stock_id];
        if (t && t.points[idx]) {{
          const q = t.points[idx].quadrant;
          if (q && quadCounts[q] !== undefined) quadCounts[q] += 1;
        }}
      }}
      document.getElementById('insight-stats').innerHTML =
        `<b>${{d}}</b> · 第 ${{idx + 1}}/${{DATES.length}} 日<br/>` +
        `持倉 <b>${{active.length}}</b> leg · <b>${{seenStock.size}}</b> 檔 · ` +
        `<b>${{batches.length}}</b>/${{N_SLOTS}} 批（槽）<br/>` +
        `<span style="font-size:11px;color:#888">` +
        `leg=每檔股票一檔持倉 · 批=訊號日一籃 · 槽=資金池</span><br/>` +
        `vs IX0001：<b style="color:${{pctColor(pf.excessPct)}}">${{fmtPct(pf.excessPct)}}</b> 超額 · 大盤 ${{fmtPct(pf.benchPct)}}`;

      const pills = document.getElementById('event-pills');
      const pillParts = [];
      todayEntries.forEach(ev => {{
        pillParts.push(`<span class="event-pill entry">進場 槽${{ev.slot_id + 1}} ${{holdDayLabel(ev, idx)}}</span>`);
      }});
      todayExits.forEach(ev => {{
        const alpha = ev.alpha_ntd;
        const alphaTxt = alpha != null ? fmtNtd(alpha) : '—';
        pillParts.push(
          `<span class="event-pill exit">出場 槽${{ev.slot_id + 1}} ${{fmtPct(ev.return_pct)}} · α${{alphaTxt}}</span>`
        );
      }});
      pills.innerHTML = pillParts.length ? pillParts.join('') : '<span style="color:#666;font-size:11px">—</span>';

      const chips = document.getElementById('insight-chips');
      if (!todayEntries.length) {{
        chips.innerHTML = '<span style="color:#666">—</span>';
      }} else {{
        chips.innerHTML = todayEntries.map(ev => {{
          const who = ev.stock_id
            ? `${{ev.stock_id}} · seg ${{(ev.seg_last ?? 0).toFixed(3)}}`
            : `${{ev.n_legs}}檔`;
          return `<span class="chip" data-signal="${{ev.signal_date}}">` +
            `槽${{ev.slot_id + 1}} · ${{who}} · ${{holdDayLabel(ev, idx)}} ` +
            `<span style="color:${{pctColor(ev.return_pct)}}">${{fmtPct(ev.return_pct)}} 最終</span></span>`;
        }}).join('');
      }}

      const dailyLegs = active.map(lg => ({{
        lg,
        daily: legDailyPnl(lg, idx),
        cum: legCumPct(lg, idx),
      }})).filter(r => r.daily != null);
      dailyLegs.sort((a, b) => (b.daily ?? 0) - (a.daily ?? 0));
      const extremes = document.getElementById('insight-extremes');
      if (idx === 0 || !dailyLegs.length) {{
        extremes.innerHTML = '<span style="color:#666">今日 leg 日損益：—</span>';
      }} else {{
        const best = dailyLegs[0];
        const worst = dailyLegs[dailyLegs.length - 1];
        extremes.innerHTML =
          `今日 leg 損益 · 最佳 <b style="color:${{pctColor(best.daily)}}">${{best.lg.stock_id}} ${{fmtNtd(best.daily)}}</b>` +
          (dailyLegs.length > 1
            ? ` · 最差 <b style="color:${{pctColor(worst.daily)}}">${{worst.lg.stock_id}} ${{fmtNtd(worst.daily)}}</b>`
            : '');
      }}

      const maxQ = Math.max(1, ...Object.values(quadCounts));
      document.getElementById('insight-quads').innerHTML =
        ['leading','weakening','lagging','improving'].map(q => {{
          const n = quadCounts[q];
          const w = (100 * n / maxQ).toFixed(0);
          return `<div class="quad-bar-row"><span>${{(QUAD_LABEL_ZH[q]||q).split(' ')[0]}}</span>` +
            `<div class="quad-bar"><i style="width:${{w}}%;background:${{QUAD_COLORS[q]}}"></i></div>` +
            `<span>${{n}}</span></div>`;
        }}).join('');
      const retRows = active.map(lg => ({{
        lg, cum: legCumPct(lg, idx), daily: legDailyPnl(lg, idx),
      }})).sort((a, b) => (b.cum ?? -999) - (a.cum ?? -999));
      const retEl = document.getElementById('insight-returns');
      if (!retRows.length) {{
        retEl.innerHTML = '<span style="color:#666">—</span>';
      }} else {{
        retEl.innerHTML = retRows.map(r => {{
          const col = pctColor(r.cum);
          const dCol = pctColor(r.daily);
          const dailyTxt = idx > 0 && r.daily != null
            ? `<span class="leg-daily" style="color:${{dCol}}">Δ${{fmtNtd(r.daily)}}</span>`
            : '';
          return `<div class="ret-row" data-id="${{r.lg.stock_id}}">` +
            `<span class="sid">${{stockLabel(r.lg.stock_id, r.lg.stock_name)}}` +
            `<span style="color:#666;font-size:10px"> 槽${{r.lg.slot_id+1}}</span></span>` +
            `<span class="nums"><span style="color:${{col}}">Σ${{fmtPct(r.cum)}}</span> ${{dailyTxt}}</span></div>`;
        }}).join('');
        retEl.querySelectorAll('.ret-row').forEach(el => {{
          el.addEventListener('click', () => {{
            focusId = el.dataset.id;
            renderFrame(idx);
          }});
        }});
      }}
      updateKpiBanner(idx);
    }}

    function syncTableHighlight(idx) {{
      const d = DATES[idx].slice(5);
      document.querySelectorAll('#l1h9-signals-table tbody tr.exec-row').forEach(tr => {{
        const entryCell = tr.cells[2]?.textContent?.trim();
        const exitCell = tr.cells[3]?.textContent?.trim();
        const onFrame = entryCell === d || exitCell === d;
        tr.classList.toggle('on-frame', onFrame);
      }});
    }}

    function initSliderMarks() {{
      const marks = document.getElementById('slider-marks');
      const entryMarks = ENTRY_DAY_INDICES.map(i => {{
        const pct = (100 * i / Math.max(DATES.length - 1, 1)).toFixed(2);
        return `<i style="left:${{pct}}%" title="${{DATES[i].slice(5)}} 進場"></i>`;
      }}).join('');
      const exitMarks = EXIT_DAY_INDICES.map(i => {{
        const pct = (100 * i / Math.max(DATES.length - 1, 1)).toFixed(2);
        return `<i class="exit-mark" style="left:${{pct}}%" title="${{DATES[i].slice(5)}} 出場"></i>`;
      }}).join('');
      marks.innerHTML = entryMarks + exitMarks;
    }}

    function jumpEntry(delta) {{
      if (!ENTRY_DAY_INDICES.length) return;
      stopPlay();
      let target;
      if (delta < 0) {{
        target = ENTRY_DAY_INDICES.filter(i => i < frameIdx).pop();
        if (target === undefined) target = ENTRY_DAY_INDICES[0];
      }} else {{
        target = ENTRY_DAY_INDICES.find(i => i > frameIdx);
        if (target === undefined) target = ENTRY_DAY_INDICES[ENTRY_DAY_INDICES.length - 1];
      }}
      renderFrame(target);
    }}

    function jumpExit(delta) {{
      if (!EXIT_DAY_INDICES.length) return;
      stopPlay();
      let target;
      if (delta < 0) {{
        target = EXIT_DAY_INDICES.filter(i => i < frameIdx).pop();
        if (target === undefined) target = EXIT_DAY_INDICES[0];
      }} else {{
        target = EXIT_DAY_INDICES.find(i => i > frameIdx);
        if (target === undefined) target = EXIT_DAY_INDICES[EXIT_DAY_INDICES.length - 1];
      }}
      renderFrame(target);
    }}

    function primaryLegForStock(stockId, idx) {{
      const legs = (LEGS_BY_STOCK[stockId] || []).filter(lg => isLegActive(lg, idx));
      if (!legs.length) return null;
      if (focusId === stockId) return legs[0];
      return legs.sort((a, b) => (entryIdxForLeg(b) ?? 0) - (entryIdxForLeg(a) ?? 0))[0];
    }}

    function renderFrame(idx) {{
      frameIdx = idx;
      slider.value = String(idx);
      const d = DATES[idx];
      frameDate.textContent = d.slice(5);
      if (frameLabel) frameLabel.textContent = d + ' · frame ' + (idx + 1) + '/' + DATES.length;
      updateFrameProjection(idx);
      flashChartIfNeeded(idx);
      updateInsight(idx);
      syncTableHighlight(idx);

      const parts = [];
      const ordered = TRAJECTORIES.slice().sort((a, b) => {{
        const ah = STOCK_IDS.has(a.stock_id) ? 0 : 1;
        const bh = STOCK_IDS.has(b.stock_id) ? 0 : 1;
        if (focusId) {{
          if (a.stock_id === focusId) return -1;
          if (b.stock_id === focusId) return 1;
        }}
        return ah - bh || a.stock_id.localeCompare(b.stock_id);
      }});

      for (const t of ordered) {{
        if (idx >= t.points.length) continue;
        const isHi = STOCK_IDS.has(t.stock_id);
        if (!isHi && !showBg.checked) continue;
        if (focusId && t.stock_id !== focusId) continue;
        if (isHi && !isStockVisible(t.stock_id, idx)) continue;

        const plg = primaryLegForStock(t.stock_id, idx);
        const ei = plg ? entryIdxForLeg(plg) : idx;
        const pts = t.points.slice(ei, idx + 1);
        const endQuad = t.points[idx].quadrant || 'lagging';
        const color = QUAD_COLORS[endQuad] || '#888';
        const showTail = pts.length >= 2 && tailDisplacement(pts) > TAIL_MIN_DISP;

        if (isHi) {{
          if (showTail) {{
            for (let i = 0; i < pts.length - 1; i++) {{
              const p1 = pts[i], p2 = pts[i + 1];
              const opacity = 0.2 + 0.15 * (i / Math.max(pts.length - 1, 1));
              parts.push(
                `<line x1="${{sx(p1.rs_ratio).toFixed(1)}}" y1="${{sy(p1.rs_momentum).toFixed(1)}}" ` +
                `x2="${{sx(p2.rs_ratio).toFixed(1)}}" y2="${{sy(p2.rs_momentum).toFixed(1)}}" ` +
                `stroke="${{color}}" stroke-width="1.8" opacity="${{opacity.toFixed(2)}}" stroke-linecap="round"/>`
              );
            }}
          }}
          pts.forEach((p, i) => {{
            const globalStep = ei + i;
            const isCurrent = globalStep === idx;
            if (!isCurrent && !showTail) return;
            const cx = sx(p.rs_ratio), cy = sy(p.rs_momentum);
            const slotCol = plg ? DAY_COLORS[plg.slot_id % DAY_COLORS.length] : '#888';
            const r = isCurrent ? 5.5 : 3.0;
            const fill = isCurrent ? color : slotCol;
            const cum = plg ? legCumPct(plg, idx) : null;
            if (isCurrent) {{
              parts.push(
                `<circle class="hi-dot" cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{r}}" ` +
                `fill="${{fill}}" stroke="#fff" stroke-width="1.4" data-id="${{t.stock_id}}" ` +
                `data-label="${{t.stock_id}} · RS ${{p.rs_ratio}} · Mom ${{p.rs_momentum}} · Σ${{fmtPct(cum)}}"/>`
              );
              if (isEntryDay(t.stock_id, idx)) {{
                parts.push(
                  `<circle cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{(r + 2).toFixed(1)}}" ` +
                  `fill="none" stroke="#88CCFF" stroke-width="0.8" opacity="0.4" class="entry-pulse" pointer-events="none"/>`
                );
              }}
              const isExitDay = plg && DATE_INDEX[plg.exit_date] === idx;
              if (isExitDay) {{
                parts.push(
                  `<circle cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{(r + 2.5).toFixed(1)}}" ` +
                  `fill="none" stroke="#E8A040" stroke-width="0.9" opacity="0.5" class="entry-pulse" pointer-events="none"/>`
                );
              }}
              if (cum != null) {{
                parts.push(
                  `<text x="${{cx.toFixed(1)}}" y="${{(cy - 14).toFixed(1)}}" text-anchor="middle" ` +
                  `fill="${{pctColor(cum)}}" font-size="10" font-weight="700">Σ${{fmtPct(cum)}}</text>`
                );
              }}
              const lbl = stockLabel(t.stock_id, t.stock_name);
              const slotTxt = plg ? ` · 槽${{plg.slot_id + 1}}` : '';
              parts.push(
                `<text x="${{(cx + 6).toFixed(1)}}" y="${{(cy + 4).toFixed(1)}}" fill="#c8c8c8" ` +
                `font-size="10">${{lbl}}${{slotTxt}}</text>`
              );
            }} else if (showTail) {{
              parts.push(
                `<circle cx="${{cx.toFixed(1)}}" cy="${{cy.toFixed(1)}}" r="${{r}}" ` +
                `fill="none" stroke="${{slotCol}}" stroke-width="1.2" opacity="0.7"/>`
              );
            }}
          }});
        }} else {{
          const p = t.points[idx];
          parts.push(
            `<circle class="bg-dot" cx="${{sx(p.rs_ratio).toFixed(1)}}" cy="${{sy(p.rs_momentum).toFixed(1)}}" r="1.1" ` +
            `fill="${{color}}" opacity="0.07" data-id="${{t.stock_id}}"/>`
          );
        }}
      }}

      layer.innerHTML = parts.join('');
      layer.querySelectorAll('circle.hi-dot').forEach(el => {{
        el.addEventListener('mouseenter', ev => {{
          tooltip.innerHTML = `<b>${{el.dataset.id}}</b><br/>${{el.dataset.label || ''}}`;
          tooltip.style.display = 'block';
          tooltip.style.left = (ev.clientX + 12) + 'px';
          tooltip.style.top = (ev.clientY + 12) + 'px';
        }});
        el.addEventListener('mousemove', ev => {{
          tooltip.style.left = (ev.clientX + 12) + 'px';
          tooltip.style.top = (ev.clientY + 12) + 'px';
        }});
        el.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
    }}

    function stopPlay() {{
      playing = false;
      if (playTimer) {{ clearInterval(playTimer); playTimer = null; }}
      document.getElementById('btn-play').textContent = '▶ 逐步';
    }}

    slider.addEventListener('input', () => {{ stopPlay(); renderFrame(parseInt(slider.value, 10)); }});
    document.getElementById('btn-prev').addEventListener('click', () => {{
      stopPlay(); renderFrame(Math.max(0, frameIdx - 1));
    }});
    document.getElementById('btn-next').addEventListener('click', () => {{
      stopPlay(); renderFrame(Math.min(DATES.length - 1, frameIdx + 1));
    }});
    document.getElementById('btn-prev-entry').addEventListener('click', () => jumpEntry(-1));
    document.getElementById('btn-next-entry').addEventListener('click', () => jumpEntry(1));
    document.getElementById('btn-prev-exit').addEventListener('click', () => jumpExit(-1));
    document.getElementById('btn-next-exit').addEventListener('click', () => jumpExit(1));
    document.getElementById('btn-play').addEventListener('click', () => {{
      if (playing) {{ stopPlay(); return; }}
      if (frameIdx >= DATES.length - 1) renderFrame(0);
      playing = true;
      document.getElementById('btn-play').textContent = '⏸ 暫停';
      playTimer = setInterval(() => {{
        if (frameIdx >= DATES.length - 1) {{ stopPlay(); return; }}
        renderFrame(frameIdx + 1);
      }}, 900);
    }});
    showBg.addEventListener('change', () => renderFrame(frameIdx));
    document.addEventListener('keydown', ev => {{
      if (ev.target.tagName === 'INPUT') return;
      if (ev.key === 'ArrowLeft') {{ stopPlay(); renderFrame(Math.max(0, frameIdx - 1)); }}
      if (ev.key === 'ArrowRight') {{ stopPlay(); renderFrame(Math.min(DATES.length - 1, frameIdx + 1)); }}
      if (ev.key === '[') jumpEntry(-1);
      if (ev.key === ']') jumpEntry(1);
      if (ev.key === '{{') jumpExit(-1);
      if (ev.key === '}}') jumpExit(1);
      if (ev.key === 'Escape') {{ focusId = null; renderFrame(frameIdx); }}
    }});

    document.querySelectorAll('#l1h9-signals-table tbody tr.exec-row').forEach(tr => {{
      tr.classList.add('hi-row');
      tr.addEventListener('click', () => {{
        const entry = tr.dataset.entry;
        document.querySelectorAll('#l1h9-signals-table tbody tr').forEach(r => r.classList.remove('active'));
        tr.classList.add('active');
        if (entry && DATE_INDEX[entry] !== undefined) renderFrame(DATE_INDEX[entry]);
      }});
    }});

    initSliderMarks();
    renderFrame(0);
  </script>
</body>
</html>"""


def main() -> int:
    if os.environ.get("RUN_RESEARCH_HTML_GEN", "0").strip() != "1":
        print(
            "SKIP: research HTML generation disabled "
            "(set RUN_RESEARCH_HTML_GEN=1 to enable)",
            file=sys.stderr,
        )
        return 0
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from project_config import DEFAULT_ETF_CODES, parse_etf_codes

    parser = argparse.ArgumentParser(description="RRG Universe scatter / trajectory HTML")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--as-of", default=None, help="訊號日 YYYY-MM-DD（預設最新）")
    parser.add_argument(
        "--dates",
        default="2026-06-15,2026-06-16,2026-06-17,2026-06-18",
        help="軌跡模式：逗號分隔訊號日（預設 0615–0618）",
    )
    parser.add_argument(
        "--date-from",
        default=None,
        help="軌跡/時間軸：起始交易日 YYYY-MM-DD（優先於 --dates）",
    )
    parser.add_argument(
        "--date-to",
        default=None,
        help="軌跡/時間軸：結束交易日（含；預設 DB 最新）",
    )
    parser.add_argument("--trajectory", action="store_true", help="輸出多日軌跡疊圖")
    parser.add_argument(
        "--trajectory-split",
        action="store_true",
        help="輸出右上/左下趨勢分面軌跡圖（含名稱標籤）",
    )
    parser.add_argument(
        "--holdings-changes",
        action="store_true",
        help="輸出指定 ETF 期間持股變動標的 RRG 軌跡",
    )
    parser.add_argument(
        "--holdings-timeline",
        action="store_true",
        help="輸出指定 ETF 持股變動標的 RRG 互動時間軸（daily bar + tail）",
    )
    parser.add_argument(
        "--l1h9-slots-timeline",
        action="store_true",
        help="輸出 00981A L1H9 多槽跟單策略 RRG 互動時間軸",
    )
    parser.add_argument(
        "--rrg-mono-slots-timeline",
        action="store_true",
        help="輸出 RRG mono + seg_last + 3槽 hold7 策略 RRG 互動時間軸",
    )
    parser.add_argument(
        "--chunge-funnel-slots-timeline",
        action="store_true",
        help="輸出 VCP funnel slot 策略 RRG 互動時間軸（預設 hold7；加 --vcp-pivot-gate / --vcp-coil-close / --chunge-entry-ready）",
    )
    parser.add_argument(
        "--chunge-entry-ready",
        action="store_true",
        help="VCP funnel timeline：entry_ready=1 · Pre/Breakout · 預設 5 槽 hold20",
    )
    parser.add_argument(
        "--vcp-pivot-gate",
        "--chunge-near-pivot",
        action="store_true",
        dest="vcp_pivot_gate",
        help="VCP Pivot Gate timeline：near pivot · breakout_close · 5槽 hold20",
    )
    parser.add_argument(
        "--vcp-coil-close",
        "--chunge-coil-close",
        action="store_true",
        dest="vcp_coil_close",
        help="VCP Coil Close timeline：near pivot · 訊號日 close · 5槽 hold20",
    )
    parser.add_argument(
        "--n-slots",
        type=int,
        default=9,
        help="L1H9 多槽：槽位數（預設 9）",
    )
    parser.add_argument(
        "--capital-ntd",
        type=float,
        default=10_000.0,
        help="L1H9 每訊號部署本金 NTD",
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=0.0,
        help="L1H9 交易成本 bps",
    )
    parser.add_argument(
        "--hold-days",
        type=int,
        default=9,
        help="L1H9 持有交易日 H",
    )
    parser.add_argument(
        "--etf-code",
        default="00981A",
        help="--holdings-changes 時的 ETF 代號",
    )
    parser.add_argument("--length", type=int, default=20)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    etf_codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        if args.holdings_changes or args.holdings_timeline or args.l1h9_slots_timeline or args.rrg_mono_slots_timeline or args.chunge_funnel_slots_timeline or args.trajectory or args.trajectory_split:
            if args.date_from:
                dates = _load_trading_dates_range(
                    conn, date_from=args.date_from, date_to=args.date_to
                )
            else:
                dates = [d.strip() for d in args.dates.split(",") if d.strip()]
            if len(dates) < 2:
                print("軌跡模式至少需要 2 個訊號日", file=sys.stderr)
                return 1
            if args.holdings_changes or args.holdings_timeline or args.l1h9_slots_timeline or args.rrg_mono_slots_timeline or args.chunge_funnel_slots_timeline:
                if args.chunge_funnel_slots_timeline:
                    from research.backtest.chunge_funnel_backtest import (
                        VCP_COIL_CLOSE,
                        VCP_PIVOT_GATE,
                        ENTRY_READY_EXECUTION_STATES,
                        ENTRY_READY_HOLD20_DEFAULTS,
                        MINERVINI_NEAR_PIVOT_STATES,
                        build_executed_legs_for_timeline,
                        build_executed_legs_for_timeline_pivot,
                    )

                    if args.vcp_pivot_gate or args.vcp_coil_close:
                        spec = VCP_PIVOT_GATE if args.vcp_pivot_gate else VCP_COIL_CLOSE
                        chunge_slots = args.n_slots if args.n_slots != 9 else spec["n_slots"]
                        chunge_hold = args.hold_days if args.hold_days != 9 else spec["hold_days"]
                        legs, executed, skipped, meta = build_executed_legs_for_timeline_pivot(
                            conn,
                            dates,
                            n_slots=chunge_slots,
                            capital_ntd=args.capital_ntd,
                            hold_days=chunge_hold,
                            min_composite=spec["min_composite"],
                            execution_states=spec["execution_states"],
                            entry_ready_only=spec["entry_ready_only"],
                            require_pivot=spec["require_pivot"],
                            min_dist_pivot_pct=spec["min_dist_pivot_pct"],
                            max_dist_pivot_pct=spec["max_dist_pivot_pct"],
                            entry_mode=spec["entry_price_mode"],
                            max_entry_wait_days=spec["max_entry_wait_days"],
                            stop_lookback_days=spec["stop_lookback_days"],
                            variant=spec["variant"],
                        )
                    else:
                        if args.chunge_entry_ready:
                            chunge_slots = (
                                args.n_slots
                                if args.n_slots != 9
                                else ENTRY_READY_HOLD20_DEFAULTS["n_slots"]
                            )
                            chunge_hold = (
                                args.hold_days
                                if args.hold_days != 9
                                else ENTRY_READY_HOLD20_DEFAULTS["hold_days"]
                            )
                            chunge_states = ENTRY_READY_EXECUTION_STATES
                            chunge_entry_ready = True
                        else:
                            chunge_slots = 3 if args.n_slots == 9 else args.n_slots
                            chunge_hold = 7 if args.hold_days == 9 else args.hold_days
                            chunge_states = (
                                "Pre-breakout",
                                "Breakout",
                                "Overextended",
                                "Extended",
                            )
                            chunge_entry_ready = False
                        legs, executed, skipped, meta = build_executed_legs_for_timeline(
                            conn,
                            dates,
                            n_slots=chunge_slots,
                            capital_ntd=args.capital_ntd,
                            hold_days=chunge_hold,
                            min_composite=45.0,
                            execution_states=chunge_states,
                            entry_ready_only=chunge_entry_ready,
                        )
                    if not legs:
                        print(
                            f"Chunge funnel 在 {dates[0]}→{dates[-1]} 無可執行 leg",
                            file=sys.stderr,
                        )
                        return 1
                    bench_closes = _load_bench_closes_for_dates(conn, dates)
                    stock_ids = {lg["stock_id"] for lg in legs}
                    all_trajectories = _load_rrg_trajectories(
                        conn,
                        dates=dates,
                        etf_codes=etf_codes,
                        length=args.length,
                        with_close=True,
                    )
                elif args.rrg_mono_slots_timeline:
                    events = []
                    mono_slots = 3 if args.n_slots == 9 else args.n_slots
                    mono_hold = 7 if args.hold_days == 9 else args.hold_days
                    legs, executed, skipped, meta = _build_rrg_mono_executed_legs(
                        conn,
                        dates,
                        n_slots=mono_slots,
                        capital_ntd=args.capital_ntd,
                        hold_days=mono_hold,
                    )
                    if not legs:
                        print(
                            f"RRG mono 在 {dates[0]}→{dates[-1]} 無可執行 leg",
                            file=sys.stderr,
                        )
                        return 1
                    bench_closes = _load_bench_closes_for_dates(conn, dates)
                    stock_ids = {lg["stock_id"] for lg in legs}
                    all_trajectories = _load_rrg_trajectories(
                        conn,
                        dates=dates,
                        etf_codes=etf_codes,
                        length=args.length,
                        with_close=True,
                    )
                elif args.l1h9_slots_timeline:
                    events = []
                    legs, executed, skipped, meta = _build_l1h9_executed_legs(
                        conn,
                        args.etf_code,
                        dates,
                        n_slots=args.n_slots,
                        capital_ntd=args.capital_ntd,
                        cost_bps=args.cost_bps,
                        hold_trading_days=args.hold_days,
                    )
                    if not legs:
                        print(
                            f"{args.etf_code} L1H9 在 {dates[0]}→{dates[-1]} 無可執行 leg",
                            file=sys.stderr,
                        )
                        return 1
                    bench_closes = _load_bench_closes_for_dates(conn, dates)
                    stock_ids = {lg["stock_id"] for lg in legs}
                    all_trajectories = _load_rrg_trajectories(
                        conn,
                        dates=dates,
                        etf_codes=etf_codes,
                        length=args.length,
                        with_close=True,
                    )
                else:
                    events = _collect_holdings_change_events(conn, args.etf_code, dates)
                    if not events:
                        print(f"{args.etf_code} 在 {dates[0]}→{dates[-1]} 無持股變動", file=sys.stderr)
                        return 1
                    stock_ids = {e["stock_id"] for e in events}
                    all_trajectories = _load_rrg_trajectories(
                        conn,
                        dates=dates,
                        etf_codes=etf_codes,
                        length=args.length,
                        with_close=args.holdings_timeline,
                    )
            else:
                events = []
                all_trajectories = []
                stock_ids = set()
                trajectories = _load_rrg_trajectories(
                    conn, dates=dates, etf_codes=etf_codes, length=args.length
                )
        else:
            events = []
            trajectories = []
            as_of, points = _load_rrg_points(
                conn, as_of_date=args.as_of, etf_codes=etf_codes, length=args.length
            )
    finally:
        conn.close()

    if args.chunge_funnel_slots_timeline:
        stamp = date.today().strftime("%Y%m%d")
        year_tag = _timeline_year_tag(args.date_from, args.date_to)
        if args.vcp_pivot_gate:
            base = f"{stamp}_vcp_pivot_gate_s5_h20_slots_rrg_timeline"
        elif args.vcp_coil_close:
            base = f"{stamp}_vcp_coil_close_s5_h20_slots_rrg_timeline"
        elif args.chunge_entry_ready:
            base = f"{stamp}_chunge_funnel_entry_ready_hold20_slots_rrg_timeline"
        else:
            base = f"{stamp}_chunge_funnel_hold7_slots_rrg_timeline"
        fname = f"{base}_{year_tag}.html" if year_tag else f"{base}.html"
        out = args.output or research_html_path("vcp", fname)
        out.parent.mkdir(parents=True, exist_ok=True)
        display_code = "VCP funnel"
        if args.vcp_pivot_gate:
            display_code = "VCP Pivot Gate"
        elif args.vcp_coil_close:
            display_code = "VCP Coil Close"
        elif args.chunge_entry_ready:
            display_code = "VCP funnel · entry_ready"
        out.write_text(
            render_l1h9_slots_timeline_html(
                etf_code=meta.get("display_code") or display_code,
                dates=dates,
                legs=legs,
                executed_signals=executed,
                skipped_signals=skipped,
                all_trajectories=all_trajectories,
                meta=meta,
                bench_closes=bench_closes,
                length=args.length,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} (legs={len(legs)}, executed={meta['n_executed']}, "
            f"skipped={meta['n_skipped']}, slots={meta['n_slots']}, "
            f"{len(dates)} frames, {dates[0]} → {dates[-1]})"
        )
        return 0

    if args.rrg_mono_slots_timeline:
        stamp = date.today().strftime("%Y%m%d")
        year_tag = _timeline_year_tag(args.date_from, args.date_to)
        base = f"{stamp}_rrg_mono_hold7_slots_rrg_timeline"
        fname = f"{base}_{year_tag}.html" if year_tag else f"{base}.html"
        out = args.output or research_html_path("rrg", fname)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_l1h9_slots_timeline_html(
                etf_code="RRG mono",
                dates=dates,
                legs=legs,
                executed_signals=executed,
                skipped_signals=skipped,
                all_trajectories=all_trajectories,
                meta=meta,
                bench_closes=bench_closes,
                length=args.length,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} (legs={len(legs)}, executed={meta['n_executed']}, "
            f"skipped={meta['n_skipped']}, slots={meta['n_slots']}, "
            f"{len(dates)} frames, {dates[0]} → {dates[-1]})"
        )
        return 0

    if args.l1h9_slots_timeline:
        stamp = date.today().strftime("%Y%m%d")
        slug = args.etf_code.lower()
        year_tag = _timeline_year_tag(args.date_from, args.date_to)
        base = f"{stamp}_{slug}_l1h9_slots_rrg_timeline"
        fname = f"{base}_{year_tag}.html" if year_tag else f"{base}.html"
        out = args.output or research_html_path("00981a-copytrade", fname)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_l1h9_slots_timeline_html(
                etf_code=args.etf_code,
                dates=dates,
                legs=legs,
                executed_signals=executed,
                skipped_signals=skipped,
                all_trajectories=all_trajectories,
                meta=meta,
                bench_closes=bench_closes,
                length=args.length,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} (legs={len(legs)}, executed={meta['n_executed']}, "
            f"skipped={meta['n_skipped']}, slots={meta['n_slots']}, "
            f"{len(dates)} frames, {dates[0]} → {dates[-1]})"
        )
        return 0

    if args.holdings_timeline:
        highlighted = [t for t in all_trajectories if t["stock_id"] in stock_ids]
        if not highlighted:
            print("持股變動標的無有效 RRG 軌跡", file=sys.stderr)
            return 1
        stamp = date.today().strftime("%Y%m%d")
        slug = args.etf_code.lower()
        out = args.output or research_html_path(
            "00981a-copytrade", f"{stamp}_{slug}_holdings_change_rrg_timeline.html"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_holdings_change_timeline_html(
                etf_code=args.etf_code,
                dates=dates,
                events=events,
                all_trajectories=all_trajectories,
                highlight_ids=stock_ids,
                length=args.length,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} ({len(events)} changes, highlight={len(stock_ids)}, "
            f"universe={len(all_trajectories)}, {len(dates)} frames, {dates[0]} → {dates[-1]})"
        )
        return 0

    if args.holdings_changes:
        highlighted = [t for t in all_trajectories if t["stock_id"] in stock_ids]
        if not highlighted:
            print("持股變動標的無有效 RRG 軌跡", file=sys.stderr)
            return 1
        stamp = date.today().strftime("%Y%m%d")
        slug = args.etf_code.lower()
        out = args.output or research_html_path(
            "00981a-copytrade", f"{stamp}_{slug}_holdings_change_rrg.html"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_holdings_change_html(
                etf_code=args.etf_code,
                dates=dates,
                events=events,
                all_trajectories=all_trajectories,
                highlight_ids=stock_ids,
                length=args.length,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} ({len(events)} changes, highlight={len(stock_ids)}, "
            f"universe={len(all_trajectories)}, {dates[0]} → {dates[-1]})"
        )
        return 0

    if args.trajectory_split:
        if not trajectories:
            print("無有效 RRG 軌跡", file=sys.stderr)
            return 1
        up_right, down_left, _other = _split_trajectories_by_trend(trajectories)
        if not up_right and not down_left:
            print("無往右上或往左下趨勢的軌跡", file=sys.stderr)
            return 1
        stamp = date.today().strftime("%Y%m%d")
        out = args.output or research_html_path(
            "rrg", f"{stamp}_rrg_universe_trajectory_split.html"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_trajectory_split_html(
                dates=dates,
                trajectories=trajectories,
                length=args.length,
                etf_codes=etf_codes,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} (up_right={len(up_right)}, down_left={len(down_left)}, "
            f"{dates[0]} → {dates[-1]})"
        )
        return 0

    if args.trajectory:
        if not trajectories:
            print("無有效 RRG 軌跡", file=sys.stderr)
            return 1
        stamp = date.today().strftime("%Y%m%d")
        out = args.output or research_html_path("rrg", f"{stamp}_rrg_universe_trajectory.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_trajectory_html(
                dates=dates,
                trajectories=trajectories,
                length=args.length,
                etf_codes=etf_codes,
            ),
            encoding="utf-8",
        )
        print(
            f"Wrote {out} ({len(trajectories)} stocks, "
            f"{dates[0]} → {dates[-1]}, {len(dates)} days)"
        )
        return 0

    if not points:
        print("無有效 RRG 點位", file=sys.stderr)
        return 1

    out = args.output or research_html_path(
        "rrg", f"{date.today().strftime('%Y%m%d')}_rrg_universe.html"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_html(as_of=as_of, points=points, length=args.length, etf_codes=etf_codes),
        encoding="utf-8",
    )
    print(f"Wrote {out} ({len(points)} stocks, as_of={as_of})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
