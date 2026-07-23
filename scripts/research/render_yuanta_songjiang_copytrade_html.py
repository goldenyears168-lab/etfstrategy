#!/usr/bin/env python3
"""Render 元大-松江 Copytrade sweep JSON → self-contained HTML report.

  PYTHONPATH=src .venv/bin/python scripts/research/render_yuanta_songjiang_copytrade_html.py
  PYTHONPATH=src .venv/bin/python scripts/research/render_yuanta_songjiang_copytrade_html.py \\
      --json reports/research/yuanta-songjiang-copytrade/20260717_yuanta_songjiang_copytrade_sweep.json
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

from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.yuanta_songjiang_copytrade_sweep import (  # noqa: E402
    load_champion_trade_ledger,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "yuanta-songjiang-copytrade"

CHAMPION_KEYS = (
    ("by_period_return", "主冠軍 · 期間報酬"),
    ("by_alpha", "次優 · 實現 α"),
    ("by_alpha_per_locked_day", "次優 · α/日"),
)


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


def _latest_json(dir_path: Path) -> Path | None:
    hits = sorted(
        dir_path.glob("*_yuanta_songjiang_copytrade_sweep.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def _fixed_cells(payload: dict) -> list[dict]:
    return [c for c in (payload.get("cells") or []) if c.get("slots_mode") == "fixed"]


def _top_cells(cells: list[dict], key: str, n: int = 20) -> list[dict]:
    return sorted(
        cells,
        key=lambda c: (
            float(c[key]) if c.get(key) is not None else -1e18
        ),
        reverse=True,
    )[:n]


def _heatmap_matrix(
    cells: list[dict],
    *,
    entry_lag: int,
    min_net_lots: float,
    top_n: int,
    metric: str = "period_return_pct",
) -> dict[str, Any]:
    """H (rows) × slots (cols) for one (L, net, top_n)."""
    subset = [
        c
        for c in cells
        if int(c.get("entry_lag_days", -1)) == entry_lag
        and float(c.get("min_net_lots") or 0) == float(min_net_lots)
        and int(c.get("top_n") or 0) == top_n
    ]
    holds = sorted({int(c["hold_trading_days"]) for c in subset})
    slots = sorted({int(c["n_slots"]) for c in subset})
    by: dict[tuple[int, int], float | None] = {}
    for c in subset:
        by[(int(c["hold_trading_days"]), int(c["n_slots"]))] = c.get(metric)
    grid: list[list[float | None]] = []
    for h in holds:
        grid.append([by.get((h, s)) for s in slots])
    return {
        "holds": holds,
        "slots": slots,
        "grid": grid,
        "metric": metric,
        "entry_lag": entry_lag,
        "min_net_lots": min_net_lots,
        "top_n": top_n,
    }


def _champion_card(title: str, c: dict | None, badge: str, *, badge_class: str = "") -> str:
    if not c:
        return f'<article class="card"><h3>{_esc(title)}</h3><p class="muted">—</p></article>'
    bcls = f"badge {badge_class}".strip()
    return f"""
    <article class="card">
      <div class="card-head">
        <h3>{_esc(title)}</h3>
        <span class="{_esc(bcls)}">{_esc(badge)}</span>
      </div>
      <p class="hero-metric">{_esc(c.get('strategy_id'))}</p>
      <dl class="kv">
        <div><dt>期間報酬率</dt><dd>{_pct(c.get('period_return_pct'))}</dd></div>
        <div><dt>實現 α (NTD)</dt><dd>{_num(c.get('recycled_total_alpha_ntd'), 0)}</dd></div>
        <div><dt>門檻 / Top-N</dt><dd>{_num(c.get('min_net_lots'), 0)} 張 · Top-{_esc(c.get('top_n'))}</dd></div>
        <div><dt>槽位</dt><dd>{_esc(c.get('n_slots'))} × 10k = {_num(c.get('total_capital_ntd'), 0)}</dd></div>
        <div><dt>捕獲率</dt><dd>{_pct(c.get('signal_capture_pct'))}</dd></div>
        <div><dt>成交筆 / 鎖倉日</dt><dd>{_esc(c.get('recycled_n_cycles'))} · {_esc(c.get('recycled_locked_days'))}</dd></div>
        <div><dt>α / locked day</dt><dd>{_num(c.get('alpha_per_locked_day'), 2)}</dd></div>
        <div><dt>進場</dt><dd>T+{_esc(int(c.get('entry_lag_days', 0)) + 1)} 開盤 · 持有 {_esc(c.get('hold_trading_days'))} 日收盤賣</dd></div>
      </dl>
    </article>"""


def _table_rows(cells: list[dict]) -> str:
    rows = []
    for i, c in enumerate(cells, 1):
        rows.append(
            "<tr>"
            f"<td class='n'>{i}</td>"
            f"<td class='mono'>{_esc(c.get('strategy_id'))}</td>"
            f"<td class='n'>{_num(c.get('min_net_lots'), 0)}</td>"
            f"<td class='n'>{_esc(c.get('top_n'))}</td>"
            f"<td class='n'>{_esc(c.get('n_slots'))}</td>"
            f"<td class='n strong'>{_pct(c.get('period_return_pct'))}</td>"
            f"<td class='n'>{_num(c.get('recycled_total_alpha_ntd'), 0)}</td>"
            f"<td class='n'>{_pct(c.get('signal_capture_pct'))}</td>"
            f"<td class='n'>{_esc(c.get('recycled_n_cycles'))}</td>"
            f"<td class='n'>{_num(c.get('alpha_per_locked_day'), 2)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _ret_class(ret: Any) -> str:
    try:
        v = float(ret)
    except (TypeError, ValueError):
        return ""
    if v > 0:
        return "pos"
    if v < 0:
        return "neg"
    return ""


def _trade_rows(trades: list[dict]) -> str:
    rows = []
    for t in trades:
        rc = _ret_class(t.get("return_pct"))
        rows.append(
            "<tr>"
            f"<td class='n'>{_esc(t.get('cycle'))}</td>"
            f"<td class='mono'>{_esc(t.get('signal_date'))}</td>"
            f"<td class='mono'>{_esc(t.get('stock_id'))}</td>"
            f"<td>{_esc(t.get('stock_name'))}</td>"
            f"<td class='n'>{_num(t.get('net_lots'), 1)}</td>"
            f"<td class='mono'>{_esc(t.get('entry_date'))}</td>"
            f"<td class='n'>{_num(t.get('entry_px'), 2)}</td>"
            f"<td class='mono'>{_esc(t.get('exit_date'))}</td>"
            f"<td class='n'>{_num(t.get('exit_px'), 2)}</td>"
            f"<td class='n {rc}'>{_pct(t.get('return_pct'))}</td>"
            f"<td class='n {rc}'>{_num(t.get('pnl_ntd'), 0)}</td>"
            f"<td class='n'>{_num(t.get('allocated_ntd'), 0)}</td>"
            f"<td class='n'>{_pct(t.get('bench_return_pct'))}</td>"
            f"<td class='n'>{_num(t.get('basket_alpha_ntd'), 0)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _ledger_panel(key: str, label: str, ledger: dict | None, *, active: bool) -> str:
    if not ledger:
        return (
            f'<div class="panel {"active" if active else ""}" data-panel="{_esc(key)}">'
            f"<p class='muted'>無交易明細（缺資料或無法重播）</p></div>"
        )
    ch = ledger.get("champion") or {}
    meta = ledger.get("meta") or {}
    trades = ledger.get("trades") or []
    return f"""
    <div class="panel {'active' if active else ''}" data-panel="{_esc(key)}">
      <div class="ledger-meta">
        <span><b>{_esc(ch.get('strategy_id'))}</b></span>
        <span>門檻 {_num(ch.get('min_net_lots'), 0)} 張 · Top-{_esc(ch.get('top_n'))}</span>
        <span>槽 {_esc(ch.get('n_slots'))}</span>
        <span>成交腿 {_esc(meta.get('n_trade_legs'))}</span>
        <span>週期 {_esc(meta.get('n_cycles'))}（跳過 {_esc(meta.get('n_skipped'))}）</span>
        <span>腿勝率 {_pct(meta.get('leg_win_rate_pct'))}</span>
        <span>合計 PnL {_num(meta.get('sum_pnl_ntd'), 0)} NTD</span>
      </div>
      <p class="section-lead" style="margin-top:0.4rem">
        進場價＝進場日開盤；賣出價＝出場日收盤。報酬率為單腿淨報酬（成本 0 bps）。
      </p>
      <div class="scroll tall">
        <table>
          <thead>
            <tr>
              <th class="n">#</th>
              <th>訊號日</th>
              <th>代號</th>
              <th>名稱</th>
              <th class="n">買超張</th>
              <th>買進日</th>
              <th class="n">買進價</th>
              <th>賣出日</th>
              <th class="n">賣出價</th>
              <th class="n">報酬率</th>
              <th class="n">PnL</th>
              <th class="n">配置</th>
              <th class="n">台指%</th>
              <th class="n">籃 α</th>
            </tr>
          </thead>
          <tbody>
            {_trade_rows(trades)}
          </tbody>
        </table>
      </div>
    </div>"""


def render_html(
    payload: dict[str, Any],
    *,
    source_name: str,
    champion_ledgers: dict[str, dict] | None = None,
) -> str:
    branch = str(payload.get("branch_label") or "元大-松江")
    champs = payload.get("champions") or {}
    phase_b = payload.get("phase_b") or {}
    fixed = _fixed_cells(payload)
    top_ret = _top_cells(fixed, "period_return_pct", 25)
    top_alpha = _top_cells(fixed, "recycled_total_alpha_ntd", 15)
    main = champs.get("by_period_return") or {}
    alpha_ch = champs.get("by_alpha") or {}
    heatmaps = [
        _heatmap_matrix(fixed, entry_lag=0, min_net_lots=50.0, top_n=1),
        _heatmap_matrix(fixed, entry_lag=2, min_net_lots=50.0, top_n=1),
        _heatmap_matrix(
            fixed,
            entry_lag=0,
            min_net_lots=50.0,
            top_n=1,
            metric="recycled_total_alpha_ntd",
        ),
    ]

    swap_ok = bool(phase_b.get("swap_beats_skip"))
    delta = phase_b.get("delta") or {}
    skip = phase_b.get("skip_when_full") or {}
    swap = phase_b.get("swap_when_full") or {}

    follow_l = int(main.get("entry_lag_days", 0)) + 1 if main else 1
    follow_h = main.get("hold_trading_days", "—") if main else "—"
    follow_net = main.get("min_net_lots", "—") if main else "—"
    follow_top = main.get("top_n", "—") if main else "—"
    follow_slots = main.get("n_slots", "—") if main else "—"
    alpha_hint = (
        f"{alpha_ch.get('strategy_id')} · {alpha_ch.get('n_slots')} 槽"
        if alpha_ch
        else "α 冠軍"
    )
    swap_follow = (
        "優先 swap（Phase B 勝出）" if swap_ok else "跳過新訊號（不要 swap）"
    )

    heat_json = json.dumps(heatmaps, ensure_ascii=False)
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
    ledger_tabs_html = "\n".join(ledger_tabs)
    ledger_panels_html = "\n".join(ledger_panels)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(branch)} Copytrade Sweep</title>
<style>
:root {{
  --bg0: #0c1118;
  --bg1: #141c27;
  --bg2: #1c2736;
  --line: #2a3a4f;
  --text: #e8eef6;
  --muted: #8fa3bb;
  --gold: #e2b657;
  --teal: #3ecfb2;
  --rose: #e07a7a;
  --ok: #6bcf8e;
  --font-d: "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif;
  --font-b: "IBM Plex Sans", "Segoe UI", "PingFang TC", "Noto Sans TC", sans-serif;
  --font-m: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: var(--text);
  font-family: var(--font-b);
  background:
    radial-gradient(1200px 600px at 10% -10%, #1e3348 0%, transparent 55%),
    radial-gradient(900px 500px at 100% 0%, #2a2416 0%, transparent 50%),
    linear-gradient(180deg, var(--bg0), #0a0e14 120%);
  min-height: 100vh;
  line-height: 1.5;
}}
.wrap {{ max-width: 1180px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }}
header.hero {{
  border-bottom: 1px solid var(--line);
  padding-bottom: 1.5rem;
  margin-bottom: 2rem;
}}
.kicker {{
  font-family: var(--font-m);
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--gold);
  margin: 0 0 0.6rem;
}}
h1 {{
  font-family: var(--font-d);
  font-weight: 600;
  font-size: clamp(1.8rem, 4vw, 2.6rem);
  margin: 0 0 0.5rem;
  letter-spacing: -0.02em;
}}
.sub {{ color: var(--muted); max-width: 46rem; margin: 0; }}
.meta {{
  display: flex; flex-wrap: wrap; gap: 0.6rem 1.2rem;
  margin-top: 1.1rem; font-family: var(--font-m); font-size: 0.8rem; color: var(--muted);
}}
.meta b {{ color: var(--text); font-weight: 500; }}
section {{ margin: 2.4rem 0; }}
section > h2 {{
  font-family: var(--font-d);
  font-size: 1.45rem;
  margin: 0 0 0.35rem;
  border-left: 3px solid var(--gold);
  padding-left: 0.7rem;
}}
.section-lead {{ color: var(--muted); margin: 0 0 1.1rem; max-width: 40rem; }}
.grid3 {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
}}
@media (max-width: 900px) {{ .grid3 {{ grid-template-columns: 1fr; }} }}
.card {{
  background: linear-gradient(165deg, var(--bg2), var(--bg1));
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 1.1rem 1.15rem 1.2rem;
}}
.card-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem; }}
.card h3 {{ margin: 0; font-size: 0.95rem; color: var(--muted); font-weight: 500; }}
.badge {{
  font-family: var(--font-m); font-size: 0.68rem; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--bg0); background: var(--gold);
  padding: 0.15rem 0.45rem; border-radius: 999px;
}}
.badge.alt {{ background: var(--teal); }}
.badge.warn {{ background: var(--rose); color: #1a1010; }}
.hero-metric {{
  font-family: var(--font-d); font-size: 2rem; margin: 0.55rem 0 0.9rem; color: var(--gold);
}}
.kv {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.55rem 0.8rem; margin: 0; }}
.kv div {{ margin: 0; }}
.kv dt {{ font-size: 0.72rem; color: var(--muted); margin: 0; }}
.kv dd {{ margin: 0.1rem 0 0; font-family: var(--font-m); font-size: 0.92rem; }}
.compare {{
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem;
}}
@media (max-width: 900px) {{ .compare {{ grid-template-columns: 1fr; }} }}
.verdict {{
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 1rem 1.2rem;
  background: var(--bg1);
  margin-bottom: 1rem;
}}
.verdict.fail {{ border-color: #5a3030; }}
.verdict.ok {{ border-color: #2d5a40; }}
.verdict strong {{ color: var(--gold); }}
.steps {{
  counter-reset: step;
  list-style: none; padding: 0; margin: 0;
  display: grid; gap: 0.7rem;
}}
.steps li {{
  counter-increment: step;
  background: var(--bg1);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0.85rem 1rem 0.85rem 3rem;
  position: relative;
}}
.steps li::before {{
  content: counter(step);
  position: absolute; left: 0.85rem; top: 0.85rem;
  width: 1.4rem; height: 1.4rem; border-radius: 50%;
  background: var(--gold); color: var(--bg0);
  font-family: var(--font-m); font-size: 0.75rem;
  display: grid; place-items: center; font-weight: 600;
}}
table {{
  width: 100%; border-collapse: collapse; font-size: 0.86rem;
  background: var(--bg1); border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
}}
th, td {{ padding: 0.45rem 0.55rem; border-bottom: 1px solid var(--line); text-align: left; }}
th {{
  font-family: var(--font-m); font-size: 0.7rem; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--muted); background: var(--bg2);
  position: sticky; top: 0;
}}
td.n, th.n {{ text-align: right; font-family: var(--font-m); }}
td.mono {{ font-family: var(--font-m); }}
td.strong {{ color: var(--gold); font-weight: 600; }}
.scroll {{ overflow: auto; max-height: 420px; border-radius: 10px; }}
.scroll.tall {{ max-height: 560px; }}
td.pos {{ color: var(--ok); }}
td.neg {{ color: var(--rose); }}
.ledger-meta {{
  display: flex; flex-wrap: wrap; gap: 0.5rem 1rem;
  font-family: var(--font-m); font-size: 0.78rem; color: var(--muted);
  margin-bottom: 0.35rem;
}}
.ledger-meta b {{ color: var(--text); font-weight: 500; }}
.heat-block {{ margin-bottom: 1.4rem; }}
.heat-block h3 {{
  font-family: var(--font-m); font-size: 0.85rem; color: var(--muted); font-weight: 500; margin: 0 0 0.6rem;
}}
.heat {{
  display: grid;
  gap: 3px;
  font-family: var(--font-m); font-size: 0.68rem;
}}
.heat .lab {{ color: var(--muted); display: grid; place-items: center; }}
.heat .cell {{
  min-height: 2rem; border-radius: 4px; display: grid; place-items: center;
  color: #0c1118; font-weight: 600;
}}
.heat .cell.empty {{ background: var(--bg2); color: var(--muted); font-weight: 400; }}
.note {{
  margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--line);
  font-size: 0.8rem; color: var(--muted);
}}
.tabs {{ display: flex; gap: 0.4rem; flex-wrap: wrap; margin-bottom: 0.8rem; }}
.tabs button {{
  font-family: var(--font-m); font-size: 0.75rem;
  background: var(--bg2); color: var(--muted); border: 1px solid var(--line);
  border-radius: 999px; padding: 0.35rem 0.75rem; cursor: pointer;
}}
.tabs button.active {{ background: var(--gold); color: var(--bg0); border-color: var(--gold); }}
.panel {{ display: none; }}
.panel.active {{ display: block; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="kicker">Research · Copytrade</p>
    <h1>{_esc(branch)}分點跟單 · 參數 Sweep</h1>
    <p class="sub">
      門檻 × Top-N 訊號 → L×H×槽位固定資金模型（槽滿跳過），再對主冠軍做 swap 對照。
      目標鍵：期間總報酬率（recycled PnL / 總本金）；並並列 α 與資金效率次優。
    </p>
    <div class="meta">
      <span>窗口 <b>{_esc(payload.get('window_start'))} → {_esc(payload.get('window_end'))}</b></span>
      <span>分點 <b>{_esc(branch)} · id {_esc(payload.get('trader_id'))}</b></span>
      <span>cells <b>{_esc(payload.get('n_cells'))}</b></span>
      <span>來源 <b>{_esc(source_name)}</b></span>
      <span>成本 <b>0 bps</b>（未扣稅費）</span>
    </div>
  </header>

  <section>
    <h2>冠軍規格</h2>
    <p class="section-lead">主排序：固定槽 · 期間報酬率。次優：實現 α、α／鎖倉日。</p>
    <div class="grid3">
      {_champion_card('主冠軍 · max 期間報酬率', champs.get('by_period_return'), 'PRIMARY')}
      {_champion_card('次優 · max 實現 α', champs.get('by_alpha'), 'ALPHA', badge_class='alt')}
      {_champion_card('次優 · max α/locked day', champs.get('by_alpha_per_locked_day'), 'EFFICIENCY', badge_class='alt')}
    </div>
  </section>

  <section>
    <h2>冠軍交易明細</h2>
    <p class="section-lead">
      依固定槽實際成交重播：訊號日 → 進場日開盤買 → 持有 H 日後收盤賣。可切換三個冠軍。
    </p>
    <div class="tabs" id="ledger-tabs">
      {ledger_tabs_html}
    </div>
    <div id="ledger-root">
      {ledger_panels_html}
    </div>
  </section>

  <section>
    <h2>怎麼跟單（可執行建議）</h2>
    <p class="section-lead">
      以主冠軍為「報酬最大化」規格；實務若要提高捕獲率，優先看 α 冠軍（{_esc(alpha_hint)}）。
      Phase B：{'swap 優於 skip' if swap_ok else 'swap 不優於 skip'}。
    </p>
    <ol class="steps">
      <li>收盤後查{_esc(branch)}淨買超 ≥ <b>{_esc(follow_net)} 張</b>，取 <b>Top-{_esc(follow_top)}</b>（等權）。</li>
      <li><b>T+{_esc(follow_l)}</b> 開盤進場（L{_esc(follow_l)}；實盤基準亦可改 L1）。</li>
      <li>持有 <b>{_esc(follow_h)}</b> 個交易日，第 H 日<strong>收盤</strong>賣出。</li>
      <li>使用 <b>{_esc(follow_slots)}</b> 槽 × 1 萬；槽滿時<strong>{_esc(swap_follow)}</strong>。</li>
    </ol>
  </section>

  <section>
    <h2>Phase B · Swap vs Skip</h2>
    <div class="verdict {'ok' if swap_ok else 'fail'}">
      <strong>結論：{'swap 優於 skip' if swap_ok else 'swap 不優於 skip-when-full'}</strong>
      · Δ期間報酬 {_pct(delta.get('period_return_pct'))}
      · Δα {_num(delta.get('recycled_total_alpha_ntd'), 0)} NTD
      · 換倉次數 {_esc(delta.get('n_swaps'))}
    </div>
    <div class="compare">
      <article class="card">
        <h3>Skip when full（Phase A）</h3>
        <dl class="kv">
          <div><dt>期間報酬率</dt><dd>{_pct(skip.get('period_return_pct'))}</dd></div>
          <div><dt>實現 α</dt><dd>{_num(skip.get('recycled_total_alpha_ntd'), 0)}</dd></div>
          <div><dt>捕獲率</dt><dd>{_pct(skip.get('signal_capture_pct'))}</dd></div>
          <div><dt>成交筆</dt><dd>{_esc(skip.get('recycled_n_cycles'))}</dd></div>
        </dl>
      </article>
      <article class="card">
        <h3>Swap when full</h3>
        <dl class="kv">
          <div><dt>期間報酬率</dt><dd>{_pct(swap.get('period_return_pct'))}</dd></div>
          <div><dt>實現 α</dt><dd>{_num(swap.get('recycled_total_alpha_ntd'), 0)}</dd></div>
          <div><dt>捕獲率</dt><dd>{_pct(swap.get('signal_capture_pct'))}</dd></div>
          <div><dt>進場 / 換倉</dt><dd>{_esc(swap.get('n_entries'))} · {_esc(swap.get('n_swaps'))}</dd></div>
        </dl>
      </article>
      <article class="card">
        <h3>解讀</h3>
        <p class="sub" style="margin:0.4rem 0 0">
          {'Swap 提高周轉後報酬／α 優於 skip，可考慮槽滿置換。'
            if swap_ok else
            'Swap 提高周轉後 Gross／α 未優於 skip，此樣本傾向槽滿跳過、把錢鎖在長 H。'}
        </p>
      </article>
    </div>
  </section>

  <section>
    <h2>熱力圖 · H × 槽位</h2>
    <p class="section-lead">固定門檻 50 張 · Top-1。顏色愈亮 = 指標愈高。</p>
    <div class="tabs" id="heat-tabs">
      <button type="button" class="active" data-i="0">L1 · 期間報酬%</button>
      <button type="button" data-i="1">L3 · 期間報酬%</button>
      <button type="button" data-i="2">L1 · 實現 α</button>
    </div>
    <div id="heat-root"></div>
  </section>

  <section>
    <h2>排行 · 期間報酬率 Top 25</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="n">#</th><th>策略</th><th class="n">門檻張</th><th class="n">TopN</th>
            <th class="n">槽</th><th class="n">報酬%</th><th class="n">α</th>
            <th class="n">捕獲%</th><th class="n">筆數</th><th class="n">α/日</th>
          </tr>
        </thead>
        <tbody>
          {_table_rows(top_ret)}
        </tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>排行 · 實現 α Top 15</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="n">#</th><th>策略</th><th class="n">門檻張</th><th class="n">TopN</th>
            <th class="n">槽</th><th class="n">報酬%</th><th class="n">α</th>
            <th class="n">捕獲%</th><th class="n">筆數</th><th class="n">α/日</th>
          </tr>
        </thead>
        <tbody>
          {_table_rows(top_alpha)}
        </tbody>
      </table>
    </div>
  </section>

  <p class="note">
    分點為券商營業據點帳戶加總，非單一操盤人身份。未建模漲跌停／滑價／稅費。
    Research only · 未寫入 strategy.yaml。Heatmap 資料內嵌於本頁。
  </p>
</div>
<script>
const HEATS = {heat_json};

function colorScale(t, mode) {{
  // t in [0,1]
  t = Math.max(0, Math.min(1, t));
  if (mode === 'alpha') {{
    // deep slate → teal
    const r = Math.round(28 + t * 34);
    const g = Math.round(40 + t * 167);
    const b = Math.round(55 + t * 123);
    return `rgb(${{r}},${{g}},${{b}})`;
  }}
  // return: slate → gold
  const r = Math.round(40 + t * 186);
  const g = Math.round(48 + t * 134);
  const b = Math.round(60 + t * 27);
  return `rgb(${{r}},${{g}},${{b}})`;
}}

function renderHeat(idx) {{
  const hm = HEATS[idx];
  const flat = hm.grid.flat().filter(v => v !== null && v !== undefined);
  const lo = Math.min(...flat);
  const hi = Math.max(...flat);
  const mode = hm.metric.includes('alpha') ? 'alpha' : 'ret';
  const cols = hm.slots.length + 1;
  let html = `<div class="heat-block"><h3>L${{hm.entry_lag + 1}} · net≥${{hm.min_net_lots}}張 · Top-${{hm.top_n}} · ${{hm.metric}}</h3>`;
  html += `<div class="heat" style="grid-template-columns: 48px repeat(${{hm.slots.length}}, minmax(56px,1fr))">`;
  html += `<div class="lab">H\\\\槽</div>`;
  for (const s of hm.slots) html += `<div class="lab">${{s}}</div>`;
  hm.holds.forEach((h, ri) => {{
    html += `<div class="lab">H${{h}}</div>`;
    hm.slots.forEach((s, ci) => {{
      const v = hm.grid[ri][ci];
      if (v === null || v === undefined) {{
        html += `<div class="cell empty">—</div>`;
        return;
      }}
      const t = hi === lo ? 0.5 : (v - lo) / (hi - lo);
      const label = mode === 'alpha' ? Math.round(v).toLocaleString() : (v.toFixed(1) + '%');
      html += `<div class="cell" style="background:${{colorScale(t, mode)}}" title="H${{h}} · ${{s}}槽 = ${{label}}">${{label}}</div>`;
    }});
  }});
  html += `</div></div>`;
  document.getElementById('heat-root').innerHTML = html;
}}

document.querySelectorAll('#heat-tabs button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('#heat-tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderHeat(Number(btn.dataset.i));
  }});
}});
renderHeat(0);

document.querySelectorAll('#ledger-tabs button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('#ledger-tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const key = btn.dataset.panel;
    document.querySelectorAll('#ledger-root .panel').forEach(p => {{
      p.classList.toggle('active', p.dataset.panel === key);
    }});
  }});
}});
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Render 元大-松江 copytrade sweep HTML")
    ap.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Sweep JSON path (default: latest under report dir)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path (default: alongside JSON)",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--no-trades",
        action="store_true",
        help="Skip replaying champion trade ledgers from DB",
    )
    ap.add_argument(
        "--no-volume-filter",
        action="store_true",
        help="Match sweep if volume filter was disabled",
    )
    args = ap.parse_args()

    src = args.json or _latest_json(OUT_DIR)
    if src is None or not src.exists():
        print(
            f"ERROR: no sweep JSON under {OUT_DIR}. Run the sweep first.",
            file=sys.stderr,
        )
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
            etf_proxy = payload.get("etf_code_proxy")
            for key, _label in CHAMPION_KEYS:
                ch = champs.get(key)
                if not ch:
                    continue
                ch_replay = dict(ch)
                if etf_proxy:
                    ch_replay["etf_code_proxy"] = etf_proxy
                print(f"replay trades: {key} {ch_replay.get('strategy_id')} …")
                ledgers[key] = load_champion_trade_ledger(
                    conn,
                    ch_replay,
                    trader_id=str(payload.get("trader_id") or "9801"),
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
