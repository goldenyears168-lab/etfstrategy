"""RRG Improving lifecycle rule sweep · walk-forward hit-rate optimization.

Compares candidate observation rules on rolling windows with two-proportion
z-tests and bootstrap mean-difference CIs. Train window selects refinements;
test window (default last 22 trading days) is held out for OOS validation.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Callable

import numpy as np
import pandas as pd

from research.backtest.rrg_improving_lifecycle_backtest import (
    BURST_PCT,
    build_lifecycle_events,
)

RuleFn = Callable[[pd.Series], bool]


@dataclass(frozen=True)
class RuleMetrics:
    rule_id: str
    label: str
    n_events: int
    n_signal_days: int
    next_mean_pct: float
    next_win_rate: float
    p_next_ge_burst: float
    ew_daily_win_rate: float
    ew_daily_mean_pct: float


@dataclass(frozen=True)
class SignificanceResult:
    rule_id: str
    baseline_id: str
    metric: str
    rule_value: float
    baseline_value: float
    delta: float
    z_stat: float | None
    p_value: float | None
    n_rule: int
    n_baseline: int
    significant_05: bool
    bootstrap_ci_lo: float | None = None
    bootstrap_ci_hi: float | None = None


def _norm_sf(z: float) -> float:
    """Standard normal survival function (two-sided helper uses 2×)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_proportion_ztest(
    x1: int, n1: int, x2: int, n2: int
) -> tuple[float, float]:
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = x1 / n1, x2 / n2
    p_pool = (x1 + x2) / (n1 + n2)
    if p_pool <= 0 or p_pool >= 1:
        return 0.0, 1.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    p_two = 2 * min(_norm_sf(abs(z)), 1 - _norm_sf(abs(z)))
    return z, min(p_two, 1.0)


def bootstrap_mean_diff(
    a: np.ndarray, b: np.ndarray, *, n_boot: int = 2000, seed: int = 42
) -> tuple[float, float]:
    if len(a) == 0 or len(b) == 0:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        sa = rng.choice(a, size=len(a), replace=True)
        sb = rng.choice(b, size=len(b), replace=True)
        diffs.append(float(sa.mean() - sb.mean()))
    lo, hi = float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))
    return lo, hi


def _rule_base_setup(r: pd.Series) -> bool:
    return bool(r["base_setup"])


def _rule_s2_core(r: pd.Series) -> bool:
    return bool(r["s2_setup_core"])


def _rule_base_flat(r: pd.Series) -> bool:
    return bool(r["base_setup"] and -0.5 <= r["sig_ret"] <= 0.5)


def _rule_base_no_chase(r: pd.Series) -> bool:
    return bool(r["base_setup"] and r["sig_ret"] < 1.0 and r["sig_ret"] > -2.0)


def _rule_base_streak4(r: pd.Series) -> bool:
    return bool(r["base_setup"] and r["improve_streak"] >= 4)


def _rule_s2_rv9899(r: pd.Series) -> bool:
    return bool(r["s2_rv_98_99"])


def _rule_s2_disp_contract(r: pd.Series) -> bool:
    return bool(r["s2_setup_core"] and r["disp_contract"])


def _rule_near_burst(r: pd.Series) -> bool:
    return bool(
        r["base_setup"]
        and 3.0 <= r["sig_ret"] < BURST_PCT
        and r["improve_streak"] >= 4
    )


def _rule_base_sig_band(r: pd.Series) -> bool:
    """Holiday-robust: base setup + flat/chase filter, no excess gate."""
    return bool(
        r["base_setup"]
        and -0.5 <= r["sig_ret"] <= 0.5
        and r["sig_ret"] > -2.0
    )


def _rule_refined_v1(r: pd.Series) -> bool:
    """Train-selected composite: base + flat + streak≥4 + RV98-99 optional boost."""
    if not r["base_setup"]:
        return False
    if r["sig_ret"] >= 1.0 or r["sig_ret"] <= -2.0:
        return False
    if not (-0.5 <= r["sig_ret"] <= 0.5):
        return False
    if r["improve_streak"] < 4:
        return False
    return True


def _rule_refined_v2(r: pd.Series) -> bool:
    """v1 + RV 98-99 or disp contraction."""
    if not _rule_refined_v1(r):
        return False
    rv_ok = not np.isnan(r["rv"]) and 98.0 <= r["rv"] < 99.0
    disp_ok = bool(r["disp_contract"])
    return rv_ok or disp_ok


def _rule_refined_v3(r: pd.Series) -> bool:
    """base + excess band + flat + streak≥4 (classic S2 tightened)."""
    if not r["s2_setup_core"]:
        return False
    if not (-0.5 <= r["sig_ret"] <= 0.5):
        return False
    return r["improve_streak"] >= 4


def _rule_s2_adjacent(r: pd.Series) -> bool:
    """S2 core only when prior session gap ≤4 days (excess not holiday-distorted)."""
    return bool(r["s2_setup_core"] and r.get("session_gap_days", 99) <= 4)


def _rule_watch_setup(r: pd.Series) -> bool:
    """Pre-burst watch: base + streak≥4 + mild day [-1%, +0.5%]."""
    return bool(
        r["base_setup"]
        and r["improve_streak"] >= 4
        and -1.0 <= r["sig_ret"] <= 0.5
        and r["sig_ret"] > -2.0
    )


def _rule_watch_setup_rv(r: pd.Series) -> bool:
    """watch_setup + RV 98–99."""
    if not _rule_watch_setup(r):
        return False
    return not np.isnan(r["rv"]) and 98.0 <= r["rv"] < 99.0


def _rule_s3_mkt_up(r: pd.Series) -> bool:
    """S3 burst on a market-up signal day."""
    return bool(r["s3_burst"] and r["bench_today_ret"] >= 0)


def _rule_s3_rv_mkt(r: pd.Series) -> bool:
    """S3 + RV 98–99 + signal-day market ≥0."""
    return bool(r["s3_rv_98_99"] and r["bench_today_ret"] >= 0)


def _rule_s3_follow(r: pd.Series) -> bool:
    return bool(r["s3_burst"])


def _rule_s3_rv9899(r: pd.Series) -> bool:
    return bool(r["s3_rv_98_99"])


CANDIDATE_RULES: list[tuple[str, str, RuleFn]] = [
    ("baseline_s2", "S2 Setup core（baseline）", _rule_s2_core),
    ("base_setup", "base setup only", _rule_base_setup),
    ("base_flat", "base + sig flat [-0.5%, +0.5%]", _rule_base_flat),
    ("base_no_chase", "base + no chase / no deep dip", _rule_base_no_chase),
    ("base_streak4", "base + Improving streak ≥4", _rule_base_streak4),
    ("base_sig_band", "base + flat（no excess · holiday robust）", _rule_base_sig_band),
    ("s2_rv9899", "S2 + RV 98–99", _rule_s2_rv9899),
    ("s2_disp_contract", "S2 + disp contraction", _rule_s2_disp_contract),
    ("near_burst", "base + 3%≤sig<5% + streak≥4", _rule_near_burst),
    ("refined_v1", "refined v1: base flat streak≥4", _rule_refined_v1),
    ("refined_v2", "refined v2: v1 + (RV98-99 or disp↓)", _rule_refined_v2),
    ("refined_v3", "refined v3: S2 + flat + streak≥4", _rule_refined_v3),
    ("s2_adjacent", "S2 + session gap ≤4（excess 有效）", _rule_s2_adjacent),
    ("watch_setup", "蓄力 watch: base streak≥4 sig∈[-1%,+0.5%]", _rule_watch_setup),
    ("watch_setup_rv", "watch + RV 98–99", _rule_watch_setup_rv),
    ("s3_mkt_up", "S3 + 信號日大盤≥0", _rule_s3_mkt_up),
    ("s3_rv_mkt", "S3 RV98-99 + 信號日大盤≥0", _rule_s3_rv_mkt),
    ("s3_burst", "S3 Improving ≥5%（隔日追蹤）", _rule_s3_follow),
    ("s3_rv9899", "S3 + RV 98–99", _rule_s3_rv9899),
]

RULE_FN_MAP: dict[str, RuleFn] = {rid: fn for rid, _, fn in CANDIDATE_RULES}


def _apply_rule(df: pd.DataFrame, rule_fn: RuleFn) -> pd.DataFrame:
    mask = df.apply(rule_fn, axis=1)
    return df.loc[mask].copy()


def _compute_metrics(rule_id: str, label: str, sub: pd.DataFrame) -> RuleMetrics:
    if sub.empty:
        return RuleMetrics(
            rule_id=rule_id,
            label=label,
            n_events=0,
            n_signal_days=0,
            next_mean_pct=0.0,
            next_win_rate=0.0,
            p_next_ge_burst=0.0,
            ew_daily_win_rate=0.0,
            ew_daily_mean_pct=0.0,
        )
    port = sub.groupby("date")["nxt_ret"].mean()
    return RuleMetrics(
        rule_id=rule_id,
        label=label,
        n_events=len(sub),
        n_signal_days=int(sub["date"].nunique()),
        next_mean_pct=round(float(sub["nxt_ret"].mean()), 4),
        next_win_rate=round(float((sub["nxt_ret"] > 0).mean()), 4),
        p_next_ge_burst=round(float((sub["nxt_ret"] >= BURST_PCT).mean()), 4),
        ew_daily_win_rate=round(float((port > 0).mean()), 4),
        ew_daily_mean_pct=round(float(port.mean()), 4),
    )


def _compare_to_baseline(
    rule_sub: pd.DataFrame,
    base_sub: pd.DataFrame,
    *,
    rule_id: str,
    baseline_id: str,
) -> list[SignificanceResult]:
    out: list[SignificanceResult] = []
    if rule_sub.empty or base_sub.empty:
        return out

    # Win rate (event-level)
    wr_r = int((rule_sub["nxt_ret"] > 0).sum())
    wr_b = int((base_sub["nxt_ret"] > 0).sum())
    nr, nb = len(rule_sub), len(base_sub)
    z, p = two_proportion_ztest(wr_r, nr, wr_b, nb)
    out.append(
        SignificanceResult(
            rule_id=rule_id,
            baseline_id=baseline_id,
            metric="next_win_rate",
            rule_value=round(wr_r / nr, 4),
            baseline_value=round(wr_b / nb, 4),
            delta=round(wr_r / nr - wr_b / nb, 4),
            z_stat=round(z, 4),
            p_value=round(p, 6),
            n_rule=nr,
            n_baseline=nb,
            significant_05=p < 0.05 and wr_r / nr > wr_b / nb,
        )
    )

    # Burst hit rate
    br_r = int((rule_sub["nxt_ret"] >= BURST_PCT).sum())
    br_b = int((base_sub["nxt_ret"] >= BURST_PCT).sum())
    z2, p2 = two_proportion_ztest(br_r, nr, br_b, nb)
    out.append(
        SignificanceResult(
            rule_id=rule_id,
            baseline_id=baseline_id,
            metric="p_next_ge_burst",
            rule_value=round(br_r / nr, 4),
            baseline_value=round(br_b / nb, 4),
            delta=round(br_r / nr - br_b / nb, 4),
            z_stat=round(z2, 4),
            p_value=round(p2, 6),
            n_rule=nr,
            n_baseline=nb,
            significant_05=p2 < 0.05 and br_r / nr > br_b / nb,
        )
    )

    # Mean return bootstrap
    lo, hi = bootstrap_mean_diff(
        rule_sub["nxt_ret"].to_numpy(), base_sub["nxt_ret"].to_numpy()
    )
    sig_mean = lo > 0
    out.append(
        SignificanceResult(
            rule_id=rule_id,
            baseline_id=baseline_id,
            metric="next_mean_pct",
            rule_value=round(float(rule_sub["nxt_ret"].mean()), 4),
            baseline_value=round(float(base_sub["nxt_ret"].mean()), 4),
            delta=round(float(rule_sub["nxt_ret"].mean() - base_sub["nxt_ret"].mean()), 4),
            z_stat=None,
            p_value=None,
            n_rule=nr,
            n_baseline=nb,
            significant_05=sig_mean,
            bootstrap_ci_lo=round(lo, 4),
            bootstrap_ci_hi=round(hi, 4),
        )
    )
    return out


def _slice_by_dates(df: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    dset = set(dates)
    return df[df["date"].isin(dset)].copy()


def run_rolling_walkforward(
    df: pd.DataFrame,
    *,
    fold_days: int = 22,
    n_folds: int = 8,
    baseline_id: str = "baseline_s2",
    compare_rules: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rolling OOS folds · count significant win-rate lifts vs baseline."""
    compare_rules = compare_rules or [
        "watch_setup",
        "watch_setup_rv",
        "s2_adjacent",
        "s3_rv9899",
        "s3_rv_mkt",
        "refined_v1",
    ]
    all_dates = sorted(df["date"].unique())
    baseline_fn = RULE_FN_MAP[baseline_id]
    folds: list[dict[str, Any]] = []

    for fi in range(n_folds):
        end_idx = len(all_dates) - fi * fold_days
        start_idx = end_idx - fold_days
        if start_idx < 0:
            break
        fold_dates = all_dates[start_idx:end_idx]
        fold_df = _slice_by_dates(df, fold_dates)
        base_sub = _apply_rule(fold_df, baseline_fn)
        base_wr = float((base_sub["nxt_ret"] > 0).mean()) if len(base_sub) else 0.0
        base_n = len(base_sub)

        rule_results = []
        for rid in compare_rules:
            fn = RULE_FN_MAP.get(rid)
            if fn is None:
                continue
            sub = _apply_rule(fold_df, fn)
            if sub.empty:
                rule_results.append({"rule_id": rid, "n": 0, "win_rate": None, "sig_vs_s2": False})
                continue
            wr = float((sub["nxt_ret"] > 0).mean())
            wins = int((sub["nxt_ret"] > 0).sum())
            base_wins = int((base_sub["nxt_ret"] > 0).sum()) if len(base_sub) else 0
            _, p = two_proportion_ztest(wins, len(sub), base_wins, base_n) if base_n else (0, 1)
            rule_results.append(
                {
                    "rule_id": rid,
                    "n": len(sub),
                    "win_rate": round(wr, 4),
                    "mean_pct": round(float(sub["nxt_ret"].mean()), 4),
                    "p_burst": round(float((sub["nxt_ret"] >= BURST_PCT).mean()), 4),
                    "sig_vs_s2": p < 0.05 and wr > base_wr,
                    "p_vs_s2": round(p, 6),
                }
            )
        folds.append(
            {
                "fold": fi + 1,
                "date_start": fold_dates[0],
                "date_end": fold_dates[-1],
                "baseline_s2_n": base_n,
                "baseline_s2_win_rate": round(base_wr, 4),
                "rules": rule_results,
            }
        )
    return folds


def _aggregate_rolling(folds: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool events across folds for pooled significance test."""
    if not folds:
        return {}
    rule_ids = {r["rule_id"] for f in folds for r in f["rules"]}
    agg: dict[str, Any] = {}
    for rid in rule_ids:
        sig_folds = sum(1 for f in folds for r in f["rules"] if r["rule_id"] == rid and r.get("sig_vs_s2"))
        n_folds_with_data = sum(1 for f in folds for r in f["rules"] if r["rule_id"] == rid and r.get("n", 0) > 0)
        avg_wr = np.mean(
            [r["win_rate"] for f in folds for r in f["rules"] if r["rule_id"] == rid and r.get("win_rate") is not None]
        )
        agg[rid] = {
            "sig_folds": sig_folds,
            "folds_with_data": n_folds_with_data,
            "avg_win_rate": round(float(avg_wr), 4) if not np.isnan(avg_wr) else None,
        }
    return agg


def run_rule_sweep(
    *,
    test_days: int = 22,
    train_days: int = 44,
    min_events: int = 8,
    burst_pct: float = BURST_PCT,
) -> dict[str, Any]:
    df, meta = build_lifecycle_events(burst_pct=burst_pct)
    all_dates = sorted(df["date"].unique())
    if len(all_dates) < test_days + 5:
        raise ValueError(f"Not enough signal days: {len(all_dates)}")

    test_dates = all_dates[-test_days:]
    train_end_idx = len(all_dates) - test_days
    train_dates = all_dates[max(0, train_end_idx - train_days) : train_end_idx]
    prior_dates = all_dates[: train_end_idx - train_days] if train_end_idx > train_days else []

    test_df = _slice_by_dates(df, test_dates)
    train_df = _slice_by_dates(df, train_dates)
    prior_df = _slice_by_dates(df, prior_dates) if prior_dates else pd.DataFrame()

    baseline_id = "baseline_s2"
    baseline_fn = _rule_s2_core

    # Full metrics per window
    windows: dict[str, Any] = {}
    for wname, wdf, wdates in [
        ("test_oos", test_df, test_dates),
        ("train_select", train_df, train_dates),
        ("prior_history", prior_df, prior_dates),
        ("last_month_insample", test_df, test_dates),
    ]:
        if wdf.empty and wname == "prior_history":
            continue
        rules_out = []
        sig_out = []
        base_sub = _apply_rule(wdf, baseline_fn) if not wdf.empty else pd.DataFrame()
        for rid, label, fn in CANDIDATE_RULES:
            sub = _apply_rule(wdf, fn) if not wdf.empty else pd.DataFrame()
            rules_out.append(asdict(_compute_metrics(rid, label, sub)))
            if rid != baseline_id and not sub.empty and not base_sub.empty:
                sig_out.extend(
                    _compare_to_baseline(sub, base_sub, rule_id=rid, baseline_id=baseline_id)
                )
        windows[wname] = {
            "date_start": wdates[0] if wdates else None,
            "date_end": wdates[-1] if wdates else None,
            "n_signal_days": len(wdates),
            "rules": rules_out,
            "vs_baseline_s2": [asdict(s) for s in sig_out],
        }

    # Select best rule on train by win rate with min events + significance
    train_rules = {r["rule_id"]: r for r in windows["train_select"]["rules"]}
    train_sig = windows["train_select"]["vs_baseline_s2"]
    sig_win = {
        s["rule_id"]
        for s in train_sig
        if s["metric"] == "next_win_rate" and s["significant_05"]
    }
    candidates = []
    for rid, label, fn in CANDIDATE_RULES:
        if rid == baseline_id:
            continue
        tr = train_rules.get(rid, {})
        if tr.get("n_events", 0) < min_events:
            continue
        candidates.append(
            {
                "rule_id": rid,
                "label": label,
                "train_win_rate": tr.get("next_win_rate", 0),
                "train_n": tr.get("n_events", 0),
                "train_sig_vs_s2": rid in sig_win,
            }
        )
    candidates.sort(
        key=lambda x: (x["train_sig_vs_s2"], x["train_win_rate"], x["train_n"]),
        reverse=True,
    )
    selected = candidates[0] if candidates else None

    # OOS validation of selected rule vs baseline
    oos_validation: dict[str, Any] | None = None
    if selected:
        sel_fn = RULE_FN_MAP[selected["rule_id"]]
        sel_sub = _apply_rule(test_df, sel_fn)
        base_sub = _apply_rule(test_df, baseline_fn)
        oos_validation = {
            "selected_rule_id": selected["rule_id"],
            "selected_label": selected["label"],
            "test_metrics": asdict(
                _compute_metrics(selected["rule_id"], selected["label"], sel_sub)
            ),
            "baseline_test_metrics": asdict(
                _compute_metrics(baseline_id, "S2 Setup core", base_sub)
            ),
            "significance": [
                asdict(s)
                for s in _compare_to_baseline(
                    sel_sub, base_sub, rule_id=selected["rule_id"], baseline_id=baseline_id
                )
            ],
        }

    # Iterative prior-window check: if selected, also test on prior_history
    prior_validation: dict[str, Any] | None = None
    if selected and not prior_df.empty:
        sel_fn = RULE_FN_MAP[selected["rule_id"]]
        sel_sub = _apply_rule(prior_df, sel_fn)
        base_sub = _apply_rule(prior_df, baseline_fn)
        prior_validation = {
            "selected_rule_id": selected["rule_id"],
            "test_metrics": asdict(
                _compute_metrics(selected["rule_id"], selected["label"], sel_sub)
            ),
            "baseline_metrics": asdict(
                _compute_metrics(baseline_id, "S2 Setup core", base_sub)
            ),
            "significance": [
                asdict(s)
                for s in _compare_to_baseline(
                    sel_sub, base_sub, rule_id=selected["rule_id"], baseline_id=baseline_id
                )
            ],
        }

    # Daily event log for test window (selected + baseline)
    daily_log: list[dict[str, Any]] = []
    if not test_df.empty:
        for d in test_dates:
            day = test_df[test_df["date"] == d]
            row: dict[str, Any] = {"date": d, "bench_nxt_ret": None}
            if not day.empty:
                row["bench_nxt_ret"] = round(float(day["bench_nxt_ret"].iloc[0]), 4)
            for rid, label, fn in CANDIDATE_RULES[:3]:  # baseline + key variants
                if rid not in ("baseline_s2", "refined_v3", "refined_v1"):
                    continue
                picks = day[day.apply(fn, axis=1)]
                if picks.empty:
                    continue
                row[f"{rid}_n"] = len(picks)
                row[f"{rid}_sids"] = picks["sid"].tolist()
                row[f"{rid}_nxt_mean"] = round(float(picks["nxt_ret"].mean()), 4)
                row[f"{rid}_nxt_win"] = bool(picks["nxt_ret"].mean() > 0)
            daily_log.append(row)

    rolling_folds = run_rolling_walkforward(df, fold_days=test_days, n_folds=8)
    rolling_agg = _aggregate_rolling(rolling_folds)

    # Pooled OOS: all data except last test window for rule discovery validation
    pooled_pre_test = df[~df["date"].isin(set(test_dates))]
    pooled_sig: list[dict[str, Any]] = []
    for rid in ("watch_setup", "watch_setup_rv", "s3_rv9899", "s3_rv_mkt", "s2_adjacent"):
        sub = _apply_rule(pooled_pre_test, RULE_FN_MAP[rid])
        base_sub = _apply_rule(pooled_pre_test, baseline_fn)
        if not sub.empty and not base_sub.empty:
            pooled_sig.extend(
                asdict(s)
                for s in _compare_to_baseline(sub, base_sub, rule_id=rid, baseline_id=baseline_id)
            )

    # Full-sample last month (includes test window) for user report — see test_oos window

    return {
        "meta": {
            **meta,
            "test_days": test_days,
            "train_days": train_days,
            "test_window": [test_dates[0], test_dates[-1]],
            "train_window": [train_dates[0], train_dates[-1]] if train_dates else None,
            "prior_window": [prior_dates[0], prior_dates[-1]] if prior_dates else None,
            "min_events": min_events,
        },
        "windows": windows,
        "train_selection": candidates[:5],
        "selected_rule": selected,
        "oos_validation": oos_validation,
        "prior_validation": prior_validation,
        "test_daily_log": daily_log,
        "rolling_walkforward": rolling_folds,
        "rolling_aggregate": rolling_agg,
        "pooled_pre_test_significance": pooled_sig,
        "generated_on": date.today().isoformat(),
    }


def render_sweep_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = [
        "# RRG Improving lifecycle · monthly rule sweep",
        "",
        f"- Test OOS window: **{meta['test_window'][0]}** → **{meta['test_window'][1]}** "
        f"（{meta['test_days']} trading days）",
        f"- Train selection window: **{meta['train_window'][0]}** → **{meta['train_window'][1]}** "
        f"（{meta['train_days']} days）",
        f"- Universe: **{meta['universe_size']}** · max bar **{meta['max_bar_date']}**",
        f"- Generated: {result.get('generated_on', '')}",
        "",
        "## Train window · rule ranking vs S2 baseline",
        "",
        "| Rule | N | 隔日勝率 | P(≥5%) | 隔日均值 | 顯著勝率↑ vs S2 |",
        "|------|---|---------|--------|---------|----------------|",
    ]
    train = result["windows"]["train_select"]
    sig_map = {
        s["rule_id"]: s
        for s in train["vs_baseline_s2"]
        if s["metric"] == "next_win_rate"
    }
    for r in sorted(train["rules"], key=lambda x: -x["next_win_rate"]):
        if r["n_events"] == 0:
            continue
        sig = sig_map.get(r["rule_id"])
        sig_mark = ""
        if sig and sig["significant_05"]:
            sig_mark = f"✓ p={sig['p_value']:.4f}"
        elif sig and r["rule_id"] != "baseline_s2":
            sig_mark = f"p={sig['p_value']:.4f}"
        lines.append(
            f"| {r['rule_id']} | {r['n_events']} | {r['next_win_rate']:.1%} | "
            f"{r['p_next_ge_burst']:.1%} | {r['next_mean_pct']:+.3f}% | {sig_mark} |"
        )

    lines.extend(["", "## Test OOS window（held-out 近一個月）", ""])
    test = result["windows"]["test_oos"]
    lines.append("| Rule | N | 隔日勝率 | P(≥5%) | 隔日均值 |")
    lines.append("|------|---|---------|--------|---------|")
    for r in sorted(test["rules"], key=lambda x: -x["next_win_rate"]):
        if r["n_events"] == 0:
            continue
        lines.append(
            f"| {r['rule_id']} | {r['n_events']} | {r['next_win_rate']:.1%} | "
            f"{r['p_next_ge_burst']:.1%} | {r['next_mean_pct']:+.3f}% |"
        )

    sel = result.get("selected_rule")
    oos = result.get("oos_validation")
    if sel and oos:
        lines.extend(
            [
                "",
                f"## Selected rule（train 選出）: **{sel['rule_id']}**",
                "",
                f"- Train: win rate **{sel['train_win_rate']:.1%}** · n={sel['train_n']} · "
                f"sig vs S2={sel['train_sig_vs_s2']}",
                "",
                "### OOS validation vs S2 baseline",
                "",
            ]
        )
        for s in oos["significance"]:
            if s["metric"] == "next_win_rate":
                lines.append(
                    f"- Win rate: **{s['rule_value']:.1%}** vs baseline **{s['baseline_value']:.1%}** "
                    f"(Δ={s['delta']:+.1%}, p={s['p_value']}, n={s['n_rule']})"
                )
            elif s["metric"] == "p_next_ge_burst":
                lines.append(
                    f"- P(≥5%): **{s['rule_value']:.1%}** vs **{s['baseline_value']:.1%}** "
                    f"(Δ={s['delta']:+.1%}, p={s['p_value']})"
                )
            elif s["metric"] == "next_mean_pct":
                lines.append(
                    f"- Mean: **{s['rule_value']:+.3f}%** vs **{s['baseline_value']:+.3f}%** "
                    f"(bootstrap 95% CI [{s['bootstrap_ci_lo']:+.3f}, {s['bootstrap_ci_hi']:+.3f}])"
                )

    pv = result.get("prior_validation")
    if pv:
        lines.extend(["", "## Prior window 反覆驗證（train 之前同期）", ""])
        for s in pv["significance"]:
            if s["metric"] == "next_win_rate":
                lines.append(
                    f"- Win rate: **{s['rule_value']:.1%}** vs S2 **{s['baseline_value']:.1%}** "
                    f"(p={s['p_value']}, n={s['n_rule']})"
                )

    rolls = result.get("rolling_walkforward", [])
    if rolls:
        lines.extend(["", "## Rolling walk-forward（8×22 交易日 fold · vs S2）", ""])
        lines.append("| Fold | 區間 | S2 n/勝率 | watch_setup | s3_rv9899 | s3_rv_mkt |")
        lines.append("|------|------|-----------|-------------|-----------|-----------|")
        for f in rolls:
            def _cell(rid: str) -> str:
                for r in f["rules"]:
                    if r["rule_id"] == rid and r.get("n", 0) > 0:
                        mark = "*" if r.get("sig_vs_s2") else ""
                        return f"{r['win_rate']:.0%} n={r['n']}{mark}"
                return "—"

            lines.append(
                f"| {f['fold']} | {f['date_start']}→{f['date_end']} | "
                f"{f['baseline_s2_win_rate']:.0%} n={f['baseline_s2_n']} | "
                f"{_cell('watch_setup')} | {_cell('s3_rv9899')} | {_cell('s3_rv_mkt')} |"
            )
        agg = result.get("rolling_aggregate", {})
        if agg:
            lines.append("")
            lines.append("**Rolling aggregate**（* = fold 內勝率顯著優於 S2, p<0.05）")
            for rid, a in sorted(agg.items(), key=lambda x: -(x[1].get("avg_win_rate") or 0)):
                if a.get("folds_with_data", 0) == 0:
                    continue
                lines.append(
                    f"- `{rid}`: avg win **{a['avg_win_rate']:.1%}** · "
                    f"sig folds **{a['sig_folds']}/{a['folds_with_data']}**"
                )

    pooled = result.get("pooled_pre_test_significance", [])
    sig_wr = [s for s in pooled if s["metric"] == "next_win_rate" and s["significant_05"]]
    if sig_wr:
        lines.extend(["", "## Pooled pre-test significance（vs S2 · 全歷史除 OOS 月）", ""])
        for s in sig_wr:
            lines.append(
                f"- **{s['rule_id']}**: win **{s['rule_value']:.1%}** vs S2 **{s['baseline_value']:.1%}** "
                f"(Δ={s['delta']:+.1%}, p={s['p_value']}, n={s['n_rule']})"
            )

    lines.extend(["", "## Test window daily log（S2 vs refined）", ""])
    for row in result.get("test_daily_log", []):
        if row.get("baseline_s2_n", 0) or row.get("refined_v3_n", 0):
            lines.append(
                f"- **{row['date']}** · mkt {row.get('bench_nxt_ret', 0):+.2f}% · "
                f"S2 n={row.get('baseline_s2_n', 0)} mean={row.get('baseline_s2_nxt_mean', '—')} · "
                f"v3 n={row.get('refined_v3_n', 0)} mean={row.get('refined_v3_nxt_mean', '—')}"
            )

    return "\n".join(lines) + "\n"
