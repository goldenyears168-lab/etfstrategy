#!/usr/bin/env python3
"""Run Detach Gate rebuy · long-sample adoption stress (1h + 5m check).

  PYTHONPATH=src .venv/bin/python scripts/research/run_detach_gate_rebuy_adoption.py

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
from research.backtest.detach_gate_rebuy_adoption_study import run_study  # noqa: E402


def esc(x: object) -> str:
    return html_mod.escape("" if x is None else str(x))


def write_html(payload: dict, out_dir: Path) -> Path:
    meta = payload.get("meta") or {}
    ge = payload.get("gate_evaluation") or {}
    verdict = payload.get("three_way_verdict") or {}
    p1 = payload.get("panel_1h") or {}
    champ = ge.get("champion_id")
    aggs = {a["variant_id"]: a for a in (p1.get("variant_aggs") or [])}
    ca = aggs.get(champ or "") or p1.get("champion_agg") or {}

    gate_rows = "".join(
        "<tr>"
        f"<td><code>{esc(gid)}</code></td>"
        f"<td>{'PASS' if g.get('pass') else 'FAIL'}</td>"
        f"<td class='muted'>{esc(g.get('detail'))}</td>"
        "</tr>"
        for gid, g in (ge.get("gates") or {}).items()
    )
    var_rows = "".join(
        "<tr>"
        f"<td><code>{esc(a.get('variant_id'))}</code></td>"
        f"<td class='n'>{esc(a.get('n_mt_with_signal'))}</td>"
        f"<td class='n'>{esc(a.get('fraction_before_trough'))}</td>"
        f"<td class='n'>{esc(a.get('median_headroom_vs_trough_pct'))}</td>"
        f"<td class='n'>{esc(a.get('buy_near_trough_rate'))}</td>"
        f"<td class='n'>{esc(a.get('fpr_sig_on_no_mt'))}</td>"
        "</tr>"
        for a in (p1.get("variant_aggs") or [])
    )
    a_v = verdict.get("A_manual_sop") or {}
    b_v = verdict.get("B_strategy_advisory") or {}
    c_v = verdict.get("C_order_auto_rebuy") or {}

    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"/>
<title>Detach Gate rebuy · long-sample adoption</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0f1419;color:#e7eef7;margin:0;padding:24px;line-height:1.45}}
.card{{background:#172029;border:1px solid #243140;border-radius:10px;padding:14px 16px;margin:10px 0}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.kpi .card{{margin:0}} .kpi b{{display:block;font-size:1.05rem;margin-top:4px}}
.muted{{color:#8fa3b5}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}} th,td{{padding:6px 8px;border-bottom:1px solid #243140}}
th{{text-align:left;color:#8fa3b5}} code{{font-size:.78rem}}
.pass{{color:#7dcea0}} .fail{{color:#f5a6a6}}
</style></head><body>
<h1>Detach Gate rebuy · long-sample adoption</h1>
<p class="muted">Research · detach-gate-trough-find · Order rebuy=false · 1h primary</p>
<div class="kpi">
  <div class="card">1h days<b>{esc(meta.get('n_days_1h'))}</b></div>
  <div class="card">5m days<b>{esc(meta.get('n_days_5m'))}</b></div>
  <div class="card">Champion<b>{esc(champ)}</b></div>
  <div class="card">Gates<b>{esc(ge.get('n_pass'))}/{esc(ge.get('n_gates'))}</b></div>
  <div class="card">(A) Manual<b>{esc(a_v.get('decision'))}</b></div>
  <div class="card">(B) Strategy<b>{esc(b_v.get('decision'))}</b></div>
  <div class="card">(C) Order<b>{esc(c_v.get('decision'))}</b></div>
</div>
<section class="card"><h2>Champion metrics</h2>
<p class="muted">FPR={esc(ca.get('fpr_sig_on_no_mt'))} · med headroom={esc(ca.get('median_headroom_vs_trough_pct'))}% ·
near={esc(ca.get('buy_near_trough_rate'))} · n RED={esc(ca.get('n_event_days'))} · n MT={esc(ca.get('n_meaningful_trough'))}</p>
</section>
<section class="card"><h2>Gate pass/fail</h2>
<table><thead><tr><th>gate</th><th>result</th><th>detail</th></tr></thead>
<tbody>{gate_rows}</tbody></table>
</section>
<section class="card"><h2>1h variants</h2>
<table><thead><tr>
<th>variant</th><th>n MT sig</th><th>% before</th><th>med head%</th><th>near</th><th>FPR</th>
</tr></thead><tbody>{var_rows}</tbody></table>
</section>
<section class="card"><h2>Coarseness</h2>
<p>{esc(verdict.get('coarseness_caveat'))}</p>
</section>
</body></html>"""
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--lookback-days", type=int, default=730)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--stamp", default=None)
    ap.add_argument("--no-5m", action="store_true")
    args = ap.parse_args(argv)

    stamp = args.stamp or f"{date.today().strftime('%Y%m%d')}_detach_gate_rebuy_adoption"
    out_dir = args.out_dir or (RESEARCH_RRG / stamp)
    result = run_study(
        date_end=args.date_end,
        lookback_days=args.lookback_days,
        out_dir=out_dir,
        include_5m=not args.no_5m,
    )
    payload = result["payload"]

    out_json = RESEARCH_RRG / f"{stamp}.json"
    out_md = RESEARCH_RRG / f"{stamp}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(result["markdown"], encoding="utf-8")
    html_path = write_html(payload, out_dir)

    meta = payload.get("meta") or {}
    ge = payload.get("gate_evaluation") or {}
    verd = payload.get("three_way_verdict") or {}
    print(
        f"1h_n={meta.get('n_days_1h')} 5m_n={meta.get('n_days_5m')} "
        f"champ={ge.get('champion_id')} gates={ge.get('n_pass')}/{ge.get('n_gates')} "
        f"A={(verd.get('A_manual_sop') or {}).get('decision')} "
        f"B={(verd.get('B_strategy_advisory') or {}).get('decision')} "
        f"C={(verd.get('C_order_auto_rebuy') or {}).get('decision')}"
    )
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
