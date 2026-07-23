#!/usr/bin/env python3
"""Run 5m-only sell-gate live readiness (no 1h · no rebuy).

  PYTHONPATH=src .venv/bin/python scripts/research/run_us_tw_sell_only_live_readiness.py
"""

from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.us_tw_sell_only_live_readiness_study import run_study  # noqa: E402


def esc(x: object) -> str:
    return html_mod.escape("" if x is None else str(x))


def img(p: Path) -> str:
    if not p.exists():
        return ""
    b = base64.b64encode(p.read_bytes()).decode("ascii")
    return f'<img alt="{esc(p.name)}" src="data:image/png;base64,{b}"/>'


def write_html(payload: dict, out_dir: Path, stamp: str) -> Path:
    v = payload.get("verdict") or {}
    c5 = payload.get("champion_5m") or {}
    live = payload.get("live_rule_candidate") or {}
    tier = (payload.get("meta") or {}).get("tiered_sop") or {}
    is_oos = payload.get("is_oos_5m") or {}
    oos = is_oos.get("oos") or {}
    fig1 = out_dir / "figs" / "5m_recall_vs_fpr.png"
    fig2 = out_dir / "figs" / "5m_daily_edge.png"
    cal = payload.get("calendar_days") or []
    cal_rows = "".join(
        "<tr>"
        f"<td>{esc(r.get('date'))}</td>"
        f"<td>{'Y' if r.get('is_event3') else ''}</td>"
        f"<td>{'Y' if r.get('triggered') else ''}</td>"
        f"<td>{esc(r.get('detach_poll'))}</td>"
        f"<td class='n'>{esc(r.get('detach_tw_from_open'))}</td>"
        f"<td class='n'>{esc(r.get('trough_from_open_pct'))}</td>"
        f"<td class='n'>{esc(r.get('exit_headroom_vs_trough_pct'))}</td>"
        "</tr>"
        for r in cal
        if r.get("triggered") or r.get("is_event3")
    )
    top_rows = "".join(
        "<tr>"
        f"<td class='mono'>{esc(r.get('strategy_id'))}</td>"
        f"<td class='n'>{esc(r.get('event_recall'))}</td>"
        f"<td class='n'>{esc(r.get('fpr_nonevent'))}</td>"
        f"<td class='n'>{esc(r.get('median_exit_headroom_pct'))}</td>"
        f"<td class='n'>{esc(r.get('min_exit_headroom_pct'))}</td>"
        f"<td class='n'>{esc(r.get('sell_near_trough_rate_on_events'))}</td>"
        f"<td>{esc(r.get('live_ops_memo_ok'))}</td>"
        "</tr>"
        for r in (payload.get("top_5m") or [])[:10]
    )
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"/>
<title>Detach Gate · trough-first</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0f1419;color:#e7eef7;margin:0;padding:24px;line-height:1.45}}
.card{{background:#172029;border:1px solid #243140;border-radius:10px;padding:14px 16px;margin:10px 0}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.kpi .card{{margin:0}} .kpi b{{display:block;font-size:1.15rem;margin-top:4px}}
.muted{{color:#8fa3b5}} .mono{{font-family:ui-monospace,Menlo,monospace;font-size:.85rem}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}} th,td{{padding:6px 8px;border-bottom:1px solid #243140}}
th{{text-align:left;color:#8fa3b5}} img{{max-width:100%;border-radius:8px;margin:8px 0}}
.ok{{color:#3dd6c6}} .bad{{color:#e67e22}}
</style></head><body>
<h1>Detach Gate · 台美脫鉤閘門（對谷底 · 可接回）</h1>
<p class="muted">評分主軸 = exit − trough headroom · 對收盤次要 · Human RED · not auto</p>
<div class="kpi">
  <div class="card">Verdict<b class="{'ok' if v.get('for_human_ops_sop') else 'bad'}">{esc(v.get('level'))}</b></div>
  <div class="card">Confidence<b>{esc(v.get('confidence_0_1'))}</b></div>
  <div class="card">Usable SOP<b>{esc(v.get('for_human_ops_sop'))}</b></div>
  <div class="card">OOS soft<b>{esc(v.get('oos_ok'))}</b></div>
  <div class="card">Auto Order<b class="bad">False</b></div>
</div>
<section class="card"><h2>Verdict</h2><p>{esc(v.get('note'))}</p></section>
<section class="card"><h2>RED · 每5分</h2>
<p class="mono">{esc(live.get('strategy_id'))}</p>
<p>{esc(live.get('rule'))}</p>
<p>recall={esc(c5.get('event_recall'))} · FPR={esc(c5.get('fpr_nonevent'))} · trig={esc(c5.get('trigger_rate'))}</p>
<p><b>vs 谷底</b> med headroom={esc(c5.get('median_exit_headroom_pct'))}% · min={esc(c5.get('min_exit_headroom_pct'))}% · sell@trough={esc(c5.get('sell_near_trough_rate_on_events'))} · saved_vs_trough={esc(c5.get('saved_vs_trough_ntd'))} NTD</p>
<p class="muted">對收盤 edge（次要）={esc(c5.get('edge_vs_hold_ntd'))} · miss={esc(c5.get('miss_dates'))}</p>
<p><b>YELLOW</b> {esc(tier.get('YELLOW_watch'))}</p>
</section>
<section class="card"><h2>5m 後半 OOS（trough）</h2>
<p>cut={esc(is_oos.get('cut'))} · recall={esc(oos.get('event_recall'))} · medH={esc(oos.get('median_exit_headroom_pct'))} · sell@trough={esc(oos.get('sell_near_trough_rate_on_events'))} · cat={esc(oos.get('catastrophic_miss_n'))}</p>
</section>
{img(fig1)}{img(fig2)}
<section class="card"><h2>Top 5m</h2>
<table><thead><tr><th>id</th><th>recall</th><th>FPR</th><th>medH%</th><th>minH%</th><th>sell@trough</th><th>ops</th></tr></thead><tbody>{top_rows}</tbody></table>
</section>
<section class="card"><h2>Calendar · events / triggers</h2>
<table><thead><tr><th>date</th><th>ev3</th><th>trig</th><th>detach</th><th>exit%</th><th>trough%</th><th>headroom%</th></tr></thead><tbody>{cal_rows}</tbody></table>
</section>
</body></html>"""
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    flat = RESEARCH_RRG / f"{stamp}.html"
    flat.write_text(html, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--stamp", default=None)
    ap.add_argument("--include-1h", action="store_true")
    args = ap.parse_args(argv)

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_us_tw_5m_sell_gate_ready"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    result = run_study(date_end=args.date_end, out_dir=out_dir, include_1h=args.include_1h)
    payload = result["payload"]
    out_json = RESEARCH_RRG / f"{stamp}.json"
    out_md = RESEARCH_RRG / f"{stamp}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")
    html_path = write_html(payload, out_dir, stamp)

    v = payload.get("verdict") or {}
    c5 = payload.get("champion_5m") or {}
    print(
        f"verdict={v.get('level')} confidence={v.get('confidence_0_1')} "
        f"usable={v.get('for_human_ops_sop')} oos_ok={v.get('oos_ok')}"
    )
    print(
        f"5m={c5.get('strategy_id')} recall={c5.get('event_recall')} "
        f"fpr={c5.get('fpr_nonevent')} medH={c5.get('median_exit_headroom_pct')} "
        f"minH={c5.get('min_exit_headroom_pct')} sell@trough={c5.get('sell_near_trough_rate_on_events')}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
