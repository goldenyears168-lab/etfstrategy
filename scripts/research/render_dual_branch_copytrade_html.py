#!/usr/bin/env python3
"""Render 雙分點 Copytrade sweep JSON → self-contained HTML.

  PYTHONPATH=src .venv/bin/python scripts/research/render_dual_branch_copytrade_html.py
  PYTHONPATH=src .venv/bin/python scripts/research/render_dual_branch_copytrade_html.py \\
      --json reports/research/dual-branch-copytrade/20260717_dual_branch_copytrade_sweep.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from render_yuanta_songjiang_copytrade_html import (  # noqa: E402
    CHAMPION_KEYS,
    _esc,
    _num,
    _pct,
    _trade_rows,
)
from research.backtest.dual_branch_copytrade_sweep import (  # noqa: E402
    load_champion_trade_ledger,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "dual-branch-copytrade"


def _latest_json(dir_path: Path) -> Path | None:
    hits = sorted(
        list(dir_path.glob("*_dual_branch_copytrade_sweep.json"))
        + list(dir_path.glob("*_dual_branch_amount_focus_sweep.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def _champion_card(title: str, c: dict | None, badge: str, *, badge_class: str = "") -> str:
    if not c:
        return (
            f'<article class="card"><h3>{_esc(title)}</h3>'
            f'<p class="muted">—</p></article>'
        )
    bcls = f"badge {badge_class}".strip()
    thr = c.get("threshold_label") or "—"
    return f"""
    <article class="card">
      <div class="card-head">
        <h3>{_esc(title)}</h3>
        <span class="{_esc(bcls)}">{_esc(badge)}</span>
      </div>
      <p class="hero-metric">{_esc(c.get('strategy_id'))}</p>
      <dl class="kv">
        <div><dt>模式 / 度量</dt><dd>{_esc(c.get('mode'))} · {_esc(c.get('metric'))}</dd></div>
        <div><dt>門檻 / Top-N</dt><dd>≥{_esc(thr)} · Top-{_esc(c.get('top_n'))}</dd></div>
        <div><dt>期間報酬率</dt><dd>{_pct(c.get('period_return_pct'))}</dd></div>
        <div><dt>實現 α (NTD)</dt><dd>{_num(c.get('recycled_total_alpha_ntd'), 0)}</dd></div>
        <div><dt>槽位</dt><dd>{_esc(c.get('n_slots'))} × 10k</dd></div>
        <div><dt>捕獲率</dt><dd>{_pct(c.get('signal_capture_pct'))}</dd></div>
        <div><dt>α / locked day</dt><dd>{_num(c.get('alpha_per_locked_day'), 2)}</dd></div>
        <div><dt>進場</dt><dd>T+{_esc(int(c.get('entry_lag_days', 0)) + 1)} 開盤 · 持有 {_esc(c.get('hold_trading_days'))} 日</dd></div>
      </dl>
    </article>"""


def _hyp_status(st: str | None) -> str:
    s = (st or "").lower()
    if s == "passed":
        return "ok"
    if s == "rejected":
        return "fail"
    return ""


def _mode_metric_rows(rows: list[dict]) -> str:
    out = []
    for i, c in enumerate(rows, 1):
        out.append(
            "<tr>"
            f"<td class='n'>{i}</td>"
            f"<td class='mono'>{_esc(c.get('mode'))}/{_esc(c.get('metric'))}</td>"
            f"<td class='mono'>{_esc(c.get('threshold_label'))}</td>"
            f"<td class='mono'>{_esc(c.get('strategy_id'))}</td>"
            f"<td class='n'>{_esc(c.get('top_n'))}</td>"
            f"<td class='n'>{_esc(c.get('n_slots'))}</td>"
            f"<td class='n strong'>{_pct(c.get('period_return_pct'))}</td>"
            f"<td class='n'>{_num(c.get('recycled_total_alpha_ntd'), 0)}</td>"
            f"<td class='n'>{_pct(c.get('signal_capture_pct'))}</td>"
            f"<td class='n'>{_num(c.get('alpha_per_locked_day'), 2)}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _ledger_panel(key: str, label: str, ledger: dict | None, *, active: bool) -> str:
    if not ledger:
        return (
            f'<div class="panel {"active" if active else ""}" data-panel="{_esc(key)}">'
            f'<p class="muted">無交易明細</p></div>'
        )
    meta = ledger.get("meta") or {}
    trades = ledger.get("trades") or []
    return f"""
    <div class="panel {"active" if active else ""}" data-panel="{_esc(key)}">
      <p class="section-lead">
        {_esc(label)} · legs={_esc(meta.get('n_trade_legs'))}
        · cycles={_esc(meta.get('n_cycles'))}
        · win%={_pct(meta.get('leg_win_rate_pct'))}
        · Σpnl={_num(meta.get('sum_pnl_ntd'), 0)}
      </p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th><th>訊號日</th><th>代碼</th><th>名稱</th><th>淨買超張</th>
              <th>進場</th><th>進價</th><th>出場</th><th>出價</th>
              <th>報酬%</th><th>PnL</th><th>配置</th><th>台指%</th><th>籃α</th>
            </tr>
          </thead>
          <tbody>
            {_trade_rows(trades)}
          </tbody>
        </table>
      </div>
    </div>"""


def render_html(
    payload: dict,
    *,
    source_name: str,
    champion_ledgers: dict[str, dict] | None = None,
) -> str:
    branch = str(payload.get("branch_label") or "雙分點")
    champs = payload.get("champions") or {}
    hyp = payload.get("hypotheses") or {}
    h1 = hyp.get("H1_amount_beats_lots") or {}
    h2 = hyp.get("H2_and_or_sum_beats_single_apld") or {}
    h3 = hyp.get("H3_or_dilution") or {}
    haf = hyp.get("H_AMOUNT_FOCUS") or {}
    by_ret = hyp.get("best_per_mode_metric_by_return") or []
    by_apld = hyp.get("best_per_mode_metric_by_apld") or []
    main = champs.get("by_period_return") or {}
    alpha_ch = champs.get("by_alpha") or {}
    rec = hyp.get("recommended_amount_sleeve") or {}

    ledgers = champion_ledgers or {}
    ledger_tabs = []
    ledger_panels = []
    for i, (key, label) in enumerate(CHAMPION_KEYS):
        active = i == 0
        ledger_tabs.append(
            f'<button type="button" class="{"active" if active else ""}" '
            f'data-panel="{_esc(key)}">{_esc(label)}</button>'
        )
        ledger_panels.append(
            _ledger_panel(key, label, ledgers.get(key), active=active)
        )

    follow = main or {}
    follow_bits = (
        f"{follow.get('mode')}/{follow.get('metric')} ≥{follow.get('threshold_label')} "
        f"Top-{follow.get('top_n')} · {follow.get('strategy_id')} · "
        f"{follow.get('n_slots')} 槽"
        if follow
        else "—"
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(branch)} Copytrade Sweep</title>
<style>
:root {{
  --bg0: #0c1118; --bg1: #141c27; --bg2: #1c2736; --line: #2a3a4f;
  --text: #e8eef6; --muted: #8fa3bb; --gold: #e2b657; --teal: #3ecfb2;
  --rose: #e07a7a; --ok: #6bcf8e;
  --font-d: "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  --font-b: "IBM Plex Sans", "Segoe UI", "PingFang TC", "Noto Sans TC", sans-serif;
  --font-m: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; color: var(--text); font-family: var(--font-b);
  background:
    radial-gradient(1200px 600px at 10% -10%, #1e3348 0%, transparent 55%),
    radial-gradient(900px 500px at 100% 0%, #2a2416 0%, transparent 50%),
    linear-gradient(180deg, var(--bg0), #0a0e14 120%);
  min-height: 100vh; line-height: 1.5;
}}
.wrap {{ max-width: 1180px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }}
header.hero {{ border-bottom: 1px solid var(--line); padding-bottom: 1.5rem; margin-bottom: 2rem; }}
.kicker {{ font-family: var(--font-m); font-size: 0.75rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--gold); margin: 0 0 0.6rem; }}
h1 {{ font-family: var(--font-d); font-size: clamp(1.6rem, 3vw, 2.3rem); margin: 0 0 0.6rem; font-weight: 600; }}
.sub {{ color: var(--muted); max-width: 48rem; margin: 0; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 0.75rem 1.25rem; margin-top: 1.1rem; font-size: 0.85rem; color: var(--muted); }}
.meta b {{ color: var(--text); font-weight: 600; }}
section {{ margin: 2.2rem 0; }}
h2 {{ font-family: var(--font-d); font-size: 1.35rem; margin: 0 0 0.4rem; }}
.section-lead {{ color: var(--muted); margin: 0 0 1rem; font-size: 0.92rem; }}
.grid3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.9rem; }}
@media (max-width: 900px) {{ .grid3 {{ grid-template-columns: 1fr; }} }}
.card {{ background: var(--bg1); border: 1px solid var(--line); border-radius: 12px; padding: 1rem 1.1rem; }}
.card-head {{ display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }}
.card h3 {{ margin: 0; font-size: 0.95rem; }}
.badge {{ font-family: var(--font-m); font-size: 0.65rem; letter-spacing: 0.06em; padding: 0.2rem 0.45rem; border-radius: 999px; background: var(--gold); color: var(--bg0); }}
.badge.alt {{ background: var(--bg2); color: var(--teal); border: 1px solid var(--line); }}
.hero-metric {{ font-family: var(--font-d); font-size: 1.6rem; margin: 0.55rem 0 0.7rem; color: var(--gold); }}
.kv {{ display: grid; gap: 0.35rem; margin: 0; }}
.kv div {{ display: flex; justify-content: space-between; gap: 0.75rem; border-top: 1px solid var(--line); padding-top: 0.3rem; }}
.kv dt {{ color: var(--muted); font-size: 0.8rem; }}
.kv dd {{ margin: 0; font-family: var(--font-m); font-size: 0.85rem; }}
.verdict {{ padding: 0.85rem 1rem; border-radius: 10px; border: 1px solid var(--line); background: var(--bg1); margin-bottom: 0.8rem; }}
.verdict.ok {{ border-color: #2f6b4a; }}
.verdict.fail {{ border-color: #6b3a3a; }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
th, td {{ padding: 0.45rem 0.55rem; border-bottom: 1px solid var(--line); text-align: left; }}
th {{ color: var(--muted); font-weight: 500; background: var(--bg2); }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
td.mono {{ font-family: var(--font-m); }}
td.strong {{ color: var(--gold); font-weight: 600; }}
.muted {{ color: var(--muted); }}
.tabs {{ display: flex; gap: 0.4rem; flex-wrap: wrap; margin-bottom: 0.8rem; }}
.tabs button {{
  font-family: var(--font-m); font-size: 0.75rem; background: var(--bg2); color: var(--muted);
  border: 1px solid var(--line); border-radius: 999px; padding: 0.35rem 0.75rem; cursor: pointer;
}}
.tabs button.active {{ background: var(--gold); color: var(--bg0); border-color: var(--gold); }}
.panel {{ display: none; }}
.panel.active {{ display: block; }}
ol.steps {{ margin: 0; padding-left: 1.2rem; color: var(--text); }}
ol.steps li {{ margin: 0.35rem 0; }}
.note {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--line); font-size: 0.8rem; color: var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="kicker">Research · Dual-branch Copytrade</p>
    <h1>松江 × 新店 · 合併 Sweep</h1>
    <p class="sub">
      張數 vs 金額門檻；訊號模式 sj / xd / and / or / sum。
      金額代理 = 淨買超股數 × 訊號日收盤價（PIT）。固定槽 · 槽滿跳過。
    </p>
    <div class="meta">
      <span>窗口 <b>{_esc(payload.get('window_start'))} → {_esc(payload.get('window_end'))}</b></span>
      <span>分點 <b>9801 · 9661</b></span>
      <span>cells <b>{_esc(payload.get('n_cells'))}</b></span>
      <span>來源 <b>{_esc(source_name)}</b></span>
      <span>成本 <b>0 bps</b></span>
    </div>
  </header>

  <section>
    <h2>假設檢定</h2>
    {f'''
    <div class="verdict {_hyp_status(haf.get('status'))}">
      <strong>H-AMOUNT-FOCUS 密度對照：{_esc(haf.get('status'))}</strong>
      · fair ret 勝 {_esc(haf.get('fair_ret_wins'))}
      · fair α/日 勝 {_esc(haf.get('fair_apld_wins'))}
      · 勝全域張數冠軍 ret={_esc(haf.get('beat_lots_global_ret'))}
        apld={_esc(haf.get('beat_lots_global_apld'))}
    </div>
    <div class="verdict ok">
      <strong>建議金額袖</strong>
      · {_esc(rec.get('mode'))}/{_esc(rec.get('metric'))}
        ≥{_esc(rec.get('threshold_label'))}
        {_esc(rec.get('strategy_id'))}
        Top-{_esc(rec.get('top_n'))}
        · {_esc(rec.get('n_slots'))} 槽
        · ret {_pct(rec.get('period_return_pct'))}
        · α/日 {_num(rec.get('alpha_per_locked_day'), 2)}
    </div>
    ''' if haf else f'''
    <div class="verdict {_hyp_status(h1.get('status'))}">
      <strong>H1 金額 vs 張數：{_esc(h1.get('status'))}</strong>
      · ret 勝 {_esc(h1.get('amount_ret_mode_wins'))}/5
      · α/日 勝 {_esc(h1.get('amount_apld_mode_wins'))}/5
    </div>
    <div class="verdict {_hyp_status(h2.get('status'))}">
      <strong>H2 and/sum α/日 vs 最佳單一分點：{_esc(h2.get('status'))}</strong>
      · single α/日 {_num((h2.get('best_single') or {{}}).get('alpha_per_locked_day'), 2)}
      · combo α/日 {_num((h2.get('best_combo') or {{}}).get('alpha_per_locked_day'), 2)}
    </div>
    <div class="verdict {_hyp_status(h3.get('status'))}">
      <strong>H3 OR 稀釋確認：{_esc(h3.get('status'))}</strong>
      · {_esc(h3.get('note'))}
    </div>
    '''}
  </section>

  <section>
    <h2>全域冠軍</h2>
    <p class="section-lead">跨所有 mode×metric 的固定槽冠軍。</p>
    <div class="grid3">
      {_champion_card('主冠軍 · max 期間報酬率', champs.get('by_period_return'), 'PRIMARY')}
      {_champion_card('次優 · max 實現 α', champs.get('by_alpha'), 'ALPHA', badge_class='alt')}
      {_champion_card('次優 · max α/locked day', champs.get('by_alpha_per_locked_day'), 'EFFICIENCY', badge_class='alt')}
    </div>
  </section>

  <section>
    <h2>怎麼跟單</h2>
    <ol class="steps">
      <li>採用主冠軍規格：<b>{_esc(follow_bits)}</b>。</li>
      <li>日常仍建議<strong>兩分點分開觀察</strong>；AND／sum 僅在研究採納後升級為主袖。</li>
      <li>金額門檻用「淨買超股數 × 當日收盤」估算，對齊網站金額視角。</li>
      <li>槽滿跳過；不與元大／富邦單一分點冠軍共用同一筆資金重複加碼。</li>
    </ol>
    <p class="section-lead" style="margin-top:0.8rem">
      α 袖參考：{_esc(alpha_ch.get('mode'))}/{_esc(alpha_ch.get('metric'))}
      {_esc(alpha_ch.get('strategy_id'))} · {_esc(alpha_ch.get('n_slots'))} 槽
      （若要以捕獲率優先）。
    </p>
  </section>

  <section>
    <h2>各 mode×metric 最佳（期間報酬）</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>mode/metric</th><th>門檻</th><th>L×H</th>
            <th>Top</th><th>槽</th><th>報酬%</th><th>α</th><th>捕獲%</th><th>α/日</th>
          </tr>
        </thead>
        <tbody>{_mode_metric_rows(by_ret)}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>各 mode×metric 最佳（α / locked day）</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>mode/metric</th><th>門檻</th><th>L×H</th>
            <th>Top</th><th>槽</th><th>報酬%</th><th>α</th><th>捕獲%</th><th>α/日</th>
          </tr>
        </thead>
        <tbody>{_mode_metric_rows(by_apld)}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>冠軍交易明細</h2>
    <div class="tabs" id="ledger-tabs">
      {"".join(ledger_tabs)}
    </div>
    <div id="ledger-root">
      {"".join(ledger_panels)}
    </div>
  </section>

  <p class="note">
    Research only · not order-layer. Branch tape ≠ single trader identity.
    Amount proxy uses signal-day close × net shares.
  </p>
</div>
<script>
document.querySelectorAll('#ledger-tabs button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('#ledger-tabs button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('#ledger-root .panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const panel = document.querySelector('#ledger-root .panel[data-panel="' + btn.dataset.panel + '"]');
    if (panel) panel.classList.add('active');
  }});
}});
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Render dual-branch copytrade HTML")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--no-trades", action="store_true")
    ap.add_argument("--no-volume-filter", action="store_true")
    args = ap.parse_args()

    src = args.json or _latest_json(OUT_DIR)
    if src is None or not src.exists():
        print(f"ERROR: no sweep JSON under {OUT_DIR}", file=sys.stderr)
        return 2
    payload = json.loads(src.read_text(encoding="utf-8"))
    out = args.out or src.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)

    ledgers: dict[str, dict] = {}
    if not args.no_trades:
        min_vol = None if args.no_volume_filter else 200_000.0
        conn = connect(args.db)
        try:
            champs = payload.get("champions") or {}
            for key, _label in CHAMPION_KEYS:
                ch = champs.get(key)
                if not ch:
                    continue
                print(f"replay trades: {key} {ch.get('strategy_id')} …")
                ledgers[key] = load_champion_trade_ledger(
                    conn,
                    ch,
                    window_start=str(payload.get("window_start")),
                    window_end=str(payload.get("window_end")),
                    min_avg_volume_shares=min_vol,
                )
                print(
                    f"  legs={ledgers[key]['meta']['n_trade_legs']} "
                    f"cycles={ledgers[key]['meta']['n_cycles']}"
                )
        finally:
            conn.close()
        trades_path = out.with_name(out.stem + "_trades.json")
        trades_path.write_text(
            json.dumps(ledgers, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {trades_path}")

    html_doc = render_html(
        payload, source_name=src.name, champion_ledgers=ledgers
    )
    out.write_text(html_doc, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
