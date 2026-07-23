#!/usr/bin/env python3
"""Render 金額×RRG sweep JSON → HTML.

  PYTHONPATH=src .venv/bin/python scripts/research/render_amount_rrg_copytrade_html.py
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from report_paths import REPORTS_RESEARCH  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "dual-branch-copytrade"


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _num(x: Any, d: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(x: Any, d: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def _latest(dir_path: Path) -> Path | None:
    hits = sorted(
        dir_path.glob("*_amount_rrg_copytrade_sweep.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def _status_cls(st: str | None) -> str:
    s = (st or "").lower()
    if s == "passed":
        return "ok"
    if s == "rejected":
        return "fail"
    return ""


def render(payload: dict, *, source_name: str) -> str:
    hyp = payload.get("hypotheses") or {}
    champs = payload.get("champions") or {}
    gates = (hyp.get("H_RRG_IMPROVES_APLD") or {}).get("by_gate") or {}
    rec = hyp.get("recommended_sleeve") or {}
    base = hyp.get("baseline_xd_amount_none") or {}

    gate_rows = []
    for g, row in gates.items():
        br = row.get("best_ret") or {}
        ba = row.get("best_apld") or {}
        gate_rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(g)}</td>"
            f"<td>{'Y' if row.get('ret_beats_none') else 'N'}</td>"
            f"<td>{'Y' if row.get('apld_beats_none') else 'N'}</td>"
            f"<td class='mono'>{_esc(br.get('threshold_label'))} {_esc(br.get('strategy_id'))}</td>"
            f"<td class='n'>{_pct(br.get('period_return_pct'))}</td>"
            f"<td class='n'>{_num(ba.get('alpha_per_locked_day'), 2)}</td>"
            f"<td class='n'>{_esc(br.get('n_signals'))}</td>"
            "</tr>"
        )

    champ_cards = []
    for key, label, badge in (
        ("by_period_return", "主冠軍 · 期間報酬", "PRIMARY"),
        ("by_alpha", "次優 · α", "ALPHA"),
        ("by_alpha_per_locked_day", "次優 · α/日", "EFFICIENCY"),
    ):
        c = champs.get(key) or {}
        champ_cards.append(
            f"""
            <article class="card">
              <div class="card-head"><h3>{_esc(label)}</h3>
                <span class="badge">{_esc(badge)}</span></div>
              <p class="hero">{_esc(c.get('strategy_id'))}</p>
              <dl class="kv">
                <div><dt>mode / thr / gate</dt>
                  <dd>{_esc(c.get('mode'))} · {_esc(c.get('threshold_label'))} · {_esc(c.get('rrg_gate'))}</dd></div>
                <div><dt>期間報酬</dt><dd>{_pct(c.get('period_return_pct'))}</dd></div>
                <div><dt>α / α/日</dt>
                  <dd>{_num(c.get('recycled_total_alpha_ntd'), 0)} · {_num(c.get('alpha_per_locked_day'), 2)}</dd></div>
                <div><dt>槽 / 捕獲</dt>
                  <dd>{_esc(c.get('n_slots'))} · {_pct(c.get('signal_capture_pct'))}</dd></div>
                <div><dt>gate pass / drop</dt>
                  <dd>{_esc(c.get('n_gate_pass_days'))} · {_esc(c.get('n_gate_drop_legs'))}</dd></div>
              </dl>
            </article>"""
        )

    h_apld = hyp.get("H_RRG_IMPROVES_APLD") or {}
    h_ret = hyp.get("H_RRG_IMPROVES_RET") or {}
    h_ad = hyp.get("H_ADAPT_P90") or {}

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>金額門檻 × RRG Copytrade</title>
<style>
:root {{
  --bg0:#0c1118; --bg1:#141c27; --bg2:#1c2736; --line:#2a3a4f;
  --text:#e8eef6; --muted:#8fa3bb; --gold:#e2b657; --teal:#3ecfb2;
  --font-d:"Iowan Old Style",Palatino,serif;
  --font-b:"IBM Plex Sans","PingFang TC",sans-serif;
  --font-m:"IBM Plex Mono",Menlo,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;color:var(--text);font-family:var(--font-b);
background:radial-gradient(1000px 500px at 10% -10%,#1e3348,transparent 55%),linear-gradient(180deg,var(--bg0),#0a0e14)}}
.wrap{{max-width:1100px;margin:0 auto;padding:2.2rem 1.2rem 3.5rem}}
.kicker{{font-family:var(--font-m);font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--gold)}}
h1{{font-family:var(--font-d);font-size:clamp(1.5rem,3vw,2.1rem);margin:.4rem 0}}
.sub{{color:var(--muted);max-width:42rem}}
.meta{{display:flex;flex-wrap:wrap;gap:.8rem 1.2rem;margin-top:1rem;color:var(--muted);font-size:.85rem}}
.meta b{{color:var(--text)}}
section{{margin:2rem 0}}
h2{{font-family:var(--font-d);font-size:1.25rem;margin:0 0 .5rem}}
.verdict{{padding:.85rem 1rem;border:1px solid var(--line);border-radius:10px;background:var(--bg1);margin:.5rem 0}}
.verdict.ok{{border-color:#2f6b4a}}.verdict.fail{{border-color:#6b3a3a}}
.grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:.8rem}}
@media(max-width:900px){{.grid3{{grid-template-columns:1fr}}}}
.card{{background:var(--bg1);border:1px solid var(--line);border-radius:12px;padding:1rem}}
.card-head{{display:flex;justify-content:space-between;align-items:center}}
.badge{{font-family:var(--font-m);font-size:.65rem;background:var(--gold);color:var(--bg0);padding:.15rem .45rem;border-radius:999px}}
.hero{{font-family:var(--font-d);font-size:1.45rem;color:var(--gold);margin:.4rem 0 .7rem}}
.kv{{display:grid;gap:.3rem;margin:0}}
.kv div{{display:flex;justify-content:space-between;gap:.6rem;border-top:1px solid var(--line);padding-top:.28rem}}
.kv dt{{color:var(--muted);font-size:.8rem}}.kv dd{{margin:0;font-family:var(--font-m);font-size:.82rem}}
.table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th,td{{padding:.4rem .5rem;border-bottom:1px solid var(--line);text-align:left}}
th{{background:var(--bg2);color:var(--muted);font-weight:500}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
td.mono{{font-family:var(--font-m)}}
.note{{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.8rem}}
ol.steps{{padding-left:1.2rem}}
</style></head><body>
<div class="wrap">
  <p class="kicker">Research · Amount × RRG</p>
  <h1>金額門檻 × RRG 疊加 Sweep</h1>
  <p class="sub">固定／自適應金額門檻，疊加 RRG 象限與動能閘門；對照純金額 baseline。</p>
  <div class="meta">
    <span>窗口 <b>{_esc(payload.get('window_start'))} → {_esc(payload.get('window_end'))}</b></span>
    <span>cells <b>{_esc(payload.get('n_cells'))}</b></span>
    <span>來源 <b>{_esc(source_name)}</b></span>
  </div>

  <section>
    <h2>假設檢定</h2>
    <div class="verdict {_status_cls(h_apld.get('status'))}">
      <strong>H_RRG_IMPROVES_APLD：{_esc(h_apld.get('status'))}</strong> · {_esc(h_apld.get('note'))}
    </div>
    <div class="verdict {_status_cls(h_ret.get('status'))}">
      <strong>H_RRG_IMPROVES_RET：{_esc(h_ret.get('status'))}</strong> · {_esc(h_ret.get('note'))}
    </div>
    <div class="verdict {_status_cls(h_ad.get('status'))}">
      <strong>H_ADAPT_P90：{_esc(h_ad.get('status'))}</strong> · {_esc(h_ad.get('note'))}
    </div>
  </section>

  <section>
    <h2>冠軍</h2>
    <div class="grid3">{"".join(champ_cards)}</div>
  </section>

  <section>
    <h2>建議規格</h2>
    <ol class="steps">
      <li>金額 ≥ <b>{_esc(rec.get('threshold_label'))}</b>（{_esc(rec.get('thr_kind'))}）</li>
      <li>RRG gate = <b>{_esc(rec.get('rrg_gate'))}</b></li>
      <li>mode <b>{_esc(rec.get('mode'))}</b> · {_esc(rec.get('strategy_id'))} · Top-{_esc(rec.get('top_n'))} · {_esc(rec.get('n_slots'))} 槽</li>
      <li>對照純金額 baseline：{_esc(base.get('threshold_label'))} {_esc(base.get('strategy_id'))} ret {_pct(base.get('period_return_pct'))}</li>
    </ol>
  </section>

  <section>
    <h2>RRG gate vs 純金額（xd fixed）</h2>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>gate</th><th>勝 ret</th><th>勝 α/日</th><th>最佳規格</th><th>ret%</th><th>α/日</th><th>n_sig</th>
      </tr></thead>
      <tbody>{"".join(gate_rows)}</tbody>
    </table></div>
  </section>

  <p class="note">PIT: branch tape + RRG close ffill on signal day. Cost 0 bps. Research only.</p>
</div></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    src = args.json or _latest(OUT_DIR)
    if not src or not src.exists():
        print(f"ERROR: no JSON under {OUT_DIR}", file=sys.stderr)
        return 2
    payload = json.loads(src.read_text(encoding="utf-8"))
    out = args.out or src.with_suffix(".html")
    out.write_text(render(payload, source_name=src.name), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
