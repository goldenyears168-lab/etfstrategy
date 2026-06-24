"""RRG mono top10 × 前一日成交量分層 · hold7 統計對照。

假說：在 D4 mono fresh 的 seg_last 前十內，以前一日成交量（T-1 volume）
取最高 3 檔 vs 最低 3 檔，持有 7 日超額是否顯著不同；並對照 seg_last 前三。
"""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from flow_returns import trading_dates_after
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import (
    _exit_date_from_entry,
    _settle_trade,
    build_fresh_mono_calendar,
)
from rrg_mono_daily_brief import HOLD_DAYS, TOP_N, ScanRow

GroupId = Literal["rrg_top3", "vol_top3", "vol_bottom3"]

GROUP_LABELS: dict[GroupId, str] = {
    "rrg_top3": "seg_last 前三（RRG 基線）",
    "vol_top3": "前一日成交量前三",
    "vol_bottom3": "前一日成交量末三",
}


@dataclass
class VolumeTierLeg:
    signal_date: str
    prev_date: str
    stock_id: str
    stock_name: str
    group: GroupId
    seg_last_rank: int
    vol_rank: int
    prev_volume: float
    seg_last: float
    return_pct: float
    excess_pct: float
    beat_bench: bool


def _prev_trading_date(full_dates: list[str], signal_date: str) -> str | None:
    if signal_date not in full_dates:
        return None
    idx = full_dates.index(signal_date)
    return full_dates[idx - 1] if idx > 0 else None


def _ranked_top10_with_volume(
    fresh: list[ScanRow],
    *,
    prev_date: str,
    volume: pd.DataFrame,
) -> list[tuple[ScanRow, int, int, float]]:
    """Return (row, seg_rank, vol_rank, prev_vol) for stocks with valid T-1 volume."""
    top = sorted(fresh, key=lambda r: (-r.seg_last, r.stock_id))[:TOP_N]
    vol_rows: list[tuple[ScanRow, float]] = []
    for row in top:
        if prev_date not in volume.index or row.stock_id not in volume.columns:
            continue
        v = float(volume.at[prev_date, row.stock_id])
        if v != v or v <= 0:
            continue
        vol_rows.append((row, v))
    min_pool = 6
    if len(vol_rows) < min_pool:
        return []

    vol_sorted = sorted(vol_rows, key=lambda x: (-x[1], x[0].stock_id))
    seg_rank_map = {row.stock_id: i + 1 for i, row in enumerate(top)}
    vol_rank_map = {row.stock_id: i + 1 for i, (row, _) in enumerate(vol_sorted)}

    out: list[tuple[ScanRow, int, int, float]] = []
    for row, v in vol_rows:
        out.append((row, seg_rank_map[row.stock_id], vol_rank_map[row.stock_id], v))
    return out


def _pick_group(
    ranked: list[tuple[ScanRow, int, int, float]],
    group: GroupId,
) -> list[tuple[ScanRow, int, int, float]]:
    if group == "rrg_top3":
        by_seg = sorted(ranked, key=lambda x: (x[1], x[0].stock_id))
        return by_seg[:3]
    if group == "vol_top3":
        by_vol = sorted(ranked, key=lambda x: (x[2], x[0].stock_id))
        return by_vol[:3]
    by_vol = sorted(ranked, key=lambda x: (-x[2], x[0].stock_id))
    return by_vol[:3]


def collect_volume_tier_legs(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> tuple[list[VolumeTierLeg], dict[str, Any]]:
    close, _opn, volume = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    calendar = build_fresh_mono_calendar(conn, trade_dates)
    legs: list[VolumeTierLeg] = []
    signal_days_used = 0
    signal_days_skipped = 0

    for signal_date in trade_dates:
        fresh = calendar.get(signal_date) or []
        prev_date = _prev_trading_date(full_dates, signal_date)
        if not prev_date:
            signal_days_skipped += 1
            continue
        ranked = _ranked_top10_with_volume(fresh, prev_date=prev_date, volume=volume)
        if len(ranked) < 6:
            signal_days_skipped += 1
            continue
        exit_date = _exit_date_from_entry(conn, full_dates, signal_date, HOLD_DAYS)
        if not exit_date:
            signal_days_skipped += 1
            continue

        signal_days_used += 1
        for group in ("rrg_top3", "vol_top3", "vol_bottom3"):
            for row, seg_rank, vol_rank, prev_vol in _pick_group(ranked, group):
                pos = {
                    "stock_id": row.stock_id,
                    "stock_name": row.stock_name,
                    "signal_date": signal_date,
                    "entry_date": signal_date,
                    "exit_date": exit_date,
                    "seg_last": row.seg_last,
                }
                settled = _settle_trade(conn, close, pos, entry_price_mode="close")
                if not settled:
                    continue
                legs.append(
                    VolumeTierLeg(
                        signal_date=signal_date,
                        prev_date=prev_date,
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        group=group,
                        seg_last_rank=seg_rank,
                        vol_rank=vol_rank,
                        prev_volume=prev_vol,
                        seg_last=row.seg_last,
                        return_pct=float(settled["return_pct"]),
                        excess_pct=float(settled["excess_pct"]),
                        beat_bench=bool(settled["beat_bench"]),
                    )
                )

    meta = {
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": HOLD_DAYS,
        "entry_price_mode": "close",
        "signal_days_used": signal_days_used,
        "signal_days_skipped": signal_days_skipped,
        "n_legs": len(legs),
    }
    return legs, meta


@dataclass
class PairedExtremeLeg:
    signal_date: str
    prev_date: str
    pool_size: int
    stock_id: str
    stock_name: str
    role: Literal["vol_highest", "vol_lowest", "rrg_top1"]
    seg_last_rank: int
    vol_rank: int
    prev_volume: float
    seg_last: float
    return_pct: float
    excess_pct: float
    beat_bench: bool


def _ranked_shortlist_with_volume(
    fresh: list[ScanRow],
    *,
    prev_date: str,
    volume: pd.DataFrame,
    min_pool: int = 3,
) -> list[tuple[ScanRow, int, int, float]]:
    """Shortlist top min(10, |fresh|) with valid T-1 volume; require >= min_pool names."""
    top = sorted(fresh, key=lambda r: (-r.seg_last, r.stock_id))[:TOP_N]
    vol_rows: list[tuple[ScanRow, float]] = []
    for row in top:
        if prev_date not in volume.index or row.stock_id not in volume.columns:
            continue
        v = float(volume.at[prev_date, row.stock_id])
        if v != v or v <= 0:
            continue
        vol_rows.append((row, v))
    if len(vol_rows) < min_pool:
        return []

    vol_sorted = sorted(vol_rows, key=lambda x: (-x[1], x[0].stock_id))
    seg_rank_map = {row.stock_id: i + 1 for i, row in enumerate(top) if row.stock_id in {r.stock_id for r, _ in vol_rows}}
    vol_rank_map = {row.stock_id: i + 1 for i, (row, _) in enumerate(vol_sorted)}

    return [
        (row, seg_rank_map[row.stock_id], vol_rank_map[row.stock_id], v)
        for row, v in vol_rows
    ]


def _settle_leg(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    full_dates: list[str],
    *,
    signal_date: str,
    row: ScanRow,
) -> dict | None:
    exit_date = _exit_date_from_entry(conn, full_dates, signal_date, HOLD_DAYS)
    if not exit_date:
        return None
    pos = {
        "stock_id": row.stock_id,
        "stock_name": row.stock_name,
        "signal_date": signal_date,
        "entry_date": signal_date,
        "exit_date": exit_date,
        "seg_last": row.seg_last,
    }
    return _settle_trade(conn, close, pos, entry_price_mode="close")


def collect_paired_extreme_legs(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    min_pool: int = 3,
) -> tuple[list[PairedExtremeLeg], dict[str, Any]]:
    """Shortlist 內 T-1 成交量最高 vs 最低（同日配對）；另附 seg_last 第 1。"""
    close, _opn, volume = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    calendar = build_fresh_mono_calendar(conn, trade_dates)

    legs: list[PairedExtremeLeg] = []
    signal_days_used = 0
    signal_days_skipped = 0

    for signal_date in trade_dates:
        fresh = calendar.get(signal_date) or []
        if len(fresh) < min_pool:
            signal_days_skipped += 1
            continue
        prev_date = _prev_trading_date(full_dates, signal_date)
        if not prev_date:
            signal_days_skipped += 1
            continue
        ranked = _ranked_shortlist_with_volume(
            fresh, prev_date=prev_date, volume=volume, min_pool=min_pool
        )
        if len(ranked) < min_pool:
            signal_days_skipped += 1
            continue

        by_vol_hi = sorted(ranked, key=lambda x: (x[2], x[0].stock_id))
        by_vol_lo = sorted(ranked, key=lambda x: (-x[2], x[0].stock_id))
        by_seg = sorted(ranked, key=lambda x: (x[1], x[0].stock_id))
        picks = [
            ("vol_highest", by_vol_hi[0]),
            ("vol_lowest", by_vol_lo[0]),
            ("rrg_top1", by_seg[0]),
        ]
        signal_days_used += 1
        pool_size = len(ranked)
        for role, (row, seg_rank, vol_rank, prev_vol) in picks:
            settled = _settle_leg(conn, close, full_dates, signal_date=signal_date, row=row)
            if not settled:
                continue
            legs.append(
                PairedExtremeLeg(
                    signal_date=signal_date,
                    prev_date=prev_date,
                    pool_size=pool_size,
                    stock_id=row.stock_id,
                    stock_name=row.stock_name,
                    role=role,
                    seg_last_rank=seg_rank,
                    vol_rank=vol_rank,
                    prev_volume=prev_vol,
                    seg_last=row.seg_last,
                    return_pct=float(settled["return_pct"]),
                    excess_pct=float(settled["excess_pct"]),
                    beat_bench=bool(settled["beat_bench"]),
                )
            )

    meta = {
        "date_start": date_start,
        "date_end": date_end,
        "hold_days": HOLD_DAYS,
        "min_pool": min_pool,
        "signal_days_used": signal_days_used,
        "signal_days_skipped": signal_days_skipped,
        "n_legs": len(legs),
    }
    return legs, meta


def _cohens_d(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled <= 0:
        return None
    return (ma - mb) / pooled


def _bootstrap_mean_diff(
    a: list[float],
    b: list[float],
    *,
    n_boot: int = 5000,
    seed: int = 42,
) -> dict[str, float | int | None]:
    if not a or not b:
        return {"n_a": len(a), "n_b": len(b)}
    rng = random.Random(seed)
    diffs: list[float] = []
    obs = sum(a) / len(a) - sum(b) / len(b)
    for _ in range(n_boot):
        sa = [a[rng.randrange(len(a))] for _ in range(len(a))]
        sb = [b[rng.randrange(len(b))] for _ in range(len(b))]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    lo = diffs[int(0.025 * n_boot)]
    hi = diffs[int(0.975 * n_boot)]
    p_two = sum(1 for d in diffs if d <= 0) / n_boot
    p_two = min(p_two, 1 - p_two) * 2
    return {
        "n_a": len(a),
        "n_b": len(b),
        "obs_mean_diff_pp": round(obs, 4),
        "ci_low_pp": round(lo, 4),
        "ci_high_pp": round(hi, 4),
        "bootstrap_p_two_sided": round(p_two, 4),
    }


def _compare_two_samples(
    a: list[float],
    b: list[float],
    *,
    label_a: str,
    label_b: str,
) -> dict[str, Any]:
    from scipy.stats import mannwhitneyu, ttest_ind

    out: dict[str, Any] = {
        "a": label_a,
        "b": label_b,
        "n_a": len(a),
        "n_b": len(b),
        "mean_a": round(sum(a) / len(a), 4) if a else None,
        "mean_b": round(sum(b) / len(b), 4) if b else None,
        "mean_diff_a_minus_b": round(sum(a) / len(a) - sum(b) / len(b), 4) if a and b else None,
        "cohens_d": round(_cohens_d(a, b), 4) if a and b else None,
    }
    if len(a) >= 2 and len(b) >= 2:
        t = ttest_ind(a, b, equal_var=False)
        u = mannwhitneyu(a, b, alternative="two-sided")
        out["welch_t"] = round(float(t.statistic), 4)
        out["welch_p"] = round(float(t.pvalue), 4)
        out["mannwhitney_u"] = round(float(u.statistic), 4)
        out["mannwhitney_p"] = round(float(u.pvalue), 4)
    else:
        out["welch_t"] = None
        out["welch_p"] = None
        out["mannwhitney_u"] = None
        out["mannwhitney_p"] = None
    out["bootstrap"] = _bootstrap_mean_diff(a, b)
    return out


def _group_excess(legs: list[VolumeTierLeg], group: GroupId) -> list[float]:
    return [lg.excess_pct for lg in legs if lg.group == group]


def _paired_signal_day_baskets(
    legs: list[VolumeTierLeg],
) -> list[dict[str, Any]]:
    """Per signal day: equal-weight mean excess for each group (paired)."""
    by_day: dict[str, dict[GroupId, list[float]]] = {}
    for lg in legs:
        by_day.setdefault(lg.signal_date, {}).setdefault(lg.group, []).append(lg.excess_pct)

    rows: list[dict[str, Any]] = []
    for day, groups in sorted(by_day.items()):
        if not all(len(groups.get(g, [])) == 3 for g in ("rrg_top3", "vol_top3", "vol_bottom3")):
            continue
        rows.append(
            {
                "signal_date": day,
                "rrg_top3": sum(groups["rrg_top3"]) / 3,
                "vol_top3": sum(groups["vol_top3"]) / 3,
                "vol_bottom3": sum(groups["vol_bottom3"]) / 3,
            }
        )
    return rows


def analyze_volume_tier_legs(legs: list[VolumeTierLeg]) -> dict[str, Any]:
    from scipy.stats import spearmanr, wilcoxon

    groups = {g: _group_excess(legs, g) for g in GROUP_LABELS}
    summaries = {
        g: summarize_periods(
            [
                {
                    "return_pct": lg.return_pct,
                    "excess_pct": lg.excess_pct,
                    "bench_return_pct": lg.return_pct - lg.excess_pct,
                    "beat_bench": lg.beat_bench,
                    "gross_win": lg.return_pct > 0,
                    "entry_date": lg.signal_date,
                }
                for lg in legs
                if lg.group == g
            ]
        )
        for g in GROUP_LABELS
    }
    for g, s in summaries.items():
        ex = groups[g]
        if ex:
            s["mean_excess_pct"] = round(sum(ex) / len(ex), 4)

    comparisons = {
        "vol_top3_vs_vol_bottom3": _compare_two_samples(
            groups["vol_top3"], groups["vol_bottom3"],
            label_a="vol_top3", label_b="vol_bottom3",
        ),
        "rrg_top3_vs_vol_top3": _compare_two_samples(
            groups["rrg_top3"], groups["vol_top3"],
            label_a="rrg_top3", label_b="vol_top3",
        ),
        "rrg_top3_vs_vol_bottom3": _compare_two_samples(
            groups["rrg_top3"], groups["vol_bottom3"],
            label_a="rrg_top3", label_b="vol_bottom3",
        ),
    }

    paired = _paired_signal_day_baskets(legs)
    paired_stats: dict[str, Any] = {"n_signal_days": len(paired)}
    if len(paired) >= 5:
        diff_vol = [r["vol_top3"] - r["vol_bottom3"] for r in paired]
        diff_rrg_voltop = [r["rrg_top3"] - r["vol_top3"] for r in paired]
        diff_rrg_volbot = [r["rrg_top3"] - r["vol_bottom3"] for r in paired]
        for key, diffs in [
            ("vol_top3_minus_vol_bottom3", diff_vol),
            ("rrg_top3_minus_vol_top3", diff_rrg_voltop),
            ("rrg_top3_minus_vol_bottom3", diff_rrg_volbot),
        ]:
            w = wilcoxon(diffs, alternative="two-sided", zero_method="wilcox")
            rng = random.Random(42)
            boot: list[float] = []
            for _ in range(5000):
                sample = [diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))]
                boot.append(sum(sample) / len(sample))
            boot.sort()
            paired_stats[key] = {
                "mean_diff_pp": round(sum(diffs) / len(diffs), 4),
                "median_diff_pp": round(sorted(diffs)[len(diffs) // 2], 4),
                "wilcoxon_stat": round(float(w.statistic), 4),
                "wilcoxon_p": round(float(w.pvalue), 4),
                "bootstrap": {
                    "obs_mean_diff_pp": round(sum(diffs) / len(diffs), 4),
                    "ci_low_pp": round(boot[int(0.025 * 5000)], 4),
                    "ci_high_pp": round(boot[int(0.975 * 5000)], 4),
                },
            }

    all_vol_rank = [lg.vol_rank for lg in legs]
    all_legs_excess = [lg.excess_pct for lg in legs]
    spearman_vol_rank: dict[str, Any] = {}
    if len(legs) >= 10:
        rho, p = spearmanr(all_vol_rank, all_legs_excess)
        spearman_vol_rank = {"rho": round(float(rho), 4), "p_two_sided": round(float(p), 4), "n": len(legs)}

    # Within top10 only (dedupe by signal+stock)
    seen: set[tuple[str, str]] = set()
    uniq_ranks: list[int] = []
    uniq_excess: list[float] = []
    for lg in legs:
        key = (lg.signal_date, lg.stock_id)
        if key in seen:
            continue
        seen.add(key)
        uniq_ranks.append(lg.vol_rank)
        uniq_excess.append(lg.excess_pct)
    spearman_within_top10: dict[str, Any] = {}
    if len(uniq_ranks) >= 10:
        rho, p = spearmanr(uniq_ranks, uniq_excess)
        spearman_within_top10 = {
            "rho": round(float(rho), 4),
            "p_two_sided": round(float(p), 4),
            "n": len(uniq_ranks),
            "interpretation": "vol_rank 1=最高量；負 rho 表示高量排名靠前 → 超額較低",
        }

    overlap: dict[str, Any] = {}
    by_day_stock: dict[str, set[str]] = {"rrg_top3": set(), "vol_top3": set(), "vol_bottom3": set()}
    for lg in legs:
        by_day_stock[lg.group].add(f"{lg.signal_date}:{lg.stock_id}")
    rrg_ids = by_day_stock["rrg_top3"]
    vol_top_ids = by_day_stock["vol_top3"]
    vol_bot_ids = by_day_stock["vol_bottom3"]
    overlap = {
        "rrg_top3_vs_vol_top3_same_stock_rate": round(
            len(rrg_ids & vol_top_ids) / len(rrg_ids) if rrg_ids else 0, 4
        ),
        "rrg_top3_vs_vol_bottom3_same_stock_rate": round(
            len(rrg_ids & vol_bot_ids) / len(rrg_ids) if rrg_ids else 0, 4
        ),
        "vol_top3_vs_vol_bottom3_disjoint": len(vol_top_ids & vol_bot_ids) == 0,
    }

    return {
        "group_summaries": summaries,
        "leg_level_comparisons": comparisons,
        "paired_signal_day_baskets": paired_stats,
        "spearman_vol_rank_vs_excess_all_legs": spearman_vol_rank,
        "spearman_vol_rank_vs_excess_unique_top10": spearman_within_top10,
        "selection_overlap": overlap,
        "group_ns": {g: len(groups[g]) for g in GROUP_LABELS},
    }


def analyze_paired_extreme_legs(legs: list[PairedExtremeLeg]) -> dict[str, Any]:
    from scipy.stats import wilcoxon

    roles = ("vol_highest", "vol_lowest", "rrg_top1")
    role_labels = {
        "vol_highest": "shortlist 內 T-1 量最高",
        "vol_lowest": "shortlist 內 T-1 量最低",
        "rrg_top1": "shortlist 內 seg_last 第 1",
    }
    by_role = {r: [lg.excess_pct for lg in legs if lg.role == r] for r in roles}
    summaries = {
        r: summarize_periods(
            [
                {
                    "return_pct": lg.return_pct,
                    "excess_pct": lg.excess_pct,
                    "bench_return_pct": lg.return_pct - lg.excess_pct,
                    "beat_bench": lg.beat_bench,
                    "gross_win": lg.return_pct > 0,
                    "entry_date": lg.signal_date,
                }
                for lg in legs
                if lg.role == r
            ]
        )
        for r in roles
    }
    for r in roles:
        ex = by_role[r]
        if ex:
            summaries[r]["mean_excess_pct"] = round(sum(ex) / len(ex), 4)

    comparisons = {
        "vol_highest_vs_vol_lowest": _compare_two_samples(
            by_role["vol_highest"], by_role["vol_lowest"],
            label_a="vol_highest", label_b="vol_lowest",
        ),
        "rrg_top1_vs_vol_highest": _compare_two_samples(
            by_role["rrg_top1"], by_role["vol_highest"],
            label_a="rrg_top1", label_b="vol_highest",
        ),
        "rrg_top1_vs_vol_lowest": _compare_two_samples(
            by_role["rrg_top1"], by_role["vol_lowest"],
            label_a="rrg_top1", label_b="vol_lowest",
        ),
    }

    by_day: dict[str, dict[str, float]] = {}
    for lg in legs:
        by_day.setdefault(lg.signal_date, {})[lg.role] = lg.excess_pct
    paired_days = [
        d for d, m in by_day.items() if "vol_highest" in m and "vol_lowest" in m
    ]
    paired_stats: dict[str, Any] = {"n_signal_days": len(paired_days)}
    if len(paired_days) >= 5:
        for key, a_role, b_role in [
            ("vol_highest_minus_vol_lowest", "vol_highest", "vol_lowest"),
            ("rrg_top1_minus_vol_highest", "rrg_top1", "vol_highest"),
            ("rrg_top1_minus_vol_lowest", "rrg_top1", "vol_lowest"),
        ]:
            diffs = [
                by_day[d][a_role] - by_day[d][b_role]
                for d in paired_days
                if a_role in by_day[d] and b_role in by_day[d]
            ]
            if len(diffs) < 5:
                continue
            w = wilcoxon(diffs, alternative="two-sided", zero_method="wilcox")
            rng = random.Random(42)
            boot = sorted(
                [
                    sum([diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))]) / len(diffs)
                    for _ in range(5000)
                ]
            )
            paired_stats[key] = {
                "mean_diff_pp": round(sum(diffs) / len(diffs), 4),
                "median_diff_pp": round(sorted(diffs)[len(diffs) // 2], 4),
                "wilcoxon_p": round(float(w.pvalue), 4),
                "bootstrap": {
                    "ci_low_pp": round(boot[int(0.025 * 5000)], 4),
                    "ci_high_pp": round(boot[int(0.975 * 5000)], 4),
                },
            }

    overlap_hi = 0
    overlap_lo = 0
    n_days = 0
    for d in paired_days:
        rrg = next((lg for lg in legs if lg.signal_date == d and lg.role == "rrg_top1"), None)
        hi = next((lg for lg in legs if lg.signal_date == d and lg.role == "vol_highest"), None)
        lo = next((lg for lg in legs if lg.signal_date == d and lg.role == "vol_lowest"), None)
        if not rrg or not hi or not lo:
            continue
        n_days += 1
        if rrg.stock_id == hi.stock_id:
            overlap_hi += 1
        if rrg.stock_id == lo.stock_id:
            overlap_lo += 1

    avg_pool = round(
        sum(lg.pool_size for lg in legs if lg.role == "vol_highest")
        / max(len(by_role["vol_highest"]), 1),
        2,
    )

    return {
        "role_labels": role_labels,
        "role_summaries": summaries,
        "leg_level_comparisons": comparisons,
        "paired_signal_day": paired_stats,
        "rrg_top1_equals_vol_highest_rate": round(overlap_hi / n_days, 4) if n_days else None,
        "rrg_top1_equals_vol_lowest_rate": round(overlap_lo / n_days, 4) if n_days else None,
        "avg_shortlist_pool_size": avg_pool,
        "role_ns": {r: len(by_role[r]) for r in roles},
    }


def render_volume_tier_markdown(
    legs: list[VolumeTierLeg],
    meta: dict[str, Any],
    analysis: dict[str, Any],
    *,
    paired_legs: list[PairedExtremeLeg] | None = None,
    paired_analysis: dict[str, Any] | None = None,
    paired_meta: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# RRG mono top10 × 前一日成交量分層 · hold7 統計對照",
        "",
        f"區間：**{meta['date_start']}** ～ **{meta['date_end']}** · "
        f"訊號日 {meta['signal_days_used']} · 略過 {meta['signal_days_skipped']} · "
        f"進場 D4 close · 出場 hold{meta['hold_days']} close",
        "",
        "## 1. 設計",
        "",
        "- **母體**：每日 mono fresh 池依 **seg_last** 取前十（與 `rrg-mono-hold7` 相同 shortlist）。",
        "- **分層**：在前十內，用 **訊號日前一交易日（T-1）成交量** 排序。",
        "- **三組**（各 3 檔 · 不重疊 vol_top vs vol_bottom）：",
        "  - **RRG 前三**：seg_last 最高 3 檔（現行填槽基線）",
        "  - **量前三**：T-1 成交量最高 3 檔",
        "  - **量末三**：T-1 成交量最低 3 檔",
        "- **統計**：腿級 Welch t / Mann-Whitney + Cohen's d + bootstrap；"
        "訊號日等權籃 Wilcoxon 配對。",
        "",
        "## 2. 描述統計（腿級 · 超額%）",
        "",
        "| 組別 | n | 勝率 vs 台指 | 均報酬% | 均超額% |",
        "|------|---|-------------|--------|--------|",
    ]
    for gid, label in GROUP_LABELS.items():
        s = analysis["group_summaries"][gid]
        lines.append(
            f"| {label} | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_return_pct', '—')} | {s.get('mean_excess_pct', '—')} |"
        )

    lines.extend(["", "## 3. 腿級假設檢定（超額% · 獨立樣本）", ""])
    for key, cmp in analysis["leg_level_comparisons"].items():
        lines.append(f"### {key}")
        lines.append("")
        lines.append(
            f"- **{cmp['a']}** mean={cmp['mean_a']}% · **{cmp['b']}** mean={cmp['mean_b']}% · "
            f"差={cmp['mean_diff_a_minus_b']}pp"
        )
        lines.append(
            f"- Welch t p={cmp['welch_p']} · Mann-Whitney p={cmp['mannwhitney_p']} · "
            f"Cohen's d={cmp['cohens_d']}"
        )
        boot = cmp.get("bootstrap") or {}
        if boot.get("ci_low_pp") is not None:
            lines.append(
                f"- Bootstrap 95% CI（均值差）: [{boot['ci_low_pp']}, {boot['ci_high_pp']}] pp · "
                f"bootstrap p≈{boot.get('bootstrap_p_two_sided')}"
            )
        lines.append("")

    lines.extend(["## 4. 訊號日配對檢定（等權三檔籃 · Wilcoxon）", ""])
    ps = analysis["paired_signal_day_baskets"]
    lines.append(f"有效訊號日：**{ps.get('n_signal_days', 0)}**（三組皆滿 3 檔）")
    lines.append("")
    for key in ("vol_top3_minus_vol_bottom3", "rrg_top3_minus_vol_top3", "rrg_top3_minus_vol_bottom3"):
        block = ps.get(key)
        if not block:
            continue
        lines.append(f"- **{key}**: mean={block['mean_diff_pp']}pp · median={block['median_diff_pp']}pp · "
                     f"Wilcoxon p={block['wilcoxon_p']}")
        b = block.get("bootstrap") or {}
        if b:
            lines.append(
                f"  - Bootstrap 95% CI: [{b.get('ci_low_pp')}, {b.get('ci_high_pp')}] pp"
            )

    lines.extend(["", "## 5. 相關與選股重疊", ""])
    sp = analysis.get("spearman_vol_rank_vs_excess_unique_top10") or {}
    if sp:
        lines.append(
            f"- Spearman（top10 內 vol_rank vs 超額）: ρ={sp.get('rho')} · p={sp.get('p_two_sided')} · n={sp.get('n')}"
        )
        lines.append(f"  - {sp.get('interpretation', '')}")
    ov = analysis.get("selection_overlap") or {}
    lines.append(
        f"- RRG前三 與 量前三 同一檔比例: {ov.get('rrg_top3_vs_vol_top3_same_stock_rate', 0):.1%}"
    )
    lines.append(
        f"- RRG前三 與 量末三 同一檔比例: {ov.get('rrg_top3_vs_vol_bottom3_same_stock_rate', 0):.1%}"
    )
    lines.append(
        f"- 量前三 vs 量末三 標的重疊: {'無' if ov.get('vol_top3_vs_vol_bottom3_disjoint') else '有'}"
    )

    lines.extend(["", "## 6. 統計解讀備註", ""])
    primary = analysis["leg_level_comparisons"]["vol_top3_vs_vol_bottom3"]
    p_welch = primary.get("welch_p")
    sig = p_welch is not None and p_welch < 0.05
    lines.append(
        f"- **主問題**（量前三 vs 量末三 · 嚴格三檔籃）：Welch p={p_welch} · "
        f"{'拒絕虛無假設（α=0.05）' if sig else '未達顯著（α=0.05）'}。"
    )
    lines.append(
        "- mono fresh 池 seldom ≥6 檔，§2–§5 檢定力不足；請以 §7 配對極值為主結論。"
    )
    lines.append(
        "- 腿級樣本非完全獨立（同日三組相關）；配對籃檢定為較保守的同日對照。"
    )
    lines.append(
        "- 此為 **Research layer** 探索；未採納為 `rrg-mono-hold7` 規格變更。"
    )

    if paired_analysis and paired_meta:
        lines.extend(
            [
                "",
                "## 7. 擴充檢定 · shortlist 內量最高 vs 量最低（同日配對）",
                "",
                f"當 mono fresh shortlist 僅 **{paired_meta.get('min_pool', 3)}+** 檔即可納入；"
                f"有效訊號日 **{paired_meta.get('signal_days_used', 0)}** · "
                f"平均池大小 **{paired_analysis.get('avg_shortlist_pool_size', '—')}** 檔。",
                "",
                "| 角色 | n | 勝率 vs 台指 | 均超額% |",
                "|------|---|-------------|--------|",
            ]
        )
        for role, label in (paired_analysis.get("role_labels") or {}).items():
            s = (paired_analysis.get("role_summaries") or {}).get(role, {})
            lines.append(
                f"| {label} | {s.get('n_periods', 0)} | "
                f"{s.get('win_rate_vs_bench_pct', '—')}% | {s.get('mean_excess_pct', '—')} |"
            )
        lines.extend(["", "### 腿級檢定", ""])
        for key, cmp in (paired_analysis.get("leg_level_comparisons") or {}).items():
            lines.append(
                f"- **{key}**: 差={cmp.get('mean_diff_a_minus_b')}pp · "
                f"Welch p={cmp.get('welch_p')} · MW p={cmp.get('mannwhitney_p')} · d={cmp.get('cohens_d')}"
            )
        lines.extend(["", "### 同日配對 Wilcoxon", ""])
        ps2 = paired_analysis.get("paired_signal_day") or {}
        lines.append(f"配對訊號日：**{ps2.get('n_signal_days', 0)}**")
        for key in (
            "vol_highest_minus_vol_lowest",
            "rrg_top1_minus_vol_highest",
            "rrg_top1_minus_vol_lowest",
        ):
            block = ps2.get(key)
            if not block:
                continue
            lines.append(
                f"- **{key}**: mean={block.get('mean_diff_pp')}pp · "
                f"Wilcoxon p={block.get('wilcoxon_p')}"
            )
            b = block.get("bootstrap") or {}
            if b:
                lines.append(f"  - Bootstrap 95% CI: [{b.get('ci_low_pp')}, {b.get('ci_high_pp')}] pp")
        lines.append(
            f"- RRG第1 = 量最高 同日比例: "
            f"{(paired_analysis.get('rrg_top1_equals_vol_highest_rate') or 0):.1%}"
        )
        lines.append(
            f"- RRG第1 = 量最低 同日比例: "
            f"{(paired_analysis.get('rrg_top1_equals_vol_lowest_rate') or 0):.1%}"
        )
        p2 = (
            (paired_analysis.get("leg_level_comparisons") or {})
            .get("vol_highest_vs_vol_lowest", {})
            .get("welch_p")
        )
        sig2 = p2 is not None and p2 < 0.05
        lines.extend(
            [
                "",
                f"**擴充檢定結論**：量最高 vs 量最低 Welch p={p2} · "
                f"{'顯著' if sig2 else '不顯著'}（α=0.05）。",
            ]
        )

    lines.append("")
    lines.append("---")
    lines.append("模組：`scripts/run_rrg_mono_volume_tier_study.py`")
    return "\n".join(lines) + "\n"
