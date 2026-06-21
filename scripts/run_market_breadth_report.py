#!/usr/bin/env python3
"""Render TradingView-style % Above 50/200 MA breadth report (standalone HTML)."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_breadth_ma import (  # noqa: E402
    BREADTH_ZONE_COLOR,
    BREADTH_ZONE_DISPLAY,
    BREADTH_ZONE_ZH,
    BREADTH_ZONES_ORDER,
    REF_LEVELS_200,
    REF_LEVELS_50,
    build_breadth_panel,
    divergence_events,
    monthly_breadth_summary,
)
from stock_db import PROJECT_ROOT  # noqa: E402

from report_paths import research_html_path

# SVG text does not inherit HTML body fonts; set CJK-capable stack explicitly.
_SVG_FONT_STYLE = (
    '<style type="text/css"><![CDATA['
    'text { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", "Noto Sans TC", sans-serif; }'
    ']]></style>'
)


def _panel_records(panel) -> list[dict]:
    rows: list[dict] = []
    for r in panel.itertuples():
        z200 = str(r.zone_200)
        rows.append(
            {
                "d": str(r.trade_date),
                "p50": round(float(r.pct_above_50), 2),
                "p200": round(float(r.pct_above_200), 2),
                "z50": str(r.zone_50),
                "z200": z200,
                "z200zh": BREADTH_ZONE_ZH.get(z200, z200),
                "c": BREADTH_ZONE_COLOR.get(z200, "#888899"),
                "bench": round(float(r.bench_close), 2) if r.bench_close is not None else None,
                "div": bool(r.divergence_flag),
            }
        )
    return rows


def _svg_tv_breadth_panels(points: list[dict]) -> str:
    """Classic two-panel layout: benchmark (top) + dual breadth lines (bottom)."""
    if not points:
        return ""
    w = 960
    pad_l, pad_r = 58, 20
    plot_l, plot_r = pad_l, w - pad_r
    top_h, gap, bot_h = 200, 28, 260
    total_h = top_h + gap + bot_h + 36
    n = len(points)
    bench = [p["bench"] for p in points if p.get("bench") is not None]
    if len(bench) < 2:
        return _svg_breadth_only(points)

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
        return top_h - 24 - (v - b_lo) / max(b_hi - b_lo, 1e-9) * (top_h - 44)

    def y_bot(v: float) -> float:
        y0 = top_h + gap
        return y0 + bot_h - 32 - v / 100.0 * (bot_h - 52)

    y0 = top_h + gap
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{total_h}" viewBox="0 0 {w} {total_h}">',
        _SVG_FONT_STYLE,
        '<rect width="100%" height="100%" fill="#131722"/>',
        f'<text x="{plot_l}" y="18" fill="#d1d4dc" font-size="13" font-weight="600">'
        f"IX0001 vs % Stocks Above Moving Average</text>",
        f'<text x="{plot_r}" y="18" text-anchor="end" fill="#787b86" font-size="10">'
        f"TradingView S5TH / S5FI style · local universe</text>",
    ]

    # --- top: benchmark ---
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = 24 + (top_h - 44) * (1 - frac)
        lines.append(
            f'<line x1="{plot_l}" y1="{y:.1f}" x2="{plot_r}" y2="{y:.1f}" stroke="#2a2e39" stroke-width="1"/>'
        )
    if b_lo < 0 < b_hi:
        zy = y_top(0.0)
        lines.append(
            f'<line x1="{plot_l}" y1="{zy:.1f}" x2="{plot_r}" y2="{zy:.1f}" '
            f'stroke="#434651" stroke-width="1"/>'
        )
    pts_b = " ".join(
        f"{x_at(i):.1f},{y_top(v):.1f}" for i, v in enumerate(bench_idx) if v is not None
    )
    lines.append(f'<polyline fill="none" stroke="#2962ff" stroke-width="1.8" points="{pts_b}"/>')
    lines.append(
        f'<text x="{plot_l}" y="{top_h - 6}" fill="#787b86" font-size="10">'
        f"加權指數 IX0001 · 區間報酬 %</text>"
    )

    # separator
    lines.append(
        f'<line x1="{plot_l}" y1="{y0:.0f}" x2="{plot_r}" y2="{y0:.0f}" stroke="#434651" stroke-width="1.2"/>'
    )

    # --- bottom: breadth zones + reference lines ---
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
            f'stroke="#434651" stroke-width="1" stroke-dasharray="4,4"/>'
        )
        lines.append(
            f'<text x="{plot_l - 4}" y="{y + 3:.1f}" text-anchor="end" fill="#787b86" font-size="9">'
            f"{ref:.0f}</text>"
        )

    pts50 = " ".join(f"{x_at(i):.1f},{y_bot(p['p50']):.1f}" for i, p in enumerate(points))
    pts200 = " ".join(f"{x_at(i):.1f},{y_bot(p['p200']):.1f}" for i, p in enumerate(points))
    lines.append(f'<polyline fill="none" stroke="#089981" stroke-width="2" points="{pts50}"/>')
    lines.append(f'<polyline fill="none" stroke="#f23645" stroke-width="2" points="{pts200}"/>')

    for i, p in enumerate(points):
        if p.get("div"):
            x = x_at(i)
            lines.append(
                f'<line x1="{x:.1f}" y1="{y0 + 4}" x2="{x:.1f}" y2="{y0 + bot_h - 8}" '
                f'stroke="#f23645" stroke-width="1" opacity="0.35"/>'
            )

    lines.append(
        f'<text x="{plot_l}" y="{y0 + 16}" fill="#089981" font-size="10">'
        f"— % above 50 MA ({points[-1]['p50']:.1f}%)</text>"
    )
    lines.append(
        f'<text x="{plot_l + 200}" y="{y0 + 16}" fill="#f23645" font-size="10">'
        f"— % above 200 MA ({points[-1]['p200']:.1f}%)</text>"
    )
    lines.append(
        f'<text x="{plot_l}" y="{total_h - 6}" fill="#787b86" font-size="10">{points[0]["d"]}</text>'
    )
    lines.append(
        f'<text x="{plot_r}" y="{total_h - 6}" text-anchor="end" fill="#787b86" font-size="10">'
        f'{points[-1]["d"]}</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines)


def _svg_breadth_only(points: list[dict]) -> str:
    """Fallback single-panel breadth chart."""
    w, h = 960, 320
    pad_l, pad_r, pad_t, pad_b = 58, 20, 36, 44
    plot_l, plot_r = pad_l, w - pad_r
    n = len(points)

    def x_at(i: int) -> float:
        return plot_l + i / max(n - 1, 1) * (plot_r - plot_l)

    def y_at(v: float) -> float:
        return h - pad_b - v / 100.0 * (h - pad_t - pad_b)

    lines = [
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        _SVG_FONT_STYLE,
        '<rect width="100%" height="100%" fill="#131722"/>',
    ]
    for ref in REF_LEVELS_50:
        y = y_at(ref)
        lines.append(
            f'<line x1="{plot_l}" y1="{y:.1f}" x2="{plot_r}" y2="{y:.1f}" stroke="#434651" stroke-dasharray="4,4"/>'
        )
    pts50 = " ".join(f"{x_at(i):.1f},{y_at(p['p50']):.1f}" for i, p in enumerate(points))
    pts200 = " ".join(f"{x_at(i):.1f},{y_at(p['p200']):.1f}" for i, p in enumerate(points))
    lines.append(f'<polyline fill="none" stroke="#089981" stroke-width="2" points="{pts50}"/>')
    lines.append(f'<polyline fill="none" stroke="#f23645" stroke-width="2" points="{pts200}"/>')
    lines.append("</svg>")
    return "\n".join(lines)


def _svg_zone_timeline(points: list[dict], *, key: str = "z200") -> str:
    if not points:
        return ""
    w, bar_h, header = 960, 14, 24
    n = len(points)
    seg_w = (w - 48) / n
    x0 = 24
    lines = [
        f'<svg width="{w}" height="{header + bar_h + 8}" viewBox="0 0 {w} {header + bar_h + 8}">',
        _SVG_FONT_STYLE,
        '<rect width="100%" height="100%" fill="#131722"/>',
        f'<text x="24" y="16" fill="#787b86" font-size="11">200MA 廣度區間 · zone_200</text>',
    ]
    for i, p in enumerate(points):
        x = x0 + i * seg_w
        color = BREADTH_ZONE_COLOR.get(p[key], "#888")
        tip = f"{p['d']} · 50MA {p['p50']:.1f}% · 200MA {p['p200']:.1f}% · {p['z200zh']}"
        lines.append(
            f'<rect x="{x:.2f}" y="{header}" width="{max(seg_w, 0.5):.2f}" height="{bar_h}" '
            f'fill="{color}" opacity="0.95"><title>{tip}</title></rect>'
        )
    lines.append("</svg>")
    return "\n".join(lines)


def _zone_summary_table(points: list[dict]) -> str:
    from collections import Counter

    counts = Counter(p["z200"] for p in points)
    total = len(points) or 1
    rows: list[str] = []
    for slug in BREADTH_ZONES_ORDER:
        cnt = counts.get(slug, 0)
        if cnt == 0:
            continue
        color = BREADTH_ZONE_COLOR[slug]
        label = BREADTH_ZONE_DISPLAY[slug]
        rows.append(
            f'<tr><td><span style="color:{color}">●</span> {label}</td>'
            f"<td>{cnt}</td><td>{cnt / total * 100:.1f}%</td></tr>"
        )
    return (
        '<table class="hypo"><tr><th>200MA 區間</th><th>天數</th><th>占比</th></tr>'
        + "".join(rows)
        + "</table>"
    )


def _monthly_table(monthly) -> str:
    if monthly.empty:
        return ""
    rows = []
    for r in monthly.itertuples():
        color = BREADTH_ZONE_COLOR.get(str(r.dominant_zone_200), "#888")
        rows.append(
            f"<tr><td>{r.month}</td><td>{int(r.days)}</td>"
            f"<td>{r.pct50_mean:.1f}%</td><td>{r.pct200_mean:.1f}%</td>"
            f"<td>{r.pct200_min:.1f}–{r.pct200_max:.1f}%</td>"
            f'<td><span style="color:{color}">{r.dominant_zone_200_zh}</span></td>'
            f"<td>{int(r.divergence_days)}</td></tr>"
        )
    return (
        '<table class="hypo"><tr><th>月</th><th>日</th><th>50MA均</th><th>200MA均</th>'
        "<th>200MA區間</th><th>主區間</th><th>背離日</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _divergence_table(events: list[dict]) -> str:
    if not events:
        return '<p class="muted">此區間無指數漲 / 50MA 廣度降之背離叢集。</p>'
    rows = []
    for e in events[-24:]:
        b20 = f"{e['bench_ret_20d_pct']:+.1f}%" if e["bench_ret_20d_pct"] is not None else "—"
        d50 = f"{e['pct50_chg_20d']:+.1f}%" if e["pct50_chg_20d"] is not None else "—"
        rows.append(
            f"<tr><td>{e['trade_date']}</td><td>{e['pct_above_50']:.1f}%</td>"
            f"<td>{e['pct_above_200']:.1f}%</td><td>{b20}</td><td>{d50}</td></tr>"
        )
    return (
        '<table class="hypo"><tr><th>日期</th><th>%&gt;50MA</th><th>%&gt;200MA</th>'
        "<th>指數20日</th><th>50MA廣度20日Δ</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _today_card(latest: dict) -> str:
    color = latest["c"]
    return f"""
    <div class="today" style="border-color:{color};background:{color}22;">
      <div class="meta">最新 · {latest['d']}</div>
      <div class="title">{latest['z200zh']} <span class="sub">(200MA 廣度 {latest['p200']:.1f}%)</span></div>
      <p class="vals">
        <span style="color:#089981">50MA {latest['p50']:.1f}%</span> ·
        <span style="color:#f23645">200MA {latest['p200']:.1f}%</span>
        {' · <span style="color:#f23645">背離</span>' if latest.get('div') else ''}
      </p>
    </div>"""


def _legend() -> str:
    parts = []
    for z in BREADTH_ZONES_ORDER:
        parts.append(
            f'<span><i style="background:{BREADTH_ZONE_COLOR[z]}"></i>'
            f"{BREADTH_ZONE_ZH[z]} ({_zone_range_label(z)})</span>"
        )
    return "".join(parts)


def _zone_range_label(z: str) -> str:
    return {
        "oversold": "<20%",
        "weak": "20–40%",
        "neutral": "40–60%",
        "strong": "60–80%",
        "overbought": ">80%",
    }.get(z, "")


def render_breadth_html(*, points: list[dict], monthly, events: list[dict], meta: dict) -> str:
    latest = points[-1]
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Market Breadth · 200MA Breadth Zones · {meta['date_start']}–{meta['date_end']}</title>
  <style>
    :root {{ --bg:#131722; --panel:#1e222d; --text:#d1d4dc; --muted:#787b86; --border:#2a2e39; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,"PingFang TC",sans-serif; line-height:1.5; }}
    .wrap {{ max-width:1000px; margin:0 auto; padding:20px 16px 48px; }}
    h1 {{ font-size:20px; margin:0 0 6px; font-weight:600; }}
    .lead {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
    .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:4px; padding:12px; margin-bottom:16px; overflow-x:auto; }}
    h2 {{ font-size:12px; margin:0 0 10px; color:var(--muted); font-weight:500; text-transform:uppercase; letter-spacing:.04em; }}
    .today {{ border:1px solid; border-radius:4px; padding:14px 16px; margin-bottom:16px; }}
    .today .meta {{ font-size:11px; color:var(--muted); }}
    .today .title {{ font-size:18px; font-weight:600; margin:6px 0; }}
    .today .sub {{ font-size:14px; color:var(--muted); font-weight:400; }}
    .today .vals {{ font-size:13px; color:var(--muted); margin:0; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:10px 16px; font-size:12px; color:var(--muted); margin:12px 0 16px; }}
    .legend span {{ display:inline-flex; align-items:center; gap:6px; }}
    .legend i {{ width:10px; height:10px; border-radius:2px; display:inline-block; }}
    table.hypo {{ width:100%; border-collapse:collapse; font-size:12px; }}
    table.hypo th, table.hypo td {{ border:1px solid var(--border); padding:8px; text-align:left; }}
    table.hypo th {{ background:#2a2e39; color:var(--muted); font-weight:500; }}
    .muted {{ color:var(--muted); font-size:13px; }}
    details {{ background:var(--panel); border:1px solid var(--border); border-radius:4px; padding:12px; margin-top:12px; }}
    summary {{ cursor:pointer; color:var(--text); font-size:13px; }}
    details li {{ color:var(--muted); font-size:12px; }}
    .foot {{ font-size:11px; color:var(--muted); margin-top:24px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Market Breadth · % Above MA</h1>
    <p class="lead">
      <strong>Breadth zone</strong>（200MA 廣度區間 · 非 swing risk posture gate）·
      對標 TradingView <code>INDEX:S5TH</code> / <code>S5FI</code> ·
      綠線 = % above 50 MA · 紅線 = % above 200 MA ·
      Universe {meta.get('universe_n')} 檔 · IX0001 ·
      {meta['date_start']}–{meta['date_end']} · {len(points)} 交易日
    </p>

    {_today_card(latest)}
    <div class="legend">{_legend()}</div>

    <div class="panel">
      <h2>Price + Breadth（TradingView 雙面板）</h2>
      {_svg_tv_breadth_panels(points)}
    </div>

    <div class="panel">
      <h2>200MA Breadth zone 色帶</h2>
      {_svg_zone_timeline(points)}
      {_zone_summary_table(points)}
    </div>

    <div class="panel">
      <h2>月度摘要</h2>
      {_monthly_table(monthly)}
    </div>

    <div class="panel">
      <h2>背離事件（指數漲 / 50MA 廣度降）</h2>
      {_divergence_table(events)}
    </div>

    <details>
      <summary>方法 · 與 RRG 分工</summary>
      <ul>
        <li>每日計算 universe 內收盤 &gt; MA50 / MA200 的占比（0–100%）。</li>
        <li>200MA <strong>Breadth zone</strong> 五區間：&lt;20 超賣 · 20–40 偏弱 · 40–60 中性 · 60–80 強勢 · &gt;80 過熱。</li>
        <li>與 <strong>Trend posture</strong>（IX0001 stage）及 copytrade <code>exposure_decision</code>（ex-post 分桶標籤）為不同維度。</li>
        <li><b>RRG holdings change</b>：持股相對輪動；<b>廣度</b>：整體參與度 — 互補。</li>
      </ul>
    </details>
    <p class="foot">研究用途 · 非投資建議 · 本地 FinMind 成分 universe，非 TWSE 官方 advance-decline。</p>
  </div>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradingView-style % Above MA breadth report")
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-12-31")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    panel = build_breadth_panel(date_start=args.date_start, date_end=args.date_end)
    if panel.empty:
        print("ERROR: no breadth data", file=sys.stderr)
        return 1

    points = _panel_records(panel)
    monthly = monthly_breadth_summary(panel)
    events = divergence_events(panel)
    meta = {
        "date_start": args.date_start,
        "date_end": args.date_end,
        "universe_n": int(panel.iloc[-1]["n_valid_50"]),
        "p50_mean": round(float(panel["pct_above_50"].mean()), 1),
        "p200_mean": round(float(panel["pct_above_200"].mean()), 1),
        "divergence_days": int(panel["divergence_flag"].sum()),
    }

    stamp = date.today().strftime("%Y%m%d")
    out = args.output or research_html_path(
        "breadth", f"{stamp}_market_breadth_ma_{args.date_start[:4]}_{args.date_end[:4]}.html"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_breadth_html(points=points, monthly=monthly, events=events, meta=meta), encoding="utf-8")
    print(f"Wrote {out} ({len(points)} days)")

    if args.json:
        payload = {"meta": meta, "points": points, "monthly": monthly.to_dict(orient="records"), "events": events}
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    latest = points[-1]
    print(f"Latest {latest['d']}: 200MA {latest['p200']:.1f}% ({latest['z200zh']}) · 50MA {latest['p50']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
