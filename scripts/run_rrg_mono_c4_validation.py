#!/usr/bin/env python3
"""C4 vs hold7 A · 近窗驗證（kbar 覆蓋 · 強勢/過熱分桶）。

用法：
  PYTHONPATH=src python scripts/run_rrg_mono_c4_validation.py \\
    --date-start 2026-05-24 --date-end 2026-06-22
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_intraday_ab import run_c4_validation  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _render_md(payload: dict) -> str:
    lines = [
        "# RRG mono · C4 近窗驗證（vs hold7 A）",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        "",
        "## 全局",
        "",
        "| 腿 | n | 均超額% | kbar% | vs A (pp) |",
        "|----|---|---------|-------|-----------|",
    ]
    for vid in ("A", "C0", "C4"):
        v = payload["variants"][vid]
        s = v["summary"]
        delta = "" if vid == "A" else v.get("delta_vs_a_pp")
        lines.append(
            f"| {vid} | {s.get('n_periods')} | {s.get('mean_excess_pct')} "
            f"| {s.get('kbar_coverage_pct')} | {delta} |"
        )

    lines += [
        "",
        "## 廣度分桶（訊號日 zone_200）",
        "",
        "| 區間 | 腿 | n | 均超額% | C4 vs A (pp) | ≥0.5pp |",
        "|------|----|---|---------|--------------|--------|",
    ]
    for zone in ("strong", "overbought"):
        zc = payload["zone_compare"][zone]
        for vid in ("A", "C4"):
            b = zc[vid]
            delta = zc.get("c4_vs_a_pp") if vid == "C4" else ""
            gate = zc.get("pass_0p5pp") if vid == "C4" else ""
            lines.append(
                f"| {zc['display']} | {vid} | {b.get('n')} | {b.get('mean_excess_pct')} "
                f"| {delta} | {gate} |"
            )

    k = payload["kbar_audit"]
    lines += [
        "",
        "## Shortlist kbar 覆蓋（fresh 前十 · 進場候選）",
        "",
        f"- stock-days：{k['stock_days']} · 命中：{k['kbar_hits']} · **{k['coverage_pct']}%**",
        f"- FinMind 日：{k['finmind_stock_days']} · Yahoo 日：{k['yahoo_stock_days']}",
        f"- {k['production_note']}",
        "",
        f"採納門檻 C4 vs A ≥0.5pp · 過熱：**{payload.get('adoption_gate_0p5pp_overbought')}** · "
        f"強勢：**{payload.get('adoption_gate_0p5pp_strong')}**（n=0 則 N/A）",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C4 near-window validation")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--date-start", default="2026-05-24")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_c4_validation(
            conn, date_start=args.date_start, date_end=args.date_end
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_c4_validation_near30d.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")

    md = out.with_suffix(".md")
    md.write_text(_render_md(payload), encoding="utf-8")
    print(f"Wrote {md}")

    for vid in ("A", "C0", "C4"):
        v = payload["variants"][vid]
        print(
            f"{vid}: n={v['summary'].get('n_periods')} "
            f"excess={v['summary'].get('mean_excess_pct')}% "
            f"kbar={v['summary'].get('kbar_coverage_pct')}% "
            f"vsA={v.get('delta_vs_a_pp')}"
        )
    for zone in ("strong", "overbought"):
        zc = payload["zone_compare"][zone]
        print(
            f"zone {zone}: C4 vs A {zc.get('c4_vs_a_pp')}pp "
            f"(pass0.5={zc.get('pass_0p5pp')})"
        )
    print(f"kbar shortlist coverage: {payload['kbar_audit']['coverage_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
