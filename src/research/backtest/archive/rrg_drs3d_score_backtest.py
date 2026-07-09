"""RRG 3-day rightward displacement (drs_3d) scoring backtest · PIT pre-release pool.

Primary outcome: drs_3d = RSR(D+3) − RSR(D) using WMA(20) RS-Ratio.
Auxiliary: flip_leading_3d, ret_3d.

Do not mix with overnight (nxt_ret) research.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.archive.rrg_pre_release_funnel_lanes import _atom_mask
from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (
    HOLD_DAYS,
    JUNE2026_LEADING_STREAK_CASES,
    _find_pre_release_signal,
    _pre_release_pool_mask,
    _years_span,
    build_pre_release_funnel_panel_3d,
)
from rrg_mono_daily_brief import _feat
from rrg_rotation import DEFAULT_LENGTH, compute_rrg_panel
from rank_stats import spearman_correlation
from stock_db import PROJECT_ROOT

# Heuristic score weights (conversation defaults · daily WMA20)
DEFAULT_WEIGHTS: dict[str, float] = {
    "w_proximity": 1.0,   # w1 · (100−RSR)⁻¹ proxy near Leading boundary
    "w_velocity": 0.5,    # w2 · max(ΔRSR, 0) · 4-day lookback dr
    "w_spread": 0.3,      # w3 · max(spread_rs, 0) · daily proxy = drs_rsr_1d
    "w_mono": 0.4,        # w4 · mono_up trajectory
    "penalty_high_disp": 0.35,
    "penalty_chase": 0.25,
    "disp_high_cutoff": 1.2,
    "chase_sig_ret": 3.0,
}

# Kinematic (trajectory-differential) score · motion-first · proximity optional
DEFAULT_KINEMATIC_WEIGHTS: dict[str, float] = {
    "w_proximity": 0.0,
    "w_velocity": 0.6,
    "w_heading": 0.5,
    "w_spread": 0.3,
    "w_mono": 0.4,
    "w_accel": 0.2,
    "penalty_high_disp": 0.35,
    "penalty_chase": 0.25,
    "disp_high_cutoff": 1.2,
    "chase_sig_ret": 3.0,
}

_HEADING_EPS = 1e-6

RSR_BAND_LO = 98.0
RSR_BAND_HI = 100.0
DISP_LO = 0.25
DISP_HI = 1.2
MIN_IMPROVE_STREAK = 3

# B1 · two-stage funnel lanes (Stage-1 structural + Stage-2 price atoms)
# H-B1 (score_d10 ∩ flat ∩ b1 ∩ disp): archived for ret_3d deploy unless §I verdict = GO
TWO_STAGE_LANES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("score_d10", "Score D10 only (Ω)", ("score_d10",)),
    ("score_top_flat_b1_disp", "Score D10 ∩ flat ∩ b1 ∩ disp↓ (H-B1)", ("score_d10", "flat", "b1", "disp")),
    ("score_top_flat_b0_disp", "Score D10 ∩ flat ∩ b0 ∩ disp↓", ("score_d10", "flat", "b0", "disp")),
    ("score_top_flat_disp", "Score D10 ∩ flat ∩ disp↓", ("score_d10", "flat", "disp")),
    ("lead3_only", "lead3 (structural)", ("lead3",)),
    ("lead3_flat_b1_disp", "lead3 ∩ flat ∩ b1 ∩ disp↓", ("lead3", "flat", "b1", "disp")),
    ("lead3_flat_disp", "lead3 ∩ flat ∩ disp↓", ("lead3", "flat", "disp")),
    ("omega_flat_b1_disp", "Ω ∩ flat ∩ b1 ∩ disp↓", ("omega", "flat", "b1", "disp")),
)

# Deep-dive lanes · bridge + relaxed H-B1 variants (§H)
DEEP_DIVE_LANES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    *TWO_STAGE_LANES,
    ("nl_flat_b1_disp", "not_leading@D ∩ flat ∩ b1 ∩ disp↓", ("not_leading_d", "flat", "b1", "disp")),
    (
        "score_d8_10_nl_flat_b1_disp",
        "score D8-10 ∩ not_leading@D ∩ flat ∩ b1 ∩ disp↓",
        ("score_d8_10", "not_leading_d", "flat", "b1", "disp"),
    ),
    ("bridge_d47_nl_moderate", "D4-7 · not_leading@D · moderate", ("decile_d47", "not_leading_d", "moderate")),
    ("bridge_d47_nl_chase", "D4-7 · not_leading@D · chase", ("decile_d47", "not_leading_d", "chase")),
    ("omega_nl_moderate", "Ω ∩ not_leading@D · moderate", ("omega", "not_leading_d", "moderate")),
    ("omega_nl_chase", "Ω ∩ not_leading@D · chase", ("omega", "not_leading_d", "chase")),
)

DEEP_DIVE_YEARLY_LANE_IDS: frozenset[str] = frozenset(
    {
        "score_top_flat_b1_disp",
        "lead3_flat_b1_disp",
        "bridge_d47_nl_moderate",
        "bridge_d47_nl_chase",
        "nl_flat_b1_disp",
        "score_d8_10_nl_flat_b1_disp",
        "omega_nl_moderate",
        "omega_nl_chase",
    }
)

WF_TRAIN_YEARS = 3
WF_TEST_YEARS = 1
GO_MIN_OOS_MEAN = 0.5
GO_MIN_OOS_N = 30
GO_MIN_EVENTS_PER_YEAR = 15.0

SIG_RET_MIN_N = 15
SIG_RET_ALPHA = 0.05


def _spearman_pvalue(ic: float, n: int) -> float:
    """Two-sided p-value · normal approx for large n."""
    if n < 3 or not np.isfinite(ic) or abs(ic) >= 1.0:
        return 1.0
    from math import erfc, sqrt

    t_stat = ic * sqrt((n - 2) / max(1e-12, 1.0 - ic * ic))
    return float(erfc(abs(t_stat) / sqrt(2)))


@dataclass(frozen=True)
class DecileStats:
    decile: int
    n: int
    mean_drs_3d: float
    median_drs_3d: float
    mean_ret_3d: float
    flip_leading_rate: float
    mean_score: float


@dataclass(frozen=True)
class ScoreEvalResult:
    pool_n: int
    omega_n: int
    spearman_ic: float
    spearman_p: float
    pool_mean_drs_3d: float
    top_decile_mean_drs_3d: float
    top_decile_lift: float
    top_decile_flip_rate: float
    pool_flip_rate: float
    top_decile_cross_100_rate: float
    pool_cross_100_rate: float
    h0_ic_near_zero: bool
    h1_top_beats_pool: bool
    deciles: tuple[DecileStats, ...]


@dataclass(frozen=True)
class ScoreABComparison:
    """Side-by-side Ω (position band) vs Ω′ (kinematic pool)."""

    position: ScoreEvalResult
    kinematic: ScoreEvalResult
    kinematic_with_proximity: ScoreEvalResult | None
    h2_kinematic_ic_near_zero: bool


def _rsr_proximity_proxy(rsr: float) -> float:
    """(100−RSR)⁻¹ capped · higher when RSR approaches 100 from below."""
    if not np.isfinite(rsr) or rsr >= 100.0:
        return 0.0
    gap = max(100.0 - rsr, 0.05)
    return min(1.0 / gap, 20.0) / 20.0


def compute_drs3d_score(
    row: pd.Series | dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """PIT score at signal day D for 3-day rightward displacement potential.

    Score = w1·proximity(RSR) + w2·max(ΔRSR_4d,0) + w3·max(drs_rsr_1d,0)
            + w4·mono_up − penalties(high disp, chase)
    """
    w = weights or DEFAULT_WEIGHTS
    if isinstance(row, pd.Series):
        row = row.to_dict()

    rsr = float(row.get("rsr_d", row.get("rv", float("nan"))))
    dr4 = float(row.get("drs_rsr_4d", float("nan")))
    dr1 = float(row.get("drs_rsr_1d", float("nan")))
    disp = float(row.get("disp", float("nan")))
    mono = bool(row.get("mono_up", False))
    sig_ret = float(row.get("sig_ret", 0.0))
    disp_contract = bool(row.get("disp_contract", False))

    prox = _rsr_proximity_proxy(rsr)
    vel = max(dr4, 0.0) if np.isfinite(dr4) else 0.0
    spread = max(dr1, 0.0) if np.isfinite(dr1) else 0.0
    mono_term = 1.0 if mono else 0.0

    raw = (
        w["w_proximity"] * prox
        + w["w_velocity"] * min(vel / 2.0, 1.0)
        + w["w_spread"] * min(spread / 1.0, 1.0)
        + w["w_mono"] * mono_term
    )

    if np.isfinite(disp) and disp >= w["disp_high_cutoff"]:
        raw -= w["penalty_high_disp"]
    if sig_ret >= w["chase_sig_ret"] and not disp_contract:
        raw -= w["penalty_chase"]

    return round(max(raw, 0.0), 6)


def compute_drs3d_score_kinematic(
    row: pd.Series | dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Motion-first PIT score · proximity optional (default weight 0).

    Uses v20, heading_rsr (rightward purity), spread_v proxy, mono_up, a_step.
    """
    w = weights or DEFAULT_KINEMATIC_WEIGHTS
    if isinstance(row, pd.Series):
        row = row.to_dict()

    rsr = float(row.get("rsr_d", row.get("rv", float("nan"))))
    dr4 = float(row.get("drs_rsr_4d", float("nan")))
    v20 = float(row.get("v20", dr4 / 4.0 if np.isfinite(dr4) else float("nan")))
    heading = float(row.get("heading_rsr", float("nan")))
    dr1 = float(row.get("drs_rsr_1d", float("nan")))
    disp = float(row.get("disp", float("nan")))
    mono = bool(row.get("mono_up", False))
    a_step = float(row.get("a_step", float("nan")))
    sig_ret = float(row.get("sig_ret", 0.0))
    disp_contract = bool(row.get("disp_contract", False))

    prox = _rsr_proximity_proxy(rsr) if w.get("w_proximity", 0.0) > 0 else 0.0
    vel = max(v20, 0.0) if np.isfinite(v20) else 0.0
    head = max(heading, 0.0) if np.isfinite(heading) else 0.0
    spread = max(dr1, 0.0) if np.isfinite(dr1) else 0.0
    mono_term = 1.0 if mono else 0.0
    accel = max(a_step, 0.0) if np.isfinite(a_step) else 0.0

    raw = (
        w.get("w_proximity", 0.0) * prox
        + w["w_velocity"] * min(vel / 1.5, 1.0)
        + w["w_heading"] * min(head, 1.0)
        + w["w_spread"] * min(spread / 1.0, 1.0)
        + w["w_mono"] * mono_term
        + w.get("w_accel", 0.0) * min(accel / 0.5, 1.0)
    )

    if np.isfinite(disp) and disp >= w["disp_high_cutoff"]:
        raw -= w["penalty_high_disp"]
    if sig_ret >= w["chase_sig_ret"] and not disp_contract:
        raw -= w["penalty_chase"]

    return round(max(raw, 0.0), 6)


def cross_sectional_percentile_rank(
    panel: pd.DataFrame,
    raw_col: str,
    *,
    date_col: str = "date",
    mask_col: str | None = None,
) -> pd.Series:
    """Percentile rank of raw_col within each date (optionally masked subset)."""
    ranks = pd.Series(np.nan, index=panel.index, dtype=float)
    mask = panel[mask_col] if mask_col and mask_col in panel.columns else pd.Series(True, index=panel.index)
    sub = panel.loc[mask]
    for _, g in sub.groupby(date_col, sort=False):
        raw = g[raw_col]
        valid = raw.notna()
        if valid.sum() == 0:
            continue
        if valid.sum() == 1:
            ranks.loc[g.index[valid]] = 1.0
            continue
        ranks.loc[g.index] = raw.rank(pct=True, method="average")
    return ranks


def compute_cross_100_3d(rsr_d: pd.Series, rsr_d3: pd.Series) -> pd.Series:
    """1[RSR(D)<100 ∧ RSR(D+3)≥100] · structural Leading-boundary cross."""
    return compute_cross_100_nd(rsr_d, rsr_d3)


def compute_cross_100_nd(rsr_d: pd.Series, rsr_dn: pd.Series) -> pd.Series:
    """1[RSR(D)<100 ∧ RSR(D+N)≥100] · structural Leading-boundary cross."""
    return (
        rsr_d.notna()
        & rsr_dn.notna()
        & (rsr_d < 100.0)
        & (rsr_dn >= 100.0)
    )


def attach_horizon_outcomes(
    panel: pd.DataFrame,
    conn: Any,
    *,
    hold_days: int,
    rrg_length: int = DEFAULT_LENGTH,
) -> pd.DataFrame:
    """Add ret_Nd · drs_Nd · flip_leading_Nd · cross_100_Nd for N=hold_days."""
    if panel.empty:
        return panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs_ratio, _, quad = compute_rrg_panel(close, bench, length=rrg_length)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    suffix = f"{hold_days}d"
    ret_col = f"ret_{suffix}"
    drs_col = f"drs_{suffix}"
    rsr_dn_col = f"rsr_d{hold_days}"
    flip_col = f"flip_leading_{suffix}"
    cross_col = f"cross_100_{suffix}"

    ret_vals: list[float] = []
    drs_vals: list[float] = []
    rsr_dn_vals: list[float] = []
    flip_vals: list[bool] = []
    cross_vals: list[bool] = []
    keep_idx: list[Any] = []

    for idx, row in panel.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or si + hold_days >= len(full_dates):
            continue
        c0 = close.at[d, sid] if sid in close.columns else np.nan
        dn = full_dates[si + hold_days]
        cn = close.at[dn, sid] if sid in close.columns else np.nan
        if pd.isna(c0) or c0 <= 0 or pd.isna(cn) or cn <= 0:
            continue

        rv0 = float(rs_ratio.at[d, sid]) if sid in rs_ratio.columns else float("nan")
        rvn = float(rs_ratio.at[dn, sid]) if sid in rs_ratio.columns else float("nan")
        keep_idx.append(idx)
        ret_vals.append((cn / c0 - 1) * 100)
        drs_vals.append(rvn - rv0 if np.isfinite(rv0) and np.isfinite(rvn) else float("nan"))
        rsr_dn_vals.append(rvn if np.isfinite(rvn) else float("nan"))
        if sid in quad.columns:
            qn = quad.at[dn, sid]
            flip_vals.append(str(qn) == "leading" if pd.notna(qn) else False)
        else:
            flip_vals.append(False)
        cross_vals.append(
            bool(
                np.isfinite(rv0)
                and np.isfinite(rvn)
                and rv0 < 100.0
                and rvn >= 100.0
            )
        )

    if not keep_idx:
        return panel.iloc[0:0].copy()

    out = panel.loc[keep_idx].copy()
    out[ret_col] = ret_vals
    out[drs_col] = drs_vals
    out[rsr_dn_col] = rsr_dn_vals
    out[flip_col] = flip_vals
    out[cross_col] = cross_vals
    return out


def attach_excess_3d(
    panel: pd.DataFrame,
    conn: Any,
    *,
    hold_days: int = HOLD_DAYS,
) -> pd.DataFrame:
    """Add bench_ret_3d · excess_3d = ret_3d − benchmark 3-day return (D close → D+N close)."""
    if panel.empty or "ret_3d" not in panel.columns:
        return panel

    from analytics.bench import bench_return_entry_to_exit

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    bench_rets: list[float] = []
    excess_vals: list[float] = []
    for _, row in panel.iterrows():
        d = str(row["date"])
        si = date_idx.get(d)
        if si is None or si + hold_days >= len(full_dates):
            bench_rets.append(float("nan"))
            excess_vals.append(float("nan"))
            continue
        dn = full_dates[si + hold_days]
        br = bench_return_entry_to_exit(conn, d, dn, entry_price_mode="close")
        r3 = float(row["ret_3d"])
        if br is None or not np.isfinite(r3):
            bench_rets.append(float("nan"))
            excess_vals.append(float("nan"))
        else:
            bench_rets.append(br)
            excess_vals.append(r3 - br)

    out = panel.copy()
    out["bench_ret_3d"] = bench_rets
    out["excess_3d"] = excess_vals
    return out


def pool_omega_mask(
    panel: pd.DataFrame,
    *,
    rsr_lo: float = RSR_BAND_LO,
    rsr_hi: float = RSR_BAND_HI,
    min_streak: int = MIN_IMPROVE_STREAK,
) -> pd.Series:
    """Pool Ω: Improving/Lagging pre-release ∩ up_right ∩ RSR band ∩ streak ∩ disp band."""
    rsr = panel["rsr_d"] if "rsr_d" in panel.columns else panel.get("rv", pd.Series(np.nan, index=panel.index))
    trend = panel["trend"] if "trend" in panel.columns else ""
    disp = panel["disp"] if "disp" in panel.columns else pd.Series(np.nan, index=panel.index)

    base = _pre_release_pool_mask(panel)
    return (
        base
        & (trend == "up_right")
        & rsr.notna()
        & (rsr >= rsr_lo)
        & (rsr < rsr_hi)
        & (panel["improve_streak"] >= min_streak)
        & disp.notna()
        & (disp >= DISP_LO)
        & (disp < DISP_HI)
    )


def pool_omega_kinematic_mask(
    panel: pd.DataFrame,
    *,
    min_streak: int = MIN_IMPROVE_STREAK,
) -> pd.Series:
    """Pool Ω′: kinematic gates only · no RSR absolute band."""
    trend = panel["trend"] if "trend" in panel.columns else ""
    disp = panel["disp"] if "disp" in panel.columns else pd.Series(np.nan, index=panel.index)

    base = _pre_release_pool_mask(panel)
    return (
        base
        & (trend == "up_right")
        & (panel["improve_streak"] >= min_streak)
        & disp.notna()
        & (disp >= DISP_LO)
        & (disp < DISP_HI)
    )


def _attach_pit_trajectory_features(df: pd.DataFrame, conn: Any) -> pd.DataFrame:
    """Attach trend · disp · mono_up · drs_rsr_4d from 4-day WMA trajectory at D."""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=DEFAULT_LENGTH)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    trends, disps, monos, dr4s = [], [], [], []
    headings, v20s, a_steps = [], [], []
    for _, row in df.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or sid not in rs_ratio.columns:
            trends.append("")
            disps.append(float("nan"))
            monos.append(False)
            dr4s.append(float("nan"))
            headings.append(float("nan"))
            v20s.append(float("nan"))
            a_steps.append(float("nan"))
            continue
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
        if not f:
            trends.append("")
            disps.append(float("nan"))
            monos.append(False)
            dr4s.append(float("nan"))
            headings.append(float("nan"))
            v20s.append(float("nan"))
            a_steps.append(float("nan"))
            continue
        dr4 = float("nan")
        if si >= 3:
            d0 = full_dates[si - 3]
            rv0 = float(rs_ratio.at[d0, sid])
            rv1 = float(f["rs_ratio"])
            if np.isfinite(rv0) and np.isfinite(rv1):
                dr4 = rv1 - rv0

        disp_val = float(f["disp"])
        segs = f.get("segs") or []
        seg_last = float(f.get("seg_last", segs[-1] if segs else 0.0))
        seg_mean = float(np.mean(segs)) if segs else 0.0
        heading = dr4 / max(disp_val, _HEADING_EPS) if np.isfinite(dr4) else float("nan")

        trends.append(str(f.get("trend") or ""))
        disps.append(disp_val)
        monos.append(bool(f.get("mono_up", False)))
        dr4s.append(dr4)
        headings.append(heading)
        v20s.append(dr4 / 4.0 if np.isfinite(dr4) else float("nan"))
        a_steps.append(seg_last - seg_mean if segs else float("nan"))

    out = df.copy()
    out["trend"] = trends
    out["disp"] = disps
    out["mono_up"] = monos
    out["drs_rsr_4d"] = dr4s
    out["heading_rsr"] = headings
    out["v20"] = v20s
    out["a_step"] = a_steps
    return out


def build_drs3d_panel(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pre-release pool with drs_3d outcomes · PIT trajectory features · score."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    if events is not None and "drs_3d" in events.columns:
        panel, meta = build_pre_release_funnel_panel_3d(events=events)
    else:
        panel, meta = build_pre_release_funnel_panel_3d(conn, events=events)

    if panel.empty:
        return panel, {**meta, "omega_n": 0}

    panel = _attach_pit_trajectory_features(panel, conn)
    if "rsr_d" not in panel.columns and "rv" in panel.columns:
        panel = panel.copy()
        panel["rsr_d"] = panel["rv"]

    panel = panel.copy()
    panel["drs3d_score"] = panel.apply(compute_drs3d_score, axis=1)
    panel["drs3d_score_kin_raw"] = panel.apply(compute_drs3d_score_kinematic, axis=1)
    panel["in_pool_omega"] = pool_omega_mask(panel)
    panel["in_pool_omega_kinematic"] = pool_omega_kinematic_mask(panel)
    panel = attach_score_decile(panel, subset_col="in_pool_omega")
    panel = attach_excess_3d(panel, conn, hold_days=HOLD_DAYS)
    panel["drs3d_score_kin"] = cross_sectional_percentile_rank(
        panel, "drs3d_score_kin_raw", mask_col="in_pool_omega_kinematic"
    )
    prox_weights = {**DEFAULT_KINEMATIC_WEIGHTS, "w_proximity": 0.3}
    panel["drs3d_score_kin_prox_raw"] = panel.apply(
        lambda r: compute_drs3d_score_kinematic(r, weights=prox_weights), axis=1
    )
    panel["drs3d_score_kin_prox"] = cross_sectional_percentile_rank(
        panel, "drs3d_score_kin_prox_raw", mask_col="in_pool_omega_kinematic"
    )
    if "rsr_d" in panel.columns and "rsr_d3" in panel.columns:
        panel["cross_100_3d"] = compute_cross_100_3d(panel["rsr_d"], panel["rsr_d3"])
    meta = {
        **meta,
        "omega_n": int(panel["in_pool_omega"].sum()),
        "omega_kinematic_n": int(panel["in_pool_omega_kinematic"].sum()),
        "structural_outcome": "drs_3d",
        "score_weights": DEFAULT_WEIGHTS,
        "kinematic_weights": DEFAULT_KINEMATIC_WEIGHTS,
    }
    return panel, meta


def evaluate_score_deciles(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega",
    score_col: str = "drs3d_score",
    drs_col: str = "drs_3d",
    ret_col: str = "ret_3d",
    flip_col: str = "flip_leading_3d",
    cross_col: str = "cross_100_3d",
    n_deciles: int = 10,
) -> ScoreEvalResult:
    """Spearman IC · decile table · H0/H1 checks on structural displacement outcome."""
    sub = panel[panel[subset_col]].copy() if subset_col and subset_col in panel.columns else panel.copy()
    sub = sub.dropna(subset=[drs_col, score_col])
    pool_n = len(panel)
    omega_n = len(sub)

    if omega_n < n_deciles:
        empty = ScoreEvalResult(
            pool_n=pool_n,
            omega_n=omega_n,
            spearman_ic=0.0,
            spearman_p=1.0,
            pool_mean_drs_3d=0.0,
            top_decile_mean_drs_3d=0.0,
            top_decile_lift=0.0,
            top_decile_flip_rate=0.0,
            pool_flip_rate=0.0,
            top_decile_cross_100_rate=0.0,
            pool_cross_100_rate=0.0,
            h0_ic_near_zero=True,
            h1_top_beats_pool=False,
            deciles=tuple(),
        )
        return empty

    ic_raw = spearman_correlation(sub[score_col].tolist(), sub[drs_col].tolist())
    ic = float(ic_raw) if ic_raw is not None else 0.0
    p_val = _spearman_pvalue(ic, omega_n)

    pool_mean = float(sub[drs_col].mean())
    pool_flip = float(sub[flip_col].mean()) if flip_col in sub.columns else 0.0
    pool_cross = float(sub[cross_col].mean()) if cross_col in sub.columns else 0.0

    sub = sub.sort_values(score_col)
    sub = sub.copy()
    sub["decile"] = pd.qcut(sub[score_col], n_deciles, labels=False, duplicates="drop") + 1

    decile_rows: list[DecileStats] = []
    for d, g in sub.groupby("decile", sort=True):
        flip_s = g[flip_col] if flip_col in g.columns else pd.Series(False, index=g.index)
        decile_rows.append(
            DecileStats(
                decile=int(d),
                n=len(g),
                mean_drs_3d=round(float(g[drs_col].mean()), 4),
                median_drs_3d=round(float(g[drs_col].median()), 4),
                mean_ret_3d=round(float(g[ret_col].mean()), 4) if ret_col in g.columns else 0.0,
                flip_leading_rate=round(float(flip_s.mean()), 4),
                mean_score=round(float(g[score_col].mean()), 4),
            )
        )

    top = decile_rows[-1] if decile_rows else None
    top_mean = top.mean_drs_3d if top else 0.0
    top_flip = top.flip_leading_rate if top else 0.0
    lift = top_mean - pool_mean

    top_g = sub[sub["decile"] == sub["decile"].max()]
    top_cross = float(top_g[cross_col].mean()) if len(top_g) and cross_col in top_g.columns else 0.0

    return ScoreEvalResult(
        pool_n=pool_n,
        omega_n=omega_n,
        spearman_ic=round(ic, 4),
        spearman_p=round(p_val, 6),
        pool_mean_drs_3d=round(pool_mean, 4),
        top_decile_mean_drs_3d=round(top_mean, 4),
        top_decile_lift=round(lift, 4),
        top_decile_flip_rate=round(top_flip, 4),
        pool_flip_rate=round(pool_flip, 4),
        top_decile_cross_100_rate=round(top_cross, 4),
        pool_cross_100_rate=round(pool_cross, 4),
        h0_ic_near_zero=abs(ic) < 0.05,
        h1_top_beats_pool=lift > 0,
        deciles=tuple(decile_rows),
    )


def _ret_outcome_stats(sub: pd.DataFrame) -> dict[str, Any]:
    """Summary stats for ret_3d / excess_3d / drs_3d · one-sided p(mean>0)."""
    rets = sub["ret_3d"].dropna() if "ret_3d" in sub.columns else pd.Series(dtype=float)
    drs = sub["drs_3d"].dropna() if "drs_3d" in sub.columns else pd.Series(dtype=float)
    excess = sub["excess_3d"].dropna() if "excess_3d" in sub.columns else pd.Series(dtype=float)
    n = len(rets)
    if n == 0:
        return {
            "n": 0,
            "mean_ret_3d": 0.0,
            "median_ret_3d": 0.0,
            "mean_excess_3d": 0.0,
            "mean_drs_3d": 0.0,
            "win_rate": 0.0,
            "p_mean_gt_zero": 1.0,
            "p_excess_gt_zero": 1.0,
            "significant_pos": False,
            "significant_excess": False,
        }
    m = float(rets.mean())
    med = float(rets.median())
    win = float((rets > 0).mean())
    dm = float(drs.mean()) if len(drs) else 0.0
    em = float(excess.mean()) if len(excess) else 0.0
    p_val = 1.0
    p_exc = 1.0
    if n >= 5:
        s = float(rets.std(ddof=1))
        if s > 1e-12:
            from math import erfc, sqrt

            t = m / (s / sqrt(n))
            p_two = erfc(abs(t) / sqrt(2))
            p_val = p_two / 2 if m > 0 else 1.0 - p_two / 2
        if len(excess) >= 5:
            se = float(excess.std(ddof=1))
            if se > 1e-12:
                from math import erfc, sqrt

                te = em / (se / sqrt(len(excess)))
                p_two_e = erfc(abs(te) / sqrt(2))
                p_exc = p_two_e / 2 if em > 0 else 1.0 - p_two_e / 2
    sig = n >= SIG_RET_MIN_N and m > 0 and p_val < SIG_RET_ALPHA
    sig_exc = len(excess) >= SIG_RET_MIN_N and em > 0 and p_exc < SIG_RET_ALPHA
    return {
        "n": n,
        "mean_ret_3d": round(m, 4),
        "median_ret_3d": round(med, 4),
        "mean_excess_3d": round(em, 4),
        "mean_drs_3d": round(dm, 4),
        "win_rate": round(win, 4),
        "p_mean_gt_zero": round(p_val, 6),
        "p_excess_gt_zero": round(p_exc, 6),
        "significant_pos": sig,
        "significant_excess": sig_exc,
    }


def attach_score_decile(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega",
    score_col: str = "drs3d_score",
    n_deciles: int = 10,
) -> pd.DataFrame:
    """Attach score_decile (1..10) within Pool Ω rows; NaN elsewhere."""
    out = panel.copy()
    out["score_decile"] = np.nan
    if subset_col not in out.columns or score_col not in out.columns:
        return out
    sub_idx = out.index[out[subset_col]]
    sub = out.loc[sub_idx].dropna(subset=[score_col])
    if len(sub) < n_deciles:
        return out
    ranked = sub[score_col].rank(method="first")
    dec = pd.qcut(ranked, n_deciles, labels=False, duplicates="drop") + 1
    out.loc[dec.index, "score_decile"] = dec.astype(float)
    return out


def _flip_at_d_label(end_q: str) -> str:
    return "leading@D" if str(end_q) == "leading" else "not_leading@D"


def _sig_ret_bucket(sig_ret: float) -> str:
    if not np.isfinite(sig_ret):
        return "unknown"
    if -1.0 <= sig_ret <= 1.0:
        return "flat"
    if sig_ret >= 3.0:
        return "chase"
    if sig_ret < -1.0:
        return "dip"
    return "moderate"


def _decile_group(decile: float) -> str:
    if not np.isfinite(decile):
        return "—"
    d = int(decile)
    if d <= 3:
        return "D1-3"
    if d <= 7:
        return "D4-7"
    return "D8-10"


def evaluate_bridge_stratification(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega",
) -> dict[str, Any]:
    """P0 bridge: score decile × flip@D × sig_ret → ret_3d sub-pools."""
    sub = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    sub = attach_score_decile(sub, subset_col=subset_col)
    sub = sub.dropna(subset=["ret_3d", "score_decile"])
    if sub.empty:
        return {"cells": [], "significant_pools": [], "n": 0}

    sub["flip_at_d"] = sub["end_q"].map(_flip_at_d_label) if "end_q" in sub.columns else "unknown"
    sub["sig_ret_bucket"] = sub["sig_ret"].map(_sig_ret_bucket) if "sig_ret" in sub.columns else "unknown"
    sub["decile_group"] = sub["score_decile"].map(_decile_group)

    cells: list[dict[str, Any]] = []
    significant: list[dict[str, Any]] = []

    group_cols = ["decile_group", "flip_at_d", "sig_ret_bucket"]
    for keys, g in sub.groupby(group_cols, observed=True):
        stats = _ret_outcome_stats(g)
        cell = {
            "decile_group": keys[0],
            "flip_at_d": keys[1],
            "sig_ret_bucket": keys[2],
            **stats,
            "flip_leading_3d_rate": round(float(g["flip_leading_3d"].mean()), 4)
            if "flip_leading_3d" in g.columns
            else 0.0,
        }
        cells.append(cell)
        if stats["significant_pos"]:
            significant.append(cell)

    # Also full decile × flip@D (finer)
    fine_cells: list[dict[str, Any]] = []
    for keys, g in sub.groupby(["score_decile", "flip_at_d"], observed=True):
        stats = _ret_outcome_stats(g)
        fine_cells.append(
            {
                "score_decile": int(keys[0]),
                "flip_at_d": keys[1],
                **stats,
            }
        )

    return {
        "n": len(sub),
        "cells": sorted(cells, key=lambda c: (-c["mean_ret_3d"], -c["n"])),
        "fine_decile_flip": sorted(fine_cells, key=lambda c: (-c["mean_ret_3d"], -c["n"])),
        "significant_pools": sorted(significant, key=lambda c: -c["mean_ret_3d"]),
    }


def _attach_funnel_atoms(panel: pd.DataFrame) -> pd.DataFrame:
    """Binary atom columns for two-stage funnel (+ score_d10 · omega · bridge atoms)."""
    out = panel.copy()
    out["atom_omega"] = out["in_pool_omega"] if "in_pool_omega" in out.columns else False
    out["atom_score_d10"] = out["score_decile"] == 10
    out["atom_score_d8_10"] = out["score_decile"] >= 8
    if "end_q" in out.columns:
        out["atom_not_leading_d"] = out["end_q"] != "leading"
    else:
        out["atom_not_leading_d"] = False
    if "score_decile" in out.columns:
        out["atom_decile_d47"] = out["score_decile"].between(4, 7)
    else:
        out["atom_decile_d47"] = False
    if "sig_ret" in out.columns:
        buckets = out["sig_ret"].map(_sig_ret_bucket)
        out["atom_moderate"] = buckets == "moderate"
        out["atom_chase"] = buckets == "chase"
    else:
        out["atom_moderate"] = False
        out["atom_chase"] = False
    for atom in ("lead3", "flat", "b1", "b0", "disp", "st7p", "ex", "dip"):
        try:
            out[f"atom_{atom}"] = _atom_mask(out, atom)
        except KeyError:
            out[f"atom_{atom}"] = False
    return out


def _lane_mask_drs3d(panel: pd.DataFrame, atoms: tuple[str, ...] | list[str]) -> pd.Series:
    """Lane mask · supports custom atoms score_d10 · omega · bridge atoms."""
    _CUSTOM = {
        "omega": "atom_omega",
        "score_d10": "atom_score_d10",
        "score_d8_10": "atom_score_d8_10",
        "not_leading_d": "atom_not_leading_d",
        "decile_d47": "atom_decile_d47",
        "moderate": "atom_moderate",
        "chase": "atom_chase",
    }
    mask = pd.Series(True, index=panel.index)
    for atom in atoms:
        col = _CUSTOM.get(atom)
        if col and col in panel.columns:
            mask &= panel[col]
        elif atom == "omega":
            mask &= panel["atom_omega"] if "atom_omega" in panel.columns else False
        elif atom == "score_d10":
            mask &= panel["atom_score_d10"] if "atom_score_d10" in panel.columns else False
        else:
            acol = f"atom_{atom}"
            if acol in panel.columns:
                mask &= panel[acol]
            else:
                mask &= _atom_mask(panel, atom)
    return mask


def evaluate_two_stage_funnels(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega",
) -> dict[str, Any]:
    """B1 · Stage-1 structural + Stage-2 price atoms · ret_3d alpha."""
    base = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    base = attach_score_decile(base, subset_col=subset_col)
    base = _attach_funnel_atoms(base)
    base = base.dropna(subset=["ret_3d"])

    lanes: list[dict[str, Any]] = []
    for lane_id, label, atoms in TWO_STAGE_LANES:
        mask = _lane_mask_drs3d(base, atoms)
        sub = base[mask]
        stats = _ret_outcome_stats(sub)
        flip_r = (
            round(float(sub["flip_leading_3d"].mean()), 4)
            if len(sub) and "flip_leading_3d" in sub.columns
            else 0.0
        )
        lanes.append(
            {
                "lane_id": lane_id,
                "label": label,
                "rule_atoms": list(atoms),
                "flip_leading_3d_rate": flip_r,
                **stats,
            }
        )

    d10 = next((ln for ln in lanes if ln["lane_id"] == "score_d10"), None)
    h_b1 = next((ln for ln in lanes if ln["lane_id"] == "score_top_flat_b1_disp"), None)
    h_b1_pass = (
        d10 is not None
        and h_b1 is not None
        and h_b1["n"] >= 5
        and (h_b1["mean_ret_3d"] or 0) > (d10["mean_ret_3d"] or 0)
    )

    return {
        "lanes": sorted(lanes, key=lambda x: (-(x["mean_ret_3d"] or -999), -(x["n"] or 0))),
        "h_b1_beats_score_d10": h_b1_pass,
        "score_d10_mean_ret_3d": d10["mean_ret_3d"] if d10 else None,
        "h_b1_mean_ret_3d": h_b1["mean_ret_3d"] if h_b1 else None,
    }


def build_year_walk_forward_folds(
    trading_dates: list[str],
    *,
    train_years: int = WF_TRAIN_YEARS,
    test_years: int = WF_TEST_YEARS,
) -> list[dict[str, Any]]:
    """Rolling year WFA · train N years → test next M years."""
    if not trading_dates:
        return []
    dates = sorted(set(trading_dates))
    years = sorted({d[:4] for d in dates})
    folds: list[dict[str, Any]] = []
    fold_id = 0
    for i in range(len(years) - train_years - test_years + 1):
        train_y = years[i : i + train_years]
        test_y = years[i + train_years : i + train_years + test_years]
        train_set, test_set = set(train_y), set(test_y)
        train_dates = [d for d in dates if d[:4] in train_set]
        test_dates = [d for d in dates if d[:4] in test_set]
        if not train_dates or not test_dates:
            continue
        folds.append(
            {
                "fold_id": fold_id,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "train_years": tuple(train_y),
                "test_years": tuple(test_y),
            }
        )
        fold_id += 1
    return folds


def compute_yearly_lane_stats(
    panel: pd.DataFrame,
    atoms: tuple[str, ...] | list[str],
    *,
    subset_col: str = "in_pool_omega",
) -> list[dict[str, Any]]:
    """Per-calendar-year ret_3d / excess_3d for a lane."""
    base = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    base = attach_score_decile(base, subset_col=subset_col)
    base = _attach_funnel_atoms(base)
    base = base.dropna(subset=["ret_3d"])
    mask = _lane_mask_drs3d(base, atoms)
    sub = base[mask].copy()
    if sub.empty:
        return []
    sub["year"] = sub["date"].astype(str).str[:4]
    rows: list[dict[str, Any]] = []
    for yr, g in sub.groupby("year", sort=True):
        stats = _ret_outcome_stats(g)
        rows.append({"year": str(yr), **stats})
    return rows


def evaluate_lane_walk_forward(
    panel: pd.DataFrame,
    atoms: tuple[str, ...] | list[str],
    *,
    subset_col: str = "in_pool_omega",
    train_years: int = WF_TRAIN_YEARS,
    test_years: int = WF_TEST_YEARS,
) -> dict[str, Any]:
    """Yearly walk-forward OOS · pooled test-fold events per lane."""
    base = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    base = attach_score_decile(base, subset_col=subset_col)
    base = _attach_funnel_atoms(base)
    base = base.dropna(subset=["ret_3d"])
    if base.empty:
        return {"folds": [], "oos_pooled": _ret_outcome_stats(base), "summary": {}}

    dates = sorted(base["date"].astype(str).unique().tolist())
    folds = build_year_walk_forward_folds(dates, train_years=train_years, test_years=test_years)
    fold_rows: list[dict[str, Any]] = []
    oos_parts: list[pd.DataFrame] = []

    for fold in folds:
        test = base[base["date"].astype(str).between(fold["test_start"], fold["test_end"])]
        mask = _lane_mask_drs3d(test, atoms)
        sub = test[mask]
        stats = _ret_outcome_stats(sub)
        fold_rows.append(
            {
                "fold_id": fold["fold_id"],
                "test_years": list(fold["test_years"]),
                "test_n": stats["n"],
                "mean_ret_3d": stats["mean_ret_3d"],
                "mean_excess_3d": stats.get("mean_excess_3d", 0.0),
                "ret_positive": stats["mean_ret_3d"] > 0,
                "excess_positive": stats.get("mean_excess_3d", 0.0) > 0,
            }
        )
        if len(sub):
            oos_parts.append(sub)

    oos_panel = pd.concat(oos_parts, ignore_index=True) if oos_parts else base.iloc[0:0]
    oos_stats = _ret_outcome_stats(oos_panel)
    n_folds = len(fold_rows)
    n_pos_ret = sum(1 for f in fold_rows if f["ret_positive"] and f["test_n"] > 0)
    n_pos_exc = sum(1 for f in fold_rows if f["excess_positive"] and f["test_n"] > 0)

    return {
        "folds": fold_rows,
        "oos_pooled": oos_stats,
        "summary": {
            "n_folds": n_folds,
            "n_folds_positive_ret": n_pos_ret,
            "n_folds_positive_excess": n_pos_exc,
            "mostly_negative_ret": n_folds > 0 and n_pos_ret < n_folds / 2,
            "mostly_negative_excess": n_folds > 0 and n_pos_exc < n_folds / 2,
            "train_years": train_years,
            "test_years": test_years,
        },
    }


def evaluate_deep_dive(
    panel: pd.DataFrame,
    *,
    subset_col: str = "in_pool_omega",
    years_span: float = 1.0,
    omega_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§H deep dive · yearly · walk-forward · baselines for promising + new lanes."""
    base = panel[panel[subset_col]].copy() if subset_col in panel.columns else panel.copy()
    base = attach_score_decile(base, subset_col=subset_col)
    base = _attach_funnel_atoms(base)
    base = base.dropna(subset=["ret_3d"])

    omega_bl = omega_baseline or _ret_outcome_stats(base)
    bench_sub = base.dropna(subset=["bench_ret_3d"]) if "bench_ret_3d" in base.columns else base.iloc[0:0]
    if len(bench_sub):
        bench_bl = {
            "n": len(bench_sub),
            "mean_ret_3d": round(float(bench_sub["bench_ret_3d"].mean()), 4),
            "mean_excess_3d": 0.0,
        }
    else:
        bench_bl = {"n": 0, "mean_ret_3d": 0.0, "mean_excess_3d": 0.0}

    lanes: list[dict[str, Any]] = []
    for lane_id, label, atoms in DEEP_DIVE_LANES:
        mask = _lane_mask_drs3d(base, atoms)
        sub = base[mask]
        stats = _ret_outcome_stats(sub)
        flip_r = (
            round(float(sub["flip_leading_3d"].mean()), 4)
            if len(sub) and "flip_leading_3d" in sub.columns
            else 0.0
        )
        events_per_year = round(stats["n"] / years_span, 2) if years_span > 0 else 0.0
        entry: dict[str, Any] = {
            "lane_id": lane_id,
            "label": label,
            "rule_atoms": list(atoms),
            "events_per_year": events_per_year,
            "flip_leading_3d_rate": flip_r,
            "vs_omega_ret_delta": round(stats["mean_ret_3d"] - omega_bl.get("mean_ret_3d", 0), 4),
            "vs_omega_excess_delta": round(
                stats.get("mean_excess_3d", 0) - omega_bl.get("mean_excess_3d", 0), 4
            ),
            **stats,
        }
        if lane_id in DEEP_DIVE_YEARLY_LANE_IDS:
            entry["yearly"] = compute_yearly_lane_stats(panel, atoms, subset_col=subset_col)
            entry["walk_forward"] = evaluate_lane_walk_forward(panel, atoms, subset_col=subset_col)
        lanes.append(entry)

    return {
        "lanes": sorted(lanes, key=lambda x: (-(x["mean_ret_3d"] or -999), -(x["n"] or 0))),
        "omega_baseline": omega_bl,
        "bench_buyhold_baseline": bench_bl,
        "years_span": years_span,
    }


def compute_go_verdict(deep_dive: dict[str, Any]) -> dict[str, Any]:
    """§I GO / WEAK / NO-GO · rule-based lanes."""
    years_span = float(deep_dive.get("years_span") or 1.0)
    omega_bl = deep_dive.get("omega_baseline", {})
    candidates: list[dict[str, Any]] = []

    for lane in deep_dive.get("lanes", []):
        if lane["lane_id"] not in DEEP_DIVE_YEARLY_LANE_IDS:
            continue
        wf = lane.get("walk_forward", {})
        oos = wf.get("oos_pooled", {})
        wf_sum = wf.get("summary", {})
        yearly = lane.get("yearly", [])

        oos_ret = float(oos.get("mean_ret_3d") or 0)
        oos_exc = float(oos.get("mean_excess_3d") or 0)
        oos_n = int(oos.get("n") or 0)
        events_py = float(lane.get("events_per_year") or 0)
        is_ret = float(lane.get("mean_ret_3d") or 0)
        is_exc = float(lane.get("mean_excess_3d") or 0)
        is_n = int(lane.get("n") or 0)

        pos_years = [y for y in yearly if y.get("mean_ret_3d", 0) > 0 and y.get("n", 0) > 0]
        single_year_driven = (
            len(yearly) >= 3
            and len(pos_years) <= 1
            and is_ret > 0
            and is_n >= 10
        )

        meets_oos = (
            oos_n >= GO_MIN_OOS_N
            and (oos_ret >= GO_MIN_OOS_MEAN or oos_exc >= GO_MIN_OOS_MEAN)
        )
        meets_freq = events_py >= GO_MIN_EVENTS_PER_YEAR
        mostly_neg = wf_sum.get("mostly_negative_excess") or wf_sum.get("mostly_negative_ret")
        excess_after_gate = is_exc > 0 or oos_exc > 0

        lane_verdict = "NO-GO"
        reasons: list[str] = []
        if not excess_after_gate and is_exc <= 0 and oos_exc <= 0:
            lane_verdict = "NO-GO"
            reasons.append("excess_3d ≤ 0 after gating")
        elif mostly_neg and oos_exc <= 0:
            lane_verdict = "NO-GO"
            reasons.append("walk-forward mostly negative")
        elif meets_oos and meets_freq and not single_year_driven:
            lane_verdict = "GO"
            reasons.append("OOS ret/excess ≥ +0.5% · n≥30 · ≥15 events/yr")
        elif is_ret > 0 and (is_n < GO_MIN_OOS_N or single_year_driven or not meets_oos):
            lane_verdict = "WEAK"
            if is_n < GO_MIN_OOS_N:
                reasons.append("n<30")
            if single_year_driven:
                reasons.append("single-year driven")
            if not meets_oos:
                reasons.append("positive IS only")
        else:
            lane_verdict = "NO-GO"
            reasons.append("criteria not met")

        candidates.append(
            {
                "lane_id": lane["lane_id"],
                "label": lane["label"],
                "verdict": lane_verdict,
                "reasons": reasons,
                "is_n": is_n,
                "is_mean_ret_3d": is_ret,
                "is_mean_excess_3d": is_exc,
                "oos_n": oos_n,
                "oos_mean_ret_3d": oos_ret,
                "oos_mean_excess_3d": oos_exc,
                "events_per_year": events_py,
                "vs_omega_excess_delta": lane.get("vs_omega_excess_delta", 0),
            }
        )

    verdict_order = {"GO": 3, "WEAK": 2, "NO-GO": 1}
    candidates.sort(
        key=lambda c: (
            verdict_order.get(c["verdict"], 0),
            c.get("oos_mean_excess_3d", 0),
            c.get("is_mean_ret_3d", 0),
        ),
        reverse=True,
    )
    top = candidates[0] if candidates else None
    overall = top["verdict"] if top else "NO-GO"

    # H-B1 D10 track: archive unless GO on H-B1 or close variant
    h_b1 = next((c for c in candidates if c["lane_id"] == "score_top_flat_b1_disp"), None)
    abandon_h_b1_d10 = h_b1 is None or h_b1["verdict"] != "GO"
    d8_variant = next((c for c in candidates if c["lane_id"] == "score_d8_10_nl_flat_b1_disp"), None)
    if d8_variant and d8_variant["verdict"] == "GO":
        abandon_h_b1_d10 = False

    return {
        "verdict": overall,
        "top_lane": top,
        "candidates": candidates,
        "abandon_h_b1_d10_track": abandon_h_b1_d10,
        "criteria": {
            "go_min_oos_mean_pct": GO_MIN_OOS_MEAN,
            "go_min_oos_n": GO_MIN_OOS_N,
            "go_min_events_per_year": GO_MIN_EVENTS_PER_YEAR,
        },
        "omega_baseline_excess": omega_bl.get("mean_excess_3d", 0),
    }


def evaluate_score_ab_comparison(panel: pd.DataFrame) -> ScoreABComparison:
    """A/B: Ω (position band + drs3d_score) vs Ω′ (kinematic + score_kin)."""
    position = evaluate_score_deciles(panel, subset_col="in_pool_omega", score_col="drs3d_score")
    kinematic = evaluate_score_deciles(
        panel, subset_col="in_pool_omega_kinematic", score_col="drs3d_score_kin"
    )

    kin_with_prox: ScoreEvalResult | None = None
    if "drs3d_score_kin_prox" in panel.columns:
        kin_with_prox = evaluate_score_deciles(
            panel, subset_col="in_pool_omega_kinematic", score_col="drs3d_score_kin_prox"
        )

    return ScoreABComparison(
        position=position,
        kinematic=kinematic,
        kinematic_with_proximity=kin_with_prox,
        h2_kinematic_ic_near_zero=kinematic.h0_ic_near_zero,
    )


def analyze_june2026_drs3d_overlap(panel: pd.DataFrame, conn: Any | None = None) -> dict[str, Any]:
    """June 2026 Improving→Leading streak cases · score rank in pool Ω."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    omega = panel[panel["in_pool_omega"]].copy() if "in_pool_omega" in panel.columns else panel
    pr_full, _ = build_pre_release_funnel_panel_3d(conn)
    rows: list[dict[str, Any]] = []
    for sid, name, d_start, d_end, streak_pct, _rv, _mom in JUNE2026_LEADING_STREAK_CASES:
        signal_d, pr_row = _find_pre_release_signal(panel, pr_full, sid, d_start)
        sig_d = signal_d or d_start
        sub = panel[(panel["sid"] == sid) & (panel["date"] == sig_d)]
        entry: dict[str, Any] = {
            "sid": sid,
            "name": name,
            "signal_date": sig_d,
            "streak_start": d_start,
            "streak_end": d_end,
            "streak_total_pct": streak_pct,
        }
        if len(sub):
            r = sub.iloc[0]
            entry.update(
                {
                    "in_pre_release_pool": True,
                    "in_pool_omega": bool(r.get("in_pool_omega", False)),
                    "drs3d_score": round(float(r["drs3d_score"]), 4),
                    "drs_3d": round(float(r["drs_3d"]), 4) if pd.notna(r.get("drs_3d")) else None,
                    "ret_3d": round(float(r["ret_3d"]), 2) if pd.notna(r.get("ret_3d")) else None,
                    "flip_leading_3d": bool(r.get("flip_leading_3d", False)),
                    "rsr_d": round(float(r.get("rsr_d", r.get("rv", float("nan")))), 2),
                    "rsr_d3": round(float(r.get("rsr_d3", float("nan"))), 2)
                    if pd.notna(r.get("rsr_d3"))
                    else None,
                    "trend": str(r.get("trend", "")),
                    "disp": round(float(r.get("disp", float("nan"))), 2)
                    if pd.notna(r.get("disp"))
                    else None,
                }
            )
        else:
            entry["in_pre_release_pool"] = pr_row is not None
            entry["in_pool_omega"] = False
        rows.append(entry)

    in_omega = [r for r in rows if r.get("in_pool_omega")]
    in_pr = [r for r in rows if r.get("in_pre_release_pool")]
    scores = [r["drs3d_score"] for r in in_omega if r.get("drs3d_score") is not None]
    omega_scores = omega["drs3d_score"].dropna().tolist() if len(omega) and "drs3d_score" in omega.columns else []
    pct90 = float(np.percentile(omega_scores, 90)) if omega_scores else 0.0
    n_top = int(sum(1 for s in scores if s >= pct90)) if scores else 0

    return {
        "cases": rows,
        "summary": {
            "n_cases": len(rows),
            "n_in_pre_release_pool": len(in_pr),
            "n_in_pool_omega": len(in_omega),
            "n_top_decile_proxy": n_top,
            "pct_cases_top_decile": round(n_top / len(in_omega), 4) if in_omega else 0.0,
            "mean_drs_3d_cases": round(float(np.mean([r["drs_3d"] for r in in_omega if r.get("drs_3d") is not None])), 4)
            if in_omega
            else 0.0,
        },
    }


def render_drs3d_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    ev = result["evaluation"]
    ab = result.get("ab_comparison", {})
    w = result.get("weights", DEFAULT_WEIGHTS)
    kw = result.get("kinematic_weights", DEFAULT_KINEMATIC_WEIGHTS)
    lines = [
        "# RRG drs_3d displacement score · 3-day hold backtest",
        "",
        f"> **Primary outcome**: `drs_3d = RSR(D+3) − RSR(D)` · WMA({meta.get('rrg_length', DEFAULT_LENGTH)}) RS-Ratio",
        f"> **Auxiliary**: `flip_leading_3d` · `cross_100_3d` · `ret_3d` · **not** overnight `nxt_ret`",
        "",
        f"- Pre-release pool: **{meta.get('pool_n', ev['pool_n'])}** stock-days",
        f"- Pool Ω (up_right · RV98–100 · streak≥3 · disp∈[0.25,1.2)): **{ev['omega_n']}**",
        f"- Pool Ω′ (kinematic · no RSR band): **{meta.get('omega_kinematic_n', ab.get('kinematic', {}).get('omega_n', '—'))}**",
        f"- Span: {meta.get('date_start', '')} → {meta.get('date_end', '')} · Generated: {meta.get('generated_on', '')}",
        "",
        "## A. Score validation (Pool Ω · position band)",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Spearman IC (score vs drs_3d) | **{ev['spearman_ic']:+.4f}** (p={ev['spearman_p']:.4f}) |",
        f"| Pool Ω mean drs_3d | {ev['pool_mean_drs_3d']:+.3f} |",
        f"| Top decile mean drs_3d | **{ev['top_decile_mean_drs_3d']:+.3f}** |",
        f"| Top decile lift | **{ev['top_decile_lift']:+.3f}** |",
        f"| Pool P(flip→Leading@D+3) | {ev['pool_flip_rate']:.1%} |",
        f"| Top decile P(flip) | {ev['top_decile_flip_rate']:.1%} |",
        f"| Pool P(cross_100_3d) | {ev.get('pool_cross_100_rate', 0):.1%} |",
        f"| Top decile P(cross_100) | {ev.get('top_decile_cross_100_rate', 0):.1%} |",
        f"| H0 (IC≈0) | {'✅ not rejected' if ev['h0_ic_near_zero'] else '❌ rejected'} |",
        f"| H1 (top decile > pool) | {'✅' if ev['h1_top_beats_pool'] else '❌'} |",
    ]

    if ab:
        kin = ab.get("kinematic", {})
        lines.extend(
            [
                "",
                "## A′. Kinematic A/B (Pool Ω′ · motion gates only)",
                "",
                "| Metric | Ω position | Ω′ kinematic |",
                "|--------|------------|--------------|",
                f"| n | {ev['omega_n']} | {kin.get('omega_n', 0)} |",
                f"| Spearman IC | {ev['spearman_ic']:+.4f} | **{kin.get('spearman_ic', 0):+.4f}** |",
                f"| Top decile lift | {ev['top_decile_lift']:+.3f} | {kin.get('top_decile_lift', 0):+.3f} |",
                f"| Top decile P(flip) | {ev['top_decile_flip_rate']:.1%} | {kin.get('top_decile_flip_rate', 0):.1%} |",
                f"| Top decile P(cross_100) | {ev.get('top_decile_cross_100_rate', 0):.1%} | "
                f"{kin.get('top_decile_cross_100_rate', 0):.1%} |",
                f"| H2′ (Ω′ IC≈0 w/o RSR band) | — | "
                f"{'✅ not rejected' if ab.get('h2_kinematic_ic_near_zero') else '❌ rejected'} |",
            ]
        )
        kin_prox = ab.get("kinematic_with_proximity")
        if kin_prox:
            lines.append(
                f"| Ω′ + proximity (w=0.3) IC | — | {kin_prox.get('spearman_ic', 0):+.4f} |"
            )

    lines.extend(
        [
            "",
            "## B. Score weights (heuristic defaults)",
            "",
            f"- Position: w_proximity={w['w_proximity']} · w_velocity={w['w_velocity']} · "
            f"w_spread={w['w_spread']} · w_mono={w['w_mono']}",
            f"- Kinematic: w_proximity={kw['w_proximity']} · w_velocity={kw['w_velocity']} · "
            f"w_heading={kw['w_heading']} · w_spread={kw['w_spread']} · w_mono={kw['w_mono']}",
            f"- penalties: high_disp≥{w['disp_high_cutoff']} (−{w['penalty_high_disp']}) · "
            f"chase sig≥{w['chase_sig_ret']}% w/o disp↓ (−{w['penalty_chase']})",
            "",
            "## C. Decile table (Pool Ω)",
            "",
            "| D | n | mean drs_3d | median | mean ret_3d | P(flip) | mean score |",
            "|---|---|-------------|--------|-------------|---------|------------|",
        ]
    )
    for d in ev.get("deciles", []):
        lines.append(
            f"| {d['decile']} | {d['n']} | {d['mean_drs_3d']:+.3f} | {d['median_drs_3d']:+.3f} | "
            f"{d['mean_ret_3d']:+.2f}% | {d['flip_leading_rate']:.1%} | {d['mean_score']:.3f} |"
        )

    june = result.get("june2026_overlap", {})
    if june:
        sm = june.get("summary", {})
        lines.extend(
            [
                "",
                "## D. June 2026 Leading streak overlap",
                "",
        f"- Cases: **{sm.get('n_cases', 0)}** · pre-release: **{sm.get('n_in_pre_release_pool', 0)}** · "
        f"in Pool Ω: **{sm.get('n_in_pool_omega', 0)}** · "
        f"top-decile proxy: **{sm.get('n_top_decile_proxy', 0)}**",
                f"- Mean drs_3d (Ω cases): **{sm.get('mean_drs_3d_cases', 0):+.3f}**",
                "",
                "| sid | name | signal D | in Ω | score | drs_3d | ret_3d | flip |",
                "|-----|------|----------|------|-------|--------|--------|------|",
            ]
        )
        for c in june.get("cases", [])[:15]:
            lines.append(
                f"| {c['sid']} | {c.get('name', '')} | {c['signal_date']} | "
                f"{'✓' if c.get('in_pool_omega') else '—'} | "
                f"{c.get('drs3d_score', '—')} | {c.get('drs_3d', '—')} | "
                f"{c.get('ret_3d', '—')} | {'✓' if c.get('flip_leading_3d') else '—'} |"
            )

    bridge = result.get("bridge_stratification", {})
    if bridge:
        sig_pools = bridge.get("significant_pools", [])
        lines.extend(
            [
                "",
                "## E. Bridge stratification (decile_group × flip@D × sig_ret)",
                "",
                f"- Pool Ω rows with score decile: **{bridge.get('n', 0)}**",
                f"- Sub-pools with ret_3d significantly > 0 (n≥{SIG_RET_MIN_N}, p<{SIG_RET_ALPHA}): **{len(sig_pools)}**",
                "",
                "| decile | flip@D | sig_ret | n | mean ret_3d | mean drs_3d | win% | p(>0) | sig |",
                "|--------|--------|---------|---|-------------|-------------|------|-------|-----|",
            ]
        )
        for c in bridge.get("cells", [])[:24]:
            sig = "✓" if c.get("significant_pos") else "—"
            lines.append(
                f"| {c['decile_group']} | {c['flip_at_d']} | {c['sig_ret_bucket']} | {c['n']} | "
                f"{c['mean_ret_3d']:+.2f}% | {c['mean_drs_3d']:+.3f} | {c['win_rate']:.1%} | "
                f"{c['p_mean_gt_zero']:.4f} | {sig} |"
            )
        if sig_pools:
            lines.extend(["", "### Significant positive ret_3d sub-pools", ""])
            for c in sig_pools[:10]:
                lines.append(
                    f"- **{c['decile_group']} · {c['flip_at_d']} · {c['sig_ret_bucket']}**: "
                    f"n={c['n']} · mean ret_3d **{c['mean_ret_3d']:+.2f}%** · win {c['win_rate']:.1%}"
                )

    funnel = result.get("two_stage_funnel", {})
    if funnel:
        ha1 = "✅" if funnel.get("h_b1_beats_score_d10") else "❌"
        lines.extend(
            [
                "",
                "## F. Two-stage funnel (Track B · ret_3d alpha)",
                "",
                f"- H-B1 (Score D10 ∩ flat ∩ b1 ∩ disp↓ > Score D10 alone): {ha1}",
                f"- Score D10 mean ret_3d: **{funnel.get('score_d10_mean_ret_3d', '—')}%**",
                f"- H-B1 mean ret_3d: **{funnel.get('h_b1_mean_ret_3d', '—')}%**",
                "",
                "| lane | atoms | n | mean ret_3d | win% | mean drs_3d | P(flip@D+3) | sig |",
                "|------|-------|---|-------------|------|-------------|-------------|-----|",
            ]
        )
        for ln in funnel.get("lanes", []):
            sig = "✓" if ln.get("significant_pos") else "—"
            atoms = "+".join(ln.get("rule_atoms", []))
            lines.append(
                f"| {ln['lane_id']} | {atoms} | {ln['n']} | {ln['mean_ret_3d']:+.2f}% | "
                f"{ln['win_rate']:.1%} | {ln['mean_drs_3d']:+.3f} | "
                f"{ln.get('flip_leading_3d_rate', 0):.1%} | {sig} |"
            )

    lines.extend(
        [
            "",
            "## G. PIT checklist",
            "",
            "- [x] Score features at D close only",
            "- [x] drs_3d / flip_leading_3d are D+3 outcomes · not for entry",
            "- [x] Separate from overnight nxt_ret research",
        ]
    )

    deep = result.get("deep_dive", {})
    verdict = result.get("verdict", {})
    if deep:
        omega_bl = deep.get("omega_baseline", {})
        bench_bl = deep.get("bench_buyhold_baseline", {})
        lines.extend(
            [
                "",
                "## H. Deep dive (yearly · walk-forward · baselines)",
                "",
                f"- Pool Ω baseline: n={omega_bl.get('n', 0)} · mean ret_3d **{omega_bl.get('mean_ret_3d', 0):+.2f}%** · "
                f"excess_3d **{omega_bl.get('mean_excess_3d', 0):+.2f}%**",
                f"- Buy-hold benchmark (3d): mean ret_3d **{bench_bl.get('mean_ret_3d', 0):+.2f}%** · "
                f"n={bench_bl.get('n', 0)}",
                "",
                "### H.1 Lane summary (IS · vs Ω)",
                "",
                "| lane | n | evt/yr | mean ret_3d | excess_3d | vs Ω excess | P(flip) | WF OOS n | OOS ret | OOS excess |",
                "|------|---|--------|-------------|-----------|-------------|---------|----------|---------|------------|",
            ]
        )
        for ln in deep.get("lanes", []):
            if ln["lane_id"] not in DEEP_DIVE_YEARLY_LANE_IDS:
                continue
            wf = ln.get("walk_forward", {})
            oos = wf.get("oos_pooled", {})
            lines.append(
                f"| {ln['lane_id']} | {ln['n']} | {ln.get('events_per_year', 0):.1f} | "
                f"{ln['mean_ret_3d']:+.2f}% | {ln.get('mean_excess_3d', 0):+.2f}% | "
                f"{ln.get('vs_omega_excess_delta', 0):+.2f}% | "
                f"{ln.get('flip_leading_3d_rate', 0):.1%} | {oos.get('n', 0)} | "
                f"{oos.get('mean_ret_3d', 0):+.2f}% | {oos.get('mean_excess_3d', 0):+.2f}% |"
            )

        for ln in deep.get("lanes", []):
            if ln["lane_id"] not in DEEP_DIVE_YEARLY_LANE_IDS or not ln.get("yearly"):
                continue
            lines.extend(
                [
                    "",
                    f"### H.2 Yearly · `{ln['lane_id']}`",
                    "",
                    "| year | n | mean ret_3d | excess_3d | win% |",
                    "|------|---|-------------|-----------|------|",
                ]
            )
            for y in ln["yearly"]:
                lines.append(
                    f"| {y['year']} | {y['n']} | {y['mean_ret_3d']:+.2f}% | "
                    f"{y.get('mean_excess_3d', 0):+.2f}% | {y['win_rate']:.1%} |"
                )
            wf = ln.get("walk_forward", {})
            if wf.get("folds"):
                lines.extend(
                    [
                        "",
                        f"Walk-forward folds ({wf['summary'].get('n_folds', 0)}):",
                        "",
                        "| test_years | n | mean ret_3d | excess_3d |",
                        "|------------|---|-------------|-----------|",
                    ]
                )
                for f in wf["folds"]:
                    yrs = ",".join(f.get("test_years", []))
                    lines.append(
                        f"| {yrs} | {f['test_n']} | {f['mean_ret_3d']:+.2f}% | "
                        f"{f.get('mean_excess_3d', 0):+.2f}% |"
                    )

    if verdict:
        v = verdict.get("verdict", "NO-GO")
        top = verdict.get("top_lane") or {}
        lines.extend(
            [
                "",
                "## I. Verdict",
                "",
                f"**{v}**",
                "",
                "### Go/No-go criteria",
                "",
                f"- **GO**: OOS mean_ret_3d or excess_3d ≥ +{GO_MIN_OOS_MEAN}% with n≥{GO_MIN_OOS_N} across folds "
                f"AND ≥{GO_MIN_EVENTS_PER_YEAR:.0f} events/year on some lane",
                "- **WEAK**: positive IS only · n<30 · or single-year driven",
                "- **NO-GO**: excess_3d ≤ 0 after gating · or walk-forward mostly negative",
                "",
                f"- Top candidate: **{top.get('lane_id', '—')}** ({top.get('label', '')}) · "
                f"verdict={top.get('verdict', '—')}",
                f"- H-B1 D10 track abandon: **{'yes' if verdict.get('abandon_h_b1_d10_track') else 'no'}**",
                "",
                "| lane | verdict | IS ret | IS excess | OOS ret | OOS excess | evt/yr | reasons |",
                "|------|---------|--------|-----------|---------|------------|--------|---------|",
            ]
        )
        for c in verdict.get("candidates", []):
            reasons = "; ".join(c.get("reasons", []))
            lines.append(
                f"| {c['lane_id']} | {c['verdict']} | {c['is_mean_ret_3d']:+.2f}% | "
                f"{c['is_mean_excess_3d']:+.2f}% | {c['oos_mean_ret_3d']:+.2f}% | "
                f"{c['oos_mean_excess_3d']:+.2f}% | {c['events_per_year']:.1f} | {reasons} |"
            )
        if v == "NO-GO" and verdict.get("abandon_h_b1_d10_track"):
            lines.append(
                "",
                "> H-B1 score-D10 track archived in module unless revived — "
                "structural drs_3d score valid; ret_3d alpha insufficient for deploy.",
            )

    return "\n".join(lines)


def run_drs3d_score_backtest(conn: Any | None = None) -> dict[str, Any]:
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_drs3d_panel(conn)
    omega = panel[panel["in_pool_omega"]] if "in_pool_omega" in panel.columns else panel
    omega_kin = (
        panel[panel["in_pool_omega_kinematic"]] if "in_pool_omega_kinematic" in panel.columns else panel
    )
    evaluation = evaluate_score_deciles(panel, subset_col="in_pool_omega")
    ab = evaluate_score_ab_comparison(panel)
    bridge = evaluate_bridge_stratification(panel, subset_col="in_pool_omega")
    funnel = evaluate_two_stage_funnels(panel, subset_col="in_pool_omega")
    june = analyze_june2026_drs3d_overlap(panel, conn)

    years = _years_span(panel["date"]) if len(panel) else 1.0
    omega_stats = _ret_outcome_stats(omega)
    deep_dive = evaluate_deep_dive(
        panel,
        subset_col="in_pool_omega",
        years_span=years,
        omega_baseline=omega_stats,
    )
    verdict = compute_go_verdict(deep_dive)
    return {
        "meta": {
            **meta,
            "generated_on": date.today().isoformat(),
            "pool_n": len(panel),
            "years_span": round(years, 3),
            "date_start": str(panel["date"].min()) if len(panel) else "",
            "date_end": str(panel["date"].max()) if len(panel) else "",
            "hold_days": HOLD_DAYS,
            "rrg_length": DEFAULT_LENGTH,
        },
        "weights": DEFAULT_WEIGHTS,
        "kinematic_weights": DEFAULT_KINEMATIC_WEIGHTS,
        "evaluation": asdict(evaluation),
        "ab_comparison": {
            "position": asdict(ab.position),
            "kinematic": asdict(ab.kinematic),
            "kinematic_with_proximity": asdict(ab.kinematic_with_proximity)
            if ab.kinematic_with_proximity
            else None,
            "h2_kinematic_ic_near_zero": ab.h2_kinematic_ic_near_zero,
        },
        "pool_omega_baseline": {
            "n": len(omega),
            "mean_drs_3d": round(float(omega["drs_3d"].mean()), 4) if len(omega) else 0.0,
            "mean_ret_3d": round(float(omega["ret_3d"].mean()), 4) if len(omega) else 0.0,
            "mean_excess_3d": omega_stats.get("mean_excess_3d", 0.0),
            "flip_leading_3d_rate": round(float(omega["flip_leading_3d"].mean()), 4)
            if len(omega) and "flip_leading_3d" in omega.columns
            else 0.0,
            "cross_100_3d_rate": round(float(omega["cross_100_3d"].mean()), 4)
            if len(omega) and "cross_100_3d" in omega.columns
            else 0.0,
        },
        "pool_omega_kinematic_baseline": {
            "n": len(omega_kin),
            "mean_drs_3d": round(float(omega_kin["drs_3d"].mean()), 4) if len(omega_kin) else 0.0,
            "mean_ret_3d": round(float(omega_kin["ret_3d"].mean()), 4) if len(omega_kin) else 0.0,
            "flip_leading_3d_rate": round(float(omega_kin["flip_leading_3d"].mean()), 4)
            if len(omega_kin) and "flip_leading_3d" in omega_kin.columns
            else 0.0,
            "cross_100_3d_rate": round(float(omega_kin["cross_100_3d"].mean()), 4)
            if len(omega_kin) and "cross_100_3d" in omega_kin.columns
            else 0.0,
        },
        "june2026_overlap": june,
        "bridge_stratification": bridge,
        "two_stage_funnel": funnel,
        "deep_dive": deep_dive,
        "verdict": verdict,
    }


def write_drs3d_report(
    result: dict[str, Any],
    out_dir: Path | None = None,
    *,
    stamp: str | None = None,
) -> tuple[Path, Path]:
    out_dir = out_dir or (PROJECT_ROOT / "reports" / "research" / "rrg")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    json_path = out_dir / f"drs3d_score_backtest_{stamp}.json"
    md_path = out_dir / f"drs3d_score_backtest_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_drs3d_markdown(result), encoding="utf-8")
    return json_path, md_path
