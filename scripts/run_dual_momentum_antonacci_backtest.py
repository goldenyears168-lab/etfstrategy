#!/usr/bin/env python3
"""Gary Antonacci 雙動能 · TW ETF 回測演示（§2.3 TradingView 對照）。"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.dual_momentum_antonacci import (  # noqa: E402
    DEFAULT_RISK_OFF_EXPOSURE,
    DEFAULT_RF_ANNUAL,
    DualMomentumResult,
    run_all_scenarios,
)
from stock_db import PROJECT_ROOT  # noqa: E402

from report_paths import RESEARCH_BREADTH  # noqa: E402

REPORTS = RESEARCH_BREADTH

SCORECARD = """
| 維度 | 評分 | 說明 |
|---|---:|---|
| A 學術支持 | 4/5 | Moskowitz、Antonacci 文獻扎实 |
| B 粒度適配 | 4/5 | 指數層有效；個股 VCP / flow 太粗 |
| C 頻率穩定 | 5/5 | 月頻／雙月頻，極穩 |
| D 任務細分 | 3/5 | 僅 risk-on/off，無任務細分 |
| E 可複製性 | 5/5 | 規則透明、全球可複製 |

**結論：** 納入 **絕對動能熔斷**（0050 12M < 0 → 全系降檔），**不當主 GPS**。
"""


def _fmt_pct(v: float) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def _svg_equity_curves(results: list[DualMomentumResult], *, width: int = 920, height: int = 320) -> str:
    colors = {
        "buy_hold_0050": "#888888",
        "gem_dual_momentum": "#2E79B5",
        "abs_only_bond_switch": "#7B64B8",
        "abs_circuit_breaker": "#52B896",
    }
    series: dict[str, pd.Series] = {}
    for r in results:
        eq = (1.0 + r.daily["strategy_return"]).cumprod()
        series[r.strategy] = (eq - 1.0) * 100.0

    all_vals = pd.concat(series.values())
    y_min = float(all_vals.min())
    y_max = float(all_vals.max())
    pad = max((y_max - y_min) * 0.08, 2.0)
    y_lo, y_hi = y_min - pad, y_max + pad
    n = len(next(iter(series.values())))
    if n < 2:
        return "<p>資料不足</p>"

    def x_at(i: int) -> float:
        return 62 + (834 * i / (n - 1))

    def y_at(v: float) -> float:
        return 50 + (200 * (y_hi - v) / (y_hi - y_lo))

    lines: list[str] = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" fill="#181818"/>',
        '<text x="62" y="18" fill="#eee" font-size="13" font-weight="600">'
        "策略累積報酬 % · 月末再平衡 · 0050 基準灰線</text>",
        f'<line x1="62" y1="{y_at(0):.1f}" x2="896" y2="{y_at(0):.1f}" stroke="#444" stroke-width="1"/>',
        f'<line x1="62" y1="50" x2="62" y2="250" stroke="#555"/>',
        f'<line x1="62" y1="250" x2="896" y2="250" stroke="#555"/>',
    ]

    bench = (1.0 + results[0].daily["bench_return"]).cumprod()
    bench_pct = (bench - 1.0) * 100.0
    pts_b = " ".join(f"{x_at(i):.1f},{y_at(float(bench_pct.iloc[i])):.1f}" for i in range(n))
    lines.append(
        f'<polyline fill="none" stroke="#666" stroke-width="1.5" stroke-dasharray="4 3" points="{pts_b}"/>'
    )

    for sid, s in series.items():
        c = colors.get(sid, "#ccc")
        pts = " ".join(f"{x_at(i):.1f},{y_at(float(s.iloc[i])):.1f}" for i in range(n))
        label = next(r.label for r in results if r.strategy == sid)
        lines.append(
            f'<polyline fill="none" stroke="{c}" stroke-width="2.2" points="{pts}"/>'
            f'<text x="700" y="{30 + list(series.keys()).index(sid) * 14}" fill="{c}" font-size="11">{html.escape(label)}</text>'
        )

    lines.append("</svg>")
    return "\n".join(lines)


def _svg_drawdown(result: DualMomentumResult) -> str:
    eq = (1.0 + result.daily["strategy_return"]).cumprod()
    dd = (eq / eq.cummax() - 1.0) * 100.0
    n = len(dd)
    y_lo = float(dd.min()) - 2
    pts = []
    for i, v in enumerate(dd):
        x = 62 + 834 * i / max(n - 1, 1)
        y = 50 + 180 * (0 - v) / max(abs(y_lo), 1)
        pts.append(f"{x:.1f},{y:.1f}")
    return f"""<svg width="920" height="260" viewBox="0 0 920 260">
<rect width="100%" fill="#181818"/>
<text x="62" y="18" fill="#eee" font-size="13" font-weight="600">回撤 % · {html.escape(result.label)}</text>
<polyline fill="none" stroke="#C85898" stroke-width="2" points="{' '.join(pts)}"/>
<line x1="62" y1="50" x2="896" y2="50" stroke="#555"/>
<text x="56" y="54" text-anchor="end" fill="#999" font-size="10">0%</text>
</svg>"""


def _to_markdown(summary: pd.DataFrame, results: list[DualMomentumResult]) -> str:
    cb = next(r for r in results if r.strategy == "abs_circuit_breaker")
    sig = cb.latest_signal
    lines = [
        "# Gary Antonacci 雙動能 · TW 回測演示（§2.3）",
        "",
        "> TradingView 參考：`vendor/tradingview/` · 資料：FinMind TaiwanStockPriceAdj（還原價）",
        f"> 回測區間：**{cb.start_date}** ～ **{cb.end_date}** · 月頻再平衡 · 12M=252 交易日",
        "",
        "## §2.3 評分卡",
        "",
        SCORECARD.strip(),
        "",
        "## 績效摘要（vs 0050 Buy & Hold）",
        "",
        "| 策略 | 累積 | CAGR | Sharpe | MDD | Calmar | 超額 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        s = r.stats
        lines.append(
            f"| {r.label} | {s['total_return_pct']:+.2f}% | {s['cagr_pct']:.2f}% | "
            f"{s['sharpe']:.2f} | {s['max_drawdown_pct']:.2f}% | {s['calmar']:.2f} | "
            f"{s['excess_return_pct']:+.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 最新絕對動能熔斷訊號（0050）",
            "",
            f"- 訊號日：{sig.get('as_of')}",
            f"- 0050 12M：{sig.get('0050_12m_pct')}%",
            f"- 姿態：**{sig.get('recommended_posture')}** · {sig.get('note')}",
            f"- 建議曝險：**{float(sig.get('exposure', 0)):.0%}**",
            "",
            "詳細圖表見同名 `.html`。",
        ]
    )
    return "\n".join(lines) + "\n"


def _to_html(summary: pd.DataFrame, results: list[DualMomentumResult]) -> str:
    cb = next(r for r in results if r.strategy == "abs_circuit_breaker")
    sig = cb.latest_signal
    svg_eq = _svg_equity_curves(results)
    svg_dd = _svg_drawdown(cb)

    stat_cards = ""
    for r in results:
        s = r.stats
        stat_cards += f"""
<div class="stat"><div class="k">{html.escape(r.label)}</div>
<div class="v">{_fmt_pct(float(s['total_return_pct']))}</div>
<div class="sub">Sharpe {s['sharpe']} · MDD {s['max_drawdown_pct']}%</div></div>"""

    reb_rows = ""
    for row in cb.rebalances[-12:]:
        reb_rows += (
            f"<tr><td>{row.signal_date}</td><td>{row.mom_0050_12m_pct}%</td>"
            f"<td>{row.asset}</td><td>{row.exposure:.0%}</td><td>{html.escape(row.note)}</td></tr>"
        )

    posture_color = "#52B896" if sig.get("abs_momentum_on") else "#C85898"
    posture_label = "Risk-On · 全系正常" if sig.get("abs_momentum_on") else "De-Risk · 熔斷降檔"

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gary Antonacci 雙動能 · TW 回測 §2.3</title>
<style>
body {{ margin:0; background:#111; color:#e8e8e8; font-family:-apple-system,"PingFang TC",sans-serif; line-height:1.55; }}
.wrap {{ max-width:980px; margin:0 auto; padding:20px 16px 40px; }}
h1 {{ font-size:22px; margin:0 0 8px; }}
.lead {{ color:#aaa; font-size:14px; margin-bottom:20px; }}
.panel {{ background:#181818; border:1px solid #333; border-radius:10px; padding:12px; margin-bottom:16px; overflow-x:auto; }}
.today-card {{ border-radius:10px; padding:16px 18px; margin-bottom:16px; border:1px solid #333; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }}
.stat {{ background:#222; border:1px solid #333; border-radius:8px; padding:10px; }}
.stat .k {{ color:#999; font-size:11px; }}
.stat .v {{ font-size:18px; font-weight:600; margin-top:4px; }}
.stat .sub {{ color:#777; font-size:11px; margin-top:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ border-bottom:1px solid #333; padding:8px 10px; text-align:left; }}
th {{ color:#aaa; }}
.score td:nth-child(2) {{ text-align:center; }}
.disclaimer {{ font-size:12px; color:#666; margin-top:24px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>Gary Antonacci 雙動能 · TW 回測演示</h1>
<p class="lead">
TradingView §2.3 · 絕對＋相對動能 · 資料 FinMind（0050 / 00646 / 00720B）<br/>
區間 {cb.start_date} ～ {cb.end_date} · 月頻再平衡 · 12M lookback = 252 交易日
</p>

<div class="today-card" style="border-color:{posture_color}55;background:{posture_color}18;">
<div style="font-size:12px;color:#999;">最新熔斷訊號 · {sig.get('as_of')}</div>
<div style="font-size:20px;font-weight:700;margin:6px 0;color:{posture_color};">{posture_label}</div>
<p style="font-size:13px;color:#bbb;margin:0;">
0050 12M = <b>{sig.get('0050_12m_pct')}%</b> · 建議曝險 <b>{float(sig.get('exposure', 0)):.0%}</b> · {html.escape(str(sig.get('note', '')))}
</p>
</div>

<div class="panel">
<h2 style="font-size:15px;margin:0 0 10px;color:#ddd;">§2.3 評分卡</h2>
<table class="score">
<tr><th>維度</th><th>評分</th><th>說明</th></tr>
<tr><td>A 學術支持</td><td>4/5</td><td>Moskowitz、Antonacci</td></tr>
<tr><td>B 粒度適配</td><td>4/5</td><td>指數層有效；個股 VCP/flow 太粗</td></tr>
<tr><td>C 頻率穩定</td><td>5/5</td><td>月頻/雙月頻，極穩</td></tr>
<tr><td>D 任務細分</td><td>3/5</td><td>僅 risk-on/off</td></tr>
<tr><td>E 可複製性</td><td>5/5</td><td>規則透明、全球可複製</td></tr>
</table>
<p style="color:#52B896;font-size:13px;margin:12px 0 0;">
<b>結論：</b>納入絕對動能熔斷（0050 12M &lt; 0 → 全系降檔），不當主 GPS。
</p>
</div>

<div class="panel"><div class="grid">{stat_cards}</div></div>
<div class="panel">{svg_eq}</div>
<div class="panel">{svg_dd}</div>

<div class="panel">
<h2 style="font-size:15px;margin:0 0 10px;color:#ddd;">熔斷策略 · 最近 12 次月末訊號</h2>
<table>
<tr><th>訊號日</th><th>0050 12M</th><th>資產</th><th>曝險</th><th>說明</th></tr>
{reb_rows}
</table>
</div>

<p class="disclaimer">
研究用途 · 未含交易成本/滑價/匯率 · GEM 全策略供對照；實務採熔斷 overlay 而非主 GPS。<br/>
Pine 參考：<code>vendor/tradingview/classic_dual_momentum_12m_antonacci.pine</code>
</p>
</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Antonacci dual momentum TW backtest demo")
    parser.add_argument("--start", default="2012-01-01", help="資料下載起始")
    parser.add_argument(
        "--backtest-start",
        default=None,
        help="回測起始（預設=三 ETF 對齊後第一個可算 12M 動能日）",
    )
    parser.add_argument("--end", default=None, help="YYYY-MM-DD，預設今天")
    parser.add_argument("--rf-annual", type=float, default=DEFAULT_RF_ANNUAL)
    parser.add_argument("--risk-off-exposure", type=float, default=DEFAULT_RISK_OFF_EXPOSURE)
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--out-html", type=Path, default=None)
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start)
    stamp = pd.Timestamp.today().strftime("%Y%m%d")
    base = f"{stamp}_dual_momentum_antonacci_tw"
    out_md = args.out_md or REPORTS / f"{base}.md"
    out_html = args.out_html or RESEARCH_BREADTH / f"{base}.html"

    summary, results = run_all_scenarios(
        start=start,
        end=end,
        backtest_start=args.backtest_start,
        rf_annual=args.rf_annual,
        risk_off_exposure=args.risk_off_exposure,
    )

    REPORTS.mkdir(parents=True, exist_ok=True)
    RESEARCH_BREADTH.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_to_markdown(summary, results), encoding="utf-8")
    out_html.write_text(_to_html(summary, results), encoding="utf-8")

    print(summary.to_string(index=False))
    print()
    cb = next(r for r in results if r.strategy == "abs_circuit_breaker")
    print("Latest circuit breaker:", cb.latest_signal)
    print(f"Wrote {out_md}")
    print(f"Wrote {out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
