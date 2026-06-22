#!/usr/bin/env python3
"""s04 月頻 vs 日頻訊號 · H9 持有 · 統計檢定。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.copytrade_backtest import compute_excess_significance  # noqa: E402
from research.backtest.finpilot_s04_layers import (  # noqa: E402
    S04_LAYER_SPECS,
    run_s04_daily_periods,
    run_s04_layer_periods,
    summarize_periods,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _compare_two_samples(
    monthly: list[dict],
    daily: list[dict],
    *,
    label_a: str = "月頻",
    label_b: str = "日頻",
) -> dict:
    ex_m = [p["excess_pct"] for p in monthly]
    ex_d = [p["excess_pct"] for p in daily]
    beat_m = sum(1 for p in monthly if p["beat_bench"])
    beat_d = sum(1 for p in daily if p["beat_bench"])
    n_m, n_d = len(monthly), len(daily)

    out: dict = {
        "n_monthly": n_m,
        "n_daily": n_d,
        "monthly_summary": summarize_periods(monthly),
        "daily_summary": summarize_periods(daily),
        "monthly_excess_sig": compute_excess_significance(
            _as_day_results(monthly)
        ),
        "daily_excess_sig": compute_excess_significance(_as_day_results(daily)),
    }

    if n_m < 3 or n_d < 3:
        out["tests"] = {"note": "樣本不足，略過兩組檢定"}
        return out

    try:
        from scipy.stats import mannwhitneyu, ttest_ind

        t_stat, p_t = ttest_ind(ex_m, ex_d, equal_var=False)
        u_stat, p_u = mannwhitneyu(ex_m, ex_d, alternative="two-sided")
        out["tests"] = {
            "excess_ttest_ind": {
                "t_stat": round(float(t_stat), 4),
                "p_value": round(float(p_t), 4),
                "interpretation": (
                    f"{'顯著' if p_t < 0.05 else '不顯著'}（α=0.05）"
                    f"：{label_a} vs {label_b} 平均超額報酬"
                ),
            },
            "excess_mannwhitney": {
                "u_stat": round(float(u_stat), 2),
                "p_value": round(float(p_u), 4),
                "interpretation": (
                    f"{'顯著' if p_u < 0.05 else '不顯著'}（α=0.05）"
                    f"：{label_a} vs {label_b} 超額分布"
                ),
            },
        }
    except Exception as exc:
        out["tests"] = {"error": str(exc)}

    # 勝率兩比例 z 檢定
    try:
        from statsmodels.stats.proportion import proportions_ztest

        count = [beat_m, beat_d]
        nobs = [n_m, n_d]
        z_stat, p_z = proportions_ztest(count, nobs)
        out["tests"]["win_rate_ztest"] = {
            "z_stat": round(float(z_stat), 4),
            "p_value": round(float(p_z), 4),
            "monthly_wr_pct": round(beat_m / n_m * 100, 2),
            "daily_wr_pct": round(beat_d / n_d * 100, 2),
            "interpretation": (
                f"{'顯著' if p_z < 0.05 else '不顯著'}（α=0.05）"
                f"：勝率 {label_a} vs {label_b}"
            ),
        }
    except Exception:
        # fallback without statsmodels
        p_pool = (beat_m + beat_d) / (n_m + n_d)
        se = (p_pool * (1 - p_pool) * (1 / n_m + 1 / n_d)) ** 0.5
        p_m = beat_m / n_m
        p_d = beat_d / n_d
        z = (p_m - p_d) / se if se > 0 else 0.0
        out["tests"]["win_rate_ztest"] = {
            "z_stat": round(z, 4),
            "p_value": None,
            "monthly_wr_pct": round(p_m * 100, 2),
            "daily_wr_pct": round(p_d * 100, 2),
            "interpretation": "近似 z（未安裝 statsmodels）",
        }

    return out


def _as_day_results(periods: list[dict]):
    """餵給 compute_excess_significance 的簡化物件。"""
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            status="complete",
            return_pct=p["return_pct"],
            bench_return_pct=p["bench_return_pct"],
        )
        for p in periods
    ]


def format_report(
    full: dict,
    overlap: dict,
    *,
    hold_days: int,
) -> str:
    lines = [
        "# s04 月頻 vs 日頻訊號 · H9 統計比較",
        "",
        "> 選股規則相同：Mom60 Top30 → ROE>0 → 等權 · T+1 開盤進 H9 收盤出",
        "> **月頻**：僅每月最後交易日訊號",
        "> **日頻**：每個交易日訊號（H9 窗可重疊，樣本非獨立）",
        f"> 持有：**H{hold_days}** · 基準 IX0001",
        "",
        "## 全樣本摘要",
        "",
        "| 頻率 | n | 勝率% | 勝率（毛） | 均超額% | excess t p | Wilcoxon p |",
        "|------|---|---------|-----------|---------|------------|------------|",
    ]
    ms = full["monthly_summary"]
    ds = full["daily_summary"]
    m_sig = full["monthly_excess_sig"]
    d_sig = full["daily_excess_sig"]
    lines.append(
        f"| 月頻 | {ms['n_periods']} | {ms['win_rate_vs_bench_pct']}% | "
        f"{ms['win_rate_gross_pct']}% | "
        f"{m_sig.get('mean_excess_pct', '—')} | {m_sig.get('p_value_ttest', '—')} | "
        f"{m_sig.get('p_value_wilcoxon', '—')} |"
    )
    lines.append(
        f"| 日頻 | {ds['n_periods']} | {ds['win_rate_vs_bench_pct']}% | "
        f"{ds['win_rate_gross_pct']}% | "
        f"{d_sig.get('mean_excess_pct', '—')} | {d_sig.get('p_value_ttest', '—')} | "
        f"{d_sig.get('p_value_wilcoxon', '—')} |"
    )

    lines.extend(["", "## 月頻 vs 日頻 兩組檢定（全樣本）", ""])
    tests = full.get("tests", {})
    if "excess_ttest_ind" in tests:
        t = tests["excess_ttest_ind"]
        lines.append(
            f"- **超額報酬 Welch t 檢定**：t={t['t_stat']}, p={t['p_value']} → {t['interpretation']}"
        )
    if "excess_mannwhitney" in tests:
        u = tests["excess_mannwhitney"]
        lines.append(
            f"- **超額 Mann–Whitney U**：U={u['u_stat']}, p={u['p_value']} → {u['interpretation']}"
        )
    if "win_rate_ztest" in tests:
        w = tests["win_rate_ztest"]
        lines.append(
            f"- **勝率 z 檢定**：月頻 {w['monthly_wr_pct']}% vs 日頻 {w['daily_wr_pct']}%"
            f", z={w['z_stat']}, p={w.get('p_value', '—')} → {w['interpretation']}"
        )

    lines.extend(["", "## L1 重疊區（2025-05-28 起）", ""])
    oms = overlap["monthly_summary"]
    ods = overlap["daily_summary"]
    lines.append(
        f"| 頻率 | n | 勝率% | 均超額% |",
    )
    lines.append(f"|------|---|---------|---------|")
    lines.append(
        f"| 月頻 | {oms['n_periods']} | {oms['win_rate_vs_bench_pct']}% | "
        f"{overlap['monthly_excess_sig'].get('mean_excess_pct', '—')} |"
    )
    lines.append(
        f"| 日頻 | {ods['n_periods']} | {ods['win_rate_vs_bench_pct']}% | "
        f"{overlap['daily_excess_sig'].get('mean_excess_pct', '—')} |"
    )
    otests = overlap.get("tests", {})
    lines.append("")
    if "excess_ttest_ind" in otests:
        t = otests["excess_ttest_ind"]
        lines.append(f"- 重疊區超額 t 檢定：p={t['p_value']} → {t['interpretation']}")
    if "win_rate_ztest" in otests:
        w = otests["win_rate_ztest"]
        lines.append(
            f"- 重疊區勝率 z 檢定：p={w.get('p_value', '—')} → {w['interpretation']}"
        )

    lines.extend(
        [
            "",
            "## 方法限制",
            "",
            "- 日頻每 9 日窗重疊，觀測值**非獨立**；兩組檢定 p 值應保守解讀。",
            "- 月頻 n 小（~80），日頻 n 大（~1500），檢定力不對稱。",
            "- 未扣交易成本；股票池為成分股聯集 ~135 檔。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_s04_freq_compare.py --write-report",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="s04 月頻 vs 日頻")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--hold-days", type=int, default=9)
    parser.add_argument(
        "--overlap-start",
        default="2025-05-28",
        help="與 L1 重疊區起始（entry_date）",
    )
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    spec = next(s for s in S04_LAYER_SPECS if s.layer_id == "L2b")
    conn = connect(args.db)

    print("月頻 s04…")
    monthly = run_s04_layer_periods(conn, spec, hold_days=args.hold_days)
    print(f"  n={len(monthly)}")

    print("日頻 s04（每交易日訊號，可能較久）…")
    daily = run_s04_daily_periods(conn, spec, hold_days=args.hold_days)
    print(f"  n={len(daily)}")

    conn.close()

    monthly_ov = [p for p in monthly if p["entry_date"] >= args.overlap_start]
    daily_ov = [p for p in daily if p["entry_date"] >= args.overlap_start]

    full = _compare_two_samples(monthly, daily)
    overlap = _compare_two_samples(monthly_ov, daily_ov, label_a="月頻", label_b="日頻")

    report = format_report(full, overlap, hold_days=args.hold_days)
    print(report)

    if args.write_report:
        out = ROOT / "reports" / f"{date.today().strftime('%Y%m%d')}_s04_monthly_vs_daily.md"
        out.write_text(report, encoding="utf-8")
        print(f"已寫入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
