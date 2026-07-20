"""Render POOL1 graduation showcase HTML · fresh∪accel + S2 · poll_5m."""

from __future__ import annotations

# Showcase 金額／複利 KPI：台股典型費率 · 手續費 6 折（0.1425% × 0.6）
CHAMPION_SHOWCASE_COST_MODEL: dict[str, object] = {
    "enabled": True,
    "buy_fee_pct": 0.1425 * 0.6,
    "sell_fee_pct": 0.1425 * 0.6,
    "sell_tax_pct": 0.3,
    "fee_discount_label": "手續費6折",
}

import gzip
import html
import json
from collections import Counter
from pathlib import Path
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
SHOWCASE_CACHE_SCHEMA = "c18acc_showcase_cache-v1"
DEFAULT_SHOWCASE_CACHE_DIR = Path("reports/research/rrg/cache")


def default_showcase_cache_path(
    *,
    date_from: str,
    date_to: str | None,
    cache_dir: Path | None = None,
) -> Path:
    root = cache_dir or DEFAULT_SHOWCASE_CACHE_DIR
    end = date_to or "latest"
    return root / f"c18acc_showcase_{date_from}_{end}.json.gz"


def resolve_showcase_cache_path(
    *,
    date_from: str,
    date_to: str | None,
    explicit: Path | None = None,
    cache_dir: Path | None = None,
) -> Path:
    if explicit is not None:
        return explicit
    root = cache_dir or DEFAULT_SHOWCASE_CACHE_DIR
    if date_to:
        hit = default_showcase_cache_path(
            date_from=date_from, date_to=date_to, cache_dir=root
        )
        if hit.exists():
            return hit
    matches = sorted(root.glob(f"c18acc_showcase_{date_from}_*.json.gz"))
    if matches:
        return matches[-1]
    return default_showcase_cache_path(date_from=date_from, date_to=date_to, cache_dir=root)


def save_showcase_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def load_showcase_cache(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    schema = data.get("schema")
    if schema != SHOWCASE_CACHE_SCHEMA:
        raise ValueError(f"unsupported cache schema: {schema!r}")
    return data


def build_triple_showcase_payload(
    conn,
    dates: list[str],
    *,
    etf_codes: tuple[str, ...],
    length: int = 20,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
) -> dict[str, Any]:
    from research.backtest.archive.c18acc_pool1_showcase import build_triple_champion_showcase_bundle

    bundle = build_triple_champion_showcase_bundle(
        conn,
        dates,
        n_slots=n_slots,
        capital_ntd=capital_ntd,
    )
    trajectories, bench_closes = load_triple_showcase_timeline_inputs(
        conn,
        bundle,
        dates,
        etf_codes=etf_codes,
        length=length,
    )
    return {
        "schema": SHOWCASE_CACHE_SCHEMA,
        "date_from": dates[0],
        "date_to": dates[-1],
        "dates": dates,
        "length": length,
        "etf_codes": list(etf_codes),
        "n_slots": n_slots,
        "capital_ntd": capital_ntd,
        "bundle": bundle,
        "trajectories": trajectories,
        "bench_closes": bench_closes,
    }


def champion_track_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = payload["bundle"]
    track = (bundle.get("tracks") or {}).get("champion")
    if not track:
        raise ValueError("cache bundle missing champion track")
    return track


def champion_trajectories_from_payload(payload: dict[str, Any]) -> list[dict]:
    track = champion_track_from_payload(payload)
    stock_ids = {lg["stock_id"] for lg in track.get("legs") or []}
    return [t for t in payload["trajectories"] if t.get("stock_id") in stock_ids]


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
    diff_label = (
        "★ bypass 獨有 leg"
        if bundle.get("showcase_mode") == "bypass_diff"
        else "獨有交易"
    )
    return f"""
<section class="panel">
  <h2 class="showcase-section-title">{diff_label}清單（{n} 筆）</h2>
  <table>
    <thead><tr>
      <th></th><th>代號</th><th>名稱</th><th>候選來源</th>
      <th>入場價</th><th>出場價</th><th>入場時間</th><th>出場時間</th>
      <th>出場原因</th><th>單筆報酬</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>
"""


SHOWCASE_CSS = """
.showcase-hero h1 { font-size:20px; margin:0 0 8px; }
.showcase-conclusion { font-size:14px; color:#ccc; line-height:1.55; margin:0 0 8px; }
.showcase-disclaimer { margin:0 0 10px; }
.showcase-kpi { margin-top:12px; }
.showcase-strategy-spec h2 { font-size:15px; margin:0 0 8px; color:#ddd; }
.strategy-spec-body { color:#aaa; font-size:13px; line-height:1.65; }
.strategy-spec-body p { margin:0 0 8px; }
.strategy-spec-body ul { margin:0; padding-left:20px; }
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
.showcase-section-title { font-size:15px; color:#ddd; margin:0 0 10px; }
tr.pool1-only-row { background:#152018; }
"""


def render_pool1_showcase_html(
    *,
    bundle: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int = 20,
    prefix_html: str | None = None,
    timeline_section_title: str | None = None,
    page_title: str | None = None,
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

    section_title = timeline_section_title or "▼ POOL1 互動 RRG 時間軸（主圖）"
    if prefix_html is None:
        prefix = (
            _quick_guide_html()
            + _hero_html(bundle)
            + _compare_table_html(bundle)
            + _case_cards_html(bundle)
            + _diff_table_html(bundle)
            + f'<p class="showcase-section-title">{section_title}</p>'
        )
    else:
        prefix = prefix_html
        if timeline_section_title and '<p class="showcase-section-title">' not in prefix_html:
            prefix = prefix_html + f'<p class="showcase-section-title">{html.escape(timeline_section_title)}</p>'

    pool1_only_ids = sorted({lg["stock_id"] for lg in bundle.get("pool1_only_legs") or []})
    inject_js = f"""
    const POOL1_ONLY_STOCK_IDS = new Set({json.dumps(pool1_only_ids)});
    const SHOWCASE = {json.dumps(bundle.get('comparison') or {}, ensure_ascii=False)};
"""

    html_out = timeline_html
    if page_title and "<title>" in html_out:
        start = html_out.find("<title>") + len("<title>")
        end = html_out.find("</title>", start)
        if end > start:
            html_out = html_out[:start] + html.escape(page_title) + html_out[end:]
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
    meta = bundle.get("bypass_meta") or bundle.get("pool1_meta") or {}
    entry_mode = str(meta.get("entry_price_mode") or "close")
    _enrich_legs_bench(conn, legs, entry_price_mode=entry_mode)
    bench_closes = _load_bench_closes_for_dates(conn, dates)
    stock_ids = {lg["stock_id"] for lg in legs}
    all_trajectories = _load_rrg_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        length=length,
        with_close=True,
        stock_ids=stock_ids,
    )
    return all_trajectories, bench_closes


def _bypass_strategy_spec_html() -> str:
    return """
<section class="panel showcase-strategy-spec">
  <h2>策略原理</h2>
  <div class="strategy-spec-body">
    <p><b>底層</b>：C18acc POOL1（候選池 <code>fresh ∪ accel</code>）· S2 出場 · <code>poll_5m</code> 盤中掃描 · 3 槽 × 10k NTD。</p>
    <p><b>本報告對照</b>：僅改一個開關 — <code>accel_sell_bypass_on_spread_lost</code>；候選池、進場、出場規則其餘相同。</p>
    <ul>
      <li><b>POOL1-P5M</b>（base）：評分換倉僅賣出 <code>avg_accel &lt; 0</code> 的 held（<code>accel_sell_negative_only</code>）。</li>
      <li><b>POOL1-bypass-P5M</b>：若標的 <code>W5−W20 spread_rs ≤ 0</code>（昨收 PIT），即使正加速亦可參與 rotation 賣出。</li>
    </ul>
    <p><b>邏輯</b>：spread 轉負代表相對強度結構走弱；在結構弱化時放寬「僅賣負加速」限制，讓槽位更快釋放給新訊號。min_hold、Weakening 連 2 日強制出場、評分 margin 等 S2 規則兩邊不變。</p>
    <p><b>leg diff</b>：兩路徑進出場指紋（stock · entry · exit · reason）不同的成交筆；★ 標記 bypass 獨有路徑。</p>
  </div>
</section>
"""


def _bypass_hero_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    bypass = bundle.get("bypass_meta") or {}
    base_ex = _fmt_pct(cmp_.get("base_mean_excess_pct"))
    bypass_ex = _fmt_pct(cmp_.get("bypass_mean_excess_pct"))
    delta_pp = _fmt_pp(cmp_.get("delta_excess_pp"))
    return f"""
<section class="showcase-hero panel">
  <h1>POOL1 · spread_lost bypass · leg diff 展示</h1>
  <p class="showcase-conclusion">
    <b>結論</b>：窗口 <b>{bundle.get('date_start')} → {bundle.get('date_end')}</b>
    （{bundle.get('n_trade_dates')} 交易日），bypass 平均超額
    <b>{bypass_ex}</b>（base POOL1 {base_ex}，<b>{delta_pp}</b>），
    換倉 {cmp_.get('bypass_swaps')} 次（base {cmp_.get('base_swaps')} 次，多 {cmp_.get('delta_swaps')} 次），
    leg {cmp_.get('bypass_n_legs')} 筆（base {cmp_.get('base_n_legs')}，多 {cmp_.get('delta_n_legs')}）。
  </p>
  <p class="note showcase-disclaimer">以下為歷史回測模擬成交紀錄，非實盤下單。</p>
  <p class="sub">
    對照 <b>POOL1-P5M</b>（accel 賣出僅負加速）vs <b>POOL1-bypass-P5M</b>（spread_rs≤0 → 正加速亦可 rotation 賣）
  </p>
  <div class="kpi-banner showcase-kpi">
    <div class="kpi-block highlight">
      <div class="label">bypass 平均超額</div>
      <div class="value">{bypass_ex}</div>
      <div class="sub">base POOL1 {base_ex}</div>
    </div>
    <div class="kpi-block">
      <div class="label">超額差異</div>
      <div class="value">{delta_pp}</div>
    </div>
    <div class="kpi-block highlight">
      <div class="label">★ bypass 獨有 leg</div>
      <div class="value">{len(bundle.get('bypass_only_legs') or [])}</div>
      <div class="sub">base 獨有 {len(bundle.get('base_only_legs') or [])}</div>
    </div>
    <div class="kpi-block">
      <div class="label">換倉差異</div>
      <div class="value">{cmp_.get('delta_swaps', '—')}</div>
      <div class="sub">base {cmp_.get('base_swaps')} → bypass {cmp_.get('bypass_swaps')}</div>
    </div>
    <div class="kpi-block">
      <div class="label">強制出場</div>
      <div class="value">base {cmp_.get('base_force_exits')} · bypass {cmp_.get('bypass_force_exits')}</div>
    </div>
  </div>
</section>
"""


def _bypass_compare_table_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}

    def _cell(v: Any, *, pp: bool = False) -> str:
        if v is None:
            return "—"
        if pp:
            return _fmt_pp(v)
        if isinstance(v, (int, float)) and not pp:
            return str(v)
        return html.escape(str(v))

    rows = [
        ("平均超額", cmp_.get("base_mean_excess_pct"), cmp_.get("bypass_mean_excess_pct"), cmp_.get("delta_excess_pp")),
        ("成交 leg 數", cmp_.get("base_n_legs"), cmp_.get("bypass_n_legs"), cmp_.get("delta_n_legs")),
        ("換倉次數", cmp_.get("base_swaps"), cmp_.get("bypass_swaps"), cmp_.get("delta_swaps")),
        ("強制出場", cmp_.get("base_force_exits"), cmp_.get("bypass_force_exits"), None),
    ]
    body = ""
    for label, base, bypass, delta in rows:
        body += (
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{_cell(base, pp=(label == '平均超額'))}</td>"
            f"<td>{_cell(bypass, pp=(label == '平均超額'))}</td>"
            f"<td>{_cell(delta, pp=(label == '平均超額'))}</td></tr>"
        )
    return f"""
<section class="panel">
  <h2>POOL1 base vs bypass 對照</h2>
  <table class="compare-table">
    <thead><tr><th>指標</th><th>POOL1 · 無 bypass</th><th>POOL1 · spread_lost bypass</th><th>差異</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</section>
"""


def _bypass_case_cards_html(bundle: dict[str, Any]) -> str:
    bypass_cards = [
        c for c in (bundle.get("case_cards") or []) if not c.get("base_only")
    ]
    if not bypass_cards:
        return """
<section class="panel">
  <h2>★ bypass 獨有 leg</h2>
  <p class="note">此窗口內 base 與 bypass 交易指紋相同。見下方時間軸。</p>
</section>
"""
    top = _top_case_cards(bypass_cards)
    total = len(bypass_cards)
    parts = [
        "<section class='panel'>",
        f"<h2>★ bypass 獨有 leg 精選（顯示 {len(top)} / {total} 筆）</h2>",
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
        parts.append(
            f"""<article class="case-card">
  <div class="case-head"><span class="star">★</span>
    <b>{html.escape(str(c.get('stock_id')))}</b>
    {_xml_escape(str(c.get('stock_name') or ''))}</div>
  <div class="case-meta">{html.escape(pool_note)}</div>
  <ul>
    <li>進場 <b>{c.get('entry_date')}</b> @ <b>{entry_px}</b>（{entry_time}）</li>
    <li>出場 <b>{c.get('exit_date')}</b> @ <b>{exit_px}</b>（{exit_time}）</li>
    <li>出場原因 <b>{html.escape(exit_zh)}</b></li>
    <li>單筆報酬 <b>{ret_s}</b></li>
  </ul>
</article>"""
        )
    parts.append("</div></section>")
    base_only = bundle.get("base_only_legs") or []
    if base_only:
        parts.append(
            f"<p class='note'>base POOL1 獨有 leg：{len(base_only)} 筆"
            f"（bypass 路徑未走 · 多為較晚換倉或不同 exit 時點）</p>"
        )
    return "\n".join(parts)


def render_bypass_showcase_html(
    *,
    bundle: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int = 20,
) -> str:
    """Compose bypass diff sections + POOL1+bypass interactive RRG timeline."""
    prefix = (
        _bypass_strategy_spec_html()
        + _bypass_hero_html(bundle)
        + _bypass_compare_table_html(bundle)
        + _bypass_case_cards_html(bundle)
        + _diff_table_html(bundle)
    )
    page_title = f"POOL1+bypass leg diff · {bundle.get('date_start')} → {bundle.get('date_end')}"
    return render_pool1_showcase_html(
        bundle=bundle,
        dates=dates,
        all_trajectories=all_trajectories,
        bench_closes=bench_closes,
        length=length,
        prefix_html=prefix,
        timeline_section_title="▼ POOL1+bypass 互動 RRG 時間軸（主圖 · ★=差異 leg）",
        page_title=page_title,
    )


TRIPLE_SHOWCASE_CSS = """
.doc-page { max-width:920px; margin:0 auto; padding:8px 0 48px; line-height:1.65; }
.doc-page > h1 { font-size:24px; margin:0 0 12px; }
.doc-page > .doc-intro { color:#999; font-size:14px; margin:0 0 28px; }
.doc-page h2 { font-size:17px; margin:32px 0 10px; color:#ddd; }
.doc-page .quick-guide-list { margin:0 0 24px; padding-left:22px; color:#aaa; font-size:14px; }
.doc-page .showcase-conclusion { font-size:14px; color:#ccc; line-height:1.6; margin:0 0 8px; }
.doc-page .compare-table { width:100%; border-collapse:collapse; font-size:13px; margin:8px 0 32px; }
.doc-page .compare-table th, .doc-page .compare-table td {
  padding:8px 10px; border-bottom:1px solid #2a2a2a; text-align:left;
}
.doc-page .compare-table .best-cell { color:#7ec8a0; font-weight:700; }
.doc-page .compare-table .prod-row td { border-top:2px solid #3a5a4a; }
.doc-page .doc-part-label {
  font-size:12px; color:#7ec8a0; letter-spacing:0.06em; text-transform:uppercase; margin:40px 0 4px;
}
.doc-track-interactive .kpi-banner {
  background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:12px 14px;
  margin-bottom:12px; display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px 16px; align-items:end;
}
.doc-track-interactive .kpi-block.highlight { background:#1f1a12; border:1px solid #4a3a20; border-radius:6px; padding:8px 10px; }
.doc-track-interactive .kpi-block.highlight[style*="121a1f"] { background:#121a1f !important; border-color:#2a4a5a !important; }
.doc-track-interactive .kpi-row2 {
  grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center;
  padding-top:8px; border-top:1px solid #2a2a2a; font-size:12px; color:#888;
}
"""


def _srcdoc_attr(html_str: str) -> str:
    return html_str.replace("&", "&amp;").replace('"', "&quot;")


def _legs_to_executed(legs: list[dict]) -> list[dict]:
    executed: list[dict] = []
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
            }
        )
    return executed


def _extract_html_body(html_str: str) -> str:
    import re

    m = re.search(r"<body[^>]*>(.*)</body>", html_str, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else html_str


def _extract_html_styles(html_str: str) -> str:
    import re

    m = re.search(r"<style>(.*?)</style>", html_str, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _render_track_timeline_html(
    *,
    track: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int,
    static_only: bool = False,
    cinema_autoplay: bool = False,
) -> str:
    legs = track["legs"]
    meta = dict(track["meta"])
    meta["showcase_mode"] = True
    meta["market_labels"] = True
    meta["document_mode"] = True
    meta["document_embed"] = True
    meta["market_track_label"] = track.get("label") or track.get("key")
    meta["market_track_note"] = track.get("spec_note") or track.get("short_label") or ""
    meta["show_exit_reason"] = True
    meta["read_guide_open"] = True
    meta["static_only"] = static_only
    meta["cinema_mode"] = not static_only
    meta["cinema_autoplay"] = cinema_autoplay and not static_only
    meta["cinema_autoplay_ms"] = 20000
    meta["embed_id"] = track.get("key") or "track"
    if not static_only:
        meta["signals_table_prefix"] = ""
    inner = render_l1h9_slots_timeline_html(
        etf_code=meta.get("display_code", track["label"]),
        dates=dates,
        legs=legs,
        executed_signals=_legs_to_executed(legs),
        skipped_signals=[],
        all_trajectories=all_trajectories,
        meta=meta,
        bench_closes=bench_closes,
        length=length,
    )
    if static_only:
        return inner
    title = f"{track['label']} · {dates[0]} → {dates[-1]}"
    if "<title>" in inner:
        start = inner.find("<title>") + len("<title>")
        end = inner.find("</title>", start)
        if end > start:
            inner = inner[:start] + html.escape(title) + inner[end:]
    return inner


def _render_track_iframe_html(
    *,
    track: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int,
) -> str:
    return _render_track_timeline_html(
        track=track,
        dates=dates,
        all_trajectories=all_trajectories,
        bench_closes=bench_closes,
        length=length,
        static_only=False,
        cinema_autoplay=True,
    )


def _triple_quick_guide_html() -> str:
    return """
  <h2>30 秒看懂</h2>
  <ol class="quick-guide-list">
    <li><b>三軌對照</b>：基準版（僅新進領先）· 採納版（雙池＋價差放行）· 對照版（進階換倉堆疊）。</li>
    <li><b>共通設定</b>：盤中 5 分 K 成交 · 3 個部位 × 1 萬元 · 最短持有 5 日、最長 10 日。</li>
    <li><b>閱讀方式</b>：先看 KPI 對照表，再依序讀各版策略說明與成交明細；採納版含可播放回測圖。</li>
  </ol>
"""


def _triple_adoption_note_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    tracks = bundle.get("tracks") or {}
    best_key = cmp_.get("best_excess_track") or "champion"
    best_label = (tracks.get(best_key) or {}).get("label", best_key)
    return f"""
  <p class="note showcase-adoption">正式採納：<b>採納版</b>。超額最高為<b>{html.escape(best_label)}</b>，採納版在報酬與複雜度間取平衡。</p>
"""


def _triple_compare_table_html(bundle: dict[str, Any]) -> str:
    cmp_ = bundle.get("comparison") or {}
    tracks = bundle.get("tracks") or {}
    s2 = cmp_.get("s2_fresh") or {}
    champ = cmp_.get("champion") or {}
    dual = cmp_.get("dual") or {}
    excess_vals = {
        "s2": float(s2.get("mean_excess_pct") or -999),
        "champ": float(champ.get("mean_excess_pct") or -999),
        "dual": float(dual.get("mean_excess_pct") or -999),
    }
    best_col = max(excess_vals, key=excess_vals.get)

    def _ex(v: Any, col: str) -> str:
        s = _fmt_pct(v)
        return f'<span class="best-cell">{s}</span>' if col == best_col else s

    rows = [
        ("平均超額", s2.get("mean_excess_pct"), champ.get("mean_excess_pct"), dual.get("mean_excess_pct"), True),
        ("成交筆數", s2.get("n_executed"), champ.get("n_executed"), dual.get("n_executed"), False),
        ("換倉次數", s2.get("swaps_total"), champ.get("swaps_total"), dual.get("swaps_total"), False),
        ("強制出場", s2.get("force_exits"), champ.get("force_exits"), dual.get("force_exits"), False),
        (
            "獨有成交筆數",
            len((tracks.get("s2_fresh") or {}).get("only_legs") or []),
            len((tracks.get("champion") or {}).get("only_legs") or []),
            len((tracks.get("dual") or {}).get("only_legs") or []),
            False,
        ),
    ]
    body = ""
    for label, a, b, c, is_pct in rows:
        if is_pct:
            body += (
                f"<tr><td>{html.escape(label)}</td>"
                f"<td>{_ex(a, 's2')}</td>"
                f'<td class="prod-row">{_ex(b, "champ")}</td>'
                f"<td>{_ex(c, 'dual')}</td></tr>"
            )
        else:
            body += (
                f"<tr><td>{html.escape(label)}</td>"
                f"<td>{a if a is not None else '—'}</td>"
                f'<td class="prod-row">{b if b is not None else "—"}</td>'
                f"<td>{c if c is not None else '—'}</td></tr>"
            )

    spec_rows = ""

    return f"""
  <h2>三軌 KPI 對照</h2>
  <table class="compare-table">
    <thead><tr>
      <th>指標</th><th>基準版</th><th>採納版</th><th>對照版</th>
    </tr></thead>
    <tbody>{body}{spec_rows}</tbody>
  </table>
  <p class="note showcase-disclaimer">以下為歷史回測模擬成交紀錄，非實盤下單。</p>
"""


def load_triple_showcase_timeline_inputs(
    conn,
    bundle: dict[str, Any],
    dates: list[str],
    *,
    etf_codes: tuple[str, ...],
    length: int = 20,
) -> tuple[list[dict], list[float | None]]:
    all_legs: list[dict] = []
    for tr in (bundle.get("tracks") or {}).values():
        all_legs.extend(tr.get("legs") or [])
    sub = {"pool1_legs": all_legs, "pool1_meta": bundle.get("pool1_meta") or {}}
    return load_showcase_timeline_inputs(conn, sub, dates, etf_codes=etf_codes, length=length)


def render_champion_showcase_html(
    *,
    bundle: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int = 20,
    universe_trajectories: list[dict] | None = None,
) -> str:
    """Single-page adopted champion showcase · chart + right trade feed + detail table."""
    track = (bundle.get("tracks") or {}).get("champion")
    if not track:
        raise ValueError("bundle missing champion track")
    chart_trajectories = universe_trajectories if universe_trajectories is not None else all_trajectories

    legs = track["legs"]
    meta = dict(track["meta"])
    meta["showcase_mode"] = True
    meta["market_labels"] = True
    meta["document_mode"] = True
    meta["document_embed"] = False
    meta["side_trade_feed"] = True
    meta["market_track_label"] = track.get("label") or "採納版（Champion）"
    meta["market_track_note"] = track.get("spec_note") or track.get("short_label") or ""
    meta["show_exit_reason"] = True
    meta["cinema_mode"] = True
    meta["cinema_autoplay"] = True
    meta["cinema_autoplay_ms"] = 20000
    meta["compound_slots"] = True
    meta["cost_model"] = dict(CHAMPION_SHOWCASE_COST_MODEL)
    meta["signals_table_prefix"] = ""
    if universe_trajectories is not None:
        meta["universe_cinema"] = True
        meta["axis_bounds"] = [90.0, 110.0]
        meta["clip_dynamic"] = False
        meta["show_dot_labels"] = True

    html_out = render_l1h9_slots_timeline_html(
        etf_code=meta.get("display_code", track["label"]),
        dates=dates,
        legs=legs,
        executed_signals=_legs_to_executed(legs),
        skipped_signals=[],
        all_trajectories=chart_trajectories,
        meta=meta,
        bench_closes=bench_closes,
        length=length,
    )
    page_title = f"C18acc 採納版回測 · {bundle.get('date_start')} → {bundle.get('date_end')}"
    if "<title>" in html_out:
        start = html_out.find("<title>") + len("<title>")
        end = html_out.find("</title>", start)
        if end > start:
            html_out = html_out[:start] + html.escape(page_title) + html_out[end:]
    return html_out


def render_triple_champion_showcase_html(
    *,
    bundle: dict[str, Any],
    dates: list[str],
    all_trajectories: list[dict],
    bench_closes: list[float | None],
    length: int = 20,
) -> str:
    tracks = bundle.get("tracks") or {}
    order = [
        ("champion", "採納版 · 互動回測", True),
        ("dual", "對照版", False),
        ("s2_fresh", "基準版", False),
    ]
    track_sections: list[str] = []
    timeline_styles = ""
    for key, part_label, is_interactive in order:
        tr = tracks.get(key)
        if not tr:
            continue
        html_part = _render_track_timeline_html(
            track=tr,
            dates=dates,
            all_trajectories=all_trajectories,
            bench_closes=bench_closes,
            length=length,
            static_only=not is_interactive,
            cinema_autoplay=is_interactive,
        )
        if is_interactive and not timeline_styles:
            timeline_styles = _extract_html_styles(html_part)
            body = _extract_html_body(html_part)
        elif is_interactive:
            body = _extract_html_body(html_part)
        else:
            body = html_part.strip()
        track_sections.append(
            f'<p class="doc-part-label">{html.escape(part_label)}</p>\n{body}'
        )

    page_title = (
        f"C18acc 三軌回測說明 · {bundle.get('date_start')} → {bundle.get('date_end')}"
    )
    compare_block = _triple_compare_table_html(bundle)
    body = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(page_title)}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; }}
    {SHOWCASE_CSS}
    {TRIPLE_SHOWCASE_CSS}
    {timeline_styles}
  </style>
</head>
<body>
<main class="doc-page">
  <h1>C18acc 三軌回測說明文件</h1>
  <p class="doc-intro">{bundle.get('date_start')} → {bundle.get('date_end')} · 請由上而下閱讀 · 採納版含可播放回測圖</p>
  {_triple_quick_guide_html()}
  {compare_block}
  {_triple_adoption_note_html(bundle)}
  {''.join(track_sections)}
</main>
</body>
</html>
"""
    return body
