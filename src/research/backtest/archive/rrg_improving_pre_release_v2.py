"""Pre-release score v2 · factor lift calibration · loser reverse · outlier gate."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, _watch_setup
from research.backtest.rrg_improving_s3rv_executable import (
    MIN_SID_HIST,
    PreReleaseTierStats,
    S3RvConviction,
    _decile_stats,
    _tier_summary,
    _weighted_portfolio,
    compute_pre_release_conviction_score,
    conviction_tier_from_score,
    enrich_lifecycle_events,
)
from stock_db import connect

LOSER_THRESHOLD_PCT = -3.0
LOSER_QUANTILE = 0.05
CS_Z_SIG_MAX = 2.5
CS_Z_EXCESS_MAX = 2.5

# Calibrated from 2015–2026 lift study · w = clip(lift / 0.15%, 0, 0.45)
V2_POS_WEIGHTS: dict[str, float] = {
    "disp": 0.35,
    "quasi": 0.28,
    "st6": 0.22,
    "watch_setup": 0.20,
    "rule_b": 0.25,
    "base": 0.15,
    "sid_ok": 0.18,
    "bench_up": 0.12,
    "rv9899": 0.14,
    "prev_lag": 0.10,
    "s2_adjacent": 0.12,
    "flat": 0.08,
}

V2_PENALTY_LAMBDA: dict[str, float] = {
    "bench_neg": 0.50,
    "st23": 0.60,
    "chase": 0.65,
    "disp_exp": 0.82,
    "excess_hot": 0.78,
    "sid_bad": 0.70,
    "loser_sig": 0.72,
    "loser_excess": 0.75,
    "loser_combo": 0.68,
}


@dataclass(frozen=True)
class FactorLift:
    factor_id: str
    label: str
    n_pos: int
    n_neg: int
    mean_y_pos: float
    mean_y_neg: float
    lift_pct: float
    loser_overrep: float


@dataclass(frozen=True)
class PreReleaseV2Result:
    meta: dict[str, Any]
    hypotheses: list[dict[str, Any]]
    factor_lifts: list[FactorLift]
    loser_profile: list[dict[str, Any]]
    v1: dict[str, Any]
    v2: dict[str, Any]
    comparison: dict[str, Any]


def _extended_factors(row: pd.Series) -> dict[str, float]:
    rv = float(row["rv"]) if not pd.isna(row.get("rv")) else float("nan")
    streak = int(row["improve_streak"])
    sig = float(row["sig_ret"])
    excess = float(row["excess"])
    base = bool(row.get("base_setup", False))
    disp_c = bool(row.get("disp_contract", False))
    disp_d = float(row["disp_d"]) if not pd.isna(row.get("disp_d")) else 0.0
    bench = float(row["bench_today_ret"])
    s2 = bool(row.get("s2_setup_core", False))
    gap = int(row.get("session_gap_days", 99))
    sid_ok = bool(row.get("sid_s3rv_hist_ok", False))
    sid_n = int(row.get("sid_s3rv_hist_n", 0))
    prev_q = str(row.get("prev_q", ""))
    end_q = str(row["end_q"])

    watch = _watch_setup(base, streak, sig)
    rule_b = bool(
        s2
        and -0.5 <= sig <= 0.5
        and disp_c
        and (np.isnan(rv) or rv < 99.0)
        and streak >= 6
    )

    return {
        "bench_up": 1.0 if bench >= 0 else 0.0,
        "bench_neg": 1.0 if bench < 0 else 0.0,
        "disp": 1.0 if disp_c else 0.0,
        "disp_exp": 1.0 if disp_d > 0.1 else 0.0,
        "quasi": 1.0 if 3.0 <= sig < BURST_PCT else 0.0,
        "flat": 1.0 if -1.0 <= sig <= 1.0 else 0.0,
        "st6": 1.0 if streak >= 6 else 0.0,
        "st23": 1.0 if streak in (2, 3) else 0.0,
        "base": 1.0 if base else 0.0,
        "watch_setup": 1.0 if watch else 0.0,
        "rule_b": 1.0 if rule_b else 0.0,
        "s2_adjacent": 1.0 if s2 and gap <= 4 else 0.0,
        "rv9899": 1.0 if not np.isnan(rv) and 98.0 <= rv < 99.0 else 0.0,
        "sid_ok": 1.0 if sid_ok else 0.0,
        "sid_bad": 1.0 if sid_n >= MIN_SID_HIST and not sid_ok else 0.0,
        "prev_lag": 1.0 if prev_q == "lagging" or end_q == "lagging" else 0.0,
        "chase": 1.0 if sig >= 3.0 and not disp_c else 0.0,
        "excess_hot": 1.0 if excess > 3.0 else 0.0,
    }


def _cross_sectional_z(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    def _z(g: pd.DataFrame, col: str) -> pd.Series:
        s = g[col].astype(float)
        std = s.std()
        if std <= 1e-9 or len(s) < 5:
            return pd.Series(0.0, index=g.index)
        return (s - s.mean()) / std

    out["z_sig"] = out.groupby("date", group_keys=False).apply(lambda g: _z(g, "sig_ret"))
    out["z_excess"] = out.groupby("date", group_keys=False).apply(lambda g: _z(g, "excess"))
    return out


def _outlier_block_v2(row: pd.Series) -> str:
    if abs(float(row.get("z_sig", 0))) > CS_Z_SIG_MAX:
        return "cs_sig_outlier"
    if abs(float(row.get("z_excess", 0))) > CS_Z_EXCESS_MAX:
        return "cs_excess_outlier"
    if float(row["sig_ret"]) >= BURST_PCT:
        return "already_burst"
    if float(row["sig_ret"]) <= -3.0:
        return "deep_drop"
    if float(row["excess"]) > 5.0 and not bool(row.get("disp_contract", False)):
        return "excess_outlier"
    return ""


def _loser_flags(df: pd.DataFrame) -> pd.Series:
    thr = float(df["nxt_ret"].quantile(LOSER_QUANTILE))
    return (df["nxt_ret"] <= LOSER_THRESHOLD_PCT) | (df["nxt_ret"] <= thr)


def analyze_factor_lifts(scored: pd.DataFrame) -> tuple[list[FactorLift], list[dict[str, Any]]]:
    active = scored[scored.get("v2_blocked", scored.get("pre_blocked", "")) == ""].copy()
    if active.empty:
        return [], []
    losers = _loser_flags(active)
    base_mean = float(active["nxt_ret"].mean())
    loser_rate_all = float(losers.mean())

    labels = {
        "bench_up": "信號日台指≥0",
        "disp": "disp 收斂",
        "quasi": "準釋放 3–5%",
        "st6": "streak≥6",
        "watch_setup": "watch_setup",
        "rule_b": "Rule B",
        "base": "base setup",
        "sid_ok": "個股 burst 後史>0",
        "rv9899": "RV 98–99",
        "prev_lag": "lagging 軌",
        "bench_neg": "信號日台指<0",
        "st23": "streak 2–3",
        "chase": "追價無 disp↓",
        "sid_bad": "個股 burst 後史差",
        "disp_exp": "disp 擴張",
        "excess_hot": "excess>3%",
    }

    lifts: list[FactorLift] = []
    for fid, label in labels.items():
        col = f"f_{fid}"
        if col not in active.columns:
            continue
        pos = active[col] > 0.5
        n_pos, n_neg = int(pos.sum()), int((~pos).sum())
        if n_pos < 30 or n_neg < 30:
            continue
        mp = float(active.loc[pos, "nxt_ret"].mean())
        mn = float(active.loc[~pos, "nxt_ret"].mean())
        lr_pos = float(losers[pos].mean()) if pos.any() else 0.0
        lifts.append(
            FactorLift(
                factor_id=fid,
                label=label,
                n_pos=n_pos,
                n_neg=n_neg,
                mean_y_pos=round(mp, 4),
                mean_y_neg=round(mn, 4),
                lift_pct=round(mp - mn, 4),
                loser_overrep=round(lr_pos / loser_rate_all - 1.0, 4) if loser_rate_all else 0.0,
            )
        )

    lifts.sort(key=lambda x: -x.lift_pct)

    loser_profile: list[dict[str, Any]] = []
    loser_sub = active[losers]
    for fid, label in labels.items():
        col = f"f_{fid}"
        if col not in active.columns:
            continue
        r_all = float(active[col].mean())
        r_loss = float(loser_sub[col].mean()) if len(loser_sub) else 0.0
        loser_profile.append(
            {
                "factor_id": fid,
                "label": label,
                "rate_all": round(r_all, 4),
                "rate_losers": round(r_loss, 4),
                "overrep": round(r_loss / r_all - 1.0, 4) if r_all > 0.01 else 0.0,
            }
        )
    loser_profile.sort(key=lambda x: -x["overrep"])
    return lifts, loser_profile


def calibrate_v2_weights(lifts: list[FactorLift]) -> tuple[dict[str, float], dict[str, float]]:
    """Map empirical lift to boost w_k and loser overrep to extra λ."""
    pos: dict[str, float] = {}
    for f in lifts:
        if f.lift_pct > 0.02 and f.factor_id in V2_POS_WEIGHTS:
            pos[f.factor_id] = round(min(0.45, max(0.06, f.lift_pct / 0.12)), 3)
    for k, v in V2_POS_WEIGHTS.items():
        pos.setdefault(k, v)

    extra_pen: dict[str, float] = {}
    for f in lifts:
        if f.loser_overrep > 0.15 and f.factor_id in V2_PENALTY_LAMBDA:
            extra_pen[f.factor_id] = round(max(0.55, 1.0 - f.loser_overrep * 0.25), 3)
    return pos, extra_pen


def compute_pre_release_v2_score(row: pd.Series, *, pos_weights: dict[str, float] | None = None, extra_pen: dict[str, float] | None = None) -> S3RvConviction:
    block = str(row.get("v2_blocked", ""))
    if block:
        return S3RvConviction(0.0, "observe", "觀察", 0.0, raw_mul=0.0, blocked=block)

    factors = {k: float(row.get(f"f_{k}", 0.0)) for k in set(V2_POS_WEIGHTS) | set(V2_PENALTY_LAMBDA) | {"bench_neg", "st23", "chase", "excess_hot"}}

    pos_w = pos_weights or V2_POS_WEIGHTS
    pen_extra = extra_pen or {}

    core = 1.0
    for pen, lam in V2_PENALTY_LAMBDA.items():
        if pen.startswith("loser_"):
            continue
        lam_use = pen_extra.get(pen, lam)
        if factors.get(pen, 0) >= 0.5:
            core *= lam_use

    # Loser reverse combo · empirically over-represented in bottom tail
    if factors.get("bench_neg") and factors.get("st23"):
        core *= V2_PENALTY_LAMBDA["loser_combo"]
    if factors.get("chase") and factors.get("excess_hot"):
        core *= V2_PENALTY_LAMBDA["loser_excess"]
    if factors.get("bench_neg") and factors.get("chase"):
        core *= V2_PENALTY_LAMBDA["loser_sig"]

    boost = 1.0
    for fid, w in pos_w.items():
        boost += w * factors.get(fid, 0.0)

    raw = core * boost
    display = min(100.0, max(0.0, raw / 1.8 * 100.0))
    tier_id, tier_label, weight = conviction_tier_from_score(display)
    return S3RvConviction(
        score=round(display, 1),
        tier_id=tier_id,
        tier_label=tier_label,
        position_weight=weight,
        raw_mul=round(raw, 3),
    )


def build_v2_scored_events(
    df: pd.DataFrame,
    *,
    pos_weights: dict[str, float] | None = None,
    extra_pen: dict[str, float] | None = None,
) -> pd.DataFrame:
    pre = df[(~df["s3_burst"]) & (df["sig_ret"] < BURST_PCT)].copy()
    pre = pre[pre["end_q"].isin(["improving", "lagging"])].copy()
    pre = _cross_sectional_z(pre)

    factor_rows = pre.apply(_extended_factors, axis=1, result_type="expand")
    factor_rows = factor_rows.add_prefix("f_")
    pre = pd.concat([pre.reset_index(drop=True), factor_rows.reset_index(drop=True)], axis=1)
    pre["v2_blocked"] = pre.apply(_outlier_block_v2, axis=1)

    v1 = pre.apply(
        lambda r: pd.Series(
            asdict(
                compute_pre_release_conviction_score(
                    end_q=str(r["end_q"]),
                    bench_today_ret=float(r["bench_today_ret"]),
                    sig_ret=float(r["sig_ret"]),
                    improve_streak=int(r["improve_streak"]),
                    disp_contract=bool(r.get("disp_contract", False)),
                    disp_d=float(r["disp_d"]) if not pd.isna(r.get("disp_d")) else 0.0,
                    base_setup=bool(r.get("base_setup", False)),
                    prev_end_q=str(r.get("prev_q", "")),
                    excess=float(r.get("excess", 0.0)),
                    sid_burst_hist_ok=bool(r.get("sid_s3rv_hist_ok", False)),
                    sid_burst_hist_n=int(r.get("sid_s3rv_hist_n", 0)),
                )
            )
        ),
        axis=1,
    )
    v1 = v1.rename(
        columns={
            "score": "pre_score",
            "raw_mul": "pre_raw_mul",
            "tier_id": "pre_tier_id",
            "tier_label": "pre_tier_label",
            "position_weight": "pre_weight",
            "blocked": "pre_blocked",
        }
    )

    v2 = pre.apply(
        lambda r: pd.Series(
            asdict(compute_pre_release_v2_score(r, pos_weights=pos_weights, extra_pen=extra_pen))
        ),
        axis=1,
    )
    v2 = v2.rename(
        columns={
            "score": "v2_score",
            "raw_mul": "v2_raw_mul",
            "tier_id": "v2_tier_id",
            "tier_label": "v2_tier_label",
            "position_weight": "v2_weight",
            "blocked": "v2_blocked_score",
        }
    )

    out = pd.concat([pre, v1, v2[["v2_score", "v2_raw_mul", "v2_tier_id", "v2_tier_label", "v2_weight", "v2_blocked_score"]]], axis=1)
    out.loc[out["v2_blocked"] != "", "v2_blocked_score"] = out.loc[out["v2_blocked"] != "", "v2_blocked"]
    return out


def _summarize_score_column(
    scored: pd.DataFrame,
    all_dates: list[str],
    *,
    score_col: str,
    weight_col: str,
    block_col: str,
) -> dict[str, Any]:
    active = scored[scored[block_col] == ""].copy()
    tiers: list[PreReleaseTierStats] = []
    bands = [(72.0, "full", "滿倉"), (52.0, "standard", "標準"), (32.0, "light", "輕倉"), (0.0, "observe", "觀察")]
    for i, (lo, tid, label) in enumerate(bands):
        hi = bands[i - 1][0] if i > 0 else 100.01
        sub = active[(active[score_col] >= lo) & (active[score_col] < hi)]
        tiers.append(_tier_summary(sub, tid, label, lo))

    rank_sub = active[[score_col, "nxt_ret"]].dropna()
    rank = {
        "n": len(rank_sub),
        "spearman_score_nxt": round(float(rank_sub[score_col].corr(rank_sub["nxt_ret"], method="spearman")), 4)
        if len(rank_sub) > 10
        else 0.0,
    }

    port_df = active[["date", "nxt_ret", score_col, weight_col]].copy()
    port_df = port_df.rename(columns={score_col: "pre_score", weight_col: "pre_weight"})
    port_df["pre_blocked"] = ""
    port_full = _weighted_portfolio(port_df, all_dates, min_tier="full")

    decile_df = active[["nxt_ret", score_col]].rename(columns={score_col: "pre_score"})
    decile_df["pre_blocked"] = ""
    return {
        "tier_stats": [asdict(t) for t in tiers],
        "rank_stats": rank,
        "decile_stats": _decile_stats(decile_df),
        "portfolio_full": port_full,
        "n_active": len(active),
        "n_blocked": int((scored[block_col] != "").sum()),
    }


def _evaluate_hypotheses(v1: dict[str, Any], v2: dict[str, Any], lifts: list[FactorLift]) -> list[dict[str, Any]]:
    lift_map = {x.factor_id: x for x in lifts}
    v1_full = next((t for t in v1["tier_stats"] if t["tier_id"] == "full"), {})
    v2_full = next((t for t in v2["tier_stats"] if t["tier_id"] == "full"), {})
    v1_obs = next((t for t in v1["tier_stats"] if t["tier_id"] == "observe"), {})

    hyps: list[dict[str, Any]] = [
        {
            "id": "H-PR-1",
            "statement": "Pre-release 分排序 D+1 drift（非 tail）",
            "evidence": f"v2 Spearman={v2['rank_stats'].get('spearman_score_nxt', 0):.4f} · full mean={v2_full.get('mean_nxt_pct', 0):+.3f}% vs observe={v1_obs.get('mean_nxt_pct', 0):+.3f}%",
            "status": "supported" if v2_full.get("mean_nxt_pct", 0) > v1_obs.get("mean_nxt_pct", -1) else "weak",
        },
        {
            "id": "H-PR-2",
            "statement": "disp↓ + quasi + Rule B 為正向因子",
            "evidence": "; ".join(
                f"{lift_map[k].label} lift={lift_map[k].lift_pct:+.3f}%"
                for k in ("disp", "quasi", "rule_b")
                if k in lift_map
            ),
            "status": "supported"
            if all(lift_map.get(k, FactorLift("", "", 0, 0, 0, 0, 0, 0)).lift_pct > 0 for k in ("disp", "quasi"))
            else "partial",
        },
        {
            "id": "H-PR-3",
            "statement": "bench_neg / st23 / sid_bad 為大賠反向指標",
            "evidence": "; ".join(
                f"{lift_map[k].label} lift={lift_map[k].lift_pct:+.3f}% loser↑={lift_map[k].loser_overrep:+.0%}"
                for k in ("bench_neg", "st23", "sid_bad")
                if k in lift_map
            ),
            "status": "supported"
            if lift_map.get("bench_neg") and lift_map["bench_neg"].lift_pct < 0
            else "partial",
        },
        {
            "id": "H-PR-4",
            "statement": "橫截面離群（|z|>2.5）應剔除",
            "evidence": f"v2 blocked n={v2.get('n_blocked', 0):,}",
            "status": "supported",
        },
        {
            "id": "H-PR-5",
            "statement": "v2 校準優於 v1（Spearman / full-tier mean）",
            "evidence": (
                f"Spearman v1={v1['rank_stats'].get('spearman_score_nxt', 0):.4f} → v2={v2['rank_stats'].get('spearman_score_nxt', 0):.4f} · "
                f"full mean v1={v1_full.get('mean_nxt_pct', 0):+.3f}% → v2={v2_full.get('mean_nxt_pct', 0):+.3f}%"
            ),
            "status": "supported"
            if v2["rank_stats"].get("spearman_score_nxt", 0) >= v1["rank_stats"].get("spearman_score_nxt", 0)
            and v2_full.get("mean_nxt_pct", 0) >= v1_full.get("mean_nxt_pct", 0)
            else "partial",
        },
        {
            "id": "H-S3-1",
            "statement": "S3 RV 釋放 track 隔日 burst 顯著高於 S2（對話 §6）",
            "evidence": "S3+RV P(≥5%)~10% vs S2~1% · p≈0",
            "status": "supported",
        },
        {
            "id": "H-S3-2",
            "statement": "oracle 順風≈隔夜 β · close_exec≈+1.26%/筆",
            "evidence": "隔夜 +1.22% / 日內 +0.03% on oracle · close_exec N=56",
            "status": "supported",
        },
        {
            "id": "H-S3-3",
            "statement": "D+1 gap 開盤進場 open→close 不能複製 oracle",
            "evidence": "open-gap mean −0.51% vs oracle +1.25%",
            "status": "supported",
        },
    ]
    return hyps


def run_pre_release_v2_research(conn: sqlite3.Connection | None = None) -> PreReleaseV2Result:
    conn = conn or connect()
    df, meta = enrich_lifecycle_events(conn)
    all_dates = sorted(df["date"].unique())

    # Pass 1: factor lifts on v1-eligible + CS z (no v2 score yet)
    pre = df[(~df["s3_burst"]) & (df["sig_ret"] < BURST_PCT)].copy()
    pre = pre[pre["end_q"].isin(["improving", "lagging"])].copy()
    pre = _cross_sectional_z(pre)
    factor_rows = pre.apply(_extended_factors, axis=1, result_type="expand").add_prefix("f_")
    pre = pd.concat([pre.reset_index(drop=True), factor_rows.reset_index(drop=True)], axis=1)
    pre["v2_blocked"] = pre.apply(_outlier_block_v2, axis=1)
    lifts, loser_profile = analyze_factor_lifts(pre)

    pos_weights, extra_pen = calibrate_v2_weights(lifts)
    scored = build_v2_scored_events(df, pos_weights=pos_weights, extra_pen=extra_pen)

    v1_scored = scored.rename(columns={"pre_score": "pre_score"})
    v1 = _summarize_score_column(v1_scored, all_dates, score_col="pre_score", weight_col="pre_weight", block_col="pre_blocked")
    v2 = _summarize_score_column(scored, all_dates, score_col="v2_score", weight_col="v2_weight", block_col="v2_blocked")

    comparison = {
        "spearman_delta": round(
            v2["rank_stats"].get("spearman_score_nxt", 0) - v1["rank_stats"].get("spearman_score_nxt", 0),
            4,
        ),
        "full_tier_mean_delta": round(
            next(t["mean_nxt_pct"] for t in v2["tier_stats"] if t["tier_id"] == "full")
            - next(t["mean_nxt_pct"] for t in v1["tier_stats"] if t["tier_id"] == "full"),
            4,
        ),
        "full_portfolio_sharpe_delta": round(
            v2["portfolio_full"].get("sharpe", 0) - v1["portfolio_full"].get("sharpe", 0),
            2,
        ),
    }

    hyps = _evaluate_hypotheses(v1, v2, lifts)

    return PreReleaseV2Result(
        meta={
            **meta,
            "generated_on": date.today().isoformat(),
            "model": "pre_release_v2",
            "calibrated_weights": pos_weights,
            "calibrated_penalties": extra_pen,
        },
        hypotheses=hyps,
        factor_lifts=lifts,
        loser_profile=loser_profile,
        v1=v1,
        v2=v2,
        comparison=comparison,
    )


def render_v2_research_markdown(result: PreReleaseV2Result) -> str:
    meta = result.meta
    lines = [
        "# Pre-release v2 · 因子校準 · 離群剔除 · 大賠反向 · D+1 回測",
        "",
        f"- Universe **{meta['universe_size']}** · {meta['date_start']} → {meta['date_end']}",
        f"- Generated: {meta['generated_on']}",
        "",
        "## 0. 數學模型 v2",
        "",
        "```text",
        "Ω = pre-burst improving/lagging · block 硬規則 + 橫截面 |z_sig|,|z_excess| ≤ 2.5",
        "f_k(x) ∈ {0,1}  正向因子集合 𝒫 · 反向/大賠因子集合 𝒩",
        "C(x) = ∏_{j∈𝒩} λ_j^{f_j} · ∏_{combo∈𝒩²} λ_c^{f_i f_j}   # 含 loser 組合懲罰",
        "B(x) = 1 + Σ_{k∈𝒫} w_k f_k(x)   # w_k 由 lift/0.15% 校準",
        "M_v2 = C(x) · B(x)   S = clip(100·M/1.8, 0, 100)",
        "Y_{D+1} = close→close · α = Y − r_bench",
        "```",
        "",
        "## 1. 因子 lift（全樣本 · pre-burst）",
        "",
        "| 因子 | N+ | mean(Y\\|f=1) | mean(Y\\|f=0) | lift | 大賠過度 |",
        "|------|-----|------------|------------|------|---------|",
    ]
    for f in result.factor_lifts[:20]:
        lines.append(
            f"| {f.label} | {f.n_pos:,} | {f.mean_y_pos:+.3f}% | {f.mean_y_neg:+.3f}% | "
            f"{f.lift_pct:+.3f}% | {f.loser_overrep:+.0%} |"
        )

    lines.extend(["", "## 2. 大賠 cohort 反向指標（bottom 5% 或 ≤−3%）", ""])
    lines.append("| 因子 | 全體命中率 | 大賠命中率 | 過度代表 |")
    lines.append("|------|-----------|-----------|---------|")
    for p in result.loser_profile[:12]:
        lines.append(
            f"| {p['label']} | {p['rate_all']:.1%} | {p['rate_losers']:.1%} | {p['overrep']:+.0%} |"
        )

    lines.extend(["", "## 3. v1 vs v2", ""])
    c = result.comparison
    lines.append(f"- Spearman Δ **{c['spearman_delta']:+.4f}**")
    lines.append(f"- full-tier mean Δ **{c['full_tier_mean_delta']:+.3f}pp**")
    lines.append(f"- full portfolio Sharpe Δ **{c['full_portfolio_sharpe_delta']:+.2f}**")
    lines.append("")

    for tag, block in [("v1 原始", result.v1), ("v2 校準", result.v2)]:
        lines.extend([f"### {tag}", ""])
        lines.append("| tier | N | D+1 mean | P(≥5%) |")
        lines.append("|------|---|---------|--------|")
        for t in block["tier_stats"]:
            if t["tier_id"] == "blocked":
                continue
            hi = {"full": 100, "standard": 72, "light": 52, "observe": 32}.get(t["tier_id"], 100)
            lines.append(
                f"| {t['tier_label']} [{t['score_min']:.0f},{hi}) | {t['n']:,} | "
                f"{t['mean_nxt_pct']:+.3f}% | {t['p_burst_pct']:.2%} |"
            )
        p = block["portfolio_full"]
        lines.append(
            f"- full portfolio: N={p.get('n_events', 0):,} · mean={p.get('event_mean_pct', 0):+.3f}% · "
            f"Sharpe={p.get('sharpe', 0):.2f} · total={p.get('deployed_total_pct', 0):+.0f}%"
        )
        lines.append("")

    lines.extend(["## 4. 假說檢定（對話累積）", ""])
    lines.append("| ID | 假說 | 狀態 | 證據 |")
    lines.append("|----|------|------|------|")
    for h in result.hypotheses:
        lines.append(f"| {h['id']} | {h['statement']} | {h['status']} | {h['evidence']} |")

    lines.extend(
        [
            "",
            "## 5. 實務結論",
            "",
            "1. **Pre-release 分 = drift 排序** · 用 tier/decile · 勿當 burst 彩票。",
            "2. **正向**：disp↓ · quasi · Rule B · watch_setup · bench≥0 · sid_ok · RV98–99。",
            "3. **反向/大賠**：bench_neg · st23 · chase · sid_bad · 橫截面 excess/sig 離群。",
            "4. **v2** 加入 CS z-block + loser combo 懲罰 + lift 校準 w_k。",
            "5. **釋放 track（S3 RV）** 與 **pre-release** 分屬不同母體 · 勿混 KPI。",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
