#!/usr/bin/env python3
"""Render 金額×RRG 冠軍：月度獲利、交易明細、Yahoo 技術分析連結。

  PYTHONPATH=src .venv/bin/python scripts/research/render_amount_rrg_champion_html.py
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


def _ret_cls(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if v > 0:
        return "pos"
    if v < 0:
        return "neg"
    return ""


def _yahoo(sid: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{sid}.TW/technical-analysis"


def _latest_trades(dir_path: Path) -> Path | None:
    hits = sorted(
        dir_path.glob("*_amount_rrg_champion_trades.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def render(payload: dict, *, source_name: str) -> str:
    ch = payload.get("champion") or {}
    meta = payload.get("meta") or {}
    months = payload.get("monthly") or []
    trades = payload.get("trades") or []

    n_slots = int(ch.get("n_slots") or 1)
    threshold = ch.get("threshold") or "80M"
    rrg_gate = ch.get("rrg_gate") or "rs_ratio>100"
    strategy_id = ch.get("strategy_id") or "L2H12"
    capital = ch.get("capital_ntd_per_slot") or 10000
    config_id = ch.get("config_id") or ""
    fair = meta.get("fair_compare") or {}
    title = (
        f"金額×RRG 1槽優化 · {threshold}"
        if fair
        else (
            f"金額×RRG 候選 · {threshold} · {n_slots}槽"
            if n_slots > 1 or str(threshold) != "80M"
            else "金額×RRG 冠軍 · 月度獲利與交易明細"
        )
    )
    kicker = (
        "Research · Fixed-capital 1-slot optimization"
        if fair
        else (
            "Research · Amount robustness candidate"
            if n_slots > 1
            else "Research · Champion sleeve"
        )
    )
    spec_line = (
        f"規格：富邦新店金額 ≥{_esc(threshold)} · {_esc(rrg_gate)} · "
        f"{_esc(strategy_id)} · {n_slots} 槽 × {_esc(capital)} NTD"
        + (f"（{_esc(config_id)}）" if config_id else "")
    )
    fair_section = ""
    if fair:
        fair_section = f"""
  <section>
    <h2>固定總資金 { _esc(fair.get('fixed_total_capital_ntd')) } · 期間報酬對照</h2>
    <div class="grid4">
      <div class="stat"><div class="l">1槽 · 全期</div><div class="v">{_pct(fair.get('oneslot_full_return_pct'))}</div></div>
      <div class="stat"><div class="l">1槽 · OOS</div><div class="v">{_pct(fair.get('oneslot_oos_return_pct'))}</div></div>
      <div class="stat"><div class="l">公平3槽 · 全期</div><div class="v">{_pct(fair.get('fair_3slot_full_return_pct'))}</div></div>
      <div class="stat"><div class="l">公平3槽 · OOS</div><div class="v">{_pct(fair.get('fair_3slot_oos_return_pct'))}</div></div>
    </div>
    <p class="lead" style="margin-top:0.9rem">
      同規格 80M · rs_ratio≥100 · L2H12；3槽每槽 = 總資金/3。OOS 切點 2025-10-01。
      舊報 414% 與本頁報酬差異來自研究引擎／資金標尺，非同一契約數字。
    </p>
  </section>
"""

    # equity curve by exit order
    eq = []
    cum = 0.0
    for t in trades:
        cum += float(t.get("pnl_ntd") or 0)
        eq.append({"date": t.get("exit_date"), "cum": round(cum, 2)})

    month_rows = []
    for m in months:
        rc = _ret_cls(m.get("pnl_ntd"))
        month_rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(m.get('month'))}</td>"
            f"<td class='n'>{_esc(m.get('n_trades'))}</td>"
            f"<td class='n {rc}'>{_num(m.get('pnl_ntd'), 0)}</td>"
            f"<td class='n {rc}'>{_num(m.get('alpha_ntd'), 0)}</td>"
            f"<td class='n'>{_pct(m.get('win_rate_pct'), 1)}</td>"
            f"<td class='n {rc}'>{_pct(m.get('avg_return_pct'), 2)}</td>"
            "</tr>"
        )

    trade_rows = []
    for t in trades:
        sid = str(t.get("stock_id") or "")
        rc = _ret_cls(t.get("return_pct"))
        yurl = t.get("yahoo_url") or _yahoo(sid)
        trade_rows.append(
            "<tr>"
            f"<td class='n'>{_esc(t.get('cycle'))}</td>"
            f"<td class='mono'>{_esc(t.get('signal_date'))}</td>"
            f"<td class='mono'><a href='{_esc(yurl)}' target='_blank' rel='noopener'>"
            f"{_esc(sid)}</a></td>"
            f"<td>{_esc(t.get('stock_name'))}</td>"
            f"<td class='n'>{_num(t.get('net_lots'), 1)}</td>"
            f"<td class='mono'>{_esc(t.get('rrg_quadrant'))}</td>"
            f"<td class='n'>{_num(t.get('rs_ratio'), 2)}</td>"
            f"<td class='n'>{_num(t.get('rs_momentum'), 2)}</td>"
            f"<td class='mono'>{_esc(t.get('entry_date'))}</td>"
            f"<td class='n'>{_num(t.get('entry_px'), 2)}</td>"
            f"<td class='mono'>{_esc(t.get('exit_date'))}</td>"
            f"<td class='n'>{_num(t.get('exit_px'), 2)}</td>"
            f"<td class='n {rc}'>{_pct(t.get('return_pct'))}</td>"
            f"<td class='n {rc}'>{_num(t.get('pnl_ntd'), 0)}</td>"
            f"<td class='n'>{_num(t.get('basket_alpha_ntd'), 0)}</td>"
            f"<td><a class='ya' href='{_esc(yurl)}' target='_blank' rel='noopener'>Yahoo 技術</a></td>"
            "</tr>"
        )

    months_json = json.dumps(months, ensure_ascii=False)
    eq_json = json.dumps(eq, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(title)}</title>
<style>
:root {{
  --bg0:#0c1118; --bg1:#141c27; --bg2:#1c2736; --line:#2a3a4f;
  --text:#e8eef6; --muted:#8fa3bb; --gold:#e2b657; --teal:#3ecfb2;
  --pos:#6bcf8e; --neg:#e07a7a;
  --font-d:"Iowan Old Style",Palatino,serif;
  --font-b:"IBM Plex Sans","PingFang TC",sans-serif;
  --font-m:"IBM Plex Mono",Menlo,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;color:var(--text);font-family:var(--font-b);
background:radial-gradient(1100px 520px at 8% -8%,#1e3348,transparent 55%),linear-gradient(180deg,var(--bg0),#090d12)}}
.wrap{{max-width:1200px;margin:0 auto;padding:2.2rem 1.2rem 4rem}}
.kicker{{font-family:var(--font-m);font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--gold)}}
h1{{font-family:var(--font-d);font-size:clamp(1.55rem,3vw,2.15rem);margin:.35rem 0 .5rem}}
.sub{{color:var(--muted);max-width:52rem;margin:0}}
.meta{{display:flex;flex-wrap:wrap;gap:.7rem 1.1rem;margin-top:1rem;color:var(--muted);font-size:.85rem}}
.meta b{{color:var(--text)}}
section{{margin:2.1rem 0}}
h2{{font-family:var(--font-d);font-size:1.28rem;margin:0 0 .55rem}}
.lead{{color:var(--muted);font-size:.92rem;margin:0 0 1rem}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:.75rem}}
@media(max-width:900px){{.grid4{{grid-template-columns:1fr 1fr}}}}
.stat{{background:var(--bg1);border:1px solid var(--line);border-radius:12px;padding:.9rem 1rem}}
.stat .l{{color:var(--muted);font-size:.78rem}}.stat .v{{font-family:var(--font-d);font-size:1.35rem;margin-top:.25rem;color:var(--gold)}}
.card{{background:var(--bg1);border:1px solid var(--line);border-radius:12px;padding:1rem 1.1rem;margin-bottom:.9rem}}
.chart{{width:100%;height:260px;display:block}}
.table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
th,td{{padding:.4rem .45rem;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{background:var(--bg2);color:var(--muted);font-weight:500}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
td.mono{{font-family:var(--font-m)}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}
a{{color:var(--teal);text-decoration:none}} a:hover{{text-decoration:underline}}
a.ya{{font-family:var(--font-m);font-size:.72rem}}
.pill{{display:inline-block;font-family:var(--font-m);font-size:.72rem;padding:.2rem .55rem;border-radius:999px;border:1px solid var(--line);background:var(--bg2);color:var(--gold);margin-right:.35rem}}
.note{{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.8rem}}
ol{{padding-left:1.2rem}} li{{margin:.35rem 0}}
</style>
</head>
<body>
<div class="wrap">
  <p class="kicker">{_esc(kicker)}</p>
  <h1>{_esc(title)}</h1>
  <p class="sub">
    {_esc(ch.get('horizon_note') or 'L2 進場、持有 12 交易日 ≈ 短線偏短中線')}。
    {spec_line}。
  </p>
  <div class="meta">
    <span>窗口 <b>{_esc(ch.get('window_start'))} → {_esc(ch.get('window_end'))}</b></span>
    <span>來源 <b>{_esc(source_name)}</b></span>
    <span>期間報酬 <b>{_pct(ch.get('period_return_pct'))}</b></span>
    <span>捕獲 <b>{_pct(meta.get('signal_capture_pct'))}</b></span>
  </div>
{fair_section}
  <section>
    <h2>這算短線還是中線？</h2>
    <p class="lead">
      <span class="pill">短線偏短中線</span>
      持有約 <b>{_esc(strategy_id)}（{_esc(ch.get('horizon_note') or '≈2.5–3 週')}）</b>，不是日內、也不是月線／季線波段。
      訊號是收盤後分點金額 + W20 Momentum，進場／出場依 {_esc(strategy_id)}。
    </p>
  </section>

  <section>
    <h2>穩定度快照</h2>
    <div class="grid4">
      <div class="stat"><div class="l">成交筆</div><div class="v">{_esc(meta.get('n_trade_legs'))}</div></div>
      <div class="stat"><div class="l">勝率</div><div class="v">{_pct(meta.get('leg_win_rate_pct'))}</div></div>
      <div class="stat"><div class="l">獲利月份占比</div><div class="v">{_pct(meta.get('positive_month_pct'), 1)}</div></div>
      <div class="stat"><div class="l">最長連虧月</div><div class="v">{_esc(meta.get('max_negative_month_streak'))}</div></div>
    </div>
    <p class="lead" style="margin-top:0.9rem">
      累計 PnL（每槽 {_esc(capital)} × {n_slots} 槽）{_num(meta.get('sum_pnl_ntd'), 0)} NTD ·
      正報酬月 {_esc(meta.get('positive_months'))}/{_esc(meta.get('n_months'))}。
      這是 robustness optimization 候選，OOS 已看過，<b>尚不宜直接當正式策略</b>。
    </p>
  </section>

  <section>
    <h2>月度獲利</h2>
    <p class="lead">依出場月加總（每筆配置 {_esc(capital)}）。柱狀＝該月 PnL。</p>
    <div class="card"><canvas id="monthChart" class="chart"></canvas></div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>月份</th><th>筆數</th><th>PnL</th><th>α</th><th>勝率</th><th>均報酬%</th>
      </tr></thead>
      <tbody>{"".join(month_rows)}</tbody>
    </table></div>
  </section>

  <section>
    <h2>累計權益曲線</h2>
    <p class="lead">依成交順序累加 PnL（未扣稅費）。</p>
    <div class="card"><canvas id="eqChart" class="chart"></canvas></div>
  </section>

  <section>
    <h2>交易明細</h2>
    <p class="lead">代碼可點進 Yahoo 股市技術分析；右側亦有捷徑。含 W20 Momentum 欄。</p>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>#</th><th>訊號</th><th>代碼</th><th>名稱</th><th>淨買超張</th>
        <th>RRG</th><th>RS-ratio</th><th>W20 Mom</th>
        <th>進場</th><th>進價</th><th>出場</th><th>出價</th>
        <th>報酬%</th><th>PnL</th><th>α</th><th>連結</th>
      </tr></thead>
      <tbody>{"".join(trade_rows)}</tbody>
    </table></div>
  </section>

  <p class="note">
    Research only · cost 0 bps · {n_slots} slot × {_esc(capital)}。正式進 strategy.yaml / order layer 前需：稅費滑價、獨立樣本外、與現有 C18acc／Leading Dip 衝突檢查。
    來源 {_esc(source_name)}。
  </p>
</div>
<script>
const months = {months_json};
const equity = {eq_json};

function drawBars(canvas, rows) {{
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth, cssH = canvas.clientHeight;
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,cssW,cssH);
  const pad = {{t:16,r:12,b:36,l:48}};
  const w = cssW - pad.l - pad.r, h = cssH - pad.t - pad.b;
  const vals = rows.map(r => +r.pnl_ntd);
  const maxA = Math.max(...vals.map(Math.abs), 1);
  ctx.strokeStyle = '#2a3a4f'; ctx.beginPath();
  ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, pad.t+h); ctx.lineTo(pad.l+w, pad.t+h); ctx.stroke();
  const zeroY = pad.t + h/2;
  ctx.strokeStyle = '#3a4a5f'; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(pad.l+w, zeroY); ctx.stroke();
  ctx.setLineDash([]);
  const bw = w / Math.max(rows.length, 1) * 0.7;
  rows.forEach((r,i) => {{
    const v = +r.pnl_ntd;
    const bh = (Math.abs(v)/maxA) * (h/2 - 4);
    const x = pad.l + (i+0.15) * (w/Math.max(rows.length, 1));
    const y = v >= 0 ? zeroY - bh : zeroY;
    ctx.fillStyle = v >= 0 ? '#6bcf8e' : '#e07a7a';
    ctx.fillRect(x, y, bw, Math.max(bh, 1));
  }});
  ctx.fillStyle = '#8fa3bb'; ctx.font = '10px monospace';
  rows.forEach((r,i) => {{
    if (i % 2 !== 0) return;
    const x = pad.l + (i+0.5) * (w/Math.max(rows.length, 1));
    ctx.save(); ctx.translate(x, pad.t+h+12); ctx.rotate(-0.6);
    ctx.fillText((r.month||'').slice(2), 0, 0); ctx.restore();
  }});
}}

function drawLine(canvas, pts) {{
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth, cssH = canvas.clientHeight;
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,cssW,cssH);
  if (!pts.length) return;
  const pad = {{t:16,r:12,b:28,l:48}};
  const w = cssW - pad.l - pad.r, h = cssH - pad.t - pad.b;
  const ys = pts.map(p => p.cum);
  const ymin = Math.min(0, ...ys), ymax = Math.max(...ys, 1);
  const xAt = i => pad.l + (pts.length===1 ? w/2 : i*(w/(pts.length-1)));
  const yAt = v => pad.t + h - ((v-ymin)/(ymax-ymin||1))*h;
  ctx.strokeStyle = '#2a3a4f';
  ctx.beginPath(); ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, pad.t+h); ctx.lineTo(pad.l+w, pad.t+h); ctx.stroke();
  ctx.strokeStyle = '#3a4a5f'; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad.l, yAt(0)); ctx.lineTo(pad.l+w, yAt(0)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = '#e2b657'; ctx.lineWidth = 2; ctx.beginPath();
  pts.forEach((p,i) => {{ const x=xAt(i), y=yAt(p.cum); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); }});
  ctx.stroke();
}}

drawBars(document.getElementById('monthChart'), months);
drawLine(document.getElementById('eqChart'), equity);
window.addEventListener('resize', () => {{
  drawBars(document.getElementById('monthChart'), months);
  drawLine(document.getElementById('eqChart'), equity);
}});
</script>
</body></html>
"""


def _latest_amount_opt(dir_path: Path, *, oneslot: bool = False) -> Path | None:
    pattern = (
        "*_fubon_xindian_amount_1slot_optimization.json"
        if oneslot
        else "*_fubon_xindian_amount_3slot_optimization.json"
    )
    hits = sorted(
        dir_path.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


def _gate_label(cfg: Any) -> str:
    parts: list[str] = []
    if getattr(cfg, "rs_ratio_min", None) is not None:
        parts.append(f"rs_ratio>={cfg.rs_ratio_min:g}")
    if getattr(cfg, "w20_momentum_min", None) is not None:
        parts.append(f"w20_momentum>={cfg.w20_momentum_min:g}")
    return " · ".join(parts) if parts else "none"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--from-amount-opt",
        nargs="?",
        const="LATEST",
        default=None,
        help="Build trades from amount 3-slot optimization JSON",
    )
    ap.add_argument(
        "--from-amount-oneslot",
        nargs="?",
        const="LATEST",
        default=None,
        help="Build trades from amount 1-slot optimization JSON (fixed 30k capital)",
    )
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    payload: dict | None = None
    src_name = ""
    out: Path | None = args.out
    opt_arg = args.from_amount_oneslot or args.from_amount_opt
    oneslot = args.from_amount_oneslot is not None

    if opt_arg is not None:
        from stock_db import DEFAULT_DB_PATH, connect
        from research.backtest.fubon_xindian_filter_study import (
            FIXED_TOTAL_CAPITAL_NTD,
            FilterConfig,
            build_amount_candidate_trades_payload,
        )

        if opt_arg == "LATEST":
            opt_path = _latest_amount_opt(
                REPORTS_RESEARCH / "fubon-xindian-copytrade",
                oneslot=oneslot,
            )
        else:
            opt_path = Path(opt_arg)
        if opt_path is None or not opt_path.exists():
            print("ERROR: no amount optimization JSON", file=sys.stderr)
            return 2
        opt = json.loads(opt_path.read_text(encoding="utf-8"))
        cand = opt.get("gate_surviving_candidate") or opt.get("final_is_selected")
        if not cand:
            print(
                "ERROR: no gate_surviving_candidate in optimization JSON",
                file=sys.stderr,
            )
            return 2
        cfg = FilterConfig(**cand["config"])
        contract = opt.get("contract") or {}
        total_cap = float(
            contract.get("total_capital_ntd")
            or contract.get("fixed_total_capital_ntd")
            or FIXED_TOTAL_CAPITAL_NTD
        )
        slot_cap = total_cap / max(int(cfg.n_slots), 1)
        build_kwargs: dict[str, Any] = {
            "config": cfg,
            "window_start": str(contract.get("window_start") or "2024-07-01"),
            "window_end": str(contract.get("window_end") or "2026-07-17"),
            "trader_id": str(contract.get("trader_id") or "9661"),
        }
        if oneslot:
            build_kwargs["capital_ntd"] = slot_cap
        conn = connect(args.db or DEFAULT_DB_PATH)
        try:
            payload = build_amount_candidate_trades_payload(conn, **build_kwargs)
        finally:
            conn.close()
        if payload and "champion" in payload:
            payload["champion"]["rrg_gate"] = _gate_label(cfg)
            if oneslot:
                peer = opt.get("fair_3slot_peer") or {}
                legacy = opt.get("legacy_80m_1slot_reference") or {}
                payload["meta"]["fair_compare"] = {
                    "fixed_total_capital_ntd": total_cap,
                    "oneslot_full_return_pct": (cand.get("metrics") or {})
                    .get("full", {})
                    .get("period_return_pct"),
                    "oneslot_oos_return_pct": (cand.get("metrics") or {})
                    .get("oos", {})
                    .get("period_return_pct"),
                    "fair_3slot_full_return_pct": (peer.get("metrics") or {})
                    .get("full", {})
                    .get("period_return_pct"),
                    "fair_3slot_oos_return_pct": (peer.get("metrics") or {})
                    .get("oos", {})
                    .get("period_return_pct"),
                    "legacy_config_id": legacy.get("config_id"),
                }
        src_name = opt_path.name
        trades_stem = (
            "20260718_amount_80m_rs100_1slot_champion_trades.json"
            if oneslot
            else "20260718_amount_100m_w20_3slot_champion_trades.json"
        )
        trades_out = OUT_DIR / trades_stem
        trades_out.parent.mkdir(parents=True, exist_ok=True)
        trades_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {trades_out}")
        print(
            f"  legs={payload['meta']['n_trade_legs']} "
            f"pnl={payload['meta']['sum_pnl_ntd']} "
            f"capture={payload['meta']['signal_capture_pct']}"
        )
        out = out or (
            OUT_DIR / "20260718_amount_80m_rs100_1slot_champion.html"
            if oneslot
            else OUT_DIR / "20260718_amount_100m_w20_3slot_champion.html"
        )
    else:
        src = args.trades or _latest_trades(OUT_DIR)
        if not src or not src.exists():
            print(f"ERROR: no champion trades JSON under {OUT_DIR}", file=sys.stderr)
            return 2
        payload = json.loads(src.read_text(encoding="utf-8"))
        src_name = src.name
        out = out or OUT_DIR / "20260718_amount_rrg_champion.html"

    assert payload is not None and out is not None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(payload, source_name=src_name), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
