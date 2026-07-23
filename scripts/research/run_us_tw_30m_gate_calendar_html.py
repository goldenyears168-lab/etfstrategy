#!/usr/bin/env python3
"""Daily calendar replay of champion L2/L2R risk gate → self-contained HTML.

  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_30m_gate_calendar_html.py

Yahoo 5m max ≈59d — full 90d is NOT available; report uses max panel.
Research layer（研究層）only — not Order layer（下單層）auto sell-all.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.us_tw_30m_rolling_risk_gate_study import (  # noqa: E402
    run_daily_gate_calendar,
)


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def fmt_num(x: Any, digits: int = 2) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return esc(x)
    if math.isnan(v) or math.isinf(v):
        return "—"
    return f"{v:.{digits}f}"


def img_b64(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _confirm_badge(label: str) -> str:
    colors = {
        "OK_EVENT": ("#1a3d2e", "#3dd6c6"),
        "FALSE_ALARM": ("#3d2a1a", "#e67e22"),
        "MISS_EVENT": ("#3d1a1a", "#e74c3c"),
        "QUIET_OK": ("#1a2a3d", "#8fa3b5"),
    }
    bg, fg = colors.get(label, ("#243140", "#e7eef7"))
    return (
        f"<span class='badge' style='background:{bg};border-color:{fg};color:{fg}'>"
        f"{esc(label)}</span>"
    )


def render_html(payload: dict[str, Any], fig_dir: Path) -> str:
    m = payload.get("meta") or {}
    s = payload.get("summary") or {}
    days = payload.get("days") or []
    top_save = payload.get("top_saved_days") or []
    top_hurt = payload.get("top_hurt_days") or []

    fig_names = (
        ("equity_vs_hold.png", "累積 PnL vs 無風控"),
        ("daily_saved_vs_hold.png", "當日少賠／少賺"),
        ("trigger_overlay.png", "出清／接回疊圖"),
    )
    figs_html = ""
    for name, alt in fig_names:
        p = fig_dir / name
        if p.exists():
            figs_html += (
                f"<figure class='card'><figcaption class='muted'>{esc(alt)}</figcaption>"
                f"<img alt='{esc(alt)}' src='{img_b64(p)}'/></figure>"
            )

    def day_row(r: dict[str, Any]) -> str:
        return (
            "<tr>"
            f"<td class='mono'>{esc(r.get('date'))}</td>"
            f"<td class='n'>{'✓' if r.get('is_event3') else ''}</td>"
            f"<td class='mono'>{esc(r.get('detach_poll') or '—')}</td>"
            f"<td class='mono'>{esc(r.get('reenter_poll') or '—')}</td>"
            f"<td>{_confirm_badge(str(r.get('confirm') or ''))}</td>"
            f"<td class='n'>{fmt_num(r.get('pnl_hold_close_pct'), 3)}</td>"
            f"<td class='n'>{fmt_num(r.get('pnl_strat_pct'), 3)}</td>"
            f"<td class='n'>{fmt_num(r.get('saved_vs_close_ntd'), 0)}</td>"
            f"<td class='n'>{fmt_num(r.get('exit_headroom_vs_trough_pct'), 3)}</td>"
            "</tr>"
        )

    all_rows = "".join(day_row(r) for r in days)
    save_rows = "".join(day_row(r) for r in top_save[:8])
    hurt_rows = "".join(day_row(r) for r in top_hurt[:6])

    thr = (
        "<thead><tr>"
        "<th>Date</th><th>Event≥3%</th><th>Detach</th><th>Rebuy</th><th>Confirm</th>"
        "<th class='n'>Hold%</th><th class='n'>Strat%</th><th class='n'>Saved NTD</th>"
        "<th class='n'>Headroom%</th>"
        "</tr></thead>"
    )

    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"/>
<title>TW↔NQ 30m gate · daily calendar</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0f1419;color:#e7eef7;margin:0;padding:24px;line-height:1.45}}
h1,h2,h3{{font-weight:650}} h1{{font-size:1.45rem}} h2{{font-size:1.15rem;margin-top:1.6rem}}
.muted{{color:#8fa3b5}} .mono{{font-family:ui-monospace,Menlo,monospace;font-size:.85rem}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}
.card{{background:#172029;border:1px solid #243140;border-radius:10px;padding:14px 16px;margin:10px 0}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}} th,td{{padding:5px 7px;border-bottom:1px solid #243140}}
th{{text-align:left;color:#8fa3b5;font-weight:600}}
.badge{{display:inline-block;padding:2px 8px;border-radius:6px;border:1px solid #3a4a5c;font-size:.78rem}}
img{{max-width:100%;border-radius:8px;margin:6px 0;background:#111}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}}
.kpi .card{{margin:0}} .kpi b{{display:block;font-size:1.12rem;margin-top:4px}}
.warn{{color:#e67e22}} .pos{{color:#3dd6c6}} .neg{{color:#e74c3c}}
</style></head><body>
<h1>TW↔NQ 30m gate · 逐日日曆回放（L2 / L2R）</h1>
<p class="muted">{esc(m.get('window'))} · days={esc(m.get('n_days'))} · events≥3%={esc(m.get('n_events3'))}<br/>
<span class="warn">{esc(m.get('note_90d'))}</span><br/>
{esc(m.get('sources'))} · notional={esc(m.get('notional_ntd'))} NTD</p>

<div class="kpi">
  <div class="card">觸發日（出清）<b>{esc(s.get('gate_trigger_days'))}</b></div>
  <div class="card">接回日（REBUY）<b>{esc(s.get('rebuy_days'))}</b></div>
  <div class="card">OK_EVENT<b class="pos">{esc(s.get('confirm_OK_EVENT'))}</b></div>
  <div class="card">FALSE_ALARM<b class="warn">{esc(s.get('confirm_FALSE_ALARM'))}</b></div>
  <div class="card">MISS_EVENT<b class="neg">{esc(s.get('confirm_MISS_EVENT'))}</b></div>
  <div class="card">QUIET_OK<b>{esc(s.get('confirm_QUIET_OK'))}</b></div>
  <div class="card">L2R vs 持有 edge<b class="{'pos' if float(s.get('edge_l2r_vs_hold_ntd') or 0) >= 0 else 'neg'}">{esc(s.get('edge_l2r_vs_hold_ntd'))}</b></div>
  <div class="card">L2 flat vs 持有<b>{esc(s.get('edge_l2_vs_hold_ntd'))}</b></div>
  <div class="card">觸發率<b>{esc(s.get('trigger_rate'))}</b></div>
  <div class="card">累積 saved（少賠）<b>{esc(s.get('saved_vs_hold_total_ntd'))}</b></div>
</div>

<section class="card">
<h2>規則提醒（champion）</h2>
<p><b>L2</b> <span class="mono">{esc(m.get('l2_id'))}</span> — DETACH spread_30m≤−0.6 ×2 poll · 只出清不接回</p>
<p><b>L2R</b> <span class="mono">{esc(m.get('l2r_id'))}</span><br/>{esc(m.get('l2r_rule'))}</p>
<p class="muted">Confirm：OK_EVENT=觸發且當日 peak→trough≥3%；FALSE_ALARM=觸發但非事件日；
MISS_EVENT=事件日未觸發；QUIET_OK=非事件且未觸發。</p>
</section>

<h2>Figures</h2>
{figs_html or '<p class="muted">（無圖）</p>'}

<h2>Top 少賠日（saved_vs_hold &gt; 0）</h2>
<div class="card" style="overflow-x:auto"><table>{thr}<tbody>{save_rows or '<tr><td colspan="9" class="muted">無</td></tr>'}</tbody></table></div>

<h2>Top 受傷日（風控輸給持有）</h2>
<div class="card" style="overflow-x:auto"><table>{thr}<tbody>{hurt_rows or '<tr><td colspan="9" class="muted">無</td></tr>'}</tbody></table></div>

<h2>全日表（L2R）</h2>
<div class="card" style="overflow-x:auto"><table>{thr}<tbody>{all_rows}</tbody></table></div>

<p class="muted">Research layer（研究層）only — 非 Strategy 採納、非 Order layer（下單層）auto sell-all。
Graduation gates pending。Yahoo 5m 無法覆蓋完整 90 日曆天。</p>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Champion L2/L2R daily gate calendar → HTML")
    ap.add_argument("--date-end", default=None, help="YYYY-MM-DD (default today)")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--stamp", default=None)
    args = ap.parse_args(argv)

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_us_tw_30m_gate_calendar"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running daily gate calendar → {out_dir}")
    payload = run_daily_gate_calendar(date_end=args.date_end, out_dir=out_dir)

    fig_dir = out_dir / "figs"
    html_path = out_dir / "index.html"
    html_path.write_text(render_html(payload, fig_dir), encoding="utf-8")

    # also drop a convenience copy next to stamp folder
    flat_html = RESEARCH_RRG / f"{stamp}.html"
    flat_html.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")

    s = payload.get("summary") or {}
    m = payload.get("meta") or {}
    print(
        f"days={m.get('n_days')} triggers={s.get('gate_trigger_days')} "
        f"rebuy={s.get('rebuy_days')} "
        f"OK={s.get('confirm_OK_EVENT')} FA={s.get('confirm_FALSE_ALARM')} "
        f"MISS={s.get('confirm_MISS_EVENT')} QUIET={s.get('confirm_QUIET_OK')}"
    )
    print(
        f"edge L2R vs hold={s.get('edge_l2r_vs_hold_ntd')} NTD · "
        f"window={m.get('window')} · {m.get('note_90d')}"
    )
    tops = payload.get("top_saved_days") or []
    for i, r in enumerate(tops[:3], 1):
        print(
            f"  top_save[{i}] {r.get('date')} saved={r.get('saved_vs_close_ntd')} "
            f"confirm={r.get('confirm')} detach={r.get('detach_poll')}"
        )
    print(f"Wrote {out_dir / 'calendar.json'}")
    print(f"Wrote {html_path}")
    print(f"Wrote {flat_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
