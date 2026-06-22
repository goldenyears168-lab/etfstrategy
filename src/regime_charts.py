"""Regime daily · compact SVG charts (breadth spark + RRG scatter)."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from market_breadth_impulse import build_impulse_panel_from_close
from market_breadth_ma import (
    BREADTH_ZONE_COLOR,
    BREADTH_ZONE_ZH,
    REF_LEVELS_200,
    build_breadth_panel,
)
from market_benchmark import load_benchmark_close
from report_paths import (
    REGIME_CHART_BREADTH,
    REGIME_CHART_RRG,
    REGIME_CHART_STAGE2,
    REGIME_CHART_WEINSTEIN,
    REGIME_CHART_ZWEIG_EMA,
    regime_snapshot_dir,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_rotation import DEFAULT_LENGTH, classify_quadrant, compute_rrg_panel
from stage_analysis import WEEKLY_MA_PERIOD, daily_to_weekly, vectorized_minervini_pass_pct
from vcp_nse_port.bars import rows_to_ohlcv_df

BAR_LOOKBACK = 280
BREADTH_CHART_DAYS = 90
PARTICIPATION_CHART_DAYS = 90
RRG_TAIL_DAYS = 4
RRG_RANK_PER_QUAD = 12
RRG_SCATTER_SNAPSHOT_MAX = 150

QUADRANT_ORDER = ("leading", "improving", "weakening", "lagging")

STAGE_RIBBON_COLOR: dict[int, str] = {
    1: "#6366f1",
    2: "#22c55e",
    3: "#f59e0b",
    4: "#ef4444",
}
STAGE_RIBBON_LABEL: dict[int, str] = {
    1: "S1 basing",
    2: "S2 advancing",
    3: "S3 topping",
    4: "S4 declining",
}

_SVG_FONT = (
    '<style type="text/css"><![CDATA['
    'text { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif; }'
    ']]></style>'
)

QUADRANT_COLORS = {
    "leading": "#1F8A65",
    "weakening": "#E8A040",
    "lagging": "#C04848",
    "improving": "#2E79B5",
}


@dataclass(frozen=True)
class RegimeChartPaths:
    breadth_spark: str | None = None
    zweig_ema_spark: str | None = None
    weinstein_weekly: str | None = None
    rrg_scatter: str | None = None
    participation_spark: str | None = None


def _breadth_records(panel: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in panel.itertuples():
        z200 = str(r.zone_200)
        rows.append(
            {
                "d": str(r.trade_date),
                "p50": round(float(r.pct_above_50), 2),
                "p200": round(float(r.pct_above_200), 2),
                "z200zh": BREADTH_ZONE_ZH.get(z200, z200),
                "c": BREADTH_ZONE_COLOR.get(z200, "#888899"),
                "bench": round(float(r.bench_close), 2) if r.bench_close is not None else None,
                "div": bool(r.divergence_flag),
            }
        )
    return rows


def render_breadth_spark_svg(points: list[dict[str, Any]]) -> str:
    """TradingView-style dual panel · last N days."""
    if not points:
        return ""
    w = 800
    pad_l, pad_r = 52, 16
    plot_l, plot_r = pad_l, w - pad_r
    top_h, gap, bot_h = 160, 22, 200
    total_h = top_h + gap + bot_h + 28
    n = len(points)
    bench = [p["bench"] for p in points if p.get("bench") is not None]
    if len(bench) < 2:
        return _render_breadth_only_svg(points)

    b0 = bench[0]
    bench_idx = [(float(p["bench"]) / b0 - 1.0) * 100.0 if p.get("bench") else None for p in points]
    numeric_b = [v for v in bench_idx if v is not None]
    b_lo, b_hi = min(numeric_b), max(numeric_b)
    pad_b = max((b_hi - b_lo) * 0.06, 3.0)
    b_lo -= pad_b
    b_hi += pad_b

    def x_at(i: int) -> float:
        return plot_l + i / max(n - 1, 1) * (plot_r - plot_l)

    def y_top(v: float) -> float:
        return top_h - 20 - (v - b_lo) / max(b_hi - b_lo, 1e-9) * (top_h - 36)

    def y_bot(v: float) -> float:
        y0 = top_h + gap
        return y0 + bot_h - 26 - v / 100.0 * (bot_h - 40)

    y0 = top_h + gap
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{total_h}" '
        f'viewBox="0 0 {w} {total_h}">',
        _SVG_FONT,
        '<rect width="100%" height="100%" fill="#131722"/>',
        f'<text x="{plot_l}" y="16" fill="#d1d4dc" font-size="12" font-weight="600">'
        f"Breadth zone · IX0001 + % Above MA</text>",
        f'<text x="{plot_r}" y="16" text-anchor="end" fill="#787b86" font-size="9">'
        f"S5TH / S5FI style</text>",
    ]

    pts_b = " ".join(
        f"{x_at(i):.1f},{y_top(v):.1f}" for i, v in enumerate(bench_idx) if v is not None
    )
    lines.append(f'<polyline fill="none" stroke="#2962ff" stroke-width="1.6" points="{pts_b}"/>')

    zone_bands = [
        (80, 100, "#C0484815"),
        (60, 80, "#1F8A6515"),
        (40, 60, "#70B0D812"),
        (20, 40, "#F0A04012"),
        (0, 20, "#7B64B815"),
    ]
    for z_lo, z_hi, fill in zone_bands:
        lines.append(
            f'<rect x="{plot_l}" y="{y_bot(z_hi):.1f}" width="{plot_r - plot_l:.1f}" '
            f'height="{y_bot(z_lo) - y_bot(z_hi):.1f}" fill="{fill}"/>'
        )
    for ref in REF_LEVELS_200:
        y = y_bot(ref)
        lines.append(
            f'<line x1="{plot_l}" y1="{y:.1f}" x2="{plot_r}" y2="{y:.1f}" '
            f'stroke="#434651" stroke-width="1" stroke-dasharray="3,3"/>'
        )

    pts50 = " ".join(f"{x_at(i):.1f},{y_bot(p['p50']):.1f}" for i, p in enumerate(points))
    pts200 = " ".join(f"{x_at(i):.1f},{y_bot(p['p200']):.1f}" for i, p in enumerate(points))
    lines.append(f'<polyline fill="none" stroke="#089981" stroke-width="1.8" points="{pts50}"/>')
    lines.append(f'<polyline fill="none" stroke="#f23645" stroke-width="1.8" points="{pts200}"/>')

    latest = points[-1]
    lines.extend(
        [
            f'<text x="{plot_l}" y="{y0 + 14}" fill="#089981" font-size="9">'
            f"50MA {latest['p50']:.1f}%</text>",
            f'<text x="{plot_l + 120}" y="{y0 + 14}" fill="#f23645" font-size="9">'
            f"200MA {latest['p200']:.1f}% · {latest['z200zh']}</text>",
            f'<text x="{plot_l}" y="{total_h - 4}" fill="#787b86" font-size="9">{points[0]["d"]}</text>',
            f'<text x="{plot_r}" y="{total_h - 4}" text-anchor="end" fill="#787b86" font-size="9">'
            f'{latest["d"]}</text>',
            "</svg>",
        ]
    )
    return "\n".join(lines)


def _render_breadth_only_svg(points: list[dict[str, Any]]) -> str:
    w, h = 800, 220
    pad_l, pad_r, pad_t, pad_b = 52, 16, 28, 32
    plot_l, plot_r = pad_l, w - pad_r
    n = len(points)

    def x_at(i: int) -> float:
        return plot_l + i / max(n - 1, 1) * (plot_r - plot_l)

    def y_bot(v: float) -> float:
        return pad_t + (h - pad_t - pad_b) - v / 100.0 * (h - pad_t - pad_b)

    pts50 = " ".join(f"{x_at(i):.1f},{y_bot(p['p50']):.1f}" for i, p in enumerate(points))
    pts200 = " ".join(f"{x_at(i):.1f},{y_bot(p['p200']):.1f}" for i, p in enumerate(points))
    latest = points[-1]
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
            _SVG_FONT,
            '<rect width="100%" height="100%" fill="#131722"/>',
            f'<polyline fill="none" stroke="#089981" stroke-width="1.8" points="{pts50}"/>',
            f'<polyline fill="none" stroke="#f23645" stroke-width="1.8" points="{pts200}"/>',
            f'<text x="{plot_l}" y="16" fill="#d1d4dc" font-size="12">'
            f"Breadth · 50MA {latest['p50']:.1f}% · 200MA {latest['p200']:.1f}%</text>",
            "</svg>",
        ]
    )


def _nice_ticks(lo: float, hi: float) -> list[float]:
    step = 2.0
    start = math.floor(lo / step) * step
    out: list[float] = []
    v = start
    while v <= hi + 0.01:
        if lo <= v <= hi or abs(v - 100) < 0.01:
            out.append(v)
        v += step
    if 100.0 not in out and lo <= 100 <= hi:
        out.append(100.0)
    return sorted(set(out))


def render_zweig_ema_spark_svg(
    series: pd.Series,
    *,
    tier_lines: tuple[float, float, float] = (45.0, 50.0, 58.0),
) -> str:
    """90-day Zweig adv/decl 10-day EMA (%)."""
    tail = (series.dropna().tail(BREADTH_CHART_DAYS) * 100.0).round(1)
    if len(tail) < 5:
        return ""
    w, h = 720, 200
    pad_l, pad_r, pad_t, pad_b = 48, 16, 32, 32
    plot_l, plot_w = pad_l, w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    n = len(tail)
    vals = [float(v) for v in tail.values]

    def x_at(i: int) -> float:
        return plot_l + i / max(n - 1, 1) * plot_w

    def y_at(v: float) -> float:
        return pad_t + plot_h - v / 100.0 * plot_h

    pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(vals))
    latest = vals[-1]
    ref_lines: list[str] = []
    for ref in tier_lines:
        y = y_at(ref)
        ref_lines.append(
            f'<line x1="{plot_l}" y1="{y:.1f}" x2="{plot_l + plot_w}" y2="{y:.1f}" '
            f'stroke="#434651" stroke-width="1" stroke-dasharray="4,4"/>'
        )
        ref_lines.append(
            f'<text x="{plot_l + plot_w + 2}" y="{y + 3:.1f}" fill="#787b86" font-size="8">{ref:.0f}%</text>'
        )
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
            _SVG_FONT,
            '<rect width="100%" height="100%" fill="#131722"/>',
            f'<text x="{pad_l}" y="20" fill="#d1d4dc" font-size="12" font-weight="600">'
            f"Zweig EMA rhythm · adv/decl 10-day EMA</text>",
            *ref_lines,
            f'<polyline fill="none" stroke="#2962ff" stroke-width="2" points="{pts}"/>',
            f'<text x="{pad_l + plot_w}" y="20" text-anchor="end" fill="#2962ff" font-size="11">'
            f"{latest:.1f}%</text>",
            f'<text x="{pad_l}" y="{h - 4}" fill="#787b86" font-size="9">{tail.index[0]}</text>',
            f'<text x="{pad_l + plot_w}" y="{h - 4}" text-anchor="end" fill="#787b86" font-size="9">'
            f"{tail.index[-1]}</text>",
            "</svg>",
        ]
    )


def _load_ix_df(conn: sqlite3.Connection, as_of: str, *, code: str = "IX0001") -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE code = ? AND source = 'tej' AND date <= ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, as_of, BAR_LOOKBACK),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    return rows_to_ohlcv_df(list(reversed(rows)))


def _weekly_bar_stage(price: float, ma: float, ma_prev: float | None) -> int:
    """Heuristic Weinstein stage for weekly ribbon (approximate)."""
    if ma <= 0:
        return 1
    ma_slope_pct = (ma - ma_prev) / ma_prev * 100.0 if ma_prev and ma_prev > 0 else 0.0
    extension_pct = (price - ma) / ma * 100.0
    ma_rising = ma_slope_pct > 0.25
    ma_falling = ma_slope_pct < -0.25
    price_above = price > ma
    if not price_above and ma_falling:
        return 4
    if price_above and ma_rising:
        return 3 if extension_pct > 12.0 else 2
    if price_above and not ma_rising:
        return 3
    if not price_above and not ma_falling:
        return 1
    return 1


def tail_direction_label(trail: list[tuple[float, float]] | None) -> str:
    if not trail or len(trail) < 2:
        return "—"
    dr = trail[-1][0] - trail[0][0]
    dm = trail[-1][1] - trail[0][1]
    if dr > 0 and dm > 0:
        return "↗ up-right"
    if dr > 0:
        return "→ up-left"
    if dm > 0:
        return "↑ down-left"
    return "↙ down-left"


def rank_rrg_points(
    points: list[dict[str, Any]],
    *,
    per_quadrant: int = RRG_RANK_PER_QUAD,
) -> list[dict[str, Any]]:
    """StockCharts-style: group by quadrant, sort by RS-Ratio / RS-Momentum."""
    buckets: dict[str, list[dict[str, Any]]] = {q: [] for q in QUADRANT_ORDER}
    for p in points:
        q = str(p.get("quadrant") or "lagging")
        if q in buckets:
            buckets[q].append(p)
    ranked: list[dict[str, Any]] = []
    for q in QUADRANT_ORDER:
        items = buckets[q]
        if q in ("leading", "weakening"):
            items = sorted(items, key=lambda x: (-float(x["rs_ratio"]), -float(x["rs_momentum"])))
        else:
            items = sorted(items, key=lambda x: (-float(x["rs_momentum"]), -float(x["rs_ratio"])))
        for p in items[:per_quadrant]:
            ranked.append(
                {
                    **p,
                    "quadrant": q,
                    "tail_dir": tail_direction_label(p.get("trail")),
                }
            )
    return ranked


def enrich_rrg_rotation_rankings(
    snap: dict[str, Any],
    conn: sqlite3.Connection,
    as_of: str,
) -> None:
    rrg = snap.get("rrg_rotation") or {}
    if not rrg.get("available"):
        return
    try:
        _, points = load_rrg_scatter_points(conn, as_of)
        rrg["ranked_symbols"] = rank_rrg_points(points)
    except (ValueError, RuntimeError):
        rrg["ranked_symbols"] = []


def render_weinstein_weekly_svg(
    df: pd.DataFrame,
    *,
    bench: str,
    stage: int,
    stage_name: str,
) -> str:
    weekly = daily_to_weekly(df)
    if len(weekly) < WEEKLY_MA_PERIOD + 8:
        return ""
    close = weekly["Close"]
    ma = close.rolling(WEEKLY_MA_PERIOD, min_periods=WEEKLY_MA_PERIOD).mean()
    sub = pd.DataFrame({"close": close, "ma": ma}).dropna().tail(52)
    if len(sub) < 8:
        return ""
    cvals = [float(v) for v in sub["close"]]
    mvals = [float(v) for v in sub["ma"]]
    n = len(sub)
    stages: list[int] = []
    for i in range(n):
        prev_ma = mvals[i - 1] if i > 0 else None
        stages.append(_weekly_bar_stage(cvals[i], mvals[i], prev_ma))

    w, h = 720, 300
    pad_l, pad_r, pad_t, pad_b = 52, 16, 36, 52
    ribbon_h = 16
    plot_l, plot_w = pad_l, w - pad_l - pad_r
    plot_h = h - pad_t - pad_b - ribbon_h - 6
    y_lo = min(min(cvals), min(mvals))
    y_hi = max(max(cvals), max(mvals))
    pad_y = (y_hi - y_lo) * 0.06 or 1.0
    y_lo -= pad_y
    y_hi += pad_y

    def x_at(i: int) -> float:
        return plot_l + i / max(n - 1, 1) * plot_w

    def y_at(v: float) -> float:
        return pad_t + plot_h - (v - y_lo) / max(y_hi - y_lo, 1e-9) * plot_h

    pts_c = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(cvals))
    pts_m = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(mvals))
    ribbon_y = pad_t + plot_h + 8
    ribbon_w = plot_w / max(n, 1)
    ribbons: list[str] = []
    for i, st in enumerate(stages):
        color = STAGE_RIBBON_COLOR.get(st, "#555")
        rx = x_at(i) - ribbon_w / 2
        ribbons.append(
            f'<rect x="{rx:.1f}" y="{ribbon_y:.1f}" width="{max(ribbon_w, 2):.1f}" '
            f'height="{ribbon_h}" fill="{color}" opacity="0.85"/>'
        )
    legend_x = pad_l
    legend_items: list[str] = []
    for st in (1, 2, 3, 4):
        color = STAGE_RIBBON_COLOR[st]
        label = STAGE_RIBBON_LABEL[st]
        legend_items.append(
            f'<rect x="{legend_x:.0f}" y="{h - 14}" width="10" height="8" fill="{color}"/>'
            f'<text x="{legend_x + 14:.0f}" y="{h - 7}" fill="#787b86" font-size="8">{label}</text>'
        )
        legend_x += 88
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
            _SVG_FONT,
            '<rect width="100%" height="100%" fill="#131722"/>',
            f'<text x="{pad_l}" y="20" fill="#d1d4dc" font-size="12" font-weight="600">'
            f"{bench} weekly · Stage {stage} · {stage_name} · 30W MA + Stage ribbon</text>",
            f'<polyline fill="none" stroke="#787b86" stroke-width="1.6" points="{pts_m}"/>',
            f'<polyline fill="none" stroke="#2962ff" stroke-width="2" points="{pts_c}"/>',
            *ribbons,
            *legend_items,
            f'<text x="{pad_l}" y="{ribbon_y - 4}" fill="#787b86" font-size="8">Stage ribbon (Weinstein)</text>',
            f'<text x="{pad_l}" y="{h - 26}" fill="#787b86" font-size="9">— close</text>',
            f'<text x="{pad_l + 56}" y="{h - 26}" fill="#787b86" font-size="9">— 30W MA</text>',
            "</svg>",
        ]
    )


def render_participation_spark_svg(
    series: pd.Series,
    *,
    as_of: str,
) -> str:
    if series.empty:
        return ""
    tail = (series.dropna().tail(PARTICIPATION_CHART_DAYS) * 100.0).round(1)
    if len(tail) < 5:
        return ""
    w, h = 720, 200
    pad_l, pad_r, pad_t, pad_b = 48, 16, 32, 32
    plot_l, plot_w = pad_l, w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    n = len(tail)
    vals = [float(v) for v in tail.values]

    def x_at(i: int) -> float:
        return plot_l + i / max(n - 1, 1) * plot_w

    def y_at(v: float) -> float:
        return pad_t + plot_h - v / 100.0 * plot_h

    pts = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(vals))
    latest = vals[-1]
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
            _SVG_FONT,
            '<rect width="100%" height="100%" fill="#131722"/>',
            f'<text x="{pad_l}" y="20" fill="#d1d4dc" font-size="12" font-weight="600">'
            f"Stage 2 participation · bulk ≥7/8 · {as_of}</text>",
            f'<line x1="{plot_l}" y1="{y_at(50):.1f}" x2="{plot_l + plot_w}" y2="{y_at(50):.1f}" '
            f'stroke="#434651" stroke-dasharray="4,4"/>',
            f'<polyline fill="none" stroke="#089981" stroke-width="2" points="{pts}"/>',
            f'<text x="{plot_l + plot_w}" y="20" text-anchor="end" fill="#089981" font-size="11">'
            f"{latest:.1f}%</text>",
            "</svg>",
        ]
    )


def _axis_bounds(points: list[dict[str, Any]], *, pad: float = 1.5) -> tuple[float, float, float, float]:
    xs = [p["rs_ratio"] for p in points]
    ys = [p["rs_momentum"] for p in points]
    xmin = min(min(xs), 100.0) - pad
    xmax = max(max(xs), 100.0) + pad
    ymin = min(min(ys), 100.0) - pad
    ymax = max(max(ys), 100.0) + pad
    return xmin, xmax, ymin, ymax


def load_rrg_scatter_points(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    length: int = DEFAULT_LENGTH,
) -> tuple[str, list[dict[str, Any]]]:
    close, _, _ = load_price_panels(conn)
    close.index = close.index.astype(str)
    if as_of not in close.index:
        valid = close.index[close.index <= as_of]
        if len(valid) == 0:
            raise ValueError(f"no stock panel on {as_of}")
        as_of = str(valid[-1])

    bench = load_benchmark_close(conn).reindex(close.index).ffill()
    sub_close = close.loc[:as_of].tail(BAR_LOOKBACK)
    sub_bench = bench.reindex(sub_close.index).ffill()
    rs_ratio, rs_mom, _quad = compute_rrg_panel(sub_close, sub_bench, length=length)
    if as_of not in rs_ratio.index:
        raise ValueError(f"RRG not ready on {as_of}")

    rrow = rs_ratio.loc[as_of]
    mrow = rs_mom.loc[as_of]
    dates = list(rs_ratio.index)
    idx = dates.index(as_of)
    tail_dates = dates[max(0, idx - RRG_TAIL_DAYS + 1) : idx + 1]

    points: list[dict[str, Any]] = []
    for sid in sub_close.columns:
        if sid not in rrow.index:
            continue
        rv = float(rrow[sid]) if pd.notna(rrow[sid]) else None
        mv = float(mrow[sid]) if pd.notna(mrow[sid]) else None
        if rv is None or mv is None:
            continue
        trail: list[tuple[float, float]] = []
        for d in tail_dates:
            if sid not in rs_ratio.columns:
                continue
            tr = rs_ratio.at[d, sid]
            tm = rs_mom.at[d, sid]
            if pd.notna(tr) and pd.notna(tm):
                trail.append((float(tr), float(tm)))
        quad = classify_quadrant(rv, mv)
        points.append(
            {
                "stock_id": str(sid),
                "rs_ratio": round(rv, 2),
                "rs_momentum": round(mv, 2),
                "quadrant": quad,
                "trail": trail,
            }
        )
    points.sort(key=lambda p: (-p["rs_ratio"], -p["rs_momentum"]))
    return as_of, points


def render_rrg_scatter_svg(
    points: list[dict[str, Any]],
    *,
    as_of: str,
    length: int = DEFAULT_LENGTH,
) -> str:
    if not points:
        return ""
    w, h = 640, 480
    margin = {"l": 48, "r": 16, "t": 32, "b": 44}
    plot_w = w - margin["l"] - margin["r"]
    plot_h = h - margin["t"] - margin["b"]
    all_xy = [(p["rs_ratio"], p["rs_momentum"]) for p in points]
    for p in points:
        all_xy.extend(p.get("trail") or [])
    xmin, xmax, ymin, ymax = _axis_bounds(
        [{"rs_ratio": x, "rs_momentum": y} for x, y in all_xy]
    )

    def sx(v: float) -> float:
        return margin["l"] + (v - xmin) / (xmax - xmin) * plot_w

    def sy(v: float) -> float:
        return margin["t"] + plot_h - (v - ymin) / (ymax - ymin) * plot_h

    x100, y100 = sx(100.0), sy(100.0)
    quad_rects = [
        ("leading", 100.0, 100.0, xmax, ymax),
        ("improving", xmin, 100.0, 100.0, ymax),
        ("weakening", 100.0, ymin, xmax, 100.0),
        ("lagging", xmin, ymin, 100.0, 100.0),
    ]
    rects: list[str] = []
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

    ticks: list[str] = []
    for v in _nice_ticks(xmin, xmax):
        x = sx(v)
        ticks.append(
            f'<line x1="{x:.1f}" y1="{margin["t"]}" x2="{x:.1f}" y2="{margin["t"] + plot_h}" '
            f'stroke="#333" stroke-dasharray="3,4"/>'
        )
    for v in _nice_ticks(ymin, ymax):
        y = sy(v)
        ticks.append(
            f'<line x1="{margin["l"]}" y1="{y:.1f}" x2="{margin["l"] + plot_w}" y2="{y:.1f}" '
            f'stroke="#333" stroke-dasharray="3,4"/>'
        )

    tails: list[str] = []
    for p in sorted(points, key=lambda x: -x["rs_ratio"])[:12]:
        trail = p.get("trail") or []
        if len(trail) < 2:
            continue
        quad = p["quadrant"] or "lagging"
        color = QUADRANT_COLORS.get(quad, "#888")
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in trail)
        tails.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="1.2" '
            f'opacity="0.65" points="{pts}"/>'
        )

    dots: list[str] = []
    for p in points:
        quad = p["quadrant"] or "lagging"
        cx, cy = sx(p["rs_ratio"]), sy(p["rs_momentum"])
        color = QUADRANT_COLORS.get(quad, "#888")
        dots.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{color}" '
            f'stroke="#111" stroke-width="0.8"/>'
        )

    labels: list[str] = []
    for p in sorted(points, key=lambda x: -x["rs_ratio"])[:10]:
        cx, cy = sx(p["rs_ratio"]), sy(p["rs_momentum"])
        labels.append(
            f'<text x="{cx + 6:.1f}" y="{cy + 3:.1f}" fill="#ccc" font-size="9">'
            f'{p["stock_id"]}</text>'
        )

    zone_labels = [
        (sx((xmin + 100) / 2), sy((100 + ymax) / 2), "improving", "Improving"),
        (sx((100 + xmax) / 2), sy((100 + ymax) / 2), "leading", "Leading"),
        (sx((100 + xmax) / 2), sy((ymin + 100) / 2), "weakening", "Weakening"),
        (sx((xmin + 100) / 2), sy((ymin + 100) / 2), "lagging", "Lagging"),
    ]
    zone_text = [
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" fill="{QUADRANT_COLORS[q]}" '
        f'font-size="11" opacity="0.5">{text}</text>'
        for x, y, q, text in zone_labels
    ]

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
{_SVG_FONT}
<rect width="{w}" height="{h}" fill="#141414"/>
{''.join(rects)}
{''.join(ticks)}
<line x1="{margin['l']}" y1="{y100:.1f}" x2="{margin['l'] + plot_w}" y2="{y100:.1f}" stroke="#666" stroke-width="1.2"/>
<line x1="{x100:.1f}" y1="{margin['t']}" x2="{x100:.1f}" y2="{margin['t'] + plot_h}" stroke="#666" stroke-width="1.2"/>
<circle cx="{x100:.1f}" cy="{y100:.1f}" r="3.5" fill="#fff" stroke="#666"/>
{''.join(zone_text)}
{''.join(tails)}
{''.join(dots)}
{''.join(labels)}
<text x="{margin['l'] + plot_w / 2:.1f}" y="{h - 6}" text-anchor="middle" fill="#bbb" font-size="10">JdK RS-Ratio →</text>
<text x="12" y="{margin['t'] + plot_h / 2:.1f}" text-anchor="middle" fill="#bbb" font-size="10"
      transform="rotate(-90 12 {margin['t'] + plot_h / 2:.1f})">RS-Momentum ↑</text>
<text x="{margin['l']}" y="20" fill="#ddd" font-size="12" font-weight="600">RRG scatter · {as_of} · WMA({length})</text>
<text x="{margin['l'] + plot_w}" y="20" text-anchor="end" fill="#888" font-size="10">{len(points)} 檔</text>
</svg>"""


def _write_svg(path: Path, svg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def _remove_legacy_flat_artifacts(track_dir: Path) -> None:
    """Drop pre-layered filenames at regime track root."""
    for pattern in (
        "breadth_spark.svg",
        "rrg_scatter.svg",
        "*_breadth_spark.svg",
        "*_rrg_scatter.svg",
        "*_regime_daily.md",
    ):
        for path in track_dir.glob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)


def _mirror_chart(latest: Path, snap: Path, svg: str) -> None:
    _write_svg(latest, svg)
    _write_svg(snap, svg)


def write_regime_charts(
    conn: sqlite3.Connection,
    as_of: str,
    track_dir: Path,
    *,
    trend_meta: dict[str, Any] | None = None,
    bench_code: str = "IX0001",
) -> RegimeChartPaths:
    """Write axis charts under axis/* and mirror into snapshots/{date}/."""
    track_dir.mkdir(parents=True, exist_ok=True)
    _remove_legacy_flat_artifacts(track_dir)

    snap_root = regime_snapshot_dir(track_dir, as_of)
    breadth_ok = zweig_ok = weinstein_ok = rrg_ok = stage2_ok = False

    panel = build_breadth_panel(conn, date_end=as_of)
    if not panel.empty:
        sub = panel[panel["trade_date"] <= as_of].tail(BREADTH_CHART_DAYS)
        if not sub.empty:
            svg = render_breadth_spark_svg(_breadth_records(sub))
            if svg:
                rel = REGIME_CHART_BREADTH
                _mirror_chart(track_dir / rel, snap_root / rel, svg)
                breadth_ok = True

    try:
        close, _, _ = load_price_panels(conn)
        impulse_panel = build_impulse_panel_from_close(close)
        impulse_panel.index = impulse_panel.index.astype(str)
        sub_z = impulse_panel[impulse_panel.index <= as_of].tail(BREADTH_CHART_DAYS)
        if not sub_z.empty and "zweig_ema" in sub_z.columns:
            svg = render_zweig_ema_spark_svg(sub_z["zweig_ema"])
            if svg:
                rel = REGIME_CHART_ZWEIG_EMA
                _mirror_chart(track_dir / rel, snap_root / rel, svg)
                zweig_ok = True
    except RuntimeError:
        pass

    meta = trend_meta or {}
    ix_df = _load_ix_df(conn, as_of, code=bench_code)
    if not ix_df.empty:
        svg = render_weinstein_weekly_svg(
            ix_df,
            bench=bench_code,
            stage=int(meta.get("stage") or 0),
            stage_name=str(meta.get("stage_name") or "unknown"),
        )
        if svg:
            rel = REGIME_CHART_WEINSTEIN
            _mirror_chart(track_dir / rel, snap_root / rel, svg)
            weinstein_ok = True

    try:
        rrg_date, rrg_points = load_rrg_scatter_points(conn, as_of)
        svg = render_rrg_scatter_svg(rrg_points, as_of=rrg_date)
        if svg:
            rel = REGIME_CHART_RRG
            _mirror_chart(track_dir / rel, snap_root / rel, svg)
            rrg_ok = True
    except (ValueError, RuntimeError):
        pass

    try:
        close, _, _ = load_price_panels(conn)
        close.index = close.index.astype(str)
        if as_of not in close.index:
            valid = close.index[close.index <= as_of]
            as_of_px = str(valid[-1]) if len(valid) else as_of
        else:
            as_of_px = as_of
        pct_series = vectorized_minervini_pass_pct(close.loc[:as_of_px], min_pass=7)
        svg = render_participation_spark_svg(pct_series, as_of=as_of_px)
        if svg:
            rel = REGIME_CHART_STAGE2
            _mirror_chart(track_dir / rel, snap_root / rel, svg)
            stage2_ok = True
    except RuntimeError:
        pass

    return RegimeChartPaths(
        breadth_spark=REGIME_CHART_BREADTH if breadth_ok else None,
        zweig_ema_spark=REGIME_CHART_ZWEIG_EMA if zweig_ok else None,
        weinstein_weekly=REGIME_CHART_WEINSTEIN if weinstein_ok else None,
        rrg_scatter=REGIME_CHART_RRG if rrg_ok else None,
        participation_spark=REGIME_CHART_STAGE2 if stage2_ok else None,
    )
