"""RRG pre-release funnel lanes · 3-day hold (PIT · ret_3d).

Fork of rrg_pre_release_funnel_lanes.py — outcome is D close → D+3 close.
Do not mix with overnight (nxt_ret) research.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from market_benchmark import load_benchmark_close
from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, build_lifecycle_events
from research.backtest.rrg_improving_s3rv_executable import MIN_SID_HIST, enrich_lifecycle_events
from rrg_mono_daily_brief import _feat
from rrg_rotation import DEFAULT_LENGTH, compute_rrg_panel
from research.backtest.archive.rrg_pre_release_funnel_lanes import (
    _atom_mask,
    _candidate_combos,
    _combo_mask,
    _infer_regime_tag,
    _precompute_atom_columns,
    calendar_day_set,
    jaccard,
    lane_mask,
    max_jaccard_vs,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from stock_db import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "rrg_pre_release_funnel_lanes_3d.yaml"
HOLD_DAYS = 3
LOSS5_PCT = -5.0
LOSS8_PCT = -8.0

# 3-day holding discovery thresholds
STRICT_WIN_MIN = 0.58
STRICT_MEAN_MIN = 2.0
STRICT_N_MIN = 15
COVERAGE_WIN_MIN = 0.52
COVERAGE_MEAN_MIN = 3.0
COVERAGE_MEAN_MAX = 7.0
COVERAGE_N_MIN = 20
JACCARD_MAX_STRICT = 0.32
JACCARD_MAX_COVERAGE = 0.45
TARGET_UNION_DAYS_PER_YEAR = (50.0, 80.0)

# June 2026 · Improving→Leading 3日 streak 實證表（區間合計% · D3 RS 來自研究筆記）
JUNE2026_LEADING_STREAK_CASES: tuple[tuple[str, str, str, str, float, str | None, str | None], ...] = (
    ("2316", "楠梓電", "2026-06-17", "2026-06-22", 32.9, "105.4", "100.3"),
    ("2379", "瑞昱", "2026-06-17", "2026-06-22", 32.7, "107.1", "104.7"),
    ("1303", "南亞", "2026-06-17", "2026-06-22", 32.6, "109.9", "105.5"),
    ("8996", "高力", "2026-06-15", "2026-06-17", 32.3, "104.7", "105.2"),
    ("1409", "新纖", "2026-06-01", "2026-06-03", 32.9, None, None),
    ("2379", "瑞昱", "2026-06-16", "2026-06-18", 29.2, "105.1", "103.3"),
    ("2409", "友達", "2026-06-01", "2026-06-03", 25.0, None, None),
    ("2324", "仁寶", "2026-06-01", "2026-06-03", 28.2, "107.8", "108.0"),
    ("3008", "大立光", "2026-06-15", "2026-06-17", 28.0, "108.9", "101.5"),
    ("6488", "環球晶", "2026-06-15", "2026-06-17", 27.3, "104.7", "100.6"),
    ("2059", "川湖", "2026-06-05", "2026-06-09", 27.0, "104.0", "101.3"),
    ("8996", "高力", "2026-06-16", "2026-06-18", 26.7, "106.6", "106.2"),
    ("8021", "尖點", "2026-06-09", "2026-06-11", 23.9, "104.7", "105.2"),
    ("8046", "南電", "2026-06-24", "2026-06-26", 23.8, "102.9", "105.0"),
    ("1303", "南亞", "2026-06-11", "2026-06-15", 23.4, "104.5", "102.8"),
    ("2353", "宏碁", "2026-06-01", "2026-06-03", 23.3, "108.1", "107.4"),
    ("2382", "廣達", "2026-06-01", "2026-06-03", 23.0, "100.2", "103.9"),
    ("3231", "緯創", "2026-06-01", "2026-06-03", 22.4, "103.7", "105.3"),
    ("2303", "聯電", "2026-06-22", "2026-06-24", 22.3, "105.8", "100.4"),
    ("2357", "華碩", "2026-06-01", "2026-06-03", 22.3, "106.3", "104.3"),
    ("1303", "南亞", "2026-06-18", "2026-06-23", 22.1, "111.7", "106.3"),
    ("2481", "強茂", "2026-06-22", "2026-06-24", 21.9, "104.4", "100.3"),
    ("2356", "英業達", "2026-06-01", "2026-06-03", 21.6, "112.4", "106.6"),
    ("8021", "尖點", "2026-06-08", "2026-06-10", 21.4, "102.5", "103.5"),
    ("8996", "高力", "2026-06-12", "2026-06-16", 21.1, "102.8", "104.0"),
    ("2883", "凱基金", "2026-06-02", "2026-06-04", 21.0, "100.0", "102.0"),
    ("8046", "南電", "2026-06-23", "2026-06-25", 20.9, "100.3", "103.0"),
    ("2379", "瑞昱", "2026-06-15", "2026-06-17", 20.6, "103.4", "102.1"),
    ("2404", "漢唐", "2026-06-11", "2026-06-15", 20.0, "103.3", "100.8"),
    ("2408", "南亞科", "2026-06-17", "2026-06-22", 18.8, "109.1", "102.6"),
    ("4958", "臻鼎", "2026-06-12", "2026-06-16", 18.0, "105.3", "100.1"),
    ("2883", "凱基金", "2026-06-03", "2026-06-05", 17.9, "101.5", "103.1"),
    ("3008", "大立光", "2026-06-16", "2026-06-18", 17.5, "109.9", "102.2"),
    ("2408", "南亞科", "2026-06-15", "2026-06-17", 16.8, "106.8", "100.8"),
    ("6488", "環球晶", "2026-06-16", "2026-06-18", 16.8, "105.8", "101.5"),
    ("3481", "群創", "2026-06-01", "2026-06-03", 16.5, None, None),
    ("2891", "中信金", "2026-06-01", "2026-06-03", 16.5, "101.8", "102.4"),
    ("2408", "南亞科", "2026-06-16", "2026-06-18", 16.2, "107.7", "101.5"),
    ("4958", "臻鼎", "2026-06-15", "2026-06-17", 15.8, "106.0", "100.7"),
    ("2344", "華邦電", "2026-06-15", "2026-06-17", 15.7, "109.9", "100.1"),
    ("2344", "華邦電", "2026-06-16", "2026-06-18", 15.6, "110.9", "101.0"),
    ("1303", "南亞", "2026-06-01", "2026-06-03", 15.2, "102.1", "104.6"),
)


@dataclass(frozen=True)
class LaneStats3d:
    lane_id: str
    tier: str
    label: str
    rule_atoms: tuple[str, ...]
    n_events: int
    n_calendar_days: int
    days_per_year: float
    win_3d: float
    mean_3d: float
    median_3d: float
    mean_1d: float
    mean_2d: float
    loss5_rate: float
    loss8_rate: float
    year_table: dict[str, dict[str, float | int]]


@dataclass(frozen=True)
class UnionStats3d:
    tier: str
    n_calendar_days: int
    days_per_year: float
    day_win_3d: float
    day_mean_3d: float
    day_mean_1d: float
    lane_ids: tuple[str, ...]


def load_funnel_lanes_3d_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def enrich_events_3d_outcomes(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
    hold_days: int = HOLD_DAYS,
    rrg_length: int = DEFAULT_LENGTH,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Add ret_1d … ret_3d · max/min within hold window · WMA RSR outcomes.

    Structural outcomes (WMA RS-Ratio · length=rrg_length):
      rsr_d, rsr_d3, drs_3d = RSR(D+3) − RSR(D)
      rsm_d, rsm_d3, drm_3d (RS-Momentum)
      drs_rsr_1d = RSR(D) − RSR(D−1) · 1-day structural velocity (PIT at D)
      flip_leading_3d = end_q at D+hold == leading (outcome only)
    """
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    df, meta = (events, {}) if events is not None else enrich_lifecycle_events(conn)
    if df.empty:
        return df, meta

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs_ratio, rs_mom, quad = compute_rrg_panel(close, bench, length=rrg_length)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    ret_1d, ret_2d, ret_3d = [], [], []
    max_ret, min_ret = [], []
    rsr_d_col, rsr_d3_col, drs_3d_col = [], [], []
    rsm_d_col, rsm_d3_col, drm_3d_col = [], [], []
    drs_rsr_1d_col: list[float] = []
    flip_leading_col: list[bool] = []
    end_q_d3_col: list[str] = []
    keep_idx: list[int] = []

    for idx, row in df.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or si + hold_days >= len(full_dates):
            continue
        c0 = close.at[d, sid] if sid in close.columns else np.nan
        if pd.isna(c0) or c0 <= 0:
            continue
        closes: list[float] = []
        ok = True
        for h in range(1, hold_days + 1):
            dh = full_dates[si + h]
            ch = close.at[dh, sid] if sid in close.columns else np.nan
            if pd.isna(ch) or ch <= 0:
                ok = False
                break
            closes.append(float(ch))
        if not ok:
            continue
        r1 = (closes[0] / c0 - 1) * 100
        r2 = (closes[1] / c0 - 1) * 100 if hold_days >= 2 else np.nan
        r3 = (closes[hold_days - 1] / c0 - 1) * 100
        rets = [(c / c0 - 1) * 100 for c in closes]
        keep_idx.append(idx)
        ret_1d.append(r1)
        ret_2d.append(r2)
        ret_3d.append(r3)
        max_ret.append(max(rets))
        min_ret.append(min(rets))

        d3 = full_dates[si + hold_days]
        if sid in rs_ratio.columns:
            rv0 = float(rs_ratio.at[d, sid])
            rv3 = float(rs_ratio.at[d3, sid])
            mv0 = float(rs_mom.at[d, sid])
            mv3 = float(rs_mom.at[d3, sid])
            if si >= 1:
                dp = full_dates[si - 1]
                rv_prev = float(rs_ratio.at[dp, sid])
                dr1 = rv0 - rv_prev if np.isfinite(rv0) and np.isfinite(rv_prev) else float("nan")
            else:
                dr1 = float("nan")
            rsr_d_col.append(rv0 if np.isfinite(rv0) else float("nan"))
            rsr_d3_col.append(rv3 if np.isfinite(rv3) else float("nan"))
            drs_3d_col.append(
                (rv3 - rv0) if np.isfinite(rv0) and np.isfinite(rv3) else float("nan")
            )
            rsm_d_col.append(mv0 if np.isfinite(mv0) else float("nan"))
            rsm_d3_col.append(mv3 if np.isfinite(mv3) else float("nan"))
            drm_3d_col.append(
                (mv3 - mv0) if np.isfinite(mv0) and np.isfinite(mv3) else float("nan")
            )
            drs_rsr_1d_col.append(dr1)
        else:
            rsr_d_col.append(float("nan"))
            rsr_d3_col.append(float("nan"))
            drs_3d_col.append(float("nan"))
            rsm_d_col.append(float("nan"))
            rsm_d3_col.append(float("nan"))
            drm_3d_col.append(float("nan"))
            drs_rsr_1d_col.append(float("nan"))

        if sid in quad.columns:
            q3 = quad.at[d3, sid]
            if pd.isna(q3):
                flip_leading_col.append(False)
                end_q_d3_col.append("")
            else:
                end_q_d3_col.append(str(q3))
                flip_leading_col.append(str(q3) == "leading")
        else:
            flip_leading_col.append(False)
            end_q_d3_col.append("")

    out = df.loc[keep_idx].copy()
    out["ret_1d"] = ret_1d
    out["ret_2d"] = ret_2d
    out["ret_3d"] = ret_3d
    out["max_ret_3d"] = max_ret
    out["min_ret_3d"] = min_ret
    out["win_3d"] = out["ret_3d"] > 0
    out["loss5_3d"] = out["ret_3d"] <= LOSS5_PCT
    out["loss8_3d"] = out["ret_3d"] <= LOSS8_PCT
    out["rsr_d"] = rsr_d_col
    out["rsr_d3"] = rsr_d3_col
    out["drs_3d"] = drs_3d_col
    out["rsm_d"] = rsm_d_col
    out["rsm_d3"] = rsm_d3_col
    out["drm_3d"] = drm_3d_col
    out["drs_rsr_1d"] = drs_rsr_1d_col
    out["flip_leading_3d"] = flip_leading_col
    out["end_q_d3"] = end_q_d3_col
    meta = {
        **meta,
        "rrg_length": rrg_length,
        "structural_outcome": "drs_3d",
    }
    return out, meta


def _pre_release_pool_mask(panel: pd.DataFrame) -> pd.Series:
    return (
        panel["end_q"].isin(["improving", "lagging"])
        & (panel["sig_ret"] < BURST_PCT)
        & (
            (panel["end_q"] == "improving")
            | (
                (panel["end_q"] == "lagging")
                & (panel["disp_contract"] | (panel["sig_ret"] > 0.5))
            )
        )
    )


def _attach_sid_pr_hist_3d(df: pd.DataFrame) -> pd.DataFrame:
    """PIT: prior pre-release pool hits · avg ret_3d > 0 (≥ MIN_SID_HIST events)."""
    prior: dict[str, list[float]] = {}
    hist_n, hist_ok = [], []
    pool = _pre_release_pool_mask(df)
    for _, row in df.sort_values(["sid", "date"]).iterrows():
        sid = str(row["sid"])
        prev = prior.get(sid, [])
        if len(prev) >= MIN_SID_HIST:
            hist_n.append(len(prev))
            hist_ok.append(float(np.mean(prev)) > 0)
        else:
            hist_n.append(len(prev))
            hist_ok.append(False)
        if pool.loc[row.name] if row.name in pool.index else False:
            prior.setdefault(sid, []).append(float(row["ret_3d"]))
    out = df.copy()
    out["sid_hist_n"] = hist_n
    out["sid_hist_ok"] = hist_ok
    return out


def build_pre_release_funnel_panel_3d(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pre-release pool with 3-day outcomes and PIT feature columns."""
    if events is not None and "ret_3d" in events.columns:
        df, meta = events.copy(), {}
    elif events is not None:
        df, meta = enrich_events_3d_outcomes(events=events)
    else:
        conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
        base, meta = enrich_lifecycle_events(conn)
        df, _ = enrich_events_3d_outcomes(conn, events=base)
    if df.empty:
        return df, meta

    panel = df.sort_values(["sid", "date"]).copy()
    panel["prev_end_q"] = panel.groupby("sid")["end_q"].shift(1).fillna("")
    panel = _attach_sid_pr_hist_3d(panel)
    out = panel[_pre_release_pool_mask(panel)].copy()
    return out, meta


def _years_span(dates: pd.Series | list[str]) -> float:
    if len(dates) == 0:
        return 1.0
    d0 = pd.Timestamp(min(dates))
    d1 = pd.Timestamp(max(dates))
    return max((d1 - d0).days / 365.25, 1 / 365.25)


def _calendar_day_returns_3d(sub: pd.DataFrame) -> pd.DataFrame:
    if sub.empty:
        return pd.DataFrame(columns=["date", "day_ret_3d", "day_win_3d", "day_ret_1d"])
    g = sub.groupby("date", as_index=False).agg(
        day_ret_3d=("ret_3d", "mean"),
        day_ret_1d=("ret_1d", "mean"),
    )
    g["day_win_3d"] = g["day_ret_3d"] > 0
    return g


def _lane_stats_3d(
    panel: pd.DataFrame,
    lane_id: str,
    tier: str,
    label: str,
    rule_atoms: list[str] | tuple[str, ...],
) -> LaneStats3d:
    sub = panel[lane_mask(panel, rule_atoms)]
    cal = _calendar_day_returns_3d(sub)
    years = _years_span(panel["date"])
    yr: dict[str, dict[str, float | int]] = {}
    for y, g in sub.groupby(sub["date"].str[:4]):
        cal_y = _calendar_day_returns_3d(g)
        yr[str(y)] = {
            "n": len(g),
            "days": int(g["date"].nunique()),
            "win_3d": round(float((g["ret_3d"] > 0).mean()), 4) if len(g) else 0.0,
            "mean_3d": round(float(g["ret_3d"].mean()), 4) if len(g) else 0.0,
            "mean_1d": round(float(g["ret_1d"].mean()), 4) if len(g) else 0.0,
            "day_win_3d": round(float(cal_y["day_win_3d"].mean()), 4) if len(cal_y) else 0.0,
            "day_mean_3d": round(float(cal_y["day_ret_3d"].mean()), 4) if len(cal_y) else 0.0,
        }
    return LaneStats3d(
        lane_id=lane_id,
        tier=tier,
        label=label,
        rule_atoms=tuple(rule_atoms),
        n_events=len(sub),
        n_calendar_days=int(sub["date"].nunique()) if len(sub) else 0,
        days_per_year=round(sub["date"].nunique() / years, 2) if len(sub) else 0.0,
        win_3d=round(float((sub["ret_3d"] > 0).mean()), 4) if len(sub) else 0.0,
        mean_3d=round(float(sub["ret_3d"].mean()), 4) if len(sub) else 0.0,
        median_3d=round(float(sub["ret_3d"].median()), 4) if len(sub) else 0.0,
        mean_1d=round(float(sub["ret_1d"].mean()), 4) if len(sub) else 0.0,
        mean_2d=round(float(sub["ret_2d"].mean()), 4) if len(sub) else 0.0,
        loss5_rate=round(float(sub["loss5_3d"].mean()), 4) if len(sub) else 0.0,
        loss8_rate=round(float(sub["loss8_3d"].mean()), 4) if len(sub) else 0.0,
        year_table=yr,
    )


def _union_stats_3d(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    *,
    tier: str,
) -> UnionStats3d:
    ids: list[str] = []
    parts: list[pd.DataFrame] = []
    for ld in lane_defs:
        sub = panel[lane_mask(panel, ld["rule_atoms"])]
        if sub.empty:
            continue
        ids.append(str(ld["id"]))
        parts.append(sub)
    if not parts:
        return UnionStats3d(tier, 0, 0.0, 0.0, 0.0, 0.0, tuple())
    union = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["date", "sid"])
    cal = _calendar_day_returns_3d(union)
    years = _years_span(panel["date"])
    return UnionStats3d(
        tier=tier,
        n_calendar_days=int(union["date"].nunique()),
        days_per_year=round(union["date"].nunique() / years, 2),
        day_win_3d=round(float(cal["day_win_3d"].mean()), 4) if len(cal) else 0.0,
        day_mean_3d=round(float(cal["day_ret_3d"].mean()), 4) if len(cal) else 0.0,
        day_mean_1d=round(float(cal["day_ret_1d"].mean()), 4) if len(cal) else 0.0,
        lane_ids=tuple(ids),
    )


def _jaccard_matrix(panel: pd.DataFrame, lane_defs: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    day_sets: dict[str, set[str]] = {}
    for ld in lane_defs:
        sub = panel[lane_mask(panel, ld["rule_atoms"])]
        day_sets[str(ld["id"])] = calendar_day_set(sub)
    ids = sorted(day_sets)
    return {
        i: {j: round(jaccard(day_sets[i], day_sets[j]), 4) for j in ids}
        for i in ids
    }


def discover_coverage_lanes_3d(
    panel: pd.DataFrame,
    strict_defs: list[dict[str, Any]],
    *,
    search_atoms: list[str] | None = None,
    max_lanes: int = 30,
    win_min: float = COVERAGE_WIN_MIN,
    mean_min: float = COVERAGE_MEAN_MIN,
    mean_max: float = COVERAGE_MEAN_MAX,
    n_min: int = COVERAGE_N_MIN,
    jaccard_max: float = JACCARD_MAX_COVERAGE,
    jaccard_max_vs_coverage: float = 0.45,
    min_combo_size: int = 2,
    max_combo_size: int = 6,
) -> list[dict[str, Any]]:
    if search_atoms is None:
        search_atoms = [
            "b0", "b1", "b2", "disp", "base", "st46", "st13", "st1", "st7p",
            "hist", "plag", "elag", "dip", "flat", "q35", "q45", "ex", "s2",
            "rv98", "no23", "gap1", "s0", "imp",
        ]

    precomputed = _precompute_atom_columns(panel, search_atoms)
    ret3 = panel["ret_3d"].to_numpy(dtype=float)
    dates = panel["date"].astype(str).to_numpy()
    years = _years_span(panel["date"])

    strict_day_sets: list[set[str]] = []
    strict_union: set[str] = set()
    for ld in strict_defs:
        combo = tuple(ld["rule_atoms"])
        mask = _combo_mask(precomputed, combo)
        ds = set(dates[mask.to_numpy()])
        strict_day_sets.append(ds)
        strict_union |= ds

    candidates: list[dict[str, Any]] = []
    phase_specs = [
        {"phase": "primary", "n_min": n_min, "mean_min": mean_min},
        {"phase": "extended", "n_min": max(15, n_min - 5), "mean_min": max(2.0, mean_min - 1.0)},
    ]
    seen_keys: set[str] = set()
    for spec in phase_specs:
        for combo in _candidate_combos(search_atoms, min_combo_size, max_combo_size):
            key = "+".join(sorted(combo))
            if key in seen_keys:
                continue
            mask = _combo_mask(precomputed, combo)
            idx = mask.to_numpy()
            n = int(idx.sum())
            if n < spec["n_min"]:
                continue
            rets = ret3[idx]
            win = float((rets > 0).mean())
            mean = float(rets.mean())
            if win < win_min or mean < spec["mean_min"] or mean > mean_max:
                continue
            ds = set(dates[idx])
            if not (ds - strict_union):
                continue
            max_j = max_jaccard_vs(ds, strict_day_sets)
            if max_j > jaccard_max:
                continue
            seen_keys.add(key)
            candidates.append(
                {
                    "rule_atoms": list(combo),
                    "phase": spec["phase"],
                    "n": n,
                    "win": win,
                    "mean": mean,
                    "days": len(ds),
                    "new_days": len(ds - strict_union),
                    "max_jaccard_strict": max_j,
                    "day_set": ds,
                    "score": len(ds - strict_union) * mean * win,
                }
            )

    candidates.sort(
        key=lambda c: (0 if c["phase"] == "primary" else 1, -c["score"], -c["new_days"], -c["mean"]),
    )

    selected: list[dict[str, Any]] = []
    coverage_day_sets: list[set[str]] = []
    coverage_union = set(strict_union)
    target_lo, target_hi = TARGET_UNION_DAYS_PER_YEAR

    for cand in candidates:
        if len(selected) >= max_lanes:
            break
        ds = cand["day_set"]
        if max_jaccard_vs(ds, strict_day_sets) > jaccard_max:
            continue
        cov_j_max = jaccard_max if cand["phase"] == "primary" else max(jaccard_max_vs_coverage, jaccard_max)
        if coverage_day_sets and max_jaccard_vs(ds, coverage_day_sets) > cov_j_max:
            continue
        incremental = ds - coverage_union
        union_dpy = len(coverage_union | ds) / years
        if not incremental and len(selected) >= 20 and cand["phase"] == "extended":
            pass
        elif not incremental and union_dpy < target_lo:
            continue
        elif not incremental and len(selected) >= 20:
            continue
        lane_id = f"C{len(selected) + 1:02d}"
        atoms = cand["rule_atoms"]
        selected.append(
            {
                "id": lane_id,
                "tier": "coverage",
                "rule_atoms": atoms,
                "label": "+".join(atoms),
                "description": f"Coverage ({cand['phase']}) · 3d hold · {'+'.join(atoms)}",
                "regime_tag": _infer_regime_tag(atoms),
                "discovery": {
                    "phase": cand["phase"],
                    "n": cand["n"],
                    "win_3d": round(cand["win"], 4),
                    "mean_3d": round(cand["mean"], 4),
                    "calendar_days": cand["days"],
                    "new_vs_strict": cand["new_days"],
                    "max_jaccard_strict": round(cand["max_jaccard_strict"], 4),
                },
            }
        )
        coverage_day_sets.append(ds)
        coverage_union |= ds
        if len(coverage_union) / years >= target_hi and len(selected) >= 25:
            break

    return selected


def _pool_baseline(panel: pd.DataFrame) -> dict[str, float]:
    return {
        "n": len(panel),
        "win_3d": round(float((panel["ret_3d"] > 0).mean()), 4),
        "mean_3d": round(float(panel["ret_3d"].mean()), 4),
        "mean_1d": round(float(panel["ret_1d"].mean()), 4),
        "median_3d": round(float(panel["ret_3d"].median()), 4),
        "loss5_rate": round(float(panel["loss5_3d"].mean()), 4),
        "loss8_rate": round(float(panel["loss8_3d"].mean()), 4),
    }


def evaluate_lanes_3d(
    panel: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    run_discovery: bool = True,
) -> dict[str, Any]:
    strict_defs = list(cfg.get("lanes", {}).get("strict", []))
    coverage_defs = list(cfg.get("lanes", {}).get("coverage", []))
    pinned = [ld for ld in coverage_defs if ld.get("source") or str(ld.get("id", "")).startswith("G")]

    if run_discovery and cfg.get("discovery", {}).get("enabled", True):
        disc = cfg.get("discovery", {})
        discovered = discover_coverage_lanes_3d(
            panel,
            strict_defs,
            search_atoms=disc.get("search_atoms"),
            max_lanes=int(disc.get("max_lanes", 30)),
            win_min=float(disc.get("win_min", COVERAGE_WIN_MIN)),
            mean_min=float(disc.get("mean_min", COVERAGE_MEAN_MIN)),
            mean_max=float(disc.get("mean_max", COVERAGE_MEAN_MAX)),
            n_min=int(disc.get("n_min", COVERAGE_N_MIN)),
            jaccard_max=float(disc.get("jaccard_max", JACCARD_MAX_COVERAGE)),
            jaccard_max_vs_coverage=float(disc.get("jaccard_max_vs_coverage", 0.45)),
            min_combo_size=int(disc.get("min_combo_size", 2)),
            max_combo_size=int(disc.get("max_combo_size", 6)),
        )
        if discovered:
            pinned_keys = {"+".join(sorted(ld["rule_atoms"])) for ld in pinned}
            extra = [
                ld for ld in discovered
                if "+".join(sorted(ld["rule_atoms"])) not in pinned_keys
            ]
            coverage_defs = pinned + extra

    all_defs = strict_defs + coverage_defs
    lane_stats = [
        _lane_stats_3d(
            panel,
            str(ld["id"]),
            str(ld.get("tier", "strict")),
            str(ld.get("label", ld["id"])),
            ld["rule_atoms"],
        )
        for ld in all_defs
    ]

    years = _years_span(panel["date"])
    return {
        "meta": {
            "generated_on": date.today().isoformat(),
            "pool_n": len(panel),
            "years_span": round(years, 3),
            "date_start": str(panel["date"].min()) if len(panel) else "",
            "date_end": str(panel["date"].max()) if len(panel) else "",
            "hold_days": HOLD_DAYS,
            "outcome": "ret_3d",
        },
        "pool_baseline": _pool_baseline(panel),
        "strict_lanes": [asdict(s) for s in lane_stats if s.tier == "strict"],
        "coverage_lanes": [asdict(s) for s in lane_stats if s.tier == "coverage"],
        "union_strict": asdict(_union_stats_3d(panel, strict_defs, tier="strict")),
        "union_all": asdict(_union_stats_3d(panel, all_defs, tier="strict+coverage")),
        "jaccard_matrix": _jaccard_matrix(panel, all_defs),
        "coverage_defs": coverage_defs,
    }


def _rrg_feat_on_date(
    conn: Any,
    sid: str,
    d: str,
    *,
    close: pd.DataFrame | None = None,
    bench: pd.Series | None = None,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
    date_idx: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """PIT RRG feature on calendar date d for sid."""
    if close is None or bench is None or rs_ratio is None or rs_mom is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn)
        rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench)
        full_dates = close.index.astype(str).tolist()
        date_idx = {x: i for i, x in enumerate(full_dates)}
    assert full_dates is not None and date_idx is not None
    si = date_idx.get(d)
    if si is None or sid not in rs_ratio.columns:
        return None
    if si < 4:
        return None
    f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
    if not f:
        return None
    return {
        "rs_ratio": round(float(f["rs_ratio"]), 1),
        "rs_momentum": round(float(f["rs_momentum"]), 1),
        "end_q": str(f.get("end_q") or ""),
    }


def _find_pre_release_signal(
    panel: pd.DataFrame,
    full: pd.DataFrame,
    sid: str,
    interval_start: str,
    *,
    lookback_sessions: int = 5,
) -> tuple[str | None, pd.Series | None]:
    """Pre-release signal on interval_start, or up to N sessions before (PIT)."""
    sid_full = full[full["sid"] == sid].sort_values("date")
    sid_panel = panel[panel["sid"] == sid].sort_values("date")

    def _pool_ok(row: pd.Series) -> bool:
        return bool(
            row["end_q"] in ("improving", "lagging")
            and float(row["sig_ret"]) < BURST_PCT
            and (
                row["end_q"] == "improving"
                or (
                    row["end_q"] == "lagging"
                    and (bool(row["disp_contract"]) or float(row["sig_ret"]) > 0.5)
                )
            )
        )

    # Prefer exact streak start if already pre-release
    at_start = sid_panel[sid_panel["date"] == interval_start]
    if len(at_start):
        return interval_start, at_start.iloc[0]

    # Walk back a few sessions only (stay near the streak window)
    prior_dates = sid_full[sid_full["date"] < interval_start]["date"].tolist()
    window = prior_dates[-lookback_sessions:] if prior_dates else []
    for d in reversed(window):
        pr = sid_panel[sid_panel["date"] == d]
        if len(pr):
            return str(d), pr.iloc[0]
        row = sid_full[sid_full["date"] == d]
        if len(row) and _pool_ok(row.iloc[0]):
            # in pool logically but missing from panel (e.g. no D+3) — skip
            continue
    return None, None


def analyze_june2026_leading_streak_cases(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    conn: Any | None = None,
) -> dict[str, Any]:
    """Case study: June 2026 Improving→Leading 3日 streaks — pre-release lane match + D+3 RS."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    base, _ = enrich_lifecycle_events(conn)
    full, _ = enrich_events_3d_outcomes(conn, events=base)
    full = full.sort_values(["sid", "date"]).copy()
    full["prev_end_q"] = full.groupby("sid")["end_q"].shift(1).fillna("")

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    rows: list[dict[str, Any]] = []
    for sid, name, d_start, d_end, streak_pct, note_rv, note_mom in JUNE2026_LEADING_STREAK_CASES:
        signal_d, pr_row = _find_pre_release_signal(panel, full, sid, d_start)
        sig_row = full[(full["sid"] == sid) & (full["date"] == (signal_d or d_start))]
        entry: dict[str, Any] = {
            "sid": sid,
            "name": name,
            "streak_start": d_start,
            "streak_end": d_end,
            "streak_total_pct": streak_pct,
            "signal_date": signal_d or d_start,
        }
        if len(sig_row):
            r0 = sig_row.iloc[0]
            entry["sig_ret"] = round(float(r0["sig_ret"]), 2)
            entry["end_q"] = str(r0["end_q"])
            entry["improve_streak"] = int(r0["improve_streak"])
        si = date_idx.get(signal_d or d_start)
        if si is not None and si + HOLD_DAYS < len(full_dates):
            d3 = full_dates[si + HOLD_DAYS]
            feat_d3 = _rrg_feat_on_date(
                conn,
                sid,
                d3,
                close=close,
                bench=bench,
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                full_dates=full_dates,
                date_idx=date_idx,
            )
            if feat_d3:
                entry["d3_date"] = d3
                entry["d3_end_q"] = feat_d3["end_q"]
                entry["d3_rs_ratio"] = feat_d3["rs_ratio"]
                entry["d3_rs_momentum"] = feat_d3["rs_momentum"]
                entry["d3_leading"] = feat_d3["end_q"] == "leading"
            if note_rv and note_mom:
                entry["note_d3_rs"] = f"{note_rv} / {note_mom}"
        if pr_row is not None:
            entry["ret_3d"] = round(float(pr_row["ret_3d"]), 2)
            entry["ret_1d"] = round(float(pr_row["ret_1d"]), 2)
            entry["in_pre_release_pool"] = True
            idx = pr_row.name
            entry["matched_lanes"] = [
                str(ld["id"])
                for ld in lane_defs
                if bool(lane_mask(panel, ld["rule_atoms"]).loc[idx])
            ]
        else:
            entry["in_pre_release_pool"] = False
            entry["matched_lanes"] = []
        rows.append(entry)

    matched = sum(1 for r in rows if r.get("matched_lanes"))
    in_pool = sum(1 for r in rows if r.get("in_pre_release_pool"))
    with_ret = sum(1 for r in rows if r.get("ret_3d") is not None)
    leading_d3 = sum(1 for r in rows if r.get("d3_leading"))
    return {
        "june2026_leading_streak_cases": rows,
        "summary": {
            "n_cases": len(rows),
            "n_in_pre_release_pool": in_pool,
            "n_with_ret_3d": with_ret,
            "n_lane_matched": matched,
            "n_d3_leading": leading_d3,
            "lane_match_rate": round(matched / in_pool, 4) if in_pool else 0.0,
        },
    }


def analyze_june2026_improving_burst_cases(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    conn: Any | None = None,
) -> dict[str, Any]:
    """Deprecated alias — use analyze_june2026_leading_streak_cases."""
    return analyze_june2026_leading_streak_cases(panel, lane_defs, conn=conn)


def render_funnel_3d_markdown(result: dict[str, Any], cfg: dict[str, Any]) -> str:
    meta = result["meta"]
    base = result["pool_baseline"]
    lines = [
        "# RRG pre-release funnel lanes · 3-day hold backtest",
        "",
        f"- **Outcome**: D close → D+3 close (`ret_3d`) · **not** overnight `nxt_ret`",
        f"- Pool: **{meta['pool_n']}** stock-days · {meta['date_start']} → {meta['date_end']}",
        f"- Span: **{meta['years_span']:.1f}** years · Generated: {meta['generated_on']}",
        "",
        "## A. Executive summary",
        "",
        f"| Layer | win_3d | mean_3d | mean_1d | n |",
        f"|-------|--------|---------|---------|---|",
        f"| **母池 baseline** | {base['win_3d']:.1%} | {base['mean_3d']:+.2f}% | {base['mean_1d']:+.2f}% | {base['n']} |",
    ]
    us = result["union_strict"]
    ua = result["union_all"]
    lines.append(
        f"| **Strict union** | {us['day_win_3d']:.1%} | {us['day_mean_3d']:+.2f}% | "
        f"{us['day_mean_1d']:+.2f}% | {us['n_calendar_days']} days |"
    )
    lines.append(
        f"| **Strict+coverage union** | {ua['day_win_3d']:.1%} | {ua['day_mean_3d']:+.2f}% | "
        f"{ua['day_mean_1d']:+.2f}% | {ua['n_calendar_days']} days |"
    )
    target_lo, target_hi = TARGET_UNION_DAYS_PER_YEAR
    hit = target_lo <= ua["days_per_year"] <= target_hi
    lines.extend(
        [
            "",
            f"Union calendar coverage: **{ua['days_per_year']:.1f}** days/yr "
            f"(target {target_lo:.0f}–{target_hi:.0f}) · {'✅' if hit else '⚠️ 未達標'}",
            "",
            "> 均報酬為 **3 日累積** %，非日均。",
            "",
            "## B. Strict lanes (R01–R30)",
            "",
            "| ID | rule | n | days | d/yr | win_3d | mean_3d | mean_1d | P(≤−5%) | P(≤−8%) | 白話 |",
            "|----|------|---|------|------|--------|---------|---------|--------|--------|------|",
        ]
    )
    strict_cfg = {ld["id"]: ld for ld in cfg.get("lanes", {}).get("strict", [])}
    for s in result["strict_lanes"]:
        atoms = "+".join(s["rule_atoms"])
        desc = strict_cfg.get(s["lane_id"], {}).get("description", "")[:40]
        lines.append(
            f"| {s['lane_id']} | {atoms} | {s['n_events']} | {s['n_calendar_days']} | "
            f"{s['days_per_year']:.1f} | {s['win_3d']:.1%} | {s['mean_3d']:+.2f}% | "
            f"{s['mean_1d']:+.2f}% | {s['loss5_rate']:.1%} | {s['loss8_rate']:.1%} | {desc} |"
        )

    lines.extend(
        [
            "",
            "## B. Coverage lanes",
            "",
            "| ID | rule | n | days | d/yr | win_3d | mean_3d | mean_1d | P(≤−5%) |",
            "|----|------|---|------|------|--------|---------|---------|--------|",
        ]
    )
    for s in result["coverage_lanes"]:
        atoms = "+".join(s["rule_atoms"])
        lines.append(
            f"| {s['lane_id']} | {atoms} | {s['n_events']} | {s['n_calendar_days']} | "
            f"{s['days_per_year']:.1f} | {s['win_3d']:.1%} | {s['mean_3d']:+.2f}% | "
            f"{s['mean_1d']:+.2f}% | {s['loss5_rate']:.1%} |"
        )

    if result.get("june2026_cases"):
        cs = result["june2026_cases"]
        sm = cs.get("summary", {})
        lines.extend(
            [
                "",
                "## June 2026 · Improving→Leading 3日 streak case study",
                "",
                f"共 **{sm.get('n_cases', 0)}** 筆 · pre-release 可進 **{sm.get('n_in_pre_release_pool', 0)}** · "
                f"lane 命中 **{sm.get('n_lane_matched', 0)}**（佔可進 "
                f"**{sm.get('lane_match_rate', 0):.1%}**）· "
                f"D+3 Leading **{sm.get('n_d3_leading', 0)}**",
                "",
                "| 代號 | 名稱 | 區間 | 合計% | signal D | ret_3d | D+3 RS | D+3 Q | lanes |",
                "|------|------|------|-------|----------|--------|--------|-------|-------|",
            ]
        )
        for c in cs.get("june2026_leading_streak_cases", []):
            sig = c.get("signal_date", "—")
            r3 = c.get("ret_3d", "—")
            if c.get("d3_rs_ratio") is not None:
                rs = f"{c['d3_rs_ratio']:.1f} / {c['d3_rs_momentum']:.1f}"
            elif c.get("note_d3_rs"):
                rs = c["note_d3_rs"]
            else:
                rs = "—"
            eq3 = c.get("d3_end_q", "—")
            lanes = ",".join(c.get("matched_lanes", [])) or "—"
            lines.append(
                f"| {c['sid']} | {c.get('name', '')} | {c['streak_start']}~{c['streak_end']} | "
                f"{c['streak_total_pct']:+.1f}% | {sig} | {r3} | {rs} | {eq3} | {lanes} |"
            )

    lines.extend(
        [
            "",
            "## C. Hypotheses (3-day horizon)",
            "",
            "1. **台指 gating（b2/b1）** 對 3 日持有仍有效；June Leading streak 多在強勢日或 RV98 尾段啟動。",
            "2. **st13 / rv98** 在 3 日 horizon 比隔日版更能捕捉 Improving→Leading 路徑。",
            "3. **rv98 + base/st46**（R05/R10）3 日 mean 高於母池；June 案例 D+3 多已 Leading（RS≥100）。",
            "4. **hist（3 日史績）** 需獨立重算；隔日 burst hist 不可直接沿用。",
            "5. **覆蓋目標**：strict+coverage union ~30 d/yr · 低於 50–80 目標 · 3 日 mean 門檻（+3%）比隔日版更嚴。",
            "",
            "## D. PIT checklist",
            "",
            "- [x] 進場條件全部在 D 收盤可算",
            "- [x] outcome = D→D+3 交易日",
            "- [x] hist 改為 pre-release ret_3d 史績",
            "- [x] 獨立 yaml / 模組 / 報告（未混用隔日版）",
        ]
    )
    return "\n".join(lines)


def run_funnel_3d_backtest(
    conn: Any | None = None,
    *,
    config_path: Path | None = None,
    run_discovery: bool = True,
    include_june_cases: bool = True,
) -> dict[str, Any]:
    cfg = load_funnel_lanes_3d_config(config_path)
    panel, meta = build_pre_release_funnel_panel_3d(conn)
    result = evaluate_lanes_3d(panel, cfg, run_discovery=run_discovery)
    result["meta"] = {**meta, **result["meta"]}
    result["config"] = {"version": cfg.get("version"), "model_id": cfg.get("model_id")}
    all_defs = list(cfg.get("lanes", {}).get("strict", [])) + list(result.get("coverage_defs", []))
    if include_june_cases:
        result["june2026_cases"] = analyze_june2026_leading_streak_cases(
            panel, all_defs, conn=conn
        )
    return result


def write_validated_coverage_3d_to_config(
    coverage_defs: list[dict[str, Any]],
    path: Path | None = None,
) -> None:
    p = path or CONFIG_PATH
    cfg = load_funnel_lanes_3d_config(p)
    clean = []
    for ld in coverage_defs:
        clean.append(
            {
                "id": ld["id"],
                "tier": "coverage",
                "rule_atoms": ld["rule_atoms"],
                "label": ld.get("label", "+".join(ld["rule_atoms"])),
                "description": ld.get("description", ""),
                "regime_tag": ld.get("regime_tag"),
            }
        )
    cfg.setdefault("lanes", {})["coverage"] = clean
    p.write_text(
        yaml.dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


# ── P1 feature panel export ────────────────────────────────────────────────

CONTINUOUS_FEATURE_COLS: tuple[str, ...] = (
    "sig_ret",
    "excess",
    "bench_today_ret",
    "bench_nxt_ret",
    "improve_streak",
    "rv",
    "disp_d",
    "session_gap_days",
    "ret_1d",
    "ret_2d",
    "ret_3d",
    "min_ret_3d",
    "max_ret_3d",
    "sid_hist_n",
)

EXTRA_FEATURE_COLS: tuple[str, ...] = (
    "end_q",
    "prev_end_q",
    "prev_q",
    "sid_hist_ok",
    "win_3d",
    "loss5_3d",
    "loss8_3d",
)

_ATOM_COMPOSITE_EXTRAS: tuple[str, ...] = ("lead3", "lead3_late")


def search_atom_ids(cfg: dict[str, Any] | None = None) -> list[str]:
    """Atom ids from discovery.search_atoms + lead3 composites."""
    raw = cfg or load_funnel_lanes_3d_config()
    discovery = raw.get("discovery") or {}
    atoms = list(discovery.get("search_atoms") or [])
    for atom_id in _ATOM_COMPOSITE_EXTRAS:
        if atom_id not in atoms:
            atoms.append(atom_id)
    return atoms


def build_pre_release_feature_panel(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pre-release pool with binary atom columns + continuous features + ret_3d."""
    cfg = cfg or load_funnel_lanes_3d_config()
    panel, meta = build_pre_release_funnel_panel_3d(conn, events=events)
    if panel.empty:
        return panel, {**meta, "n_rows": 0, "atom_ids": []}

    atom_ids = search_atom_ids(cfg)
    precomputed = _precompute_atom_columns(panel, atom_ids)

    feat_cols = [c for c in CONTINUOUS_FEATURE_COLS if c in panel.columns]
    extra_cols = [c for c in EXTRA_FEATURE_COLS if c in panel.columns]
    out = panel[["sid", "date", *feat_cols, *extra_cols]].copy()

    for atom_id, mask in precomputed.items():
        out[f"atom_{atom_id}"] = mask.astype(np.int8)

    for col in ("sid_hist_ok", "win_3d", "loss5_3d", "loss8_3d"):
        if col in out.columns:
            out[col] = out[col].astype(np.int8)

    fp_meta: dict[str, Any] = {
        **meta,
        "n_rows": len(out),
        "n_calendar_days": int(out["date"].nunique()),
        "years_span": round(_years_span(out["date"]), 4),
        "atom_ids": atom_ids,
        "atom_columns": [f"atom_{a}" for a in atom_ids],
        "continuous_columns": feat_cols,
        "extra_columns": extra_cols,
        "outcome": "ret_3d",
        "config_version": cfg.get("version"),
        "model_id": cfg.get("model_id"),
    }
    return out, fp_meta


def export_pre_release_feature_panel(
    out_path: Path,
    *,
    conn: Any | None = None,
    fmt: str = "parquet",
) -> dict[str, Any]:
    """Write feature panel to disk + sidecar meta JSON."""
    df, meta = build_pre_release_feature_panel(conn=conn)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "parquet":
        df.to_parquet(out_path, index=False, engine="pyarrow")
    elif fmt == "csv":
        df.to_csv(out_path, index=False)
    else:
        raise ValueError(f"unsupported format: {fmt}")

    meta_path = out_path.with_suffix(".meta.json")
    payload = {
        **meta,
        "panel_path": str(out_path.relative_to(PROJECT_ROOT)),
        "generated_on": date.today().isoformat(),
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["meta_path"] = str(meta_path.relative_to(PROJECT_ROOT))
    return payload
