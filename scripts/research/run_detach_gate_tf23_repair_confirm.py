#!/usr/bin/env python3
"""Run TF-2∩TF-3 · Detach Gate repair-confirm (manual rebuy candidacy).

  PYTHONPATH=src .venv/bin/python scripts/research/run_detach_gate_tf23_repair_confirm.py

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
from research.backtest.detach_gate_tf23_repair_confirm_study import run_study  # noqa: E402


def esc(x: object) -> str:
    return html_mod.escape("" if x is None else str(x))


HEADLINE_IDS = [
    "A_tf2",
    "B_tf3_d0.05",
    "B_tf3_d0.1",
    "B_tf3_d0.15",
    "C_arm_tf3_d0.05",
    "C_arm_tf3_d0.1",
    "C_arm_tf3_d0.15",
    "D_cool_red_K30_d0.1",
    "D_cool_imp_K30_d0.1",
    "D_cool_imp_K45_d0.1",
    "E_tf3_follow_d0.1",
    "C_E_arm_follow_d0.1",
    "D_E_cool_imp30_follow_d0.1",
    "G_C_arm_tf3_d0.1_cut1230",
    "G_D_cool_imp_K30_d0.1_cut1230",
    "G_D_cool_imp_K30_d0.1_cut1300",
    "G_C_E_arm_follow_d0.1_cut1230",
]


def write_html(payload: dict, out_dir: Path) -> Path:
    meta = payload.get("meta") or {}
    aggs = {a["variant_id"]: a for a in (payload.get("variant_aggs") or [])}
    rec = payload.get("recommendation") or {}
    case = payload.get("case_20260714") or {}

    rows = "".join(
        "<tr>"
        f"<td><code>{esc(vid)}</code></td>"
        f"<td class='n'>{esc((aggs.get(vid) or {}).get('n_mt_with_signal'))}</td>"
        f"<td class='n'>{esc((aggs.get(vid) or {}).get('fraction_before_trough'))}</td>"
        f"<td class='n'>{esc((aggs.get(vid) or {}).get('median_lead_min_before'))}</td>"
        f"<td class='n'>{esc((aggs.get(vid) or {}).get('median_headroom_vs_trough_pct'))}</td>"
        f"<td class='n'>{esc((aggs.get(vid) or {}).get('buy_near_trough_rate'))}</td>"
        f"<td class='n'>{esc((aggs.get(vid) or {}).get('fpr_sig_on_no_mt'))}</td>"
        "</tr>"
        for vid in HEADLINE_IDS
        if vid in aggs
    )
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"/>
<title>TF-2∩TF-3 · repair confirm</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0f1419;color:#e7eef7;margin:0;padding:24px;line-height:1.45}}
.card{{background:#172029;border:1px solid #243140;border-radius:10px;padding:14px 16px;margin:10px 0}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.kpi .card{{margin:0}} .kpi b{{display:block;font-size:1.1rem;margin-top:4px}}
.muted{{color:#8fa3b5}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}} th,td{{padding:6px 8px;border-bottom:1px solid #243140}}
th{{text-align:left;color:#8fa3b5}} code{{font-size:.78rem}}
</style></head><body>
<h1>TF-2∩TF-3 · repair confirm (manual rebuy)</h1>
<p class="muted">Research · detach-gate-trough-find · Order rebuy=false · {esc(meta.get('window'))}</p>
<div class="kpi">
  <div class="card">Panel days<b>{esc(meta.get('n_days'))}</b></div>
  <div class="card">Manual pick<b>{esc(rec.get('manual_use'))}</b></div>
  <div class="card">TF-4<b>skipped</b></div>
  <div class="card">Case day<b>{esc(meta.get('primary_case_day'))}</b></div>
</div>
<section class="card"><h2>Headline variants</h2>
<table><thead><tr>
<th>variant</th><th>n MT sig</th><th>% before</th><th>med lead</th><th>med headroom%</th><th>near</th><th>FPR(no-MT)</th>
</tr></thead><tbody>{rows}</tbody></table>
</section>
<section class="card"><h2>Verdict</h2>
<p>{esc(rec.get('rationale'))}</p>
</section>
<section class="card"><h2>Case 2026-07-14</h2>
<p>RED={esc(case.get('event_poll'))} · trough={esc(case.get('post_event_trough_poll'))} ·
IMPROVE arm={esc(case.get('improve_poll'))} · DD={esc(case.get('post_event_dd_pct'))}%</p>
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

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_detach_gate_tf23_repair_confirm"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    result = run_study(date_end=args.date_end, out_dir=out_dir)
    payload = result["payload"]

    out_json = RESEARCH_RRG / f"{stamp}.json"
    out_md = RESEARCH_RRG / f"{stamp}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")
    html_path = write_html(payload, out_dir)

    meta = payload.get("meta") or {}
    rec = payload.get("recommendation") or {}
    best = rec.get("best_agg") or {}
    print(
        f"n_days={meta.get('n_days')} variants={len(meta.get('variant_ids') or [])} "
        f"manual={rec.get('manual_use')} "
        f"FPR={best.get('fpr_sig_on_no_mt')} headroom={best.get('median_headroom_vs_trough_pct')}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
