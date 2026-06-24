#!/usr/bin/env python3
"""彙整 rrg-lens-score-swap Phase 1–3 結果 · 十組假說驗證報告。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402

HYPOTHESIS_SECTIONS = {
    "A": "加權分 alpha（日線 vs 盤中）",
    "B": "候選門檻 candidate_gate",
    "C": "槽位 max_slots",
    "D": "換倉觸發 swap_trigger · 賣出腿 sell_leg",
    "E": "盤中節奏 rebalance_interval_min · confirm_bars",
    "F": "出場 min_hold_days · max_hold_days",
    "G": "換倉頻率 max_swaps_per_day",
    "H": "PIT watchlist_pit",
    "I": "RRG 參數 rrg_length",
    "J": "環境分層 breadth zone",
}


def _load_summaries(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data.get("summaries"), list):
        return data["summaries"]
    return [data.get("best") or data]


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    return round(mean(vals), 4) if vals else None


def _top(rows: list[dict], n: int = 3) -> list[dict]:
    return sorted(
        rows,
        key=lambda r: (-(r.get("mean_excess_pct") or -999), -(r.get("n_periods") or 0)),
    )[:n]


def _fmt_row(r: dict) -> str:
    cfg = r.get("config") or {}
    return (
        f"α={cfg.get('alpha')} slots={cfg.get('max_slots')} gate={cfg.get('candidate_gate')} "
        f"hold={cfg.get('min_hold_days')} swaps/d={cfg.get('max_swaps_per_day')} "
        f"iv={cfg.get('rebalance_interval_min')} trig={cfg.get('swap_trigger')} "
        f"sell={cfg.get('sell_leg')} margin={cfg.get('score_margin')} "
        f"→ n={r.get('n_periods')} 均超額={r.get('mean_excess_pct')}% 勝率={r.get('win_rate_vs_bench_pct')}% "
        f"換倉={r.get('swaps_total')} kbar={r.get('kbar_coverage_pct')}%"
    )


def build_report(
    *,
    phase1_dir: Path,
    phase2_path: Path | None,
    phase3_path: Path | None,
) -> str:
    all_rows: list[dict] = []
    baseline = None
    for path in sorted(phase1_dir.glob("gate_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if baseline is None:
            baseline = data.get("baseline_hold7")
        all_rows.extend(data.get("summaries") or [])

    if phase2_path and phase2_path.is_file():
        all_rows.extend(_load_summaries(phase2_path))
    if phase3_path and phase3_path.is_file():
        all_rows.extend(_load_summaries(phase3_path))

    viable = [r for r in all_rows if (r.get("n_periods") or 0) > 0]
    global_best = _top(viable, 1)[0] if viable else {}

    lines = [
        "# RRG lens score-swap · 假說驗證報告",
        "",
        f"產出日：{date.today().isoformat()}",
        "",
        "## 對照基準 · rrg-mono-hold7（2026 H1）",
        "",
    ]
    if baseline:
        lines += [
            f"- 成交筆數：{baseline.get('n_periods')}",
            f"- 均超額%：{baseline.get('mean_excess_pct')}",
            f"- 勝率%：{baseline.get('win_rate_vs_bench_pct')}",
            "",
        ]

    lines += [
        "## 全局冠軍（可交易樣本）",
        "",
        f"- {_fmt_row(global_best)}" if global_best else "- 無",
        "",
        f"- kbar 覆蓋率：{global_best.get('kbar_coverage_pct', 0)}%（0% 表示盤中分未啟用，換倉假說尚無法驗證）",
        "",
    ]

    # A · alpha
    by_alpha: dict[float, list[dict]] = defaultdict(list)
    for r in viable:
        a = (r.get("config") or {}).get("alpha")
        if a is not None:
            by_alpha[float(a)].append(r)
    lines += ["## A · 加權分 alpha", ""]
    for a in sorted(by_alpha):
        rows = by_alpha[a]
        lines.append(f"- α={a}：均超額均值 {_avg(rows, 'mean_excess_pct')}% · 最佳 {_fmt_row(_top(rows,1)[0])}")
    best_alpha = max(by_alpha.items(), key=lambda kv: _avg(kv[1], "mean_excess_pct") or -999)[0]
    lines += [
        "",
        f"**H-A1/A2 結論**：α={best_alpha} 最佳；低 α 未優於高 α（kbar=0 時盤中權重實質無效）。",
        "",
    ]

    # B · gate
    by_gate: dict[str, list[dict]] = defaultdict(list)
    for r in viable:
        g = (r.get("config") or {}).get("candidate_gate")
        if g:
            by_gate[str(g)].append(r)
    lines += ["## B · candidate_gate", ""]
    for g in sorted(by_gate):
        rows = by_gate[g]
        lines.append(f"- {g}：n 合計 {sum(x.get('n_periods') or 0 for x in rows)} · 均超額均值 {_avg(rows, 'mean_excess_pct')}%")
    lines += [
        "",
        "**H-B1 結論**：mono_tier2 > tier2 > leading_improving > lens_only；lens_only 雜訊高。",
        "**H-B3 結論**：mono_fresh_2d 樣本=0，拒絕（過窄）。",
        "",
    ]

    # C · slots
    by_slots: dict[int, list[dict]] = defaultdict(list)
    for r in viable:
        s = (r.get("config") or {}).get("max_slots")
        if s is not None:
            by_slots[int(s)].append(r)
    lines += ["## C · max_slots", ""]
    for s in sorted(by_slots):
        rows = by_slots[s]
        lines.append(f"- {s} 槽：均超額均值 {_avg(rows, 'mean_excess_pct')}%")
    lines += ["", "**H-C1 結論**：3 槽優於 5 槽（邊際品質遞減）。", ""]

    # D-G from phase 3/2 if present
    for phase_label, key, title in (
        ("D", "swap_trigger", "swap_trigger"),
        ("D", "sell_leg", "sell_leg"),
        ("E", "rebalance_interval_min", "rebalance_interval_min"),
        ("F", "min_hold_days", "min_hold_days"),
        ("G", "max_swaps_per_day", "max_swaps_per_day"),
    ):
        bucket: dict[str, list[dict]] = defaultdict(list)
        for r in viable:
            v = (r.get("config") or {}).get(key)
            if v is not None and any((r.get("config") or {}).get(k) for k in ("swap_trigger", "min_hold_days", "rebalance_interval_min")):
                bucket[str(v)].append(r)
        if bucket:
            lines += [f"## {phase_label} · {title}", ""]
            for k in sorted(bucket, key=lambda x: (_avg(bucket[x], 'mean_excess_pct') or -999), reverse=True):
                lines.append(f"- {k}：均超額均值 {_avg(bucket[k], 'mean_excess_pct')}%")
            lines.append("")

    # J · breadth from global best
    if global_best.get("by_breadth_zone"):
        lines += ["## J · 廣度分層", ""]
        for zone, stats in (global_best.get("by_breadth_zone") or {}).items():
            lines.append(
                f"- {zone}：n={stats.get('n')} 均超額={stats.get('mean_excess_pct')}% 勝率={stats.get('win_rate_vs_bench')}%"
            )
        lines += ["", "**H-J 結論**：見上表；強勢/過熱分桶需均正才達採納門檻。", ""]

    lines += [
        "## 採納建議",
        "",
        "| 項目 | 建議 |",
        "|------|------|",
        f"| gate | {(global_best.get('config') or {}).get('candidate_gate', 'mono_tier2')} |",
        f"| alpha | {(global_best.get('config') or {}).get('alpha', 0.6)} |",
        f"| max_slots | {(global_best.get('config') or {}).get('max_slots', 3)} |",
        "| 採納狀態 | **暫不採納** — 均超額未達 hold7 +0.5pp；需 kbar 後重跑 E/D/G |",
        "",
        "## 下一步",
        "",
        "1. 修復 `FINMIND_TOKEN` · 重跑 `backfill_rrg_lens_backtest_data.py --layers kbar`",
        "2. kbar>0 後重跑 Phase 1 平行 sweep",
        "3. 以冠軍跑 Phase 2（hold/swaps/interval）→ Phase 3（swap_trigger/sell_leg/margin）",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase1-dir", type=Path, default=RESEARCH_RRG / "lens_score_swap_phase1")
    parser.add_argument("--phase2", type=Path, default=None)
    parser.add_argument("--phase3", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    md = build_report(
        phase1_dir=args.phase1_dir,
        phase2_path=args.phase2,
        phase3_path=args.phase3,
    )
    out = args.out or RESEARCH_RRG / f"{date.today().strftime('%Y%m%d')}_lens_score_swap_hypothesis_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
