#!/usr/bin/env python3
"""HTML report for TW↔NQ 5m state-machine study."""

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


STATE_COLORS = {
    "SYNC": "#5bd47a",
    "DETACH": "#e67e22",
    "REPAIRING": "#3dd6c6",
    "REVERSAL": "#f0b429",
}


def state_badge(st: str) -> str:
    c = STATE_COLORS.get(st, "#8fa3b5")
    return f"<span class='badge' style='background:{c}22;color:{c};border-color:{c}55'>{esc(st)}</span>"


def render(payload: dict[str, Any], fig_dir: Path) -> str:
    m = payload.get("meta") or {}
    best = payload.get("best_strategy") or {}
    tops = payload.get("top_strategies") or []
    capped = payload.get("top_under_fpr_cap") or []
    cases = payload.get("cases") or []
    grid = payload.get("registered_grid") or {}
    base = payload.get("baseline_prior") or {}
    spot = payload.get("spotlight_20260714") or {}
    spot707 = payload.get("spotlight_20260707") or {}

    top_rows = []
    for r in tops:
        top_rows.append(
            "<tr>"
            f"<td class='mono'>{esc(r.get('strategy_id'))}</td>"
            f"<td class='n'>{esc(r.get('net_edge_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('net_edge_after_fee_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('event_recall'))}</td>"
            f"<td class='n'>{esc(r.get('false_trigger_rate_on_nonevent'))}</td>"
            f"<td class='n'>{esc(r.get('is_net_edge_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('oos_net_edge_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('reenter'))}</td>"
            "</tr>"
        )

    cap_rows = []
    if not capped:
        cap_rows.append("<tr><td colspan='4' class='muted'>No candidate under FPR soft cap — reject auto-graduation</td></tr>")
    else:
        for r in capped:
            cap_rows.append(
                "<tr>"
                f"<td class='mono'>{esc(r.get('strategy_id'))}</td>"
                f"<td class='n'>{esc(r.get('net_edge_ntd'))}</td>"
                f"<td class='n'>{esc(r.get('false_trigger_rate_on_nonevent'))}</td>"
                f"<td class='n'>{esc(r.get('event_recall'))}</td>"
                "</tr>"
            )

    # 7/14 timeline
    tl_html = ""
    if spot.get("timeline"):
        rows = []
        for row in spot["timeline"]:
            rows.append(
                "<tr>"
                f"<td>{esc(row.get('poll'))}</td>"
                f"<td>{state_badge(str(row.get('state')))}</td>"
                f"<td class='n'>{esc(row.get('tw_from_open'))}</td>"
                f"<td class='n'>{esc(row.get('nq_from_open'))}</td>"
                f"<td class='n'>{esc(row.get('cum_spread'))}</td>"
                f"<td class='n'>{esc(row.get('tw_win'))}</td>"
                f"<td class='n'>{esc(row.get('nq_win'))}</td>"
                f"<td class='n'>{esc(row.get('corr'))}</td>"
                f"<td class='n'>{esc(row.get('same_sign'))}</td>"
                "</tr>"
            )
        tl_html = f"""
        <section class="card">
          <h3>2026-07-14 · state timeline（champion）</h3>
          <p>DETACH <b>{esc(spot.get('detach_poll'))}</b> · REPAIR/reenter <b>{esc(spot.get('reenter_poll'))}</b>
          · all REPAIRING {esc(spot.get('repair_polls'))}</p>
          <p class="muted mono">runs: {esc(spot.get('states_compact'))}</p>
          <div class="scroll">
          <table><thead><tr>
            <th>Poll</th><th>State</th><th>TW%</th><th>NQ%</th><th>cum</th><th>tw_win</th><th>nq_win</th><th>corr</th><th>ss</th>
          </tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        </section>"""

    trap_html = ""
    if spot707:
        trap_html = f"""
        <section class="card">
          <h3>2026-07-07 · late-kill trap</h3>
          <p>PTT {esc(spot707.get('peak_to_trough_pct'))}% · close {esc(spot707.get('close_from_open_pct'))}% ·
          trough {esc(spot707.get('trough_from_open_pct'))}%</p>
          <p>DETACH <b>{esc(spot707.get('detach_poll'))}</b> · REPAIR <b>{esc(spot707.get('reenter_poll'))}</b></p>
          <p class="muted mono">runs: {esc(spot707.get('states_compact'))}</p>
        </section>"""

    case_html = []
    for c in cases:
        if not c.get("present"):
            case_html.append(f"<section class='card'><h3>{esc(c.get('date'))} · missing</h3></section>")
            continue
        rows = []
        for sid, o in (c.get("strategy_outcomes") or {}).items():
            rows.append(
                "<tr>"
                f"<td class='mono'>{esc(sid)}</td>"
                f"<td>{esc(o.get('detach_poll'))}</td>"
                f"<td>{esc(o.get('reenter_poll'))}</td>"
                f"<td class='n'>{esc(o.get('detach_tw_from_open'))}</td>"
                f"<td class='n'>{esc(o.get('saved_vs_close_ntd'))}</td>"
                f"<td class='n'>{esc(o.get('false_alarm_opp_cost_ntd', 0))}</td>"
                "</tr>"
            )
        fig = fig_dir / f"case_{str(c['date']).replace('-', '')}_state.png"
        fig_block = f"<img src='{img(fig)}' alt='case'/>" if fig.is_file() else ""
        case_html.append(
            f"""
            <section class="card">
              <h3>{esc(c['date'])} · PTT {esc(round(c['peak_to_trough_pct'],2))}% ·
              close {esc(round(c['close_from_open_pct'],2))}% · trough {esc(round(c['trough_from_open_pct'],2))}%</h3>
              {fig_block}
              <table><thead><tr><th>Strategy</th><th>Detach</th><th>Repair</th><th>Exit TW%</th><th>Saved</th><th>Opp</th></tr></thead>
              <tbody>{''.join(rows)}</tbody></table>
            </section>"""
        )

    helped = payload.get("repair_helped_vs_baseline")
    css = """
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=IBM+Plex+Mono:wght@400;500&family=Source+Sans+3:wght@400;600;700&display=swap');
    :root{--bg:#0f1419;--panel:#172029;--ink:#e8eef4;--muted:#8fa3b5;--line:#2a3a4a;--accent:#3dd6c6;--ok:#5bd47a;--warn:#f0b429}
    body{margin:0;background:radial-gradient(900px 480px at 10% -10%,#1a3a3a,transparent),var(--bg);color:var(--ink);font:16px/1.55 "Source Sans 3",sans-serif}
    .wrap{max-width:1140px;margin:0 auto;padding:36px 20px 72px}
    h1{font-family:Fraunces,serif;font-size:clamp(1.7rem,3.4vw,2.4rem);margin:0 0 10px}
    .eyebrow{color:var(--accent);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}
    .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}
    .kpi{background:#121c26;border:1px solid var(--line);border-radius:10px;padding:10px}
    .kpi .lbl{color:var(--muted);font-size:12px}.kpi .val{font-weight:700;margin-top:4px;color:var(--ok)}
    table{width:100%;border-collapse:collapse;font-size:12.5px}
    th,td{border-bottom:1px solid var(--line);padding:6px 7px;text-align:left}
    td.n{text-align:right;font-family:"IBM Plex Mono",monospace;font-size:11.5px}
    .mono{font-family:"IBM Plex Mono",monospace;font-size:11.5px}
    img{width:100%;border-radius:10px;border:1px solid var(--line);margin:8px 0}
    .muted{color:var(--muted)}
    .badge{display:inline-block;border:1px solid;border-radius:6px;padding:1px 7px;font-size:11px;font-family:"IBM Plex Mono",monospace}
    .scroll{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px}
    @media(max-width:800px){.kpis{grid-template-columns:1fr 1fr}}
    """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TW↔NQ 5m state machine</title><style>{css}</style></head>
<body><div class="wrap">
<p class="eyebrow">Research layer · 5m state machine · SYNC / DETACH / REVERSAL / REPAIRING</p>
<h1>TW↔NQ 同步／斷裂／修復狀態機</h1>
<p class="muted">Notional {esc(f"{m.get('notional_ntd'):,.0f}" if m.get('notional_ntd') else "—")} NTD/日 ·
registered grid <b>{esc(grid.get('n_strategies'))}</b> · pick <code>{esc(m.get('pick_rule'))}</code> ·
{esc(m.get('sources'))}</p>
<div class="kpis">
  <div class="kpi"><div class="lbl">視窗</div><div class="val">{esc(m.get('date_start'))} → {esc(m.get('date_end'))}</div></div>
  <div class="kpi"><div class="lbl">樣本日 / 事件≥3%</div><div class="val">{esc(m.get('n_days'))} / {esc(m.get('n_events3'))}</div></div>
  <div class="kpi"><div class="lbl">Best net edge</div><div class="val">{esc(best.get('net_edge_ntd'))} NTD</div></div>
  <div class="kpi"><div class="lbl">OOS holds +</div><div class="val">{esc(m.get('oos_holds_positive'))}</div></div>
</div>
<section class="card">
  <h3>Verdict</h3>
  <p>Champion：<code>{esc(best.get('strategy_id'))}</code> — {esc(best.get('name'))}</p>
  <p>Net <b>{esc(best.get('net_edge_ntd'))}</b> · after fee <b>{esc(best.get('net_edge_after_fee_ntd'))}</b> ·
  Recall≥3% {esc(best.get('event_recall'))} · FPR non-ev {esc(best.get('false_trigger_rate_on_nonevent'))}</p>
  <p>IS net={esc(best.get('is_net_edge_ntd'))} · OOS net={esc(best.get('oos_net_edge_ntd'))}
br/>
  Prior baseline `{esc(base.get('strategy_id'))}` net={esc(base.get('net_edge_ntd'))} ·
  rich repair helped vs baseline: <b>{esc(helped)}</b></p>
  <p class="muted">{esc(m.get('graduation'))}</p>
</section>
<section class="card"><h3>Registered grid</h3>
<p class="muted">{esc(grid.get('axes_note'))}</p>
<p class="mono">n={esc(grid.get('n_strategies'))} · cum={esc(grid.get('cum_grid'))} · win={esc(grid.get('win_grid'))} ·
w={esc(grid.get('window_grid'))} · confirm={esc(grid.get('confirm_grid'))} ·
repair_corr={esc(grid.get('repair_corr_min'))} · ss={esc(grid.get('repair_same_sign_min'))} ·
Δcum={esc(grid.get('repair_delta_cum'))} · FPR soft cap={esc(grid.get('fpr_soft_cap'))}</p>
</section>
<section class="card"><h3>Top strategies</h3>
<table><thead><tr><th>ID</th><th>Net</th><th>After fee</th><th>Recall</th><th>FPR</th><th>IS</th><th>OOS</th><th>Reenter</th></tr></thead>
<tbody>{''.join(top_rows)}</tbody></table></section>
<section class="card"><h3>Under FPR≤{esc(m.get('fpr_soft_cap'))}</h3>
<table><thead><tr><th>ID</th><th>Net</th><th>FPR</th><th>Recall</th></tr></thead>
<tbody>{''.join(cap_rows)}</tbody></table></section>
{tl_html}
{trap_html}
{''.join(case_html)}
<footer class="muted">Research layer only · not Order-layer auto sell-all</footer>
</div></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--json",
        type=Path,
        default=RESEARCH_RRG / f"{__import__('datetime').date.today().strftime('%Y%m%d')}_us_tw_5m_state_machine.json",
    )
    args = ap.parse_args(argv)
    if not args.json.is_file():
        # fallback common stamp
        alt = RESEARCH_RRG / "20260715_us_tw_5m_state_machine.json"
        if alt.is_file():
            args.json = alt
    if not args.json.is_file():
        raise SystemExit(f"JSON not found: {args.json}")
    payload = json.loads(args.json.read_text(encoding="utf-8"))
    fig_dir = args.json.parent / args.json.stem / "figs"
    doc = render(payload, fig_dir)
    out = args.json.with_suffix(".html")
    out.write_text(doc, encoding="utf-8")
    bundle = args.json.parent / args.json.stem
    if bundle.is_dir():
        (bundle / "report.html").write_text(doc, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
