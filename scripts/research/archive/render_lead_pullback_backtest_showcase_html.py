"""強勢回踩 · pool1-style 回測 showcase（交易紀錄 · 累計報酬 · RRG timeline）。"""

from __future__ import annotations

import html
import json
from typing import Any

from render_rrg_universe_html import (
    _enrich_legs_bench,
    _load_bench_closes_for_dates,
    _load_rrg_trajectories,
    _xml_escape,
    render_l1h9_slots_timeline_html,
)


def _fmt_pct(v: Any, *, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.{digits}f}%"


def _fmt_px(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v):.2f}"


SHOWCASE_CSS = """
.showcase-hero h1 { font-size:20px; margin:0 0 8px; color:#1F8A65; }
.showcase-kpi { margin-top:12px; }
.compare-table { width:100%; border-collapse:collapse; font-size:13px; }
.compare-table th, .compare-table td { padding:8px 10px; border-bottom:1px solid #2a2a2a; text-align:left; }
.case-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
.case-card { background:#1a1a1a; border:1px solid #2a4a3a; border-radius:8px; padding:12px 14px; font-size:13px; }
.case-head { font-size:15px; margin-bottom:8px; }
.case-head .star { color:#7ec8a0; margin-right:6px; }
.case-meta { color:#999; margin-bottom:8px; line-height:1.45; }
.pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-right:6px; }
.pill-pullback { background:#1f3a2a; color:#7ec8a0; border:1px solid #3a5a4a; }
.pill-win { background:#1a2a1a; color:#9fd4b8; }
.pill-loss { background:#2a1a1a; color:#d4a0a0; }
.case-card ul { margin:0; padding-left:18px; color:#bbb; }
.showcase-section-title { font-size:15px; color:#ddd; margin:24px 0 8px; }
tr.ledger-row:hover { background:#1a2220; cursor:pointer; }
"""


def _hero_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    meta = bundle.get("lpb_meta") or {}
    return f"""
<section class="showcase-hero panel">
  <h1>強勢回踩 · 預設規格 · 3槽回測</h1>
  <p class="sub">
    <b>lead_pullback_buy</b> · W20 Leading + pullback spread≥2 · fresh K ·
    出場 <b>{bundle.get('exit_spec', {}).get('label', 'hold_5d')}</b> ·
    窗口 <b>{bundle.get('date_start')} → {bundle.get('date_end')}</b>
    （{bundle.get('n_trade_dates')} TD）·
    <span style="color:#7ec8a0">buy-signal-radar observe-only · 非主倉</span>
  </p>
  <div class="kpi-banner showcase-kpi">
    <div class="kpi-block highlight" style="background:#152018;border-color:#2a4a3a">
      <div class="label">回踩 3槽累計報酬</div>
      <div class="value">{_fmt_pct(cmp_.get('lpb_total_return_pct'))}</div>
      <div class="sub">vs C18acc {_fmt_pct(cmp_.get('s2_total_return_pct'))}</div>
    </div>
    <div class="kpi-block">
      <div class="label">Δ slot 報酬</div>
      <div class="value">{cmp_.get('delta_total_return_pp', '—')} pp</div>
    </div>
    <div class="kpi-block highlight" style="background:#152018;border-color:#2a4a3a">
      <div class="label">mean leg 超額</div>
      <div class="value">{_fmt_pct(cmp_.get('lpb_mean_excess_pct'), digits=3)}</div>
      <div class="sub">vs C18acc {_fmt_pct(cmp_.get('s2_mean_excess_pct'), digits=3)}</div>
    </div>
    <div class="kpi-block">
      <div class="label">執行 / 略過</div>
      <div class="value">{cmp_.get('lpb_n_executed', '—')} / {cmp_.get('lpb_n_skipped', '—')}</div>
      <div class="sub">捕捉 {cmp_.get('lpb_capture_pct', '—')}%</div>
    </div>
    <div class="kpi-block">
      <div class="label">本金</div>
      <div class="value">{meta.get('total_capital_ntd', 30000):,.0f}</div>
      <div class="sub">{meta.get('n_slots', 3)} 槽 × {meta.get('capital_ntd', 10000):,.0f}</div>
    </div>
  </div>
</section>
"""


def _compare_table_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    rows = [
        ("3槽累計報酬", cmp_.get("s2_total_return_pct"), cmp_.get("lpb_total_return_pct"), cmp_.get("delta_total_return_pp")),
        ("mean leg 超額", cmp_.get("s2_mean_excess_pct"), cmp_.get("lpb_mean_excess_pct"), cmp_.get("delta_excess_pp")),
        ("執行 leg 數", cmp_.get("s2_n_executed"), cmp_.get("lpb_n_executed"), None),
        ("略過訊號", "—", cmp_.get("lpb_n_skipped"), None),
        ("CAGR（slot sim）", cmp_.get("s2_cagr_pct"), cmp_.get("lpb_cagr_pct"), _pct_delta(cmp_.get("s2_cagr_pct"), cmp_.get("lpb_cagr_pct"))),
    ]

    def _cell(v: Any, *, pp: bool = False, pct: bool = False) -> str:
        if v is None or v == "—":
            return "—"
        if pp:
            return f"{float(v):+.4f} pp"
        if pct:
            return _fmt_pct(v, digits=3 if isinstance(v, float) and abs(float(v)) < 10 else 2)
        return html.escape(str(v))

    body = ""
    for label, s2, lpb, delta in rows:
        is_pct = label in ("3槽累計報酬", "mean leg 超額", "CAGR（slot sim）")
        body += (
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{_cell(s2, pp=(label == 'mean leg 超額'), pct=is_pct)}</td>"
            f"<td>{_cell(lpb, pp=(label == 'mean leg 超額'), pct=is_pct)}</td>"
            f"<td>{_cell(delta, pp=True, pct=is_pct)}</td></tr>"
        )
    return f"""
<section class="panel">
  <h2>C18acc+S2 vs 強勢回踩（同窗 · 同 3槽 × 10k）</h2>
  <table class="compare-table">
    <thead><tr><th>指標</th><th>C18acc+S2</th><th>強勢回踩</th><th>Δ</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
  <p class="note">回踩 leg 報酬 = 盤中 poll 價差 − 成本；slot 累計 = 日收盤 mark-to-market 組合 NAV（與 C18acc timeline 同口徑）。</p>
</section>
"""


def _pct_delta(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)


def _case_cards_html(bundle: dict[str, Any]) -> str:
    cards = bundle.get("case_cards") or []
    if not cards:
        return ""
    parts = ['<section class="panel"><h2>精選成交 leg（spread Top 報酬）</h2><div class="case-grid">']
    for c in cards:
        pill = "pill-win" if c.get("tag") == "winner" else "pill-loss"
        parts.append(
            f"""<article class="case-card">
  <div class="case-head"><span class="star">★</span>
    <b>{html.escape(str(c.get('stock_id')))}</b>
    {_xml_escape(str(c.get('stock_name') or ''))}</div>
  <div class="case-meta">
    <span class="pill pill-pullback">spread {c.get('spread_dist', '—')}</span>
    <span class="pill {pill}">{_fmt_pct(c.get('return_pct'))}</span>
  </div>
  <ul>
    <li>進場 <b>{c.get('entry_date')}</b> @{_fmt_px(c.get('entry_px'))} → 出場 <b>{c.get('exit_date')}</b> @{_fmt_px(c.get('exit_px'))}</li>
    <li>exit <b>{html.escape(str(c.get('exit_reason')))}</b></li>
  </ul>
</article>"""
        )
    parts.append("</div></section>")
    return "\n".join(parts)


def _trade_ledger_html(bundle: dict[str, Any]) -> str:
    rows_html: list[str] = []
    for r in bundle.get("trade_ledger") or []:
        ret_col = "#6BCB94" if float(r.get("return_pct") or 0) >= 0 else "#E07070"
        pnl_txt = f"{float(r['pnl_ntd']):+,.0f}" if r.get("pnl_ntd") is not None else "—"
        rows_html.append(
            f"<tr class='hi-row ledger-row exec-row' data-entry='{r.get('entry_date')}'>"
            f"<td>{r.get('idx')}</td>"
            f"<td>{r.get('stock_id')}</td>"
            f"<td>{_xml_escape(str(r.get('stock_name') or ''))}</td>"
            f"<td>{r.get('spread_dist', '—')}</td>"
            f"<td>{_fmt_px(r.get('entry_px'))}</td>"
            f"<td>{_fmt_px(r.get('exit_px'))}</td>"
            f"<td>{str(r.get('entry_date', ''))[5:]}</td>"
            f"<td>{str(r.get('exit_date', ''))[5:]}</td>"
            f"<td style='color:{ret_col}'>{_fmt_pct(r.get('return_pct'))}</td>"
            f"<td>{_fmt_pct(r.get('excess_pct'), digits=3)}</td>"
            f"<td>{pnl_txt}</td>"
            f"<td>{_fmt_pct(r.get('cum_return_pct'), digits=2)}</td>"
            f"</tr>"
        )
    if not rows_html:
        return ""
    return f"""
<section class="panel">
  <h2>交易紀錄 · 價格 · 報酬 · 累計</h2>
  <table>
    <thead><tr>
      <th>#</th><th>代號</th><th>名稱</th><th>spread</th>
      <th>進場價</th><th>出場價</th><th>進場</th><th>出場</th>
      <th>leg 報酬</th><th>超額</th><th>PnL NTD</th><th>累計%</th>
    </tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
  <p class="note">累計% = 已結算 leg PnL 累加 ÷ 總本金 30,000 NTD（非複利 NAV）。下方時間軸為組合 MTM 累計。</p>
</section>
"""


def render_lead_pullback_backtest_showcase_html(
    *,
    bundle: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int = 20,
) -> str:
    legs = bundle["lpb_legs"]
    executed = []
    for lg in legs:
        ret = lg.get("return_pct")
        bench = lg.get("bench_return_pct")
        alpha_ntd = None
        if ret is not None and bench is not None:
            alpha_ntd = float(lg.get("allocated_ntd") or 0) * (float(ret) - float(bench)) / 100.0
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
                "deployed_ntd": lg.get("allocated_ntd"),
                "pnl_ntd": lg.get("pnl_ntd"),
                "return_pct": ret,
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "bench_return_pct": bench,
                "alpha_ntd": alpha_ntd,
                "exit_reason": lg.get("exit_reason"),
            }
        )

    meta = dict(bundle["lpb_meta"])
    meta["showcase"] = {"comparison": bundle.get("comparison")}

    timeline_html = render_l1h9_slots_timeline_html(
        etf_code=meta.get("display_code", "LPB-DEFAULT"),
        dates=dates,
        legs=legs,
        executed_signals=executed,
        skipped_signals=bundle.get("lpb_skipped") or [],
        all_trajectories=all_trajectories,
        meta=meta,
        bench_closes=bench_closes,
        length=length,
    )

    prefix = (
        _hero_html(bundle)
        + _compare_table_html(bundle)
        + _case_cards_html(bundle)
        + _trade_ledger_html(bundle)
        + '<p class="showcase-section-title">▼ 強勢回踩 · 互動 RRG 時間軸（組合累計 · 槽位 · 進出場）</p>'
    )

    html_out = timeline_html
    if "<style>" in html_out:
        html_out = html_out.replace("<style>", f"<style>{SHOWCASE_CSS}", 1)
    insert_at = html_out.find('<div class="wrap">')
    if insert_at >= 0:
        html_out = html_out[:insert_at] + prefix + html_out[insert_at:]

    inject_js = f"""
    const SHOWCASE = {json.dumps(bundle.get('comparison') or {}, ensure_ascii=False)};
"""
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
    legs = bundle["lpb_legs"]
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
