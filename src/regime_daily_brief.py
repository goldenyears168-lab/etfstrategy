#!/usr/bin/env python3
"""Regime layer daily brief · four-axis diagnostic memo."""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from typing import Any

from regime_charts import RegimeChartPaths, enrich_rrg_rotation_rankings, write_regime_charts
from regime_config import load_regime_config
from regime_daily_guide import (
    BRIEF_LEAD,
    BRIEF_TITLE_PREFIX,
    GUIDE_BREADTH,
    GUIDE_BREADTH_IMPULSE,
    GUIDE_BREADTH_LEVEL,
    GUIDE_BREADTH_RHYTHM,
    GUIDE_HEADER,
    GUIDE_MINERVINI_UNIVERSE,
    GUIDE_RRG,
    GUIDE_SYNOPSIS,
    GUIDE_TREND,
    MINERVINI_ROWS,
    PRODUCT_LAYER_ONCE,
    SEC_BREADTH,
    SEC_BREADTH_IMPULSE,
    SEC_BREADTH_LEVEL,
    SEC_BREADTH_RHYTHM,
    SEC_MINERVINI_UNIVERSE,
    SEC_RRG,
    SEC_SYNOPSIS,
    SEC_TREND,
    quadrant_display,
    stage_display,
    tail_display,
)
from regime_daily_html import render_regime_daily_html, render_regime_embed_html
from regime_interpret import (
    interpret_breadth_composite,
    interpret_breadth_impulse,
    interpret_breadth_level,
    interpret_breadth_rhythm,
    interpret_market_structure,
    interpret_rrg,
    interpret_stage2,
    interpret_trend,
)
from regime_snapshot import build_regime_snapshot
from report_paths import (
    canonical_daily_brief_path,
    canonical_daily_track_dir,
    ensure_daily_dir,
    regime_snapshot_brief_path,
)
from stage_analysis import STAGE_NAMES
from stock_db import DEFAULT_DB_PATH, connect

STRATEGY_ID = "regime-daily"


def _regime_html_enabled() -> bool:
    return os.environ.get("RUN_REGIME_EMBED_HTML", "0").strip() == "1"


def _guide_block(text: str) -> list[str]:
    lines: list[str] = [""]
    for part in text.split("\n"):
        lines.append(f"> {part}" if part.strip() else ">")
    lines.append("")
    return lines


def _latest_ix_date(conn, *, code: str = "IX0001") -> str | None:
    row = conn.execute(
        """
        SELECT MAX(date) AS d FROM daily_bars
        WHERE code = ? AND source = 'tej'
        """,
        (code,),
    ).fetchone()
    if not row or not row["d"]:
        return None
    return str(row["d"])


def _fmt_delta(val: float | None, *, suffix: str = "pp") -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}{suffix}"


def _minervini_table(minervini: dict[str, Any] | None) -> list[str]:
    if not minervini:
        return ["—"]
    flags = minervini.get("criteria") or []
    detail = minervini.get("criteria_detail") or {}
    lines = [
        "",
        "| Criterion | 說明 | Pass |",
        "|-----------|------|------|",
    ]
    for i, (key, _en, zh) in enumerate(MINERVINI_ROWS):
        passed: bool | None = None
        if key in detail:
            passed = bool(detail[key].get("passed"))
        elif i < len(flags):
            passed = bool(flags[i])
        mark = "✓" if passed else ("✗" if passed is False else "—")
        if key == "c8_rs_rank_above_70" and i >= len(flags):
            mark = "—（略過）"
        lines.append(f"| {_en} | {zh} | {mark} |")
    met = minervini.get("criteria_met")
    total = minervini.get("criteria_total")
    lines.append("")
    lines.append(f"*Summary: {met}/{total} passed*")
    return lines


def _chart_line(charts: RegimeChartPaths | None, attr: str, alt: str) -> list[str]:
    if not charts:
        return []
    rel = getattr(charts, attr, None)
    if not rel:
        return []
    return ["", f"![{alt}]({rel})", ""]


def _rrg_ranked_table_md(r: dict[str, Any]) -> list[str]:
    rows = r.get("ranked_symbols") or []
    if not rows:
        return []
    lines = [
        "",
        "### RRG symbol table（Kempenaer · StockCharts）",
        "",
        "依象限排序；RS-Ratio（JdK）· RS-Momentum · 4-day tail。",
        "",
        "| Quadrant | Symbol | RS-Ratio | RS-Mom | Tail |",
        "|----------|--------|----------|--------|------|",
    ]
    for row in rows:
        q = row.get("quadrant") or "—"
        lines.append(
            f"| {quadrant_display(q)} | {row.get('stock_id')} | "
            f"{row.get('rs_ratio')} | {row.get('rs_momentum')} | "
            f"{tail_display(row.get('tail_dir'))} |"
        )
    return lines


def render_regime_daily_markdown(
    snap: dict[str, Any],
    *,
    ref: str,
    bench: str,
    charts: RegimeChartPaths | None = None,
) -> str:
    b = snap["breadth_zone_200"]
    t = snap["trend_posture"]
    r = snap["rrg_rotation"]
    s = snap["stage2_participation"]
    stamp = ref.replace("-", "")

    lines: list[str] = [
        f"# {BRIEF_TITLE_PREFIX} · {ref}",
        "",
        f"> {BRIEF_LEAD}",
        "",
        *_guide_block(PRODUCT_LAYER_ONCE),
        *_guide_block(GUIDE_HEADER),
        "> **含圖請開** [`daily_brief.html`](daily_brief.html)（瀏覽器）· "
        "Cursor Markdown 預覽**不顯示**本地 SVG。",
        "",
        f"## {SEC_SYNOPSIS}",
        "",
        *_guide_block(GUIDE_SYNOPSIS),
        interpret_market_structure(b, t, r, s, bench=bench),
        "",
        "---",
        "",
        f"## {SEC_BREADTH}",
        "",
        *_guide_block(GUIDE_BREADTH),
        "",
        f"### {SEC_BREADTH_LEVEL}",
        "",
        f"**{b.get('display', '—')}**" if b.get("available") else "—",
        "",
    ]

    if b.get("available"):
        lines.extend(_guide_block(GUIDE_BREADTH_LEVEL))
        lines.extend(
            [
                "**Notes**",
                "",
                interpret_breadth_level(b),
                "",
                "| Reading | Value |",
                "|---------|-------|",
                f"| % above 200-day MA | {b.get('pct_above_200')}% |",
                f"| % above 50-day MA | {b.get('pct_above_50')}% |",
                f"| 5d Δ (50 / 200) | {_fmt_delta(b.get('pct50_delta_5d'))} / "
                f"{_fmt_delta(b.get('pct200_delta_5d'))} |",
                f"| 50 vs 200 spread | {b.get('participation_gap')}pp |",
                f"| Advance/decline divergence | {'yes' if b.get('divergence_flag') else 'no'} |",
                f"| Universe n | {b.get('n_valid')} |",
            ]
        )
        lines.extend(
            _chart_line(charts, "breadth_spark", "% Above MA · index + breadth panel")
        )

        rhythm = b.get("rhythm") or {}
        lines.extend(["", f"### {SEC_BREADTH_RHYTHM}", ""])
        if rhythm.get("available"):
            lines.extend(_guide_block(GUIDE_BREADTH_RHYTHM))
            lines.extend(
                [
                    f"**{rhythm.get('display', '—')}**",
                    "",
                    "**Notes**",
                    "",
                    interpret_breadth_rhythm(rhythm),
                    "",
                    "| Reading | Value |",
                    "|---------|-------|",
                    f"| Zweig adv/decl 10-day EMA | {rhythm.get('zweig_ema_pct')}% |",
                    f"| Rhythm tier | {rhythm.get('zweig_ema_tier')} |",
                    f"| 5d Δ | {_fmt_delta(rhythm.get('zweig_ema_delta_5d'))} |",
                ]
            )
            lines.extend(
                _chart_line(charts, "zweig_ema_spark", "Zweig EMA rhythm · 90d")
            )
        else:
            lines.append(str(rhythm.get("error", "N/A")))

        imp = b.get("impulse") or {}
        lines.extend(["", f"### {SEC_BREADTH_IMPULSE}", ""])
        if imp.get("available"):
            lines.extend(_guide_block(GUIDE_BREADTH_IMPULSE))
            lines.extend(
                [
                    "**Notes**",
                    "",
                    interpret_breadth_impulse(imp),
                    "",
                    "| Reading | Value |",
                    "|---------|-------|",
                    f"| Deemer 10-day adv/decl | {imp.get('deemer_ratio')} |",
                    f"| Zweig thrust today | {'yes' if imp.get('zweig_thrust_today') else 'no'} |",
                    f"| Deemer BAM today | {'yes' if imp.get('deemer_bam_today') else 'no'} |",
                    f"| Thrust window active | {'yes' if imp.get('thrust_active') else 'no'} |",
                    f"| Days remaining | {imp.get('thrust_days_remaining')} / {imp.get('thrust_hold_days')} |",
                ]
            )
        else:
            lines.append(str(imp.get("error", "N/A")))

        composite = interpret_breadth_composite(b)
        if composite:
            lines.extend(["", "**Combined read**", "", composite])
    else:
        lines.append(str(b.get("error", "N/A")))

    lines.extend(["", f"## {SEC_TREND}", ""])

    if t.get("available"):
        stage = t.get("stage")
        stage_name = t.get("stage_name") or STAGE_NAMES.get(int(stage or 0), "unknown")
        lines.extend(_guide_block(GUIDE_TREND))
        lines.extend(
            [
                f"### {bench} · **{stage_display(stage, stage_name)}**",
                "",
                "**Notes**",
                "",
                interpret_trend(t, bench=bench),
                "",
                "| Reading | Value |",
                "|---------|-------|",
            ]
        )
        w = t.get("weinstein") or {}
        hl = w.get("higher_lows")
        lines.extend(
            [
                f"| 30-week MA slope | {w.get('ma_slope_pct')}% |",
                f"| Extension vs 30-week MA | {w.get('extension_pct')}% |",
                f"| Higher lows | {'yes' if hl else 'no'} |",
                f"| Price above 30-week MA | {'yes' if w.get('price_above_ma30') else 'no'} |",
            ]
        )
        lines.extend(
            _chart_line(charts, "weinstein_weekly", f"{bench} weekly · Weinstein Stage ribbon")
        )
        lines.extend(["", f"### Minervini Trend Template · {bench}", ""])
        lines.extend(_minervini_table(t.get("minervini")))
    else:
        lines.append(str(t.get("error", "N/A")))

    lines.extend(["", f"## {SEC_RRG}", ""])

    if r.get("available"):
        health = float(r.get("rotation_health_pct") or 0)
        lines.extend(_guide_block(GUIDE_RRG))
        lines.extend(
            [
                f"**Leading + Improving: {health:.1f}%** · n={r.get('universe_n')}",
                "",
                "**Notes**",
                "",
                interpret_rrg(r),
                "",
                "| Quadrant | Count | Share |",
                "|----------|------:|------:|",
            ]
        )
        for q in ("leading", "improving", "weakening", "lagging"):
            lines.append(
                f"| {quadrant_display(q)} | {r['counts'].get(q, 0)} | "
                f"{r['pct'].get(q, 0)}% |"
            )
        mig = r.get("migrations") or {}
        if any(mig.values()):
            lines.extend(
                [
                    "",
                    "| 1-day quadrant migration | Count |",
                    "|--------------------------|------:|",
                    f"| Improving → Leading | {mig.get('improving_to_leading', 0)} |",
                    f"| Leading → Weakening | {mig.get('leading_to_weakening', 0)} |",
                    f"| Lagging → Improving | {mig.get('lagging_to_improving', 0)} |",
                    f"| Weakening → Lagging | {mig.get('weakening_to_lagging', 0)} |",
                ]
            )
        lines.extend(_rrg_ranked_table_md(r))
        lines.extend(_chart_line(charts, "rrg_scatter", "RRG scatter · Kempenaer"))
    else:
        lines.append(str(r.get("error", "N/A")))

    lines.extend(["", f"## {SEC_MINERVINI_UNIVERSE}", ""])

    if s.get("available"):
        lines.extend(_guide_block(GUIDE_MINERVINI_UNIVERSE))
        lines.extend(
            [
                f"**Pass rate {s.get('pass_pct')}%** · {s.get('note')}",
                "",
                "**Notes**",
                "",
                interpret_stage2(s, b),
                "",
                "| Reading | Value |",
                "|---------|-------|",
                f"| Pass rate | {s.get('pass_pct')}% |",
                f"| 5d Δ | {_fmt_delta(s.get('pass_delta_5d'))} |",
                f"| Universe n | {s.get('universe_n')} |",
            ]
        )
        lines.extend(
            _chart_line(charts, "participation_spark", "Minervini universe pass rate · 90d")
        )
    else:
        lines.append(str(s.get("error", "N/A")))

    lines.extend(
        [
            "",
            "---",
            f"config: `config/regime.yaml` · 基準 {bench} · 資料日 {ref} · "
            f"快照 `snapshots/{stamp}/`",
        ]
    )
    return "\n".join(lines) + "\n"


def build_regime_daily_markdown(
    conn,
    *,
    as_of: str | None = None,
    charts: RegimeChartPaths | None = None,
) -> str:
    cfg = load_regime_config()
    bench = str(cfg.get("benchmark_code") or "IX0001")
    ref = as_of or _latest_ix_date(conn, code=bench) or date.today().isoformat()
    snap = build_regime_snapshot(conn, ref, benchmark_code=bench)
    enrich_rrg_rotation_rankings(snap, conn, ref)
    return render_regime_daily_markdown(snap, ref=ref, bench=bench, charts=charts)


def write_regime_daily_reports(
    conn,
    *,
    track_dir: Path | None = None,
    as_of: str | None = None,
    quiet: bool = False,
) -> Path:
    ensure_daily_dir()
    if track_dir is not None:
        out_track = track_dir
        latest = track_dir / "daily_brief.md"
    else:
        latest = canonical_daily_brief_path(STRATEGY_ID)
        out_track = canonical_daily_track_dir(STRATEGY_ID)
    out_track.mkdir(parents=True, exist_ok=True)
    cfg = load_regime_config()
    bench = str(cfg.get("benchmark_code") or "IX0001")
    ref = as_of or _latest_ix_date(conn, code=bench) or date.today().isoformat()
    snap = build_regime_snapshot(conn, ref, benchmark_code=bench)
    enrich_rrg_rotation_rankings(snap, conn, ref)
    trend = snap.get("trend_posture") or {}
    charts = write_regime_charts(
        conn,
        ref,
        out_track,
        bench_code=bench,
        trend_meta={
            "stage": trend.get("stage"),
            "stage_name": trend.get("stage_name"),
        },
    )
    md = render_regime_daily_markdown(snap, ref=ref, bench=bench, charts=charts)
    snap_brief = regime_snapshot_brief_path(out_track, ref)
    snap_brief.parent.mkdir(parents=True, exist_ok=True)
    snap_brief.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    if _regime_html_enabled():
        html_out = render_regime_daily_html(
            snap, ref=ref, bench=bench, charts=charts, track_dir=out_track
        )
        embed_out = render_regime_embed_html(
            snap, ref=ref, bench=bench, charts=charts, track_dir=out_track
        )
        (snap_brief.parent / "daily_brief.html").write_text(html_out, encoding="utf-8")
        (snap_brief.parent / "daily_brief.embed.html").write_text(
            embed_out, encoding="utf-8"
        )
        (out_track / "daily_brief.html").write_text(html_out, encoding="utf-8")
        (out_track / "daily_brief.embed.html").write_text(embed_out, encoding="utf-8")
    if not quiet:
        print(f"Wrote {latest}" + (" + daily_brief.html" if _regime_html_enabled() else ""))
    return latest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Regime four-axis diagnostic daily brief")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: latest IX)")
    p.add_argument("--write-reports", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--human", action="store_true", help="Print markdown to stdout")
    args = p.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.write_reports or args.human:
            path = write_regime_daily_reports(conn, as_of=args.as_of, quiet=args.quiet)
            if args.human:
                print(path.read_text(encoding="utf-8"))
        else:
            print(build_regime_daily_markdown(conn, as_of=args.as_of))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
