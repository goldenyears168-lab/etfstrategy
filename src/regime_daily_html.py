"""Regime daily brief · HTML view (inline SVG — for browser)."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from regime_charts import RegimeChartPaths
from regime_daily_guide import (
    BRIEF_LEAD,
    BRIEF_TITLE_PREFIX,
    BRIEF_TITLE_ZH,
    GUIDE_BREADTH,
    GUIDE_BREADTH_IMPULSE,
    GUIDE_BREADTH_LEVEL,
    GUIDE_BREADTH_RHYTHM,
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
from stage_analysis import STAGE_NAMES

_STYLES = """
:root { --bg:#0d1117; --panel:#161b22; --text:#e6edf3; --muted:#8b949e; --border:#30363d; --accent:#58a6ff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif; line-height:1.55; }
.wrap { max-width:920px; margin:0 auto; padding:24px 20px 48px; }
h1 { font-size:1.5rem; margin:0 0 8px; }
.lead { color:var(--muted); font-size:0.9rem; margin:0 0 20px; }
.banner { background:#1f3a5f; border:1px solid #388bfd44; border-radius:8px;
  padding:12px 16px; margin-bottom:24px; font-size:0.92rem; }
.banner a { color:var(--accent); }
section { background:var(--panel); border:1px solid var(--border); border-radius:8px;
  padding:16px 18px; margin-bottom:20px; }
h2 { font-size:1.1rem; margin:0 0 12px; border-bottom:1px solid var(--border); padding-bottom:8px; }
h3 { font-size:0.95rem; margin:16px 0 8px; color:var(--muted); }
.interpret { background:#0d1117; border-left:3px solid var(--accent); padding:10px 14px;
  margin:12px 0; font-size:0.92rem; color:#c9d1d9; }
.guide { background:#161b22; border:1px solid var(--border); border-radius:6px;
  padding:12px 14px; margin:10px 0 14px; font-size:0.88rem; color:#b1bac4; line-height:1.6; }
.guide ul { margin:8px 0 0 18px; padding:0; }
.guide li { margin:4px 0; }
.chart { margin:14px 0; overflow-x:auto; border-radius:6px; border:1px solid var(--border); }
.chart svg { display:block; max-width:100%; height:auto; }
table { width:100%; border-collapse:collapse; font-size:0.88rem; margin:10px 0; }
th, td { border:1px solid var(--border); padding:6px 10px; text-align:left; }
th { background:#21262d; color:var(--muted); font-weight:500; }
td.num { text-align:right; }
.quad-leading { color:#3fb950; } .quad-improving { color:#58a6ff; }
.quad-weakening { color:#d29922; } .quad-lagging { color:#f85149; }
.pass-y { color:#3fb950; } .pass-n { color:#f85149; }
.foot { color:var(--muted); font-size:0.8rem; margin-top:24px; }
strong.hl { color:#fff; }
.kpi-strip { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin:0 0 20px; }
.kpi { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
.kpi-label { font-size:0.72rem; color:var(--muted); margin:0 0 4px; }
.kpi-value { font-size:1.15rem; font-weight:600; margin:0; line-height:1.3; }
.kpi-sub { font-size:0.78rem; color:var(--muted); margin:4px 0 0; }
.subtitle { color:var(--muted); font-size:0.85rem; margin:-4px 0 16px; }
@media (max-width:720px) {
  .kpi-strip { grid-template-columns:repeat(2, minmax(0,1fr)); }
  .chart { -webkit-overflow-scrolling:touch; }
  table { display:block; overflow-x:auto; }
}
"""

_EMBED_SCOPE = ".regime-embed"


def _scoped_styles(scope: str) -> str:
    out: list[str] = []
    for line in _STYLES.strip().splitlines():
        s = line.strip()
        if not s or s.startswith("@media"):
            out.append(line)
            continue
        if s.startswith(":root"):
            out.append(s.replace(":root", scope, 1))
        elif s.startswith("*"):
            out.append(f"{scope} {s}")
        else:
            out.append(f"{scope} {s}")
    return "\n".join(out)


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _rich_text(text: object) -> str:
    """Escape HTML; convert markdown **bold** to <strong>."""
    parts = re.split(r"\*\*(.+?)\*\*", str(text))
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(_esc(part))
        else:
            out.append(f"<strong>{_esc(part)}</strong>")
    return "".join(out)


def _guide_html(text: str) -> str:
    blocks = text.split("\n\n")
    parts: list[str] = ['<div class="guide">']
    for block in blocks:
        lines = block.split("\n")
        if lines and all(ln.startswith("- ") for ln in lines):
            items = "".join(f"<li>{_rich_text(ln[2:])}</li>" for ln in lines)
            parts.append(f"<ul>{items}</ul>")
        elif block.startswith("- "):
            items = "".join(
                f"<li>{_rich_text(line[2:])}</li>" for line in lines if line.startswith("- ")
            )
            parts.append(f"<ul>{items}</ul>")
        else:
            parts.append(f"<p>{_rich_text(block)}</p>")
    parts.append("</div>")
    return "".join(parts)


def _rrg_ranked_table_html(r: dict[str, Any]) -> str:
    rows = r.get("ranked_symbols") or []
    if not rows:
        return ""
    body = ""
    for row in rows:
        q = str(row.get("quadrant") or "")
        body += (
            f'<tr><td class="quad-{q}">{_esc(quadrant_display(q))}</td>'
            f"<td>{_esc(row.get('stock_id'))}</td>"
            f'<td class="num">{row.get("rs_ratio")}</td>'
            f'<td class="num">{row.get("rs_momentum")}</td>'
            f"<td>{_esc(tail_display(row.get('tail_dir')))}</td></tr>"
        )
    return (
        "<h3>RRG symbol table</h3>"
        "<p class=\"guide\">Kempenaer RRG · sorted by quadrant · RS-Ratio (JdK) · RS-Momentum · 4-day tail。</p>"
        "<table><thead><tr><th>Quadrant</th><th>Symbol</th>"
        "<th>RS-Ratio</th><th>RS-Mom</th><th>Tail</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _inline_svg(track_dir: Path, rel: str | None) -> str:
    if not rel:
        return ""
    path = track_dir / rel
    if not path.is_file():
        return f'<p class="muted">Chart missing: {_esc(rel)}</p>'
    raw = path.read_text(encoding="utf-8")
    return f'<div class="chart">{raw}</div>'


def _minervini_rows(minervini: dict[str, Any] | None) -> str:
    if not minervini:
        return "<p>—</p>"
    flags = minervini.get("criteria") or []
    detail = minervini.get("criteria_detail") or {}
    rows: list[str] = []
    for i, (key, en, zh) in enumerate(MINERVINI_ROWS):
        passed: bool | None = None
        if key in detail:
            passed = bool(detail[key].get("passed"))
        elif i < len(flags):
            passed = bool(flags[i])
        if passed is True:
            cell = '<td class="pass-y">✓</td>'
        elif passed is False:
            cell = '<td class="pass-n">✗</td>'
        else:
            cell = "<td>—</td>"
        rows.append(
            f"<tr><td>{_esc(en)}</td><td>{_esc(zh)}</td>{cell}</tr>"
        )
    met = minervini.get("criteria_met")
    total = minervini.get("criteria_total")
    summary = f"<p><em>Summary: {met}/{total} passed</em></p>" if met is not None else ""
    return (
        "<table><thead><tr><th>Criterion</th><th>說明</th><th>Pass</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>{summary}"
    )


def _kpi_strip_html(snap: dict[str, Any], *, bench: str, ref: str) -> str:
    b = snap.get("breadth_zone_200") or {}
    t = snap.get("trend_posture") or {}
    r = snap.get("rrg_rotation") or {}
    s = snap.get("stage2_participation") or {}
    breadth_val = f"{b.get('pct_above_200', '—')}%"
    breadth_sub = str(b.get("display") or "—") if b.get("available") else "—"
    if t.get("available"):
        stage = t.get("stage")
        name = t.get("stage_name") or STAGE_NAMES.get(int(stage or 0), "—")
        trend_val, trend_sub = f"Stage {stage}", stage_display(stage, name)
    else:
        trend_val, trend_sub = "—", bench
    rrg_val = f"{r.get('rotation_health_pct', '—')}%"
    rrg_sub = "Leading + Improving" if r.get("available") else "—"
    pass_val = f"{s.get('pass_pct', '—')}%"
    pass_sub = "Minervini ≥7/8" if s.get("available") else "—"
    return (
        f'<div class="kpi-strip" data-trade-date="{_esc(ref)}">'
        f'<div class="kpi" data-metric="breadth_200ma"><p class="kpi-label">200MA 廣度</p>'
        f'<p class="kpi-value">{_esc(breadth_val)}</p><p class="kpi-sub">{_esc(breadth_sub)}</p></div>'
        f'<div class="kpi" data-metric="weinstein_stage"><p class="kpi-label">Weinstein 階段</p>'
        f'<p class="kpi-value">{_esc(trend_val)}</p><p class="kpi-sub">{_esc(trend_sub)}</p></div>'
        f'<div class="kpi" data-metric="rrg_health"><p class="kpi-label">RRG 健康度</p>'
        f'<p class="kpi-value">{_esc(rrg_val)}</p><p class="kpi-sub">{_esc(rrg_sub)}</p></div>'
        f'<div class="kpi" data-metric="stage2_pass"><p class="kpi-label">Stage 2</p>'
        f'<p class="kpi-value">{_esc(pass_val)}</p><p class="kpi-sub">{_esc(pass_sub)}</p></div>'
        "</div>"
    )


def _build_regime_sections(
    snap: dict[str, Any],
    *,
    ref: str,
    bench: str,
    charts: RegimeChartPaths,
    track_dir: Path,
    include_guides: bool,
) -> list[str]:
    b, t, r, s = snap["breadth_zone_200"], snap["trend_posture"], snap["rrg_rotation"], snap["stage2_participation"]
    c = charts
    parts: list[str] = [f'<section id="synopsis"><h2>{_esc(SEC_SYNOPSIS)}</h2>']
    if include_guides:
        parts.append(_guide_html(GUIDE_SYNOPSIS))
    parts.append(
        f'<div class="interpret synopsis-body">{_rich_text(interpret_market_structure(b, t, r, s, bench=bench))}</div></section>'
    )
    if b.get("available"):
        rhythm, impulse = b.get("rhythm") or {}, b.get("impulse") or {}
        parts.append(f'<section id="breadth"><h2>{_esc(SEC_BREADTH)}</h2>')
        if include_guides:
            parts.append(_guide_html(GUIDE_BREADTH))
        parts.extend([
            f"<h3>{_esc(SEC_BREADTH_LEVEL)}</h3>",
            f'<p><strong class="hl">{_esc(b.get("display"))}</strong></p>',
        ])
        if include_guides:
            parts.append(_guide_html(GUIDE_BREADTH_LEVEL))
        parts.extend([
            "<h4>Notes</h4>",
            f'<div class="interpret">{_rich_text(interpret_breadth_level(b))}</div>',
            "<table><tbody>",
            f"<tr><td>% above 200-day MA</td><td class=\"num\">{b.get('pct_above_200')}%</td></tr>",
            f"<tr><td>% above 50-day MA</td><td class=\"num\">{b.get('pct_above_50')}%</td></tr>",
            f"<tr><td>50 vs 200 spread</td><td class=\"num\">{b.get('participation_gap')}pp</td></tr>",
            f"<tr><td>Advance/decline divergence</td><td>{'yes' if b.get('divergence_flag') else 'no'}</td></tr>",
            "</tbody></table>", _inline_svg(track_dir, c.breadth_spark),
        ])
        if rhythm.get("available"):
            parts.extend([f"<h3>{_esc(SEC_BREADTH_RHYTHM)}</h3>", f'<p><strong class="hl">{_esc(rhythm.get("display"))}</strong></p>'])
            if include_guides:
                parts.append(_guide_html(GUIDE_BREADTH_RHYTHM))
            parts.extend([
                "<h4>Notes</h4>", f'<div class="interpret">{_rich_text(interpret_breadth_rhythm(rhythm))}</div>',
                "<table><tbody>",
                f"<tr><td>Zweig adv/decl 10-day EMA</td><td class=\"num\">{rhythm.get('zweig_ema_pct')}%</td></tr>",
                f"<tr><td>Rhythm tier</td><td>{rhythm.get('zweig_ema_tier')}</td></tr>",
                "</tbody></table>", _inline_svg(track_dir, c.zweig_ema_spark),
            ])
        if impulse.get("available"):
            parts.append(f"<h3>{_esc(SEC_BREADTH_IMPULSE)}</h3>")
            if include_guides:
                parts.append(_guide_html(GUIDE_BREADTH_IMPULSE))
            parts.extend([
                "<h4>Notes</h4>", f'<div class="interpret">{_rich_text(interpret_breadth_impulse(impulse))}</div>',
                "<table><tbody>",
                f"<tr><td>Deemer 10-day adv/decl</td><td class=\"num\">{impulse.get('deemer_ratio')}</td></tr>",
                f"<tr><td>Thrust window active</td><td>{'yes' if impulse.get('thrust_active') else 'no'}</td></tr>",
                f"<tr><td>Days remaining</td><td>{impulse.get('thrust_days_remaining')} / {impulse.get('thrust_hold_days')}</td></tr>",
                "</tbody></table>",
            ])
        composite = interpret_breadth_composite(b)
        if composite:
            parts.extend(["<h4>Combined read</h4>", f'<div class="interpret">{_rich_text(composite)}</div>'])
        parts.append("</section>")
    if t.get("available"):
        stage = t.get("stage")
        name = t.get("stage_name") or STAGE_NAMES.get(int(stage or 0), "unknown")
        parts.append(f'<section id="trend"><h2>{_esc(SEC_TREND)}</h2>')
        if include_guides:
            parts.append(_guide_html(GUIDE_TREND))
        parts.extend([
            f"<h3>{_esc(bench)} · {_esc(stage_display(stage, name))}</h3>", "<h3>Notes</h3>",
            f'<div class="interpret">{_rich_text(interpret_trend(t, bench=bench))}</div>',
            _inline_svg(track_dir, c.weinstein_weekly), "<h3>Minervini Trend Template</h3>",
            _minervini_rows(t.get("minervini")), "</section>",
        ])
    if r.get("available"):
        quad_rows = "".join(
            f"<tr><td>{_esc(quadrant_display(q))}</td><td class=\"num\">{r['counts'].get(q, 0)}</td>"
            f"<td class=\"num\">{r['pct'].get(q, 0)}%</td></tr>"
            for q in ("leading", "improving", "weakening", "lagging")
        )
        mig = r.get("migrations") or {}
        mig_block = ""
        if any(mig.values()):
            mig_block = (
                "<h3>1-day quadrant migration</h3><table><tbody>"
                f"<tr><td>Improving → Leading</td><td class=\"num\">{mig.get('improving_to_leading', 0)}</td></tr>"
                f"<tr><td>Leading → Weakening</td><td class=\"num\">{mig.get('leading_to_weakening', 0)}</td></tr>"
                f"<tr><td>Lagging → Improving</td><td class=\"num\">{mig.get('lagging_to_improving', 0)}</td></tr>"
                f"<tr><td>Weakening → Lagging</td><td class=\"num\">{mig.get('weakening_to_lagging', 0)}</td></tr>"
                "</tbody></table>"
            )
        parts.append(f'<section id="rrg"><h2>{_esc(SEC_RRG)}</h2>')
        if include_guides:
            parts.append(_guide_html(GUIDE_RRG))
        parts.extend([
            f"<p><strong>Leading + Improving: {r.get('rotation_health_pct')}%</strong></p>", "<h3>Notes</h3>",
            f'<div class="interpret">{_rich_text(interpret_rrg(r))}</div>',
            '<table><thead><tr><th>Quadrant</th><th>Count</th><th>Share</th></tr></thead>',
            f"<tbody>{quad_rows}</tbody></table>", mig_block, _rrg_ranked_table_html(r),
            _inline_svg(track_dir, c.rrg_scatter), "</section>",
        ])
    if s.get("available"):
        parts.append(f'<section id="stage2"><h2>{_esc(SEC_MINERVINI_UNIVERSE)}</h2>')
        if include_guides:
            parts.append(_guide_html(GUIDE_MINERVINI_UNIVERSE))
        parts.extend([
            f"<p><strong>Pass rate {s.get('pass_pct')}%</strong> · {_esc(s.get('note'))}</p>", "<h3>Notes</h3>",
            f'<div class="interpret">{_rich_text(interpret_stage2(s, b))}</div>',
            _inline_svg(track_dir, c.participation_spark), "</section>",
        ])
    return parts


def render_regime_embed_html(
    snap: dict[str, Any], *, ref: str, bench: str,
    charts: RegimeChartPaths | None, track_dir: Path,
) -> str:
    c = charts or RegimeChartPaths()
    sections = _build_regime_sections(snap, ref=ref, bench=bench, charts=c, track_dir=track_dir, include_guides=False)
    return "".join([
        f"<style>{_scoped_styles(_EMBED_SCOPE)}</style>",
        f'<article class="regime-embed" data-trade-date="{_esc(ref)}" data-bench="{_esc(bench)}" data-layer="regime">',
        _kpi_strip_html(snap, bench=bench, ref=ref), *sections, "</article>",
    ])


def render_regime_daily_html(
    snap: dict[str, Any], *, ref: str, bench: str,
    charts: RegimeChartPaths | None, track_dir: Path, embed: bool = False,
) -> str:
    if embed:
        return render_regime_embed_html(snap, ref=ref, bench=bench, charts=charts, track_dir=track_dir)
    stamp = ref.replace("-", "")
    c = charts or RegimeChartPaths()
    sections = _build_regime_sections(snap, ref=ref, bench=bench, charts=c, track_dir=track_dir, include_guides=True)
    return "\n".join([
        "<!DOCTYPE html>", f'<html lang="zh-Hant"><head><meta charset="utf-8"/>',
        f"<title>{_esc(BRIEF_TITLE_ZH)} · {_esc(ref)}</title>",
        f"<style>{_STYLES}</style></head><body><div class=\"wrap\">",
        f"<h1>{_esc(BRIEF_TITLE_ZH)} · {_esc(ref)}</h1>",
        f'<p class="subtitle">{_esc(BRIEF_TITLE_PREFIX)} · {_esc(BRIEF_LEAD)}</p>',
        _guide_html(PRODUCT_LAYER_ONCE), _kpi_strip_html(snap, bench=bench, ref=ref), *sections,
        f'<p class="foot">config/regime.yaml · benchmark {_esc(bench)} · as_of {_esc(ref)} · snapshot snapshots/{stamp}/</p>',
        "</div></body></html>",
    ])
