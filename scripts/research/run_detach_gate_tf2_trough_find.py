#!/usr/bin/env python3
"""Run TF-2 · Detach Gate trough-find (gap_pulse IMPROVE vs post-event trough).

  PYTHONPATH=src .venv/bin/python scripts/research/run_detach_gate_tf2_trough_find.py

Research only — does not change Order-layer rebuy:false.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.detach_gate_tf2_trough_find_study import run_study  # noqa: E402


def esc(x: object) -> str:
    return html_mod.escape("" if x is None else str(x))


def write_html(payload: dict, out_dir: Path) -> Path:
    meta = payload.get("meta") or {}
    p = (payload.get("primary_frozen_red") or {}).get("agg_improve") or {}
    flip = (payload.get("primary_frozen_red") or {}).get("agg_flip") or {}
    s = (payload.get("secondary_spread_arm") or {}).get("agg_improve") or {}
    case = ((payload.get("case_20260714") or {}).get("frozen_red")) or {}
    ev_rows = "".join(
        "<tr>"
        f"<td>{esc(r.get('date'))}</td>"
        f"<td>{esc(r.get('event_poll'))}</td>"
        f"<td class='n'>{esc(r.get('post_event_trough_poll'))}</td>"
        f"<td class='n'>{esc(r.get('post_event_dd_pct'))}</td>"
        f"<td>{esc(r.get('improve_poll'))}</td>"
        f"<td class='n'>{esc(r.get('improve_lead_min'))}</td>"
        f"<td>{'Y' if r.get('improve_before_trough') else ''}</td>"
        f"<td class='n'>{esc(r.get('improve_headroom_vs_trough_pct'))}</td>"
        "</tr>"
        for r in ((payload.get("primary_frozen_red") or {}).get("days_with_event") or [])
    )
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"/>
<title>TF-2 · IMPROVE → trough</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0f1419;color:#e7eef7;margin:0;padding:24px;line-height:1.45}}
.card{{background:#172029;border:1px solid #243140;border-radius:10px;padding:14px 16px;margin:10px 0}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.kpi .card{{margin:0}} .kpi b{{display:block;font-size:1.15rem;margin-top:4px}}
.muted{{color:#8fa3b5}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}} th,td{{padding:6px 8px;border-bottom:1px solid #243140}}
th{{text-align:left;color:#8fa3b5}}
</style></head><body>
<h1>TF-2 · gap_pulse IMPROVE → post-event trough</h1>
<p class="muted">Research · detach-gate-trough-find · no Order auto-rebuy · {esc(meta.get('window'))}</p>
<div class="kpi">
  <div class="card">Panel days<b>{esc(meta.get('n_days'))}</b></div>
  <div class="card">RED events<b>{esc(p.get('n_event_days'))}</b></div>
  <div class="card">MT + IMPROVE<b>{esc(p.get('n_mt_with_signal'))}</b></div>
  <div class="card">Med lead (before)<b>{esc(p.get('median_lead_min_before'))} min</b></div>
  <div class="card">Before trough<b>{esc(p.get('fraction_signal_before_trough'))}</b></div>
  <div class="card">FPR after trough<b>{esc(p.get('fpr_signal_after_trough'))}</b></div>
</div>
<section class="card"><h2>Primary frozen RED · IMPROVE</h2>
<p>med headroom={esc(p.get('median_headroom_vs_trough_pct'))}% · buy@near={esc(p.get('buy_near_trough_rate'))} ·
FPR(no-MT)={esc(p.get('fpr_improve_no_meaningful_trough'))}</p>
<p class="muted">Flip: med lead={esc(flip.get('median_lead_min_before'))} · before={esc(flip.get('fraction_signal_before_trough'))}</p>
</section>
<section class="card"><h2>Secondary spread≤−0.6 arm</h2>
<p>n={esc(s.get('n_event_days'))} · MT sig={esc(s.get('n_mt_with_signal'))} ·
med lead={esc(s.get('median_lead_min_before'))} · before={esc(s.get('fraction_signal_before_trough'))}</p>
</section>
<section class="card"><h2>Case 2026-07-14</h2>
<p>RED={esc(case.get('event_poll'))} · trough={esc(case.get('post_event_trough_poll'))} ·
IMPROVE={esc(case.get('improve_poll'))} · lead={esc(case.get('improve_lead_min'))} min ·
before={esc(case.get('improve_before_trough'))} · headroom={esc(case.get('improve_headroom_vs_trough_pct'))}%</p>
</section>
<section class="card"><h2>RED event days</h2>
<table><thead><tr>
<th>date</th><th>RED</th><th>trough</th><th>DD%</th><th>IMPROVE</th><th>lead</th><th>before</th><th>headroom</th>
</tr></thead><tbody>{ev_rows}</tbody></table>
</section>
</body></html>"""
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--stamp", default=None)
    args = ap.parse_args(argv)

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_detach_gate_tf2_trough_find"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    result = run_study(date_end=args.date_end, out_dir=out_dir)
    payload = result["payload"]

    out_json = RESEARCH_RRG / f"{stamp}.json"
    out_md = RESEARCH_RRG / f"{stamp}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")
    html_path = write_html(payload, out_dir)

    meta = payload.get("meta") or {}
    p = (payload.get("primary_frozen_red") or {}).get("agg_improve") or {}
    case = ((payload.get("case_20260714") or {}).get("frozen_red")) or {}
    print(
        f"n_days={meta.get('n_days')} red_events={p.get('n_event_days')} "
        f"mt_sig={p.get('n_mt_with_signal')} med_lead={p.get('median_lead_min_before')} "
        f"before={p.get('fraction_signal_before_trough')} "
        f"fpr_after={p.get('fpr_signal_after_trough')}"
    )
    print(
        f"7/14 RED={case.get('event_poll')} trough={case.get('post_event_trough_poll')} "
        f"IMPROVE={case.get('improve_poll')} lead={case.get('improve_lead_min')}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
