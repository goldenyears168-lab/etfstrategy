#!/usr/bin/env python3
"""Render US–TW divergence sell-all gate study as a standalone HTML report.

  PYTHONPATH=src .venv/bin/python scripts/research/render_us_tw_divergence_sell_gate_html.py
  PYTHONPATH=src .venv/bin/python scripts/research/render_us_tw_divergence_sell_gate_html.py \\
      --json reports/research/rrg/20260714_us_tw_divergence_sell_gate.json
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


def _pct(x: Any, digits: int = 1) -> str:
    if x is None:
        return "—"
    try:
        return f"{100.0 * float(x):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _num(x: Any, digits: int = 3) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _img_b64(path: Path) -> str:
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _status_class(status: str) -> str:
    s = (status or "").lower()
    if s == "accepted":
        return "st-accepted"
    if s == "weak":
        return "st-weak"
    return "st-rejected"


def _rule_label(rule: dict[str, Any] | None) -> str:
    if not rule:
        return "—"
    feat = rule.get("feature") or ""
    direction = rule.get("direction") or ""
    thr = rule.get("threshold")
    lead = rule.get("lead_label") or ""
    if feat:
        body = f"{feat} {direction} {thr}"
    else:
        body = rule.get("name") or "—"
    return f"{body}" + (f" · @{lead}" if lead else "")


def render_html(payload: dict[str, Any], *, fig_root: Path) -> str:
    meta = payload.get("meta") or {}
    events = payload.get("events") or []
    sens = payload.get("sensitivity") or {}
    registry = payload.get("hypothesis_registry") or []
    tops = payload.get("top_rule_candidates") or []
    h5 = payload.get("h5_style_compare") or {}
    crash = payload.get("crash_like_eval") or {}
    notes = payload.get("rejected_notes") or meta.get("data_notes") or []
    plot_paths = payload.get("plot_paths") or []

    # Resolve figures
    figs: list[tuple[str, Path]] = []
    for p in plot_paths:
        path = Path(p)
        if not path.is_file():
            alt = fig_root / path.name
            path = alt if alt.is_file() else path
        if path.is_file():
            figs.append((path.stem, path))
    # Also include any remaining figs in directory
    if fig_root.is_dir():
        known = {p for _, p in figs}
        for path in sorted(fig_root.glob("*.png")):
            if path not in known:
                figs.append((path.stem, path))

    top0 = tops[0] if tops else None
    top_ev = (top0 or {}).get("eval") or {}
    top_oos = top_ev.get("oos") or {}
    top_rule = top_ev.get("rule") or {}

    # KPI strip
    kpi_html = f"""
    <div class="kpis">
      <div class="kpi"><div class="lbl">視窗</div><div class="val">{_esc(meta.get("date_start"))} → {_esc(meta.get("date_end"))}</div></div>
      <div class="kpi"><div class="lbl">樣本日</div><div class="val">{_esc(meta.get("n_days"))}</div></div>
      <div class="kpi"><div class="lbl">主事件 (≥3%)</div><div class="val accent">{_esc(meta.get("n_events_primary"))}</div></div>
      <div class="kpi"><div class="lbl">IS 截止</div><div class="val">{_esc(meta.get("is_end"))}</div></div>
      <div class="kpi"><div class="lbl">Hedge β</div><div class="val">{_esc(meta.get("median_hedge_beta"))}</div></div>
    </div>"""

    # Verdict card
    verdict_html = ""
    if top0:
        verdict_html = f"""
        <section class="card verdict">
          <div class="eyebrow">Research · sell-all gate · 可操作結論</div>
          <h2>主候選 <span class="pill {_status_class(top_ev.get('status') or '')}">{_esc(top_ev.get("status") or "—")}</span>
            {_esc(top0.get("hypothesis_id"))} · {_esc(top0.get("title"))}</h2>
          <p class="rule mono">{_esc(_rule_label(top_rule))}</p>
          <div class="metrics">
            <div><span>OOS precision</span><b>{_pct(top_oos.get("precision"))}</b></div>
            <div><span>OOS recall</span><b>{_pct(top_oos.get("recall"))}</b></div>
            <div><span>OOS FPR</span><b>{_pct(top_oos.get("false_alarm_rate"))}</b></div>
            <div><span>觸發日中位 DD</span><b>{_num(top_oos.get("trig_median_dd"), 2)}%</b></div>
            <div><span>觸發 / 事件</span><b>{_esc(top_oos.get("n_triggers"))} / {_esc(top_oos.get("n_events"))}</b></div>
          </div>
          <p class="note">可作 Research 風險升級（human review）。事件稀少、方差大——
          <strong>不宜</strong> Order layer（下單層）自動 sell-all。</p>
        </section>"""

    # Narrative
    narrative = f"""
    <section class="card">
      <h3>什麼偏離 → 台股容易死很大</h3>
      <p>在多數日子美股期貨與台股同步的前提下，事件日較常伴隨：
      NQ overnight 偏弱，以及開盤後 60m <code>TW−NQ</code> spread 明顯更負。
      最大殺傷日（如 2025-04-07/09）常是 <strong>overnight 已裂 + 開盤後台股相對 NQ 繼續擴大弱勢</strong>；
      也有 NQ 穩、台股獨殺的日子。同步下殺事件均值 DD 略高於獨殺，但獨殺規則假警高。</p>
      <p>Crash-like 子集（primary≥3% 且 close≤−2% 或 open→low≥2.5%）：n={_esc(crash.get("n_crash_like_events"))}。
      單純 overnight / ES / SOX / 共整合殘差不足以獨立當自動全平倉。</p>
      <div class="h5box">
        <div>solo_kill 事件均 DD <b>{_num(h5.get("solo_kill_event_mean_dd"), 2)}%</b> · n={_esc(h5.get("n_solo_kill_events"))}</div>
        <div>sync_down 事件均 DD <b>{_num(h5.get("sync_down_event_mean_dd"), 2)}%</b> · n={_esc(h5.get("n_sync_down_events"))}</div>
      </div>
    </section>"""

    # Sensitivity table
    sens_rows = []
    for key, row in sens.items():
        if not isinstance(row, dict):
            continue
        sens_rows.append(
            "<tr>"
            f"<td>{_esc(key)}</td>"
            f"<td class='n'>{_esc(row.get('n_events'))}</td>"
            f"<td class='n'>{_pct(row.get('event_rate'), 2)}</td>"
            f"<td class='n'>{_esc(row.get('overlap_with_primary'))}</td>"
            "</tr>"
        )
    sens_html = f"""
    <section class="card">
      <h3>事件定義敏感度</h3>
      <p class="muted">主事件：Yahoo <code>^TWII</code> 1h peak→trough · 台北 09:00–13:30；小時棒不足則退回 IX0001 日線 (H−L)/H。</p>
      <table>
        <thead><tr><th>Definition</th><th>n</th><th>rate</th><th>overlap primary</th></tr></thead>
        <tbody>{''.join(sens_rows)}</tbody>
      </table>
    </section>"""

    # Events table
    ev_rows = []
    for e in events:
        ev_rows.append(
            "<tr>"
            f"<td>{_esc(e.get('date'))}</td>"
            f"<td class='n'>{_num(e.get('event_magnitude'), 2)}</td>"
            f"<td class='n'>{_num(e.get('daily_hl_pct'), 2)}</td>"
            f"<td class='n'>{_num(e.get('daily_close_chg_pct'), 2)}</td>"
            f"<td class='n'>{_num(e.get('intraday_peak_to_trough_1h'), 2)}</td>"
            "</tr>"
        )
    events_html = f"""
    <section class="card">
      <h3>事件日（primary · n={len(events)}）</h3>
      <div class="table-wrap">
      <table>
        <thead><tr><th>Date</th><th>Mag%</th><th>Daily HL%</th><th>Close chg%</th><th>Peak→Trough 1h</th></tr></thead>
        <tbody>{''.join(ev_rows)}</tbody>
      </table>
      </div>
    </section>"""

    # Hypothesis registry
    hyp_rows = []
    for h in registry:
        best = h.get("best") or {}
        # Special H7: side-by-side NQ vs ES overnight
        if isinstance(best.get("nq"), dict) and isinstance(best.get("es"), dict):
            nq_oos = (best["nq"].get("oos") or {})
            es_oos = (best["es"].get("oos") or {})
            prec_s = f"NQ {_pct(nq_oos.get('precision'))} / ES {_pct(es_oos.get('precision'))}"
            rec_s = f"NQ {_pct(nq_oos.get('recall'))} / ES {_pct(es_oos.get('recall'))}"
            fpr_s = f"NQ {_pct(nq_oos.get('false_alarm_rate'))} / ES {_pct(es_oos.get('false_alarm_rate'))}"
            lead = "open"
            rule_s = "NQ vs ES overnight compare"
            status = h.get("status") or "rejected"
        else:
            oos = best.get("oos") or {}
            rule = best.get("rule") or {}
            prec_s = _pct(oos.get("precision")) if oos.get("precision") is not None else "—"
            rec_s = _pct(oos.get("recall")) if oos.get("recall") is not None else "—"
            fpr_s = _pct(oos.get("false_alarm_rate")) if oos.get("false_alarm_rate") is not None else "—"
            lead = rule.get("lead_label") or "—"
            rule_s = _rule_label(rule)
            status = best.get("status") or h.get("status") or "—"
        hyp_rows.append(
            "<tr>"
            f"<td><b>{_esc(h.get('hypothesis_id'))}</b></td>"
            f"<td>{_esc(h.get('title'))}</td>"
            f"<td><span class='pill {_status_class(status)}'>{_esc(status)}</span></td>"
            f"<td class='n'>{_esc(prec_s)}</td>"
            f"<td class='n'>{_esc(rec_s)}</td>"
            f"<td class='n'>{_esc(fpr_s)}</td>"
            f"<td>{_esc(lead)}</td>"
            f"<td class='mono small'>{_esc(rule_s)}</td>"
            "</tr>"
        )
    hyp_html = f"""
    <section class="card">
      <h3>假設登錄表 · OOS</h3>
      <div class="table-wrap">
      <table class="compact">
        <thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Prec</th><th>Recall</th><th>FPR</th><th>Lead</th><th>Rule</th></tr></thead>
        <tbody>{''.join(hyp_rows)}</tbody>
      </table>
      </div>
    </section>"""

    # Top candidates detail
    cand_blocks = []
    for i, c in enumerate(tops, 1):
        ev = c.get("eval") or {}
        rule = ev.get("rule") or {}
        is_ = ev.get("is") or {}
        oos = ev.get("oos") or {}
        cand_blocks.append(
            f"""
            <article class="cand">
              <header>
                <span class="rank">#{i}</span>
                <h4>{_esc(c.get("hypothesis_id"))} — {_esc(c.get("title"))}</h4>
                <span class="pill {_status_class(ev.get('status') or '')}">{_esc(ev.get("status") or "—")}</span>
              </header>
              <p class="mono small">{_esc(_rule_label(rule))}</p>
              <div class="split">
                <div><div class="lbl">IS</div>
                  prec {_pct(is_.get("precision"))} · rec {_pct(is_.get("recall"))} ·
                  FPR {_pct(is_.get("false_alarm_rate"))} · trig {_esc(is_.get("n_triggers"))}</div>
                <div><div class="lbl">OOS</div>
                  prec {_pct(oos.get("precision"))} · rec {_pct(oos.get("recall"))} ·
                  FPR {_pct(oos.get("false_alarm_rate"))} · med DD {_num(oos.get("trig_median_dd"), 2)}%</div>
              </div>
            </article>"""
        )
    tops_html = f"""
    <section class="card">
      <h3>Top sell-all gate candidates</h3>
      <div class="cand-grid">{''.join(cand_blocks)}</div>
    </section>"""

    # Recommendation
    rec_html = """
    <section class="card">
      <h3>保守建議</h3>
      <ol>
        <li><strong>主候選（H2）</strong>：台股日盤 10:00，若 <code>TW ret(from open) − NQ ret ≤ −1.2%</code> →
            升級風險警示（human review）。OOS 假警約 4%/日、精度約 0.36、召回約 0.38。</li>
        <li><strong>加強召回（H1b）</strong>：<code>nq_overnight_open_pct + spread_60m ≤ −1.0</code> →
            召回更高但假警升至 ~9%。</li>
        <li><strong>AND（H10/H10b）</strong>：嚴格交集在本樣本幾乎殺光召回；最多當極罕見紅燈旁證。</li>
        <li>Overnight-only / ES-only / SOX / residual-z：<strong>rejected</strong> 作為獨立 sell-all。</li>
      </ol>
    </section>"""

    # Figures
    fig_blocks = []
    captions = {
        "align_20250409_sync_down_event": "同步下殺事件 · 2025-04-09",
        "align_20260423_tw_solo_kill_event": "台股獨殺事件 · 2026-04-23",
        "align_20260608_large_event": "大型事件 · 2026-06-08",
        "align_20250407_large_event": "大型事件 · 2025-04-07",
        "align_20250408_large_event": "大型事件 · 2025-04-08",
        "align_20250820_false_alarm": "假警報日 · 2025-08-20",
        "align_20260309_false_alarm": "假警報日 · 2026-03-09",
        "align_20260714_case_study_end": "案例日 · 2026-07-14（IX0001 lag 由 ^TWII 補齊）",
        "factor_box_event_vs_none": "事件日 vs 非事件日 · 關鍵因子分布",
        "timeline_gate_triggers": "閘門觸發 timeline",
    }
    for stem, path in figs:
        try:
            src = _img_b64(path)
        except OSError:
            continue
        cap = captions.get(stem, stem)
        fig_blocks.append(
            f"""
            <figure>
              <img src="{src}" alt="{_esc(stem)}" loading="lazy"/>
              <figcaption>{_esc(cap)}</figcaption>
            </figure>"""
        )
    figs_html = f"""
    <section class="card">
      <h3>Figures</h3>
      <div class="fig-grid">{''.join(fig_blocks)}</div>
    </section>"""

    # Data gaps
    note_items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
    notes_html = f"""
    <section class="card">
      <h3>資料限制</h3>
      <ul>{note_items or "<li>—</li>"}</ul>
      <p class="muted">本報告屬 Research layer（研究層）。任何實盤用途須再經 Order layer（下單層）政策——
      非 Regime layer（環境層）診斷，亦未採納為 strategy。</p>
    </section>"""

    css = """
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Mono:wght@400;500&family=Source+Sans+3:wght@400;600;700&display=swap');
    :root {
      --bg: #0f1419;
      --panel: #172029;
      --panel2: #1e2a36;
      --ink: #e8eef4;
      --muted: #8fa3b5;
      --line: #2a3a4a;
      --accent: #3dd6c6;
      --accent2: #f0b429;
      --danger: #ff6b6b;
      --ok: #5bd47a;
      --weak: #f0b429;
      --rej: #8fa3b5;
      --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
      --sans: "Source Sans 3", "Noto Sans TC", "PingFang TC", sans-serif;
      --display: "Fraunces", "Noto Serif TC", Georgia, serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--ink); background:
        radial-gradient(1200px 600px at 10% -10%, #1a3a3a 0%, transparent 55%),
        radial-gradient(900px 500px at 100% 0%, #2a2438 0%, transparent 50%),
        var(--bg);
      font: 16px/1.55 var(--sans);
    }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 40px 22px 80px; }
    header.hero { margin-bottom: 28px; }
    header.hero .brand {
      font-family: var(--display); font-size: clamp(2rem, 4vw, 2.8rem);
      letter-spacing: -0.02em; margin: 0 0 8px; line-height: 1.15;
    }
    header.hero .sub { color: var(--muted); max-width: 62ch; margin: 0; }
    .kpis {
      display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 22px 0 26px;
    }
    @media (max-width: 800px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
    .kpi {
      background: linear-gradient(180deg, var(--panel2), var(--panel));
      border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px;
    }
    .kpi .lbl { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
    .kpi .val { font-weight: 700; margin-top: 4px; font-size: 1.05rem; }
    .kpi .val.accent { color: var(--accent); }
    .card {
      background: color-mix(in srgb, var(--panel) 92%, #000);
      border: 1px solid var(--line); border-radius: 16px; padding: 22px 22px 18px;
      margin-bottom: 16px;
      box-shadow: 0 18px 40px rgba(0,0,0,.22);
    }
    .card h3, .card h2 { font-family: var(--display); margin: 0 0 12px; font-weight: 600; }
    .card h3 { font-size: 1.35rem; }
    .eyebrow { color: var(--accent); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 8px; }
    .verdict h2 { font-size: 1.55rem; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .rule { color: var(--accent2); margin: 8px 0 14px; }
    .mono { font-family: var(--mono); }
    .small { font-size: 13px; }
    .muted { color: var(--muted); }
    .note { color: var(--muted); border-left: 3px solid var(--accent2); padding-left: 12px; }
    .metrics {
      display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 14px 0;
    }
    @media (max-width: 800px) { .metrics { grid-template-columns: repeat(2, 1fr); } }
    .metrics div {
      background: rgba(255,255,255,.03); border-radius: 10px; padding: 10px 12px;
    }
    .metrics span { display: block; color: var(--muted); font-size: 12px; }
    .metrics b { font-size: 1.15rem; }
    .pill {
      display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px;
      font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
      border: 1px solid transparent;
    }
    .st-accepted { color: #062616; background: var(--ok); }
    .st-weak { color: #2a1c00; background: var(--weak); }
    .st-rejected { color: #e8eef4; background: #3a4654; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px 8px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    td.n { text-align: right; font-variant-numeric: tabular-nums; font-family: var(--mono); font-size: 12.5px; }
    .table-wrap { overflow-x: auto; }
    table.compact td, table.compact th { padding: 6px 7px; }
    .h5box {
      display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px;
    }
    .h5box div {
      background: rgba(61,214,198,.08); border: 1px solid rgba(61,214,198,.25);
      border-radius: 10px; padding: 10px 12px; font-size: 14px;
    }
    .cand-grid { display: grid; gap: 12px; }
    .cand {
      border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px;
      background: rgba(255,255,255,.02);
    }
    .cand header { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .cand h4 { margin: 0; font-size: 1.05rem; }
    .rank {
      width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center;
      background: var(--accent); color: #04231f; font-weight: 800; font-size: 12px;
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; color: var(--muted); font-size: 13px; }
    .split .lbl { color: var(--ink); font-weight: 600; margin-bottom: 2px; }
    @media (max-width: 700px) { .split, .h5box { grid-template-columns: 1fr; } }
    .fig-grid {
      display: grid; grid-template-columns: 1fr; gap: 18px;
    }
    figure { margin: 0; }
    figure img {
      width: 100%; height: auto; border-radius: 12px; border: 1px solid var(--line);
      background: #0a0e12;
    }
    figcaption { color: var(--muted); font-size: 13px; margin-top: 6px; }
    code {
      font-family: var(--mono); font-size: .9em;
      background: rgba(255,255,255,.06); padding: 1px 6px; border-radius: 5px;
    }
    ol { padding-left: 1.2rem; }
    ol li { margin: 8px 0; }
    footer {
      margin-top: 28px; color: var(--muted); font-size: 12px; text-align: center;
    }
    """

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>US–TW divergence · sell-all gate · {_esc(meta.get("date_end"))}</title>
  <style>{css}</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <p class="eyebrow">Research layer · US futures × TW market</p>
      <h1 class="brand">美股–台股偏離 · Sell-all 閘門研究</h1>
      <p class="sub">主事件：盤中 peak→trough ≥ {_esc(meta.get("threshold_pct"))}%（{_esc(meta.get("primary_event_def"))}）。
      資料：{_esc(meta.get("ix_daily_source"))} · {_esc(meta.get("tw_intraday_source"))} · {_esc(meta.get("us_intraday_source"))}。</p>
    </header>
    {kpi_html}
    {verdict_html}
    {narrative}
    {sens_html}
    {events_html}
    {hyp_html}
    {tops_html}
    {rec_html}
    {figs_html}
    {notes_html}
    <footer>Generated from 20260714_us_tw_divergence_sell_gate.json · single-file report with embedded figures</footer>
  </div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render US–TW sell-gate HTML report")
    ap.add_argument(
        "--json",
        type=Path,
        default=RESEARCH_RRG / "20260714_us_tw_divergence_sell_gate.json",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.json.is_file():
        print(f"BLOCKER: missing json {args.json}", file=sys.stderr)
        return 1

    payload = json.loads(args.json.read_text(encoding="utf-8"))
    fig_root = args.json.with_suffix("")  # .../20260714_us_tw_divergence_sell_gate
    if not (fig_root / "figs").is_dir():
        # try sibling bundle folder next to json
        fig_root = args.json.parent / args.json.stem
    figs_dir = fig_root / "figs"

    html_doc = render_html(payload, fig_root=figs_dir)
    out = args.out or args.json.with_suffix(".html")
    out.write_text(html_doc, encoding="utf-8")
    # also write into bundle
    if fig_root.is_dir():
        bundle_html = fig_root / "report.html"
        bundle_html.write_text(html_doc, encoding="utf-8")
        print(f"Wrote {bundle_html}")
    print(f"Wrote {out}")
    print(f"size={out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
