"""L1-F1：訊號日 leg 數分桶 × 持有天數 H 矩陣（§11 探索假說）。"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date
from typing import Sequence

from .copytrade_backtest import _paired_significance, simulate_capital_recycling, simulate_fixed_slots
from .copytrade_regime_horizon import _wilcoxon_vs_zero, summarize_regime_sweet_spots
from stock_db import (
    load_copytrade_signal_days_for_run,
    persist_copytrade_regime_horizon,
    persist_copytrade_research_conclusions,
)

ENTRY_ROW = "L1"
BUCKET_FIELD = "leg_count"
DEFAULT_HORIZONS: tuple[int, ...] = (5, 9, 10, 15, 20, 27)
DEFAULT_MATRIX_BATCH = "00981a-copytrade-h20-20260617"
DEFAULT_EXTENDED_BATCH = "00981a-copytrade-l1h45-20260618"


def leg_count_bucket(n_legs: int) -> str:
    if n_legs == 1:
        return "1"
    if 2 <= n_legs <= 4:
        return "2-4"
    if 5 <= n_legs <= 10:
        return "5-10"
    return "11+"


def batch_for_horizon(h: int, *, matrix_batch: str, extended_batch: str) -> str:
    return matrix_batch if h <= 20 else extended_batch


def _load_l1_run_id(
    conn: sqlite3.Connection,
    h: int,
    *,
    matrix_batch: str,
    extended_batch: str,
) -> str | None:
    batch_id = batch_for_horizon(h, matrix_batch=matrix_batch, extended_batch=extended_batch)
    try:
        row = conn.execute(
            """
            SELECT run_id FROM copytrade_runs
            WHERE batch_id = ? AND strategy_id = ?
            """,
            (batch_id, f"L1H{h}"),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row["run_id"]) if row else None


def build_leg_bucket_horizon_rows(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
) -> list[dict]:
    """leg 桶 × H：從既有 L1H matrix runs 篩選訊號日（依該日 n_legs）。"""
    bucket_days: dict[str, dict[int, list[dict]]] = {}
    for h in horizons:
        run_id = _load_l1_run_id(
            conn, h, matrix_batch=matrix_batch, extended_batch=extended_batch
        )
        if not run_id:
            continue
        for d in load_copytrade_signal_days_for_run(conn, run_id):
            if d["status"] != "complete":
                continue
            bucket = leg_count_bucket(int(d["n_legs"]))
            bucket_days.setdefault(bucket, {}).setdefault(h, []).append(dict(d))

    summary_rows: list[dict] = []
    for bucket in ("1", "2-4", "5-10", "11+"):
        if bucket not in bucket_days:
            continue
        prev_total = 0.0
        for h in sorted(horizons):
            days = bucket_days[bucket].get(h, [])
            excess = [
                float(d["return_pct"] or 0) - float(d["bench_return_pct"] or 0)
                for d in days
            ]
            alphas = [float(d["alpha_ntd"] or 0) for d in days]
            total_alpha = sum(alphas)
            n = len(days)
            mean_excess = sum(excess) / n if n else None
            win_pct = (
                round(100.0 * sum(1 for a in alphas if a > 0) / n, 2) if n else None
            )
            p_w = _wilcoxon_vs_zero(excess)
            summary_rows.append(
                {
                    "etf_code": etf_code,
                    "entry_row": ENTRY_ROW,
                    "bucket_field": BUCKET_FIELD,
                    "bucket_value": bucket,
                    "horizon": h,
                    "n_signal_days": n,
                    "total_alpha_ntd": round(total_alpha, 2),
                    "mean_excess_pct": round(mean_excess, 4) if mean_excess is not None else None,
                    "win_rate_vs_bench_pct": win_pct,
                    "p_value_wilcoxon": p_w,
                    "is_significant": int(p_w is not None and p_w < 0.05),
                    "marginal_total_alpha_ntd": round(total_alpha - prev_total, 2),
                }
            )
            prev_total = total_alpha
    return summary_rows


def _day_alpha_map(
    conn: sqlite3.Connection,
    h: int,
    *,
    matrix_batch: str,
    extended_batch: str,
) -> dict[str, dict]:
    run_id = _load_l1_run_id(
        conn, h, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    if not run_id:
        return {}
    out: dict[str, dict] = {}
    for d in load_copytrade_signal_days_for_run(conn, run_id):
        if d["status"] != "complete":
            continue
        out[str(d["signal_date"])] = dict(d)
    return out


def evaluate_l1_f1(
    conn: sqlite3.Connection,
    *,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    baseline_h: int = 9,
    candidate_h: int = 20,
    target_bucket: str = "5-10",
) -> dict[str, object]:
    """L1-F1：5–10 桶延長 H 是否優於 H9（配對同日 α / 勝率）。"""
    base = _day_alpha_map(
        conn, baseline_h, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    cand = _day_alpha_map(
        conn, candidate_h, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    common = sorted(set(base) & set(cand))
    subset = [
        sd
        for sd in common
        if leg_count_bucket(int(base[sd]["n_legs"])) == target_bucket
    ]
    if len(subset) < 5:
        return {
            "hypothesis_id": "L1-F1",
            "target_bucket": target_bucket,
            "baseline_h": baseline_h,
            "candidate_h": candidate_h,
            "n_paired": len(subset),
            "verdict": "insufficient_n",
        }

    base_alphas = [float(base[sd]["alpha_ntd"]) for sd in subset]
    cand_alphas = [float(cand[sd]["alpha_ntd"]) for sd in subset]
    diffs = [c - b for c, b in zip(cand_alphas, base_alphas)]
    base_excess = [
        float(base[sd]["return_pct"]) - float(base[sd]["bench_return_pct"])
        for sd in subset
    ]
    cand_excess = [
        float(cand[sd]["return_pct"]) - float(cand[sd]["bench_return_pct"])
        for sd in subset
    ]
    paired = _paired_significance(diffs)

    base_win = sum(1 for a in base_alphas if a > 0)
    cand_win = sum(1 for a in cand_alphas if a > 0)
    n = len(subset)

    # 成功門檻（§11）：候選 H 累計 α 升且勝率升
    cum_improved = sum(cand_alphas) > sum(base_alphas)
    win_improved = cand_win > base_win
    adopted = cum_improved and win_improved and (paired.get("p_value_wilcoxon") or 1) < 0.05

    if adopted:
        verdict = "adopt_extend_h"
    elif cum_improved and win_improved:
        verdict = "explore_extend_h"
    elif cum_improved:
        verdict = "alpha_only"
    else:
        verdict = "reject_extend_h"

    return {
        "hypothesis_id": "L1-F1",
        "target_bucket": target_bucket,
        "baseline_h": baseline_h,
        "candidate_h": candidate_h,
        "n_paired": n,
        "cum_alpha_baseline": round(sum(base_alphas), 2),
        "cum_alpha_candidate": round(sum(cand_alphas), 2),
        "cum_alpha_delta": round(sum(cand_alphas) - sum(base_alphas), 2),
        "win_rate_baseline_pct": round(100.0 * base_win / n, 2),
        "win_rate_candidate_pct": round(100.0 * cand_win / n, 2),
        "win_rate_delta_pp": round(100.0 * (cand_win - base_win) / n, 2),
        "mean_excess_baseline_pct": round(sum(base_excess) / n, 4),
        "mean_excess_candidate_pct": round(sum(cand_excess) / n, 4),
        "paired_p_wilcoxon": paired.get("p_value_wilcoxon"),
        "verdict": verdict,
        "adopted": adopted,
    }


def _mann_whitney_two_sample(
    a: list[float], b: list[float], *, alternative: str = "two-sided"
) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    try:
        from scipy.stats import mannwhitneyu

        _, p = mannwhitneyu(a, b, alternative=alternative)
        return round(float(p), 4)
    except Exception:
        return None


def _kruskal_wallis(groups: list[list[float]]) -> float | None:
    valid = [g for g in groups if len(g) >= 3]
    if len(valid) < 2:
        return None
    try:
        from scipy.stats import kruskal

        _, p = kruskal(*valid)
        return round(float(p), 4)
    except Exception:
        return None


def collect_bucket_paired_diffs(
    conn: sqlite3.Connection,
    *,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    baseline_h: int = 9,
    candidate_h: int = 20,
) -> dict[str, list[dict]]:
    """各 leg 桶：同日 H_candidate − H_baseline 的 α / excess 配對差。"""
    base = _day_alpha_map(
        conn, baseline_h, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    cand = _day_alpha_map(
        conn, candidate_h, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    common = sorted(set(base) & set(cand))
    out: dict[str, list[dict]] = {b: [] for b in ("1", "2-4", "5-10", "11+")}
    for sd in common:
        bucket = leg_count_bucket(int(base[sd]["n_legs"]))
        alpha_diff = float(cand[sd]["alpha_ntd"]) - float(base[sd]["alpha_ntd"])
        excess_diff = (
            float(cand[sd]["return_pct"]) - float(cand[sd]["bench_return_pct"])
        ) - (float(base[sd]["return_pct"]) - float(base[sd]["bench_return_pct"]))
        out[bucket].append(
            {
                "signal_date": sd,
                "n_legs": int(base[sd]["n_legs"]),
                "alpha_diff_ntd": round(alpha_diff, 2),
                "excess_diff_pct": round(excess_diff, 4),
            }
        )
    return out


def evaluate_l1_h3(
    conn: sqlite3.Connection,
    *,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    baseline_h: int = 9,
    candidate_h: int = 20,
) -> dict[str, object]:
    """
    L1-H3：桶 × H 交互——延長 H 的邊際效益是否因 leg 桶而異？

    H0：各桶同日 Δα(H_candidate−H_baseline) 來自同一分布。
    Ha：至少一桶邊際不同；並檢定 5–10 的日均 Δα 是否高於 2–4。
    """
    paired = collect_bucket_paired_diffs(
        conn,
        matrix_batch=matrix_batch,
        extended_batch=extended_batch,
        baseline_h=baseline_h,
        candidate_h=candidate_h,
    )

    bucket_rows: list[dict[str, object]] = []
    alpha_groups: list[list[float]] = []
    for bucket in ("1", "2-4", "5-10", "11+"):
        rows = paired[bucket]
        alpha_diffs = [float(r["alpha_diff_ntd"]) for r in rows]
        excess_diffs = [float(r["excess_diff_pct"]) for r in rows]
        sig_a = _paired_significance(alpha_diffs)
        sig_e = _paired_significance(excess_diffs)
        bucket_rows.append(
            {
                "bucket": bucket,
                "n_paired": len(rows),
                "cum_alpha_delta": round(sum(alpha_diffs), 2) if alpha_diffs else 0.0,
                "mean_alpha_delta_ntd": sig_a.get("mean_diff"),
                "median_alpha_delta_ntd": (
                    round(statistics.median(alpha_diffs), 2) if alpha_diffs else None
                ),
                "mean_excess_delta_pct": sig_e.get("mean_diff"),
                "p_wilcoxon_alpha_vs_0": sig_a.get("p_value_wilcoxon"),
                "p_wilcoxon_excess_vs_0": sig_e.get("p_value_wilcoxon"),
            }
        )
        if len(alpha_diffs) >= 3:
            alpha_groups.append(alpha_diffs)

    kw_alpha = _kruskal_wallis(alpha_groups)
    excess_groups = [
        [float(r["excess_diff_pct"]) for r in paired[b]]
        for b in ("1", "2-4", "5-10", "11+")
        if len(paired[b]) >= 3
    ]
    kw_excess = _kruskal_wallis(excess_groups)

    def _pairwise(ref: str, other: str) -> dict[str, object]:
        a = [float(r["alpha_diff_ntd"]) for r in paired[ref]]
        b = [float(r["alpha_diff_ntd"]) for r in paired[other]]
        return {
            "ref": ref,
            "other": other,
            "n_ref": len(a),
            "n_other": len(b),
            "mean_ref": round(sum(a) / len(a), 2) if a else None,
            "mean_other": round(sum(b) / len(b), 2) if b else None,
            "mean_diff_ref_minus_other": (
                round(sum(a) / len(a) - sum(b) / len(b), 2) if a and b else None
            ),
            "p_mann_whitney_two_sided": _mann_whitney_two_sample(a, b),
            "p_mann_whitney_greater": _mann_whitney_two_sample(a, b, alternative="greater"),
        }

    contrasts = [
        _pairwise("5-10", "2-4"),
        _pairwise("5-10", "11+"),
        _pairwise("5-10", "1"),
        _pairwise("2-4", "11+"),
    ]

    row_510 = next(r for r in bucket_rows if r["bucket"] == "5-10")
    base = _day_alpha_map(
        conn, baseline_h, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    ex_510 = [
        float(base[sd]["return_pct"]) - float(base[sd]["bench_return_pct"])
        for sd in base
        if leg_count_bucket(int(base[sd]["n_legs"])) == "5-10"
    ]
    ex_24 = [
        float(base[sd]["return_pct"]) - float(base[sd]["bench_return_pct"])
        for sd in base
        if leg_count_bucket(int(base[sd]["n_legs"])) == "2-4"
    ]
    h9_510_excess = round(sum(ex_510) / len(ex_510), 4) if ex_510 else None
    h9_24_excess = round(sum(ex_24) / len(ex_24), 4) if ex_24 else None

    interaction_supported = kw_alpha is not None and kw_alpha < 0.05
    urgent_510 = (
        h9_510_excess is not None
        and h9_510_excess <= 0.1
        and row_510.get("p_wilcoxon_alpha_vs_0") is not None
        and float(row_510["p_wilcoxon_alpha_vs_0"]) < 0.05
    )
    contrast_510_vs_24 = next(c for c in contrasts if c["ref"] == "5-10" and c["other"] == "2-4")
    higher_daily_510 = (
        contrast_510_vs_24.get("mean_diff_ref_minus_other") is not None
        and float(contrast_510_vs_24["mean_diff_ref_minus_other"]) > 0
        and contrast_510_vs_24.get("p_mann_whitney_greater") is not None
        and float(contrast_510_vs_24["p_mann_whitney_greater"]) < 0.05
    )

    if interaction_supported and urgent_510:
        verdict = "interaction_5_10_needs_extension"
    elif interaction_supported:
        verdict = "interaction_supported"
    elif urgent_510:
        verdict = "5_10_marginal_only"
    else:
        verdict = "no_clear_interaction"

    return {
        "hypothesis_id": "L1-H3",
        "baseline_h": baseline_h,
        "candidate_h": candidate_h,
        "bucket_rows": bucket_rows,
        "kruskal_wallis_alpha_p": kw_alpha,
        "kruskal_wallis_excess_p": kw_excess,
        "pairwise_contrasts": contrasts,
        "h9_mean_excess_5_10_pct": h9_510_excess,
        "h9_mean_excess_2_4_pct": h9_24_excess,
        "verdict": verdict,
        "interaction_supported": interaction_supported,
        "urgent_5_10_extend": urgent_510,
        "higher_daily_delta_5_10_vs_2_4": higher_daily_510,
    }


def format_l1_h3_markdown(result: dict[str, object]) -> str:
    lines = [
        "# 00981A L1-H3 · Leg 桶 × H 交互檢定",
        "",
        f"> 配對：H{result['candidate_h']} − H{result['baseline_h']}（同日訊號 · α NTD 與 excess%）",
        f"> matrix `{DEFAULT_MATRIX_BATCH}` · extended `{DEFAULT_EXTENDED_BATCH}`",
        "",
        "## 假說",
        "",
        "- **H0**：延長 H 的邊際效益（Δα）在各 leg 桶間無差異。",
        "- **Ha**：桶 × H 存在交互——至少一桶的 Δ(H20−H9) 分布不同。",
        "- **子假說**：5–10 桶 H9 近乎無 excess，但延長 H 的日均 Δα 不亞於（甚至高於）2–4。",
        "",
        f"**判決**：`{result['verdict']}`",
        "",
        "| 檢定 | p |",
        "|------|---|",
        f"| Kruskal–Wallis（各桶 Δα） | {result.get('kruskal_wallis_alpha_p')} |",
        f"| Kruskal–Wallis（各桶 Δexcess%） | {result.get('kruskal_wallis_excess_p')} |",
        "",
        "## 各桶配對 Δ(H20−H9)",
        "",
        "| 桶 | n | cum Δα | 日均 Δα | 中位 Δα | 日均 Δexcess% | p(W) vs 0 |",
        "|----|---|--------|---------|---------|---------------|-----------|",
    ]
    for row in result["bucket_rows"]:
        lines.append(
            f"| {row['bucket']} | {row['n_paired']} | {row['cum_alpha_delta']:+,.0f} | "
            f"{row['mean_alpha_delta_ntd']} | {row['median_alpha_delta_ntd']} | "
            f"{row['mean_excess_delta_pct']} | {row['p_wilcoxon_alpha_vs_0']} |"
        )

    lines.extend(
        [
            "",
            f"H9 基準 mean excess%：**5–10 = {result.get('h9_mean_excess_5_10_pct')}%** · "
            f"**2–4 = {result.get('h9_mean_excess_2_4_pct')}%**",
            "",
            "## 桶間對照（Δα 分布 · Mann–Whitney）",
            "",
            "| A | B | mean Δα A | mean Δα B | A−B | p(雙尾) | p(A>B) |",
            "|---|---|-----------|-----------|-----|---------|--------|",
        ]
    )
    for c in result["pairwise_contrasts"]:
        lines.append(
            f"| {c['ref']} | {c['other']} | {c['mean_ref']} | {c['mean_other']} | "
            f"{c['mean_diff_ref_minus_other']} | {c['p_mann_whitney_two_sided']} | "
            f"{c['p_mann_whitney_greater']} |"
        )

    flags = [
        f"- 全局交互（KW α）：{'✅' if result.get('interaction_supported') else '❌'}",
        f"- 5–10 H9 近零但延長 H 顯著：{'✅' if result.get('urgent_5_10_extend') else '❌'}",
        f"- 5–10 日均 Δα > 2–4（單尾）：{'✅' if result.get('higher_daily_delta_5_10_vs_2_4') else '❌'}",
    ]
    lines.extend(["", "## 旗標", ""] + flags)
    lines.extend(
        [
            "",
            "## 解讀",
            "",
            "- **交互成立** → 應採 **分桶定 H**（P3），不宜全局統一 H9 或 H20。",
            "- 若 5–10 日均 Δα 高於 2–4：代表「延長 H」對 5–10 **單日邊際**更大，",
            "  雖然 2–4 累計 α 仍靠樣本數取勝。",
            "- KW 不顯著時，延長 H 可能為 **全局牛市長持效應**（見 L1-H1）。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \\",
            "  --analyze-l1-h3 --write-report",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def run_l1_h3_study(
    conn: sqlite3.Connection,
    *,
    etf_code: str = "00981A",
    batch_id: str | None = None,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    persist: bool = True,
) -> dict[str, object]:
    study_batch = batch_id or f"{etf_code.lower()}-l1h3-{date.today().strftime('%Y%m%d')}"
    result = evaluate_l1_h3(
        conn, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    if persist:
        persist_copytrade_research_conclusions(
            conn,
            study_batch,
            [
                {
                    "etf_code": etf_code,
                    "analysis_type": "leg_bucket_h_interaction",
                    "entry_row": ENTRY_ROW,
                    "metric_key": "L1-H3",
                    "horizon": int(result["candidate_h"]),
                    "metric_value": result["kruskal_wallis_alpha_p"],
                    "conclusion_zh": (
                        f"L1-H3 桶×H 交互：KW(Δα) p={result['kruskal_wallis_alpha_p']} · "
                        f"5–10 延長H緊迫={'是' if result['urgent_5_10_extend'] else '否'} · "
                        f"5–10日均Δα>2–4={'是' if result['higher_daily_delta_5_10_vs_2_4'] else '否'} "
                        f"→ **{result['verdict']}**。"
                    ),
                    "details_json": json.dumps(result, ensure_ascii=False, default=str),
                }
            ],
            replace_types=("leg_bucket_h_interaction",),
        )
    return {"batch_id": study_batch, "l1_h3": result}


# --- L1-P1～P3：分桶持有政策 × 單池實現超額 ---

DEFAULT_PER_SIGNAL_NTD = 10_000.0
DEFAULT_POLICY_SLOTS = 9  # 9 萬 / 1 萬


def _policy_bucket_h_map(sweet_spots: list[dict]) -> dict[str, int]:
    return {str(s["bucket_value"]): int(s["sweet_spot_h"]) for s in sweet_spots}


def default_l1_policies(sweet_spots: list[dict] | None = None) -> list[dict[str, object]]:
    """P1 uniform H9 · P2 5–10→H20 · P3 bucket H* · P4 skip 5–10 @ H9。"""
    sweet = _policy_bucket_h_map(sweet_spots or [])
    return [
        {
            "policy_id": "P1_uniform_h9",
            "label": "全局 H9",
            "default_h": 9,
            "bucket_h": {},
            "skip_buckets": (),
        },
        {
            "policy_id": "P2_extend_5_10",
            "label": "5–10→H20，其餘 H9",
            "default_h": 9,
            "bucket_h": {"5-10": 20},
            "skip_buckets": (),
        },
        {
            "policy_id": "P3_bucket_sweet",
            "label": "分桶 Optimal hold (H*)（矩陣峰值）",
            "default_h": 9,
            "bucket_h": sweet or {"5-10": 20},
            "skip_buckets": (),
        },
        {
            "policy_id": "P3_bucket_practical",
            "label": "分桶實務（5–10→H20，11+→H15，其餘 H9）",
            "default_h": 9,
            "bucket_h": {"5-10": 20, "11+": 15},
            "skip_buckets": (),
        },
        {
            "policy_id": "P4_skip_5_10",
            "label": "skip 5–10 日 @ H9",
            "default_h": 9,
            "bucket_h": {},
            "skip_buckets": ("5-10",),
        },
    ]


def build_policy_signal_days(
    conn: sqlite3.Connection,
    policy: dict[str, object],
    *,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
) -> tuple[list[dict], dict[str, object]]:
    """依政策從各 L1H run 拼裝訊號日序列（同日只取一個 H 的 outcome）。"""
    default_h = int(policy["default_h"])
    bucket_h: dict[str, int] = dict(policy.get("bucket_h") or {})
    skip_buckets = set(policy.get("skip_buckets") or ())
    needed_h = {default_h, *bucket_h.values()}
    h_maps: dict[int, dict[str, dict]] = {}
    for h in needed_h:
        h_maps[h] = _day_alpha_map(
            conn, h, matrix_batch=matrix_batch, extended_batch=extended_batch
        )
    base = h_maps[default_h]
    merged: list[dict] = []
    bucket_counts: dict[str, int] = {}
    h_counts: dict[int, int] = {}
    skipped = 0
    missing = 0
    for sd in sorted(base):
        n_legs = int(base[sd]["n_legs"])
        bucket = leg_count_bucket(n_legs)
        if bucket in skip_buckets:
            skipped += 1
            continue
        h = int(bucket_h.get(bucket, default_h))
        chosen = h_maps.get(h, {}).get(sd)
        if not chosen:
            missing += 1
            continue
        row = dict(chosen)
        row["policy_horizon"] = h
        row["policy_bucket"] = bucket
        merged.append(row)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        h_counts[h] = h_counts.get(h, 0) + 1
    return merged, {
        "n_universe": len(base),
        "n_taken": len(merged),
        "n_skipped": skipped,
        "n_missing_h": missing,
        "bucket_counts": bucket_counts,
        "horizon_counts": h_counts,
    }


def evaluate_bucket_policy(
    conn: sqlite3.Connection,
    policy: dict[str, object],
    *,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    n_slots: int = 1,
    per_signal_ntd: float = DEFAULT_PER_SIGNAL_NTD,
) -> dict[str, object]:
    days, meta = build_policy_signal_days(
        conn, policy, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    if n_slots <= 1:
        sim = simulate_capital_recycling(conn, days, capital_ntd=per_signal_ntd)
    else:
        sim = simulate_fixed_slots(
            conn, days, n_slots=n_slots, capital_ntd=per_signal_ntd
        )
    alphas = [float(d.get("alpha_ntd") or 0) for d in days]
    excess = [
        float(d.get("return_pct") or 0) - float(d.get("bench_return_pct") or 0)
        for d in days
    ]
    n = len(days)
    win_pct = round(100.0 * sum(1 for a in alphas if a > 0) / n, 2) if n else None
    return {
        "policy_id": policy["policy_id"],
        "label": policy["label"],
        "default_h": policy["default_h"],
        "bucket_h": policy.get("bucket_h") or {},
        "skip_buckets": list(policy.get("skip_buckets") or ()),
        "n_signal_days": n,
        "total_alpha_ntd": round(sum(alphas), 2),
        "mean_excess_pct": round(sum(excess) / n, 4) if n else None,
        "win_rate_vs_bench_pct": win_pct,
        **sim,
        **meta,
        "n_slots": n_slots,
        "per_signal_ntd": per_signal_ntd,
    }


def evaluate_l1_policies(
    conn: sqlite3.Connection,
    *,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    sweet_spots: list[dict] | None = None,
    n_slots: int = 1,
    per_signal_ntd: float = DEFAULT_PER_SIGNAL_NTD,
) -> dict[str, object]:
    policies = default_l1_policies(sweet_spots)
    rows = [
        evaluate_bucket_policy(
            conn,
            p,
            matrix_batch=matrix_batch,
            extended_batch=extended_batch,
            n_slots=n_slots,
            per_signal_ntd=per_signal_ntd,
        )
        for p in policies
    ]
    by_id = {str(r["policy_id"]): r for r in rows}
    p1 = by_id["P1_uniform_h9"]
    for r in rows:
        r["delta_recycled_vs_p1"] = round(
            float(r["recycled_total_alpha_ntd"] or 0)
            - float(p1["recycled_total_alpha_ntd"] or 0),
            2,
        )
        r["delta_total_vs_p1"] = round(
            float(r["total_alpha_ntd"] or 0) - float(p1["total_alpha_ntd"] or 0), 2
        )
        wr = r.get("win_rate_vs_bench_pct")
        p1_wr = p1.get("win_rate_vs_bench_pct")
        r["delta_win_rate_pp"] = (
            round(float(wr) - float(p1_wr), 2)
            if wr is not None and p1_wr is not None
            else None
        )
    best_recycled = max(rows, key=lambda r: float(r["recycled_total_alpha_ntd"] or 0))
    best_total = max(rows, key=lambda r: float(r["total_alpha_ntd"] or 0))
    p2 = by_id["P2_extend_5_10"]
    p2_beats_p1 = float(p2["recycled_total_alpha_ntd"] or 0) > float(
        p1["recycled_total_alpha_ntd"] or 0
    )
    if best_recycled["policy_id"] == "P2_extend_5_10" and p2_beats_p1:
        verdict = "adopt_p2_extend_5_10"
    elif best_recycled["policy_id"] == "P1_uniform_h9":
        verdict = "keep_uniform_h9"
    elif str(best_recycled["policy_id"]).startswith("P3"):
        verdict = "explore_bucket_sweet"
    else:
        verdict = f"best_{best_recycled['policy_id']}"
    return {
        "hypothesis_id": "L1-P1-P3",
        "policies": rows,
        "best_recycled_policy_id": best_recycled["policy_id"],
        "best_total_policy_id": best_total["policy_id"],
        "p2_beats_p1_recycled": p2_beats_p1,
        "verdict": verdict,
        "n_slots": n_slots,
        "per_signal_ntd": per_signal_ntd,
    }


def format_l1_policy_markdown(result: dict[str, object], *, slots_result: dict[str, object] | None = None) -> str:
    slot_label = (
        "單池 1 槽"
        if int(result.get("n_slots") or 1) <= 1
        else f"{result['n_slots']} 槽 × {result['per_signal_ntd']:,.0f}"
    )
    lines = [
        "# 00981A L1-P1～P3 · 分桶持有政策模擬",
        "",
        f"> {slot_label} · 等權 {result['per_signal_ntd']:,.0f}/訊號日 · α vs IX0001",
        f"> matrix `{DEFAULT_MATRIX_BATCH}` · extended `{DEFAULT_EXTENDED_BATCH}`",
        "",
        "## 政策定義",
        "",
        "| ID | 說明 |",
        "|----|------|",
        "| **P1** | 全局 H9（基準） |",
        "| **P2** | 5–10 leg 日 → H20，其餘 H9 |",
        "| **P3_sweet** | 各桶矩陣累計 α 峰值 H |",
        "| **P3_practical** | 5–10→H20，11+→H15，其餘 H9 |",
        "| **P4** | skip 5–10 日 @ H9（L1-C1 對照） |",
        "",
        f"**判決**：`{result['verdict']}` · "
        f"單池最佳：**{result['best_recycled_policy_id']}**",
        "",
        "## 結果（Primary 累計 α · Secondary 單池實現超額）",
        "",
        "| 政策 | n | 累計 α | 單池實現超額 | 輪動次數 | 捕獲% | 勝率% | Δ實現超額 vs P1 |",
        "|------|---|--------|------------|----------|-------|---------|-------------|",
    ]
    for r in result["policies"]:
        cap = r.get("signal_capture_pct")
        cap_s = f"{cap:.1f}" if cap is not None else "—"
        wr = r.get("win_rate_vs_bench_pct")
        wr_s = f"{wr:.1f}" if wr is not None else "—"
        lines.append(
            f"| {r['policy_id']} | {r['n_signal_days']} | "
            f"{r['total_alpha_ntd']:+,.0f} | {r['recycled_total_alpha_ntd']:+,.0f} | "
            f"{r['recycled_n_cycles']} | {cap_s} | {wr_s} | "
            f"{r['delta_recycled_vs_p1']:+,.0f} |"
        )

    if slots_result:
        lines.extend(
            [
                "",
                f"## 9 槽對照（{DEFAULT_POLICY_SLOTS} × {result['per_signal_ntd']:,.0f} = "
                f"{DEFAULT_POLICY_SLOTS * float(result['per_signal_ntd']):,.0f} NTD）",
                "",
                "| 政策 | 單池實現超額 | 9 槽實現超額 | Δ vs P1 | 捕獲% |",
                "|------|------------|------------|---------|-------|",
            ]
        )
        p1_slots = next(
            r for r in slots_result["policies"] if r["policy_id"] == "P1_uniform_h9"
        )
        p1_rec = float(p1_slots["recycled_total_alpha_ntd"] or 0)
        for r in result["policies"]:
            sr = next(
                x for x in slots_result["policies"] if x["policy_id"] == r["policy_id"]
            )
            cap = sr.get("signal_capture_pct")
            cap_s = f"{cap:.1f}" if cap is not None else "—"
            delta = round(float(sr["recycled_total_alpha_ntd"] or 0) - p1_rec, 2)
            lines.append(
                f"| {r['policy_id']} | {r['recycled_total_alpha_ntd']:+,.0f} | "
                f"{sr['recycled_total_alpha_ntd']:+,.0f} | {delta:+,.0f} | {cap_s} |"
            )

    lines.extend(
        [
            "",
            "## 解讀",
            "",
            "- **Primary**（累計 α）：假設每訊號日各 1 萬、持倉可重疊（無資金約束）。",
            "- **Secondary**（單池實現超額）：上一筆 exit 前不接新訊號；延長 H 會降捕獲率。",
            "- P2 若單池實現超額 > P1 → L1-F1 在實盤資金約束下仍值得做。",
            "- **若 P2 單池實現超額 < P1**：延長 H 的 Primary α 增益被 **捕獲率下降** 抵消；需額外槽位（見 9 槽表）。",
            "- P3_sweet 用 H27 等長持，捕獲率通常更低；看實現超額 是否仍勝 P1。",
            "- P4 僅提勝率時參考；單池實現超額 多數低於 P1/P2。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \\",
            "  --analyze-l1-policy --write-report",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def run_l1_policy_study(
    conn: sqlite3.Connection,
    *,
    etf_code: str = "00981A",
    batch_id: str | None = None,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    n_slots: int = 1,
    per_signal_ntd: float = DEFAULT_PER_SIGNAL_NTD,
    persist: bool = True,
) -> dict[str, object]:
    study_batch = batch_id or f"{etf_code.lower()}-l1policy-{date.today().strftime('%Y%m%d')}"
    summary_rows = build_leg_bucket_horizon_rows(
        conn,
        etf_code=etf_code,
        horizons=horizons,
        matrix_batch=matrix_batch,
        extended_batch=extended_batch,
    )
    sweet_spots = summarize_regime_sweet_spots(summary_rows, bucket_field=BUCKET_FIELD)
    single = evaluate_l1_policies(
        conn,
        matrix_batch=matrix_batch,
        extended_batch=extended_batch,
        sweet_spots=sweet_spots,
        n_slots=1,
        per_signal_ntd=per_signal_ntd,
    )
    slots = evaluate_l1_policies(
        conn,
        matrix_batch=matrix_batch,
        extended_batch=extended_batch,
        sweet_spots=sweet_spots,
        n_slots=DEFAULT_POLICY_SLOTS,
        per_signal_ntd=per_signal_ntd,
    )
    if persist:
        best = next(
            r for r in single["policies"] if r["policy_id"] == single["best_recycled_policy_id"]
        )
        persist_copytrade_research_conclusions(
            conn,
            study_batch,
            [
                {
                    "etf_code": etf_code,
                    "analysis_type": "leg_bucket_policy",
                    "entry_row": ENTRY_ROW,
                    "metric_key": "L1-P1-P3",
                    "horizon": 0,
                    "metric_value": best["recycled_total_alpha_ntd"],
                    "conclusion_zh": (
                        f"L1 政策模擬（單池）：最佳 {single['best_recycled_policy_id']} "
                        f"實現超額 {best['recycled_total_alpha_ntd']:+,.0f} · "
                        f"P2 vs P1 Δ {next(r for r in single['policies'] if r['policy_id']=='P2_extend_5_10')['delta_recycled_vs_p1']:+,.0f} · "
                        f"→ **{single['verdict']}**。"
                    ),
                    "details_json": json.dumps(
                        {"single_pool": single, "slots_9": slots, "sweet_spots": sweet_spots},
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            ],
            replace_types=("leg_bucket_policy",),
        )
    return {
        "batch_id": study_batch,
        "sweet_spots": sweet_spots,
        "single_pool": single,
        "slots_9": slots,
    }


def format_leg_bucket_horizon_markdown(
    *,
    etf_code: str,
    batch_id: str,
    summary_rows: list[dict],
    sweet_spots: list[dict],
    l1_f1: dict[str, object],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> str:
    lines = [
        f"# 00981A L1-F1 · Leg 桶 × H 矩陣",
        "",
        f"> batch `{batch_id}` · {etf_code} · L1 T+1 開盤 · 等權 1 萬/訊號日 · α vs IX0001",
        f"> matrix `{DEFAULT_MATRIX_BATCH}`（H≤20）· extended `{DEFAULT_EXTENDED_BATCH}`（H>20）",
        "",
        "## L1-F1 假說檢定",
        "",
        "**H0**：5–10 leg 日延長持有（H20）不能同時提升累計 α 與勝率（相對 H9）。",
        "**Ha**：延長 H 優於 skip 5–10 日（H9 基準）。",
        "",
        "| 指標 | H9（5–10 桶） | H20（5–10 桶） | Δ |",
        "|------|---------------|----------------|---|",
        f"| 配對 n | {l1_f1.get('n_paired')} | — | — |",
        f"| 累計 α | {l1_f1.get('cum_alpha_baseline'):+,.0f} | "
        f"{l1_f1.get('cum_alpha_candidate'):+,.0f} | "
        f"{l1_f1.get('cum_alpha_delta'):+,.0f} |",
        f"| 勝率% | {l1_f1.get('win_rate_baseline_pct')}% | "
        f"{l1_f1.get('win_rate_candidate_pct')}% | "
        f"{l1_f1.get('win_rate_delta_pp'):+.2f} pp |",
        f"| mean excess% | {l1_f1.get('mean_excess_baseline_pct')} | "
        f"{l1_f1.get('mean_excess_candidate_pct')} | — |",
        f"| 配對 p(W) | — | — | {l1_f1.get('paired_p_wilcoxon')} |",
        "",
        f"**判決**：`{l1_f1.get('verdict')}` · "
        f"採納延長 H：{'✅' if l1_f1.get('adopted') else '❌'}",
        "",
        "## 各 Leg 桶 Optimal hold (H*)（累計 α 峰值）",
        "",
        "| 桶 | n@H* | H* | 累計 α@H* | mean excess% |",
        "|----|--------|--------|-------------|--------------|",
    ]
    for s in sweet_spots:
        lines.append(
            f"| {s['bucket_value']} | {s['n_signal_days_at_sweet']} | "
            f"H{s['sweet_spot_h']} | {s['sweet_spot_total_alpha_ntd']:+,.0f} | "
            f"{s['mean_excess_at_sweet'] or 0:.3f} |"
        )

    for bucket in ("1", "2-4", "5-10", "11+"):
        rows = [r for r in summary_rows if r["bucket_value"] == bucket]
        if not rows:
            continue
        lines.extend(["", f"## 桶 `{bucket}` × H", ""])
        lines.append("| H | n | 勝率% | 累計 α | mean excess% | Δ累計 α | p(W) |")
        lines.append("|---|-----|---------|--------|--------------|---------|------|")
        for r in sorted(rows, key=lambda x: int(x["horizon"])):
            p = r["p_value_wilcoxon"]
            p_s = f"{p:.4f}" if p is not None else "—"
            wr = r.get("win_rate_vs_bench_pct")
            wr_s = f"{wr:.1f}" if wr is not None else "—"
            lines.append(
                f"| H{r['horizon']} | {r['n_signal_days']} | {wr_s} | "
                f"{r['total_alpha_ntd']:+,.0f} | {r['mean_excess_pct'] or 0:.3f} | "
                f"{r['marginal_total_alpha_ntd']:+,.0f} | {p_s} |"
            )

    lines.extend(
        [
            "",
            "## 解讀",
            "",
            "- **5–10 桶 @ H9** 為 α 黑洞（累計 ≈0）；**@ H20** 轉正 → 支持「延長 H」方向。",
            "- 若 L1-F1 未達採納門檻，實盤仍可用 **H1 skip_5_10 提勝率**（§10.2），但單池實現超額 可能降。",
            "- 桶內 n 小時（尤其 `1` 桶）Wilcoxon 檢定力不足，以趨勢為主。",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/run_00981a_copytrade_backtest.py \\",
            "  --analyze-leg-bucket-horizon --write-report",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def run_leg_bucket_horizon_study(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    batch_id: str | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    matrix_batch: str = DEFAULT_MATRIX_BATCH,
    extended_batch: str = DEFAULT_EXTENDED_BATCH,
    persist: bool = True,
) -> dict[str, object]:
    study_batch = batch_id or f"{etf_code.lower()}-l1f1-leg-bucket-{date.today().strftime('%Y%m%d')}"

    summary_rows = build_leg_bucket_horizon_rows(
        conn,
        etf_code=etf_code,
        horizons=horizons,
        matrix_batch=matrix_batch,
        extended_batch=extended_batch,
    )
    sweet_spots = summarize_regime_sweet_spots(summary_rows, bucket_field=BUCKET_FIELD)
    l1_f1 = evaluate_l1_f1(
        conn, matrix_batch=matrix_batch, extended_batch=extended_batch
    )
    l1_f1_h27 = evaluate_l1_f1(
        conn,
        matrix_batch=matrix_batch,
        extended_batch=extended_batch,
        candidate_h=27,
    )

    if persist:
        persist_copytrade_regime_horizon(conn, study_batch, summary_rows)
        verdict_zh = (
            f"L1-F1（5–10 桶 × H）：H9 cum α {l1_f1['cum_alpha_baseline']:+,.0f} "
            f"勝率 {l1_f1['win_rate_baseline_pct']}% → H20 "
            f"{l1_f1['cum_alpha_candidate']:+,.0f} / {l1_f1['win_rate_candidate_pct']}% "
            f"(Δα {l1_f1['cum_alpha_delta']:+,.0f} · Δ勝率 {l1_f1['win_rate_delta_pp']:+.2f}pp · "
            f"p={l1_f1['paired_p_wilcoxon']}) → **{l1_f1['verdict']}**。"
        )
        persist_copytrade_research_conclusions(
            conn,
            study_batch,
            [
                {
                    "etf_code": etf_code,
                    "analysis_type": "leg_bucket_horizon",
                    "entry_row": ENTRY_ROW,
                    "metric_key": "L1-F1",
                    "horizon": l1_f1["candidate_h"],
                    "metric_value": l1_f1["cum_alpha_delta"],
                    "conclusion_zh": verdict_zh,
                    "details_json": json.dumps(
                        {
                            "horizons": list(horizons),
                            "matrix_batch": matrix_batch,
                            "extended_batch": extended_batch,
                            "l1_f1_h20": l1_f1,
                            "l1_f1_h27": l1_f1_h27,
                            "sweet_spots": sweet_spots,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            replace_types=("leg_bucket_horizon",),
        )

    return {
        "batch_id": study_batch,
        "summary_rows": summary_rows,
        "sweet_spots": sweet_spots,
        "l1_f1": l1_f1,
        "l1_f1_h27": l1_f1_h27,
    }
