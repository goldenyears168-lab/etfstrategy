#!/usr/bin/env python3
"""HTML report for TW↔NQ rolling 30m risk-gate study."""

from __future__ import annotations

import argparse
import base64
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from report_paths import RESEARCH_RRG  # noqa: E402


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def img(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def render(payload: dict[str, Any], fig_dir: Path) -> str:
    m = payload.get("meta") or {}
    best = payload.get("best_strategy") or {}
    std = payload.get("risk_control_standard") or {}
    tops = payload.get("top_strategies") or []
    hr = payload.get("high_recall_band") or {}
    spot = payload.get("spotlight_20260714") or {}
    spot707 = payload.get("spotlight_20260707") or {}
    cases = payload.get("cases") or []

    top_rows = "".join(
        "<tr>"
        f"<td class='mono'>{esc(r.get('strategy_id'))}</td>"
        f"<td class='n'>{esc(r.get('event_recall'))}</td>"
        f"<td class='n'>{esc(r.get('miss_rate'))}</td>"
        f"<td class='n'>{esc(r.get('sell_near_trough_rate_on_events'))}</td>"
        f"<td class='n'>{esc(r.get('reenter_before_trough_rate_on_events'))}</td>"
        f"<td class='n'>{esc(r.get('median_exit_headroom_vs_trough_pct'))}</td>"
        f"<td class='n'>{esc(r.get('false_trigger_rate_on_nonevent'))}</td>"
        f"<td class='n'>{esc(r.get('net_edge_ntd'))}</td>"
        "</tr>"
        for r in tops
    )

    hr_rows = ""
    if not hr.get("top"):
        hr_rows = "<tr><td colspan='4' class='muted'>No strategy in high-recall band</td></tr>"
    else:
        hr_rows = "".join(
            "<tr>"
            f"<td class='mono'>{esc(r.get('strategy_id'))}</td>"
            f"<td class='n'>{esc(r.get('event_recall'))}</td>"
            f"<td class='n'>{esc(r.get('sell_near_trough_rate_on_events'))}</td>"
            f"<td class='n'>{esc(r.get('net_edge_ntd'))}</td>"
            "</tr>"
            for r in hr["top"]
        )

    tl = spot.get("timeline") or []
    tl_rows = "".join(
        "<tr>"
        f"<td>{esc(r.get('poll'))}</td>"
        f"<td><span class='badge'>{esc(r.get('state'))}</span></td>"
        f"<td class='n'>{esc(r.get('tw_from_open'))}</td>"
        f"<td class='n'>{esc(r.get('nq_from_open'))}</td>"
        f"<td class='n'>{esc(r.get('spread_30m'))}</td>"
        f"<td class='n'>{esc(r.get('same_sign'))}</td>"
        "</tr>"
        for r in tl
    )

    case_blocks = []
    for c in cases:
        ch = c.get("champion") or {}
        case_blocks.append(
            f"<section class='card'><h3>{esc(c.get('date'))} · PTT {esc(c.get('peak_to_trough'))}%</h3>"
            f"<p class='muted'>close {esc(c.get('close_from_open'))}% · trough {esc(c.get('trough_from_open'))}% "
            f"(~{esc(c.get('trough_poll_close'))})</p>"
            f"<p>DETACH <b>{esc(ch.get('detach_poll'))}</b> · headroom {esc(ch.get('exit_headroom_vs_trough_pct'))}% · "
            f"REPAIR <b>{esc(ch.get('reenter_poll'))}</b> · sell_near={esc(ch.get('sell_near_trough'))} · "
            f"buy_pre={esc(ch.get('reenter_before_trough'))}</p></section>"
        )

    fig1 = fig_dir / "recall_vs_sell_near.png"
    fig2 = fig_dir / "case_20260714.png"
    figs = ""
    if fig1.exists():
        figs += f"<img alt='recall vs sell-near' src='{img(fig1)}'/>"
    if fig2.exists():
        figs += f"<img alt='7/14' src='{img(fig2)}'/>"

    l2 = std.get("L2_gate_on") or {}
    l2r = std.get("L2R_optional_rebuy") or {}
    metrics = l2.get("metrics") or {}

    howto = (std.get("how_to_read_pulse") or {}).get("simple") or []
    howto_html = "".join(f"<li>{esc(x)}</li>" for x in howto)

    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"/>
<title>TW↔NQ 30m rolling risk gate</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0f1419;color:#e7eef7;margin:0;padding:24px;line-height:1.45}}
h1,h2,h3{{font-weight:650}} h1{{font-size:1.45rem}} h2{{font-size:1.15rem;margin-top:1.6rem}}
.muted{{color:#8fa3b5}} .mono{{font-family:ui-monospace,Menlo,monospace;font-size:.85rem}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}
.card{{background:#172029;border:1px solid #243140;border-radius:10px;padding:14px 16px;margin:10px 0}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}} th,td{{padding:6px 8px;border-bottom:1px solid #243140}}
th{{text-align:left;color:#8fa3b5;font-weight:600}}
.badge{{display:inline-block;padding:2px 8px;border-radius:6px;border:1px solid #3a4a5c;font-size:.8rem}}
img{{max-width:100%;border-radius:8px;margin:8px 0;background:#111}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.kpi .card{{margin:0}} .kpi b{{display:block;font-size:1.15rem;margin-top:4px}}
</style></head><body>
<h1>TW↔NQ rolling 30m · 風控 + 變化率儀表</h1>
<p class="muted">{esc(m.get('window'))} · days={esc(m.get('n_days'))} · events≥3%={esc(m.get('n_events3'))}<br/>
{esc(m.get('presentation'))}</p>

<div class="kpi">
  <div class="card">Recall<b>{esc(best.get('event_recall'))}</b></div>
  <div class="card">Sell@trough<b>{esc(best.get('sell_near_trough_rate_on_events'))}</b></div>
  <div class="card">L2R cool<b>{esc((l2r.get('metrics') or {}).get('cooldown_minutes'))}m</b></div>
  <div class="card">δ / 窗<b>{esc((l2r.get('metrics') or {}).get('outperform_margin'))} / {esc((l2r.get('metrics') or {}).get('repair_window_min'))}m</b></div>
  <div class="card">7/14 hit<b>{esc((l2r.get('metrics') or {}).get('hit_714_1125_1200'))}</b></div>
  <div class="card">L2R edge<b>{esc((l2r.get('metrics') or {}).get('net_edge_ntd'))}</b></div>
</div>

<section class="card">
<h2>怎麼讀（簡單）</h2>
<ol>{howto_html}</ol>
</section>

<section class="card">
<h2>風控標準</h2>
<p><b>L1</b> — {esc((std.get('L1_watch') or {}).get('rule'))}</p>
<p><b>L2</b> — <span class="mono">{esc(l2.get('strategy_id'))}</span><br/>{esc(l2.get('rule'))}</p>
<p><b>L2R</b> — <span class="mono">{esc(l2r.get('strategy_id'))}</span><br/>{esc(l2r.get('definition'))}</p>
</section>

<section class="card">
<h2>Champion</h2>
<p class="mono">{esc(best.get('strategy_id'))}</p>
<p>{esc(best.get('name'))}</p>
<p>Missed events: {esc(best.get('missed_event_dates'))}</p>
<p class="muted">假警可再買回；優先少漏賣／少賣在谷／少谷前接回。Research only — 不自動 Order 全平。</p>
</section>

<h2>Figures</h2>
{figs}

<h2>2026-07-14</h2>
<div class="card">
<p>DETACH <b>{esc(spot.get('detach_poll'))}</b> · trough~<b>{esc(spot.get('trough_poll_close'))}</b> ·
REBUY <b>{esc(spot.get('rebuy_poll'))}</b></p>
<p>headroom={esc(spot.get('exit_headroom'))}% · sell_near={esc(spot.get('sell_near_trough'))} ·
rebuy_before_trough={esc(spot.get('rebuy_before_trough'))}</p>
<table><thead><tr><th>Poll</th><th>State</th><th>TW%</th><th>NQ%</th><th>spread_30m</th><th>ss</th></tr></thead>
<tbody>{tl_rows}</tbody></table>
</div>

<h2>2026-07-07 late kill</h2>
<div class="card">
<p>DETACH <b>{esc(spot707.get('detach_poll'))}</b> · trough~<b>{esc(spot707.get('trough_poll_close'))}</b> ·
REPAIR <b>{esc(spot707.get('reenter_poll'))}</b></p>
</div>

<h2>Top (recall → timing → edge)</h2>
<table><thead><tr>
<th>ID</th><th>Recall</th><th>Miss</th><th>Sell@trough</th><th>Buy@pre</th><th>Headroom</th><th>FPR</th><th>Edge</th>
</tr></thead><tbody>{top_rows}</tbody></table>

<h2>High-recall band (floor={esc(hr.get('floor'))})</h2>
<table><thead><tr><th>ID</th><th>Recall</th><th>Sell@trough</th><th>Edge</th></tr></thead>
<tbody>{hr_rows}</tbody></table>

<h2>Cases</h2>
{''.join(case_blocks)}

<p class="muted">Graduation: {esc(m.get('graduation'))}</p>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default=None)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)
    stamp = args.stamp or f"{__import__('datetime').date.today().strftime('%Y%m%d')}_us_tw_30m_rolling_risk_gate"
    jp = args.json or (RESEARCH_RRG / f"{stamp}.json")
    payload = json.loads(jp.read_text(encoding="utf-8"))
    fig_dir = RESEARCH_RRG / stamp / "figs"
    html_out = RESEARCH_RRG / f"{stamp}.html"
    html_out.write_text(render(payload, fig_dir), encoding="utf-8")
    print(f"Wrote {html_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
