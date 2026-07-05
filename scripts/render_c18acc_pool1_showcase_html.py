"""Render POOL1 graduation showcase HTML · fresh∪accel + S2 · poll_5m."""

from __future__ import annotations

import html
import json
from collections import Counter
from typing import Any

from render_rrg_universe_html import (
    EXIT_REASON_ZH,
    _enrich_legs_bench,
    _format_exit_reason_zh,
    _format_intraday_time,
    _format_trade_px,
    _load_bench_closes_for_dates,
    _load_rrg_trajectories,
    _xml_escape,
    render_l1h9_slots_timeline_html,
)

SHOWCASE_TOP_N_CASES = 5


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.2f}%"


def _fmt_pp(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.2f} pp"


def _quick_guide_html() -> str:
    return """
<section class="panel showcase-quick-guide">
  <h2>30 秒看懂</h2>
  <ol class="quick-guide-list">
    <li><b>這是什麼</b>：POOL1 候選池擴充的<b>回測展示</b>（模擬成交，非實盤）。</li>
    <li><b>改了什麼</b>：候選池從 S2 的 <code>fresh</code> 擴成 <code>fresh + 加速標的</code>；出場規則相同。</li>
    <li><b>3 槽資金池</b>：最多同時持有 3 檔，每檔 10,000 NTD；槽滿時新訊號略過。</li>
    <li><b>怎麼賣</b>：滿 10 日到期 · 評分換倉 · Weakening 連 2 日且虧 ≥5% 強制出場。</li>
    <li><b>怎麼看圖</b>：拖時間軸或點表格列；藍點=進場、金點=出場；★=POOL1 獨有交易。</li>
  </ol>
</section>
"""


def _exit_reason_legend_html(legs: list[dict]) -> str:
    counts = Counter(lg.get("exit_reason") or "?" for lg in legs)
    items = []
    for code, label in EXIT_REASON_ZH.items():
        n = counts.get(code, 0)
        if n:
            items.append(f"<span class='exit-legend-item'><b>{html.escape(label)}</b> {n} 筆</span>")
    body = " · ".join(items) if items else "—"
    return f"""
<div class="exit-reason-legend panel">
  <div class="exit-legend-title">出場原因圖例（全窗口 {len(legs)} 筆）</div>
  <div class="exit-legend-body">{body}</div>
  <div class="exit-legend-note note">出場條件於前一日日切判定；成交價多為出場日 09:30，無 1 分 K 時取收盤價。</div>
</div>
"""


def _hero_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    p1 = bundle.get("pool1_meta") or {}
    s2_ex = _fmt_pct(cmp_.get("s2_mean_excess_pct"))
    p1_ex = _fmt_pct(cmp_.get("pool1_mean_excess_pct"))
    delta_pp = _fmt_pp(cmp_.get("delta_excess_pp"))
    return f"""
<section class="showcase-hero panel">
  <h1>POOL1 候選池擴充 · 回測展示</h1>
  <p class="showcase-conclusion">
    <b>結論</b>：窗口 <b>{bundle.get('date_start')} → {bundle.get('date_end')}</b>
    （{bundle.get('n_trade_dates')} 交易日），候選池擴充後平均超額
    <b>{p1_ex}</b>（S2 {s2_ex}，<b>{delta_pp}</b>），
    換倉 {cmp_.get('pool1_swaps')} 次（S2 {cmp_.get('s2_swaps')} 次，多 {cmp_.get('delta_swaps')} 次）。
  </p>
  <p class="note showcase-disclaimer">以下為歷史回測模擬成交紀錄，非實盤下單。</p>
  <p class="sub">
    對照 <b>S2-P5M</b>（僅 fresh）vs <b>POOL1-P5M</b>（fresh ∪ 加速標的）·
    出場規則不變 · 盤中每 5 分掃描進場
  </p>
  <div class="kpi-banner showcase-kpi">
    <div class="kpi-block highlight">
      <div class="label">POOL1 平均超額</div>
      <div class="value">{p1_ex}</div>
      <div class="sub">對照 S2 {s2_ex}</div>
    </div>
    <div class="kpi-block">
      <div class="label">超額差異</div>
      <div class="value">{delta_pp}</div>
    </div>
    <div class="kpi-block highlight">
      <div class="label">換倉次數差異</div>
      <div class="value">{cmp_.get('delta_swaps', '—')}</div>
      <div class="sub">S2 {cmp_.get('s2_swaps')} → POOL1 {cmp_.get('pool1_swaps')}</div>
    </div>
    <div class="kpi-block">
      <div class="label">強制出場</div>
      <div class="value">S2 {cmp_.get('s2_force_exits')} · POOL1 {cmp_.get('pool1_force_exits')}</div>
    </div>
    <div class="kpi-block">
      <div class="label">進場時機</div>
      <div class="value">{p1.get('timing_mode', 'poll_5m')}</div>
      <div class="sub">候選池 fresh ∪ accel</div>
    </div>
  </div>
</section>
"""


def _compare_table_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    rows = [
        ("平均超額", cmp_.get("s2_mean_excess_pct"), cmp_.get("pool1_mean_excess_pct"), cmp_.get("delta_excess_pp")),
        ("換倉次數", cmp_.get("s2_swaps"), cmp_.get("pool1_swaps"), cmp_.get("delta_swaps")),
        ("強制出場", cmp_.get("s2_force_exits"), cmp_.get("pool1_force_exits"), None),
    ]

    def _cell(v: Any, *, pp: bool = False) -> str:
        if v is None:
            return "—"
        if pp:
            return _fmt_pp(v)
        if isinstance(v, (int, float)) and not pp:
            return str(v)
        return html.escape(str(v))

    body = ""
    for label, s2, p1, delta in rows:
        body += (
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{_cell(s2, pp=(label == '平均超額'))}</td>"
            f"<td>{_cell(p1, pp=(label == '平均超額'))}</td>"
            f"<td>{_cell(delta, pp=(label == '平均超額'))}</td></tr>"
        )
    return f"""
<section class="panel">
  <h2>S2 vs POOL1 對照</h2>
  <table class="compare-table">
    <thead><tr><th>指標</th><th>S2 · 僅 fresh</th><th>POOL1 · fresh + 加速</th><th>差異</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</section>
"""


def _top_case_cards(cards: list[dict], *, n: int = SHOWCASE_TOP_N_CASES) -> list[dict]:
    if len(cards) <= n:
        return cards
    ranked = sorted(
        cards,
        key=lambda c: abs(float(c.get("return_pct") or 0)),
        reverse=True,
    )
    return ranked[:n]


def _case_cards_html(bundle: dict[str, Any]) -> str:
    cards = bundle.get("case_cards") or []
    if not cards:
        return """
<section class="panel">
  <h2>POOL1 獨有交易</h2>
  <p class="note">此窗口內 S2 與 POOL1 交易指紋相同。見下方時間軸。</p>
</section>
"""
    top = _top_case_cards(cards)
    total = len(cards)
    parts = [
        "<section class='panel'>",
        f"<h2>POOL1 獨有交易精選（★ vs S2 · 顯示 {len(top)} / {total} 筆）</h2>",
        "<div class='case-grid'>",
    ]
    for c in top:
        ret = c.get("return_pct")
        ret_s = f"{float(ret):+.2f}%" if ret is not None else "—"
        entry_px = _format_trade_px(c.get("entry_px"))
        exit_px = _format_trade_px(c.get("exit_px"))
        entry_time = _format_intraday_time(c.get("entry_date"), c.get("entry_minute"))
        exit_time = _format_intraday_time(c.get("exit_date"), c.get("exit_minute"), poll_fallback=True)
        exit_zh = _format_exit_reason_zh(c.get("exit_reason"))
        pool_note = str(c.get("pool_tag_note") or "")
        if c.get("pool_tag") == "fresh":
            pool_note = "RRG 新進 mono 標的"
        elif c.get("pool_tag") == "accel-only":
            pool_note = "加速標的（mono_tier2 · 4 日加速 · 非 fresh）"
        parts.append(
            f"""<article class="case-card">
  <div class="case-head"><span class="star">★</span>
    <b>{html.escape(str(c.get('stock_id')))}</b>
    {_xml_escape(str(c.get('stock_name') or ''))}</div>
  <div class="case-meta">
    <span class="pill {'pill-fresh' if c.get('pool_tag') == 'fresh' else 'pill-accel'}">{html.escape(str(c.get('pool_tag')))}</span>
    {html.escape(pool_note)}
  </div>
  <ul>
    <li>進場 <b>{c.get('entry_date')}</b> @ <b>{entry_px}</b>（{entry_time}）</li>
    <li>出場 <b>{c.get('exit_date')}</b> @ <b>{exit_px}</b>（{exit_time}）</li>
    <li>出場原因 <b>{html.escape(exit_zh)}</b></li>
    <li>單筆報酬 <b>{ret_s}</b></li>
  </ul>
</article>"""
        )
    parts.append("</div></section>")
    return "\n".join(parts)


def _diff_table_html(bundle: dict[str, Any]) -> str:
    rows: list[str] = []
    for lg in bundle.get("pool1_legs") or []:
        tag = lg.get("showcase_tag", "shared")
        if tag != "pool1_only":
            continue
        ret = lg.get("return_pct")
        ret_s = f"{float(ret):+.2f}%" if ret is not None else "—"
        rows.append(
            f"<tr class='hi-row pool1-only-row'>"
            f"<td>★</td><td>{lg.get('stock_id')}</td>"
            f"<td>{_xml_escape(str(lg.get('stock_name') or ''))}</td>"
            f"<td>{lg.get('pool_tag', '—')}</td>"
            f"<td>{_format_trade_px(lg.get('entry_px'))}</td>"
            f"<td>{_format_trade_px(lg.get('exit_px'))}</td>"
            f"<td>{_format_intraday_time(lg.get('entry_date'), lg.get('entry_minute'))}</td>"
            f"<td>{_format_intraday_time(lg.get('exit_date'), lg.get('exit_minute'), poll_fallback=True)}</td>"
            f"<td>{html.escape(_format_exit_reason_zh(lg.get('exit_reason')))}</td>"
            f"<td>{ret_s}</td></tr>"
        )
    if not rows:
        return ""
    n = len(rows)
    return f"""
<section class="panel">
  <details class="showcase-collapsed">
    <summary>獨有交易清單（{n} 筆 · 點開展開）</summary>
    <table>
      <thead><tr>
        <th></th><th>代號</th><th>名稱</th><th>候選來源</th>
        <th>入場價</th><th>出場價</th><th>入場時間</th><th>出場時間</th>
        <th>出場原因</th><th>單筆報酬</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </details>
</section>
"""


SHOWCASE_CSS = """
.showcase-hero h1 { font-size:20px; margin:0 0 8px; }
.showcase-conclusion { font-size:14px; color:#ccc; line-height:1.55; margin:0 0 8px; }
.showcase-disclaimer { margin:0 0 10px; }
.showcase-kpi { margin-top:12px; }
.showcase-quick-guide h2 { font-size:15px; margin:0 0 8px; color:#ddd; }
.quick-guide-list { margin:0; padding-left:20px; color:#aaa; font-size:13px; line-height:1.6; }
.exit-reason-legend { margin-bottom:12px; padding:10px 14px; }
.exit-legend-title { font-size:13px; color:#ccc; font-weight:600; margin-bottom:6px; }
.exit-legend-body { font-size:12px; color:#999; line-height:1.55; }
.exit-legend-item b { color:#bbb; }
.compare-table { width:100%; border-collapse:collapse; font-size:13px; }
.compare-table th, .compare-table td { padding:8px 10px; border-bottom:1px solid #2a2a2a; text-align:left; }
.case-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
.case-card { background:#1a1a1a; border:1px solid #3a4a3a; border-radius:8px; padding:12px 14px; font-size:13px; }
.case-head { font-size:15px; margin-bottom:8px; }
.case-head .star { color:#7ec8a0; margin-right:6px; }
.case-meta { color:#999; margin-bottom:8px; line-height:1.45; }
.pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-right:6px; }
.pill-fresh { background:#2a3a4a; color:#8ab4d4; }
.pill-accel { background:#1f3a2a; color:#7ec8a0; border:1px solid #3a5a4a; }
.case-card ul { margin:0; padding-left:18px; color:#bbb; }
.showcase-section-title { font-size:15px; color:#ddd; margin:24px 0 8px; }
.showcase-collapsed summary { cursor:pointer; color:#ccc; font-weight:600; font-size:14px; }
.showcase-collapsed table { margin-top:10px; }
tr.pool1-only-row { background:#152018; }
"""


def render_pool1_showcase_html(
    *,
    bundle: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int = 20,
) -> str:
    """Compose hero + diff sections + POOL1 interactive RRG timeline."""
    legs = bundle["pool1_legs"]
    executed = []
    for lg in legs:
        executed.append(
            {
                "signal_date": lg.get("signal_date") or lg["entry_date"],
                "entry_date": lg["entry_date"],
                "exit_date": lg["exit_date"],
                "slot_id": lg.get("slot_id", 0),
                "n_legs": 1,
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name", ""),
                "seg_last": lg.get("seg_last"),
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "deployed_ntd": lg.get("allocated_ntd"),
                "pnl_ntd": lg.get("pnl_ntd"),
                "return_pct": lg.get("return_pct"),
                "bench_return_pct": lg.get("bench_return_pct"),
                "alpha_ntd": None,
                "exit_reason": lg.get("exit_reason"),
                "pool_tag": lg.get("pool_tag"),
                "showcase_tag": lg.get("showcase_tag"),
            }
        )

    meta = dict(bundle["pool1_meta"])
    meta["showcase"] = {
        "pool1_only_count": len(bundle.get("pool1_only_legs") or []),
        "comparison": bundle.get("comparison"),
    }
    meta["showcase_mode"] = True
    meta["show_exit_reason"] = True
    meta["read_guide_open"] = True
    meta["signals_table_prefix"] = _exit_reason_legend_html(legs)

    timeline_html = render_l1h9_slots_timeline_html(
        etf_code=meta.get("display_code", "POOL1-P5M"),
        dates=dates,
        legs=legs,
        executed_signals=executed,
        skipped_signals=[],
        all_trajectories=all_trajectories,
        meta=meta,
        bench_closes=bench_closes,
        length=length,
    )

    prefix = (
        _quick_guide_html()
        + _hero_html(bundle)
        + _compare_table_html(bundle)
        + _case_cards_html(bundle)
        + _diff_table_html(bundle)
        + '<p class="showcase-section-title">▼ POOL1 互動 RRG 時間軸（主圖）</p>'
    )

    pool1_only_ids = sorted({lg["stock_id"] for lg in bundle.get("pool1_only_legs") or []})
    inject_js = f"""
    const POOL1_ONLY_STOCK_IDS = new Set({json.dumps(pool1_only_ids)});
    const SHOWCASE = {json.dumps(bundle.get('comparison') or {}, ensure_ascii=False)};
"""

    html_out = timeline_html
    if "<style>" in html_out:
        html_out = html_out.replace("<style>", f"<style>{SHOWCASE_CSS}", 1)
    insert_at = html_out.find('<div class="wrap">')
    if insert_at >= 0:
        html_out = html_out[:insert_at] + prefix + html_out[insert_at:]
    marker = "renderFrame(0);\n  </script>"
    if marker in html_out:
        html_out = html_out.replace(marker, inject_js + "    " + marker, 1)
    return html_out


def load_showcase_timeline_inputs(
    conn,
    bundle: dict[str, Any],
    dates: list[str],
    *,
    etf_codes: tuple[str, ...],
    length: int = 20,
) -> tuple[list[dict], list[float | None]]:
    legs = bundle["pool1_legs"]
    _enrich_legs_bench(conn, legs, entry_price_mode="close")
    bench_closes = _load_bench_closes_for_dates(conn, dates)
    stock_ids = {lg["stock_id"] for lg in legs}
    all_trajectories = _load_rrg_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        length=length,
        with_close=True,
    )
    all_trajectories = [t for t in all_trajectories if t["stock_id"] in stock_ids]
    return all_trajectories, bench_closes
