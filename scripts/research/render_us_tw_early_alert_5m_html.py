#!/usr/bin/env python3
"""Render early-alert 5m study HTML (figures embedded).

  PYTHONPATH=src .venv/bin/python scripts/research/render_us_tw_early_alert_5m_html.py
"""

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


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _pct(x: Any, d: int = 1) -> str:
    if x is None:
        return "—"
    try:
        return f"{100 * float(x):.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def _img(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def render(payload: dict[str, Any], fig_dir: Path) -> str:
    meta = payload.get("meta") or {}
    user = payload.get("user_rule_10m_m0p6") or {}
    h2 = payload.get("h2_analog_60m_m1p2_on_5m") or {}
    tops = payload.get("top_candidates") or []
    cases = payload.get("cases") or []
    first = payload.get("first_hit_on_events") or {}

    def card_rule(title: str, ev: dict[str, Any]) -> str:
        if not ev:
            return ""
        f, o = ev.get("full") or {}, ev.get("oos") or {}
        st = ev.get("status") or "—"
        return f"""
        <article class="card">
          <h3>{_esc(title)} <span class="pill st-{_esc(st)}">{_esc(st)}</span></h3>
          <p class="mono">{_esc((ev.get('rule') or {}).get('name'))} · @{_esc((ev.get('rule') or {}).get('lead_label'))}</p>
          <div class="metrics">
            <div><span>Full prec</span><b>{_pct(f.get('precision'))}</b></div>
            <div><span>Full rec</span><b>{_pct(f.get('recall'))}</b></div>
            <div><span>Full FPR</span><b>{_pct(f.get('false_alarm_rate'))}</b></div>
            <div><span>Remain DD med</span><b>{_esc(ev.get('remain_dd_after_trig_median'))}%</b></div>
            <div><span>OOS prec/rec</span><b>{_pct(o.get('precision'))} / {_pct(o.get('recall'))}</b></div>
          </div>
        </article>"""

    top_rows = []
    for e in tops[:10]:
        f = e.get("full") or {}
        top_rows.append(
            "<tr>"
            f"<td>{_esc(e.get('family'))}</td>"
            f"<td class='mono'>{_esc(e['rule']['name'])}</td>"
            f"<td><span class='pill st-{_esc(e.get('status'))}'>{_esc(e.get('status'))}</span></td>"
            f"<td class='n'>{_pct(f.get('precision'))}</td>"
            f"<td class='n'>{_pct(f.get('recall'))}</td>"
            f"<td class='n'>{_pct(f.get('false_alarm_rate'))}</td>"
            f"<td class='n'>{_esc(e.get('remain_dd_after_trig_median'))}</td>"
            f"<td class='n'>{_esc(e.get('horizon_min'))}</td>"
            "</tr>"
        )

    hit_rows = []
    for k, v in first.items():
        hit_rows.append(
            "<tr>"
            f"<td>{_esc(k)}</td>"
            f"<td class='n'>{_esc(v.get('median_min_among_hits'))}</td>"
            f"<td class='n'>{_pct(v.get('hit_by_10m'))}</td>"
            f"<td class='n'>{_pct(v.get('hit_by_20m'))}</td>"
            f"<td class='n'>{_pct(v.get('hit_by_30m'))}</td>"
            f"<td class='n'>{_pct(v.get('never_hit_rate'))}</td>"
            f"<td class='n'>{_esc(v.get('n_hit'))}/{_esc(v.get('n_events'))}</td>"
            "</tr>"
        )

    case_html = []
    for c in cases:
        if not c.get("present"):
            continue
        rows = []
        for h in c.get("horizons") or []:
            rows.append(
                "<tr>"
                f"<td class='n'>{_esc(h['horizon_min'])}</td>"
                f"<td class='n'>{_esc(h['tw_ret'])}</td>"
                f"<td class='n'>{_esc(h['nq_ret'])}</td>"
                f"<td class='n'>{_esc(h['spread'])}</td>"
                f"<td>{'✓' if h['user_rule_spread_le_0.6'] else '—'}</td>"
                f"<td>{'✓' if h['h2_like_spread_le_1.2'] else '—'}</td>"
                f"<td class='n'>{_esc(h['remain_dd_after'])}</td>"
                "</tr>"
            )
        case_html.append(
            f"""
            <section class="card">
              <h3>Case {_esc(c['date'])} · peak→trough {_esc(round(c.get('peak_to_trough_pct') or 0, 2))}%</h3>
              <p class="muted">first hit min: <code>{_esc(c.get('first_hit_min'))}</code></p>
              <table><thead><tr><th>+min</th><th>TW</th><th>NQ</th><th>spread</th><th>≤−0.6</th><th>≤−1.2</th><th>remain DD</th></tr></thead>
              <tbody>{''.join(rows)}</tbody></table>
            </section>"""
        )

    figs = []
    for p in payload.get("plot_paths") or []:
        path = Path(p)
        if not path.is_file():
            path = fig_dir / Path(p).name
        if path.is_file():
            figs.append(
                f"<figure><img src='{_img(path)}' alt='{_esc(path.stem)}'/>"
                f"<figcaption>{_esc(path.stem)}</figcaption></figure>"
            )

    css = """
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=IBM+Plex+Mono:wght@400;500&family=Source+Sans+3:wght@400;600;700&display=swap');
    :root{--bg:#101820;--panel:#1a2632;--ink:#e8eef4;--muted:#8fa3b5;--line:#2a3a4a;--accent:#3dd6c6;--warn:#f0b429;--ok:#5bd47a}
    body{margin:0;background:radial-gradient(900px 500px at 0% 0%,#1a3a3a,transparent),var(--bg);color:var(--ink);font:16px/1.55 "Source Sans 3",sans-serif}
    .wrap{max-width:1080px;margin:0 auto;padding:36px 20px 72px}
    h1{font-family:Fraunces,serif;font-size:clamp(1.8rem,3.5vw,2.5rem);margin:0 0 8px}
    .eyebrow{color:var(--accent);letter-spacing:.08em;text-transform:uppercase;font-size:12px}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}
    .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}
    .kpi{background:#121c26;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
    .kpi .lbl{color:var(--muted);font-size:12px}.kpi .val{font-weight:700;margin-top:4px}
    .metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
    .metrics div{background:rgba(255,255,255,.03);border-radius:8px;padding:8px}
    .metrics span{display:block;color:var(--muted);font-size:11px}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{border-bottom:1px solid var(--line);padding:7px;text-align:left}
    td.n{text-align:right;font-family:"IBM Plex Mono",monospace;font-size:12px}
    .mono{font-family:"IBM Plex Mono",monospace;font-size:13px;color:var(--warn)}
    .muted{color:var(--muted)}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;text-transform:uppercase}
    .st-accepted{background:var(--ok);color:#062616}.st-weak{background:var(--warn);color:#2a1c00}.st-rejected{background:#3a4654;color:#e8eef4}
    .fig-grid{display:grid;gap:16px} figure{margin:0} img{width:100%;border-radius:10px;border:1px solid var(--line)}
    ol{padding-left:1.2rem} li{margin:8px 0}
    @media(max-width:800px){.kpis,.metrics{grid-template-columns:1fr 1fr}}
    """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>US–TW early alert 5m · {_esc(meta.get('date_end'))}</title><style>{css}</style></head>
<body><div class="wrap">
<p class="eyebrow">Research · 5m early alert overlay</p>
<h1>開盤後 10–60 分鐘 · TW−NQ 偏離警報掃瞄</h1>
<p class="muted">{_esc(meta.get('note'))}</p>
<div class="kpis">
  <div class="kpi"><div class="lbl">視窗</div><div class="val">{_esc(meta.get('date_start'))} → {_esc(meta.get('date_end'))}</div></div>
  <div class="kpi"><div class="lbl">樣本日</div><div class="val">{_esc(meta.get('n_days'))}</div></div>
  <div class="kpi"><div class="lbl">事件 ≥3%</div><div class="val">{_esc(meta.get('n_events_primary'))}</div></div>
  <div class="kpi"><div class="lbl">IS 截止</div><div class="val">{_esc(meta.get('is_end'))}</div></div>
</div>
{card_rule("用戶規則 · spread_10m ≤ −0.6 @ 09:10", user)}
{card_rule("H2 類比 · spread_60m ≤ −1.2 @ 10:00（5m 子樣本）", h2)}
<section class="card"><h3>分層建議</h3>
<ol>
<li><b>黃燈 09:10</b>：spread≤−0.6 → 停加碼（FPR 偏高）</li>
<li><b>橙燈 09:10</b>：spread≤−1.2 或 TW≤−1.0 → 較乾淨的早警報</li>
<li><b>紅燈確認 09:20</b>：10m&amp;20m spread≤−0.8</li>
<li><b>主閘門 10:00</b>：保留 H2 spread_60m≤−1.2</li>
<li>午後才殺的事件（如 7/7）早警報必然漏接</li>
</ol></section>
<section class="card"><h3>Top candidates</h3>
<table><thead><tr><th>Family</th><th>Rule</th><th>Status</th><th>Prec</th><th>Rec</th><th>FPR</th><th>RemainDD</th><th>H</th></tr></thead>
<tbody>{''.join(top_rows)}</tbody></table></section>
<section class="card"><h3>事件日首次觸及 spread 的時間</h3>
<table><thead><tr><th>Level</th><th>Median min (hits)</th><th>By 10m</th><th>By 20m</th><th>By 30m</th><th>Never</th><th>Hit/N</th></tr></thead>
<tbody>{''.join(hit_rows)}</tbody></table></section>
{''.join(case_html)}
<section class="card"><h3>Figures</h3><div class="fig-grid">{''.join(figs)}</div></section>
<footer class="muted">Research layer only · not Order-layer auto sell-all</footer>
</div></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=RESEARCH_RRG / "20260714_us_tw_early_alert_5m.json")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    payload = json.loads(args.json.read_text(encoding="utf-8"))
    fig_dir = args.json.parent / args.json.stem / "figs"
    if not fig_dir.is_dir():
        fig_dir = Path(str(args.json.with_suffix("")) + "/figs")
    doc = render(payload, fig_dir)
    out = args.out or args.json.with_suffix(".html")
    out.write_text(doc, encoding="utf-8")
    bundle = args.json.parent / args.json.stem
    if bundle.is_dir():
        (bundle / "report.html").write_text(doc, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
