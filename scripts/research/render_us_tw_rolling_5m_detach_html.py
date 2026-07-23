#!/usr/bin/env python3
"""HTML report for rolling 5m detach avoided-loss study."""

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
    tops = payload.get("top_strategies") or []
    cases = payload.get("cases") or []

    top_rows = []
    for r in tops:
        top_rows.append(
            "<tr>"
            f"<td class='mono'>{esc(r.get('strategy_id'))}</td>"
            f"<td class='n'>{esc(r.get('net_edge_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('sum_saved_vs_close_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('sum_opp_cost_ntd'))}</td>"
            f"<td class='n'>{esc(r.get('event_recall'))}</td>"
            f"<td class='n'>{esc(r.get('false_trigger_rate_on_nonevent'))}</td>"
            f"<td class='n'>{esc(r.get('n_triggers'))}</td>"
            "</tr>"
        )

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
                f"<td class='n'>{esc(o.get('detach_tw_from_open'))}</td>"
                f"<td class='n'>{esc(o.get('saved_vs_close_ntd'))}</td>"
                f"<td class='n'>{esc(o.get('false_alarm_opp_cost_ntd', 0))}</td>"
                f"<td>{esc(o.get('reenter_poll'))}</td>"
                "</tr>"
            )
        fig = fig_dir / f"case_{str(c['date']).replace('-', '')}_poll5.png"
        fig_block = f"<img src='{img(fig)}' alt='case'/>" if fig.is_file() else ""
        case_html.append(
            f"""
            <section class="card">
              <h3>{esc(c['date'])} · PTT {esc(round(c['peak_to_trough_pct'],2))}% ·
              close {esc(round(c['close_from_open_pct'],2))}% · trough {esc(round(c['trough_from_open_pct'],2))}%</h3>
              {fig_block}
              <table><thead><tr><th>Strategy</th><th>Detach</th><th>Exit TW%</th><th>Saved NTD</th><th>Opp NTD</th><th>Reenter</th></tr></thead>
              <tbody>{''.join(rows)}</tbody></table>
            </section>"""
        )

    css = """
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=IBM+Plex+Mono:wght@400;500&family=Source+Sans+3:wght@400;600;700&display=swap');
    :root{--bg:#0f1419;--panel:#172029;--ink:#e8eef4;--muted:#8fa3b5;--line:#2a3a4a;--accent:#3dd6c6;--ok:#5bd47a;--warn:#f0b429}
    body{margin:0;background:radial-gradient(900px 480px at 10% -10%,#1a3a3a,transparent),var(--bg);color:var(--ink);font:16px/1.55 "Source Sans 3",sans-serif}
    .wrap{max-width:1100px;margin:0 auto;padding:36px 20px 72px}
    h1{font-family:Fraunces,serif;font-size:clamp(1.7rem,3.4vw,2.4rem);margin:0 0 10px}
    .eyebrow{color:var(--accent);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}
    .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}
    .kpi{background:#121c26;border:1px solid var(--line);border-radius:10px;padding:10px}
    .kpi .lbl{color:var(--muted);font-size:12px}.kpi .val{font-weight:700;margin-top:4px;color:var(--ok)}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{border-bottom:1px solid var(--line);padding:7px;text-align:left}
    td.n{text-align:right;font-family:"IBM Plex Mono",monospace;font-size:12px}
    .mono{font-family:"IBM Plex Mono",monospace;font-size:12px}
    img{width:100%;border-radius:10px;border:1px solid var(--line);margin:8px 0}
    .muted{color:var(--muted)}
    @media(max-width:800px){.kpis{grid-template-columns:1fr 1fr}}
    """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TW↔NQ 5m detach · avoided loss</title><style>{css}</style></head>
<body><div class="wrap">
<p class="eyebrow">Research · rolling 5m poll · detach / repair</p>
<h1>斷裂／復原參數掃瞄 · 最高可省止損？</h1>
<p class="muted">Notional {esc(f"{m.get('notional_ntd'):,.0f}" if m.get('notional_ntd') else "—")} NTD/日 · 目標最大化 net_edge = 省虧 − 假警少賺 · {esc(m.get('sources'))}</p>
<div class="kpis">
  <div class="kpi"><div class="lbl">視窗</div><div class="val">{esc(m.get('date_start'))} → {esc(m.get('date_end'))}</div></div>
  <div class="kpi"><div class="lbl">樣本日</div><div class="val">{esc(m.get('n_days'))}</div></div>
  <div class="kpi"><div class="lbl">事件≥3%</div><div class="val">{esc(m.get('n_events3'))}</div></div>
  <div class="kpi"><div class="lbl">Best net edge</div><div class="val">{esc(best.get('net_edge_ntd'))} NTD</div></div>
</div>
<section class="card">
  <h3>Verdict</h3>
  <p>最佳策略：<code>{esc(best.get('strategy_id'))}</code> — {esc(best.get('name'))}</p>
  <p>Saved vs close <b>{esc(best.get('sum_saved_vs_close_ntd'))}</b> − Opp cost <b>{esc(best.get('sum_opp_cost_ntd'))}</b>
  = Net <b>{esc(best.get('net_edge_ntd'))}</b> NTD · Recall≥3% {esc(best.get('event_recall'))} ·
  非事件假觸發 {esc(best.get('false_trigger_rate_on_nonevent'))}</p>
  <p class="muted">正 net edge 代表「有辦法」在此短樣本假設下；否或接近 0 則斷裂警報偏噪音。非 Order 自動全平依據。</p>
</section>
<section class="card"><h3>Top strategies</h3>
<table><thead><tr><th>ID</th><th>Net edge</th><th>Saved</th><th>Opp</th><th>Recall</th><th>FPR</th><th>Trigs</th></tr></thead>
<tbody>{''.join(top_rows)}</tbody></table></section>
{''.join(case_html)}
<footer class="muted">Research layer only</footer>
</div></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=RESEARCH_RRG / "20260715_us_tw_rolling_5m_detach.json")
    args = ap.parse_args(argv)
    if not args.json.is_file():
        # try yesterday stamp if today differs
        alt = RESEARCH_RRG / "20260714_us_tw_rolling_5m_detach.json"
        args.json = args.json if args.json.is_file() else alt
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
