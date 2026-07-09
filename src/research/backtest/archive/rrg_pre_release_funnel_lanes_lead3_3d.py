"""RRG lead3 funnel lanes · 3-day hold · Improving RV98–100 streak≥4 sub-pool.

lead3 atom (PIT · signal-day D):
  end_q=improving ∧ RV∈[98,100) ∧ improve_streak≥4

Outcome: ret_3d (D→D+3) · auxiliary flip_leading_3d (D+3 end_q=leading).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (
    HOLD_DAYS,
    LOSS5_PCT,
    LOSS8_PCT,
    JUNE2026_LEADING_STREAK_CASES,
    LaneStats3d,
    UnionStats3d,
    _calendar_day_returns_3d,
    _find_pre_release_signal,
    _lane_stats_3d,
    _pool_baseline,
    _union_stats_3d,
    _years_span,
    analyze_june2026_leading_streak_cases,
    build_pre_release_funnel_panel_3d,
    enrich_events_3d_outcomes,
)
from research.backtest.archive.rrg_pre_release_funnel_lanes import (
    _atom_mask,
    _combo_mask,
    _infer_regime_tag,
    _jaccard_matrix,
    _precompute_atom_columns,
    calendar_day_set,
    jaccard,
    lane_mask,
    max_jaccard_vs,
)
from rrg_rotation import compute_rrg_panel
from stock_db import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "rrg_pre_release_funnel_lanes_lead3_3d.yaml"

LEAD3_STRICT_WIN_MIN = 0.58
LEAD3_STRICT_MEAN_MIN = 3.0
LEAD3_STRICT_N_MIN = 8
LEAD3_COVERAGE_WIN_MIN = 0.55
LEAD3_COVERAGE_MEAN_MIN = 3.0
LEAD3_COVERAGE_MEAN_MAX = 8.0
LEAD3_COVERAGE_N_MIN = 10
JACCARD_MAX = 0.32
JACCARD_MAX_COVERAGE = 0.40
LOSS5_RATE_MAX = 0.15
TARGET_UNION_DAYS_PER_YEAR = (15.0, 45.0)

# H2a · moderate-bench validation target
H2A_B1_COMBO: tuple[str, ...] = ("lead3", "b1", "disp", "hist", "q35")
# H3a · late streak (st7p within lead3) ortho sweep
H3A_FORCE_ATOMS: frozenset[str] = frozenset({"st7p"})
# H4a · st7p coil (June case atoms) · H4b · high-ret lift combo
H4A_ST7P_COIL_COMBOS: tuple[tuple[str, ...], ...] = (
    ("lead3", "st7p", "disp", "flat"),
    ("lead3", "st7p", "disp", "flat", "hist"),
    ("lead3", "st7p", "disp", "flat", "gap1"),
)
H4B_HIGH_RET_COMBOS: tuple[tuple[str, ...], ...] = (
    ("lead3", "disp", "hist", "q35"),
    ("lead3", "b2", "disp", "hist", "q35"),
    ("lead3", "disp", "hist", "q35", "st46"),
)
H4_COVERAGE_MEAN_MIN = 2.5
H4_COVERAGE_WIN_MIN = 0.55
H4_COVERAGE_N_MIN = 15
H4_COVERAGE_JACCARD_MAX = 0.45
CASE_BACKTRACK_SESSIONS = 10
HIGH_RET_3D_PCT = 15.0

ARCHETYPE_LABELS: dict[str, str] = {
    "b2_burst": "強台指×disp burst",
    "base_coil": "base setup 蓄力",
    "s2_dip": "S2×dip 蓄力",
    "plag_flip": "前日 Lagging flip",
    "b1_moderate": "溫和台指 bench",
    "late_streak": "late streak st7p+",
    "st7p_coil": "st7p×disp/flat 蓄力",
    "high_ret_lift": "disp×hist×q35 高報酬 lift",
}

# 因子維度 · 每條 lane 每維最多 1 個 atom（正交微策略）
LEAD3_FACTOR_DIMENSIONS: dict[str, list[str]] = {
    "bench": ["b2", "b1", "b0"],
    "rrg_shape": ["disp", "base", "plag", "s2"],
    "streak_ref": ["st46", "st7p", "no23"],
    "price": ["q45", "q35", "dip", "flat", "ex"],
    "quality": ["hist", "gap1"],
}

LEAD3_EXTRA_ATOMS = [
    "b0", "b1", "b2", "disp", "base", "st46", "st7p", "lead3_late", "hist", "plag",
    "dip", "flat", "q35", "q45", "ex", "gap1", "no23", "s2",
]


def load_lead3_funnel_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def infer_archetype_tag(rule_atoms: list[str] | tuple[str, ...]) -> str:
    """Map lane rule_atoms → discovery archetype (one per strict tier)."""
    atoms = set(rule_atoms) - {"lead3", "lead3_late"}
    if "st7p" in atoms or "lead3_late" in rule_atoms:
        if "flat" in atoms or ("base" in atoms and "disp" in atoms):
            return "st7p_coil"
        return "late_streak"
    if "plag" in atoms:
        return "plag_flip"
    if "b1" in atoms and "b2" not in atoms:
        return "b1_moderate"
    if "disp" in atoms and "hist" in atoms and "q35" in atoms and "b2" not in atoms and "base" not in atoms:
        return "high_ret_lift"
    if "s2" in atoms and "dip" in atoms:
        return "s2_dip"
    if "base" in atoms:
        return "base_coil"
    if "b2" in atoms:
        return "b2_burst"
    return "other"


def _combo_stats_row(
    precomputed: dict[str, pd.Series],
    panel: pd.DataFrame,
    combo: tuple[str, ...],
) -> dict[str, Any] | None:
    """Full combo stats incl. loss tail · None if empty."""
    mask = _combo_mask(precomputed, combo).to_numpy()
    n = int(mask.sum())
    if n == 0:
        return None
    ret = panel["ret_3d"].to_numpy(dtype=float)
    flip = panel["flip_leading_3d"].to_numpy(dtype=bool)
    dates = panel["date"].astype(str).to_numpy()
    rets = ret[mask]
    return {
        "rule_atoms": list(combo),
        "n": n,
        "win_3d": round(float((rets > 0).mean()), 4),
        "mean_3d": round(float(rets.mean()), 4),
        "median_3d": round(float(np.median(rets)), 4),
        "loss5_rate": round(float((rets <= LOSS5_PCT).mean()), 4),
        "loss8_rate": round(float((rets <= LOSS8_PCT).mean()), 4),
        "min_ret_3d": round(float(rets.min()), 4),
        "flip_leading_3d": round(float(flip[mask].mean()), 4),
        "calendar_days": len(set(dates[mask])),
        "day_set": set(dates[mask]),
        "archetype_tag": infer_archetype_tag(combo),
    }


def _lane_stats_with_loss(panel: pd.DataFrame, ld: dict[str, Any]) -> dict[str, Any]:
    st = _lane_stats_3d(
        panel,
        str(ld["id"]),
        str(ld.get("tier", "strict")),
        str(ld.get("label", ld["id"])),
        ld["rule_atoms"],
    )
    sub = panel[lane_mask(panel, ld["rule_atoms"])]
    d = asdict(st)
    d["archetype_tag"] = ld.get("archetype_tag") or infer_archetype_tag(ld["rule_atoms"])
    d["min_ret_3d"] = round(float(sub["min_ret_3d"].min()), 4) if len(sub) else 0.0
    return d


def _attach_flip_leading_3d(df: pd.DataFrame, conn: Any) -> pd.DataFrame:
    """PIT auxiliary: D+3 quadrant == leading (outcome only · not for entry)."""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    _, _, quad = compute_rrg_panel(close, bench)
    full_dates = close.index.astype(str).tolist()
    date_idx = {d: i for i, d in enumerate(full_dates)}

    flip, end_q_d3 = [], []
    for _, row in df.iterrows():
        d, sid = str(row["date"]), str(row["sid"])
        si = date_idx.get(d)
        if si is None or si + HOLD_DAYS >= len(full_dates) or sid not in quad.columns:
            flip.append(False)
            end_q_d3.append("")
            continue
        d3 = full_dates[si + HOLD_DAYS]
        q = quad.at[d3, sid]
        if pd.isna(q):
            flip.append(False)
            end_q_d3.append("")
        else:
            end_q_d3.append(str(q))
            flip.append(str(q) == "leading")
    out = df.copy()
    out["flip_leading_3d"] = flip
    out["end_q_d3"] = end_q_d3
    return out


def build_lead3_funnel_panel_3d(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pre-release pool ∩ lead3 · ret_3d + flip_leading_3d."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_pre_release_funnel_panel_3d(conn, events=events)
    if panel.empty:
        return panel, meta
    if "rv" not in panel.columns:
        panel = panel.copy()
        panel["rv"] = np.nan
    lead3 = _atom_mask(panel, "lead3")
    out = panel[lead3].copy()
    if "flip_leading_3d" not in out.columns:
        out = _attach_flip_leading_3d(out, conn)
    meta = {**meta, "lead3_pool_n": len(out), "lead3_flip_rate": round(float(out["flip_leading_3d"].mean()), 4) if len(out) else 0.0}
    return out, meta


def _lead3_baseline(panel: pd.DataFrame) -> dict[str, float]:
    base = _pool_baseline(panel)
    base["flip_leading_3d_rate"] = round(float(panel["flip_leading_3d"].mean()), 4) if len(panel) else 0.0
    base["mean_ret_when_flip"] = (
        round(float(panel.loc[panel["flip_leading_3d"], "ret_3d"].mean()), 4)
        if panel["flip_leading_3d"].any()
        else 0.0
    )
    return base


def _candidate_lead3_combos(extra_atoms: list[str], min_extra: int = 1, max_extra: int = 4) -> list[tuple[str, ...]]:
    combos: list[tuple[str, ...]] = []
    for k in range(min_extra, max_extra + 1):
        for extra in itertools.combinations(extra_atoms, k):
            combos.append(tuple(["lead3", *sorted(extra)]))
    return combos


def _candidate_ortho_combos(
    dimensions: dict[str, list[str]] | None = None,
    *,
    min_dims: int = 2,
) -> list[tuple[str, ...]]:
    """One atom per dimension at most · mandatory lead3 prefix."""
    dimensions = dimensions or LEAD3_FACTOR_DIMENSIONS
    opts: list[list[str]] = [[]]
    for _dim, atoms in dimensions.items():
        new: list[list[str]] = []
        for base in opts:
            new.append(base)
            for a in atoms:
                new.append(base + [a])
        opts = new
    out: list[tuple[str, ...]] = []
    for extra in opts:
        if len(extra) < min_dims:
            continue
        out.append(tuple(["lead3", *sorted(extra)]))
    return out


def _dedupe_combo_mask(
    precomputed: dict[str, pd.Series],
    combo: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    """Drop redundant atoms that don't change the hit set (e.g. no23 in lead3 pool)."""
    atoms = list(combo)
    mask = _combo_mask(precomputed, tuple(atoms))
    n = int(mask.sum())
    changed = True
    while changed and len(atoms) > 2:
        changed = False
        for a in list(atoms):
            if a == "lead3":
                continue
            trial = [x for x in atoms if x != a]
            if _combo_mask(precomputed, tuple(trial)).sum() == n:
                atoms = trial
                changed = True
                break
    return tuple(atoms), n


def analyze_lead3_factor_lift(
    panel: pd.DataFrame,
    *,
    extra_atoms: list[str] | None = None,
    n_min: int = 8,
) -> list[dict[str, Any]]:
    """Single-atom lift vs lead3 pool baseline on ret_3d."""
    extra_atoms = extra_atoms or LEAD3_EXTRA_ATOMS
    pre = _precompute_atom_columns(panel, ["lead3", *extra_atoms])
    ret = panel["ret_3d"].to_numpy(dtype=float)
    base_mean = float(ret.mean())
    base_win = float((ret > 0).mean())
    rows: list[dict[str, Any]] = []
    for atom in extra_atoms:
        m = pre[atom].to_numpy()
        n = int(m.sum())
        if n < n_min:
            continue
        rets = ret[m]
        rows.append(
            {
                "atom": atom,
                "dimension": next((d for d, xs in LEAD3_FACTOR_DIMENSIONS.items() if atom in xs), "other"),
                "n": n,
                "win_3d": round(float((rets > 0).mean()), 4),
                "mean_3d": round(float(rets.mean()), 4),
                "lift_mean": round(float(rets.mean()) - base_mean, 4),
                "lift_win": round(float((rets > 0).mean()) - base_win, 4),
            }
        )
    rows.sort(key=lambda r: (-r["lift_mean"], -r["mean_3d"]))
    return rows


def analyze_lead3_factor_phi(
    panel: pd.DataFrame,
    *,
    extra_atoms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Phi coefficient between atom masks · flags same-dimension redundancy."""
    extra_atoms = extra_atoms or LEAD3_EXTRA_ATOMS
    pre = _precompute_atom_columns(panel, extra_atoms)
    rows: list[dict[str, Any]] = []
    for a, b in itertools.combinations(extra_atoms, 2):
        ma, mb = pre[a].to_numpy(), pre[b].to_numpy()
        n11 = int((ma & mb).sum())
        n10 = int((ma & ~mb).sum())
        n01 = int((~ma & mb).sum())
        n00 = int((~ma & ~mb).sum())
        denom = np.sqrt((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00))
        phi = float((n11 * n00 - n10 * n01) / denom) if denom else 0.0
        dim_a = next((d for d, xs in LEAD3_FACTOR_DIMENSIONS.items() if a in xs), "?")
        dim_b = next((d for d, xs in LEAD3_FACTOR_DIMENSIONS.items() if b in xs), "?")
        rows.append(
            {
                "atom_a": a,
                "atom_b": b,
                "phi": round(phi, 4),
                "same_dimension": dim_a == dim_b,
                "dim_a": dim_a,
                "dim_b": dim_b,
            }
        )
    rows.sort(key=lambda r: -abs(r["phi"]))
    return rows


def sweep_lead3_combos_3d(
    panel: pd.DataFrame,
    *,
    mean_min: float = LEAD3_STRICT_MEAN_MIN,
    win_min: float = LEAD3_STRICT_WIN_MIN,
    n_min: int = LEAD3_STRICT_N_MIN,
    ortho_only: bool = True,
    max_extra: int = 5,
    require_atoms: set[str] | None = None,
    max_loss5_rate: float | None = None,
) -> list[dict[str, Any]]:
    """Exhaustive sweep on lead3 pool · returns ranked combo stats."""
    pre = _precompute_atom_columns(panel, ["lead3", *LEAD3_EXTRA_ATOMS])
    combos = (
        _candidate_ortho_combos(min_dims=2)
        if ortho_only
        else _candidate_lead3_combos(LEAD3_EXTRA_ATOMS, 2, max_extra)
    )
    if require_atoms:
        combos = [c for c in combos if require_atoms <= set(c)]
    seen: set[tuple[str, ...]] = set()
    rows: list[dict[str, Any]] = []
    for combo in combos:
        combo_d, n = _dedupe_combo_mask(pre, combo)
        if combo_d in seen or n < n_min:
            continue
        seen.add(combo_d)
        row = _combo_stats_row(pre, panel, combo_d)
        if row is None:
            continue
        if row["mean_3d"] < mean_min or row["win_3d"] < win_min:
            continue
        if max_loss5_rate is not None and row["loss5_rate"] > max_loss5_rate:
            continue
        row["score"] = row["mean_3d"] * row["win_3d"]
        rows.append(row)
    rows.sort(key=lambda r: (-r["mean_3d"], -r["win_3d"], -r["n"]))
    return rows


def discover_lead3_lanes_3d(
    panel: pd.DataFrame,
    *,
    tier: str = "strict",
    max_lanes: int = 15,
    win_min: float = LEAD3_STRICT_WIN_MIN,
    mean_min: float = LEAD3_STRICT_MEAN_MIN,
    n_min: int = LEAD3_STRICT_N_MIN,
    mean_max: float = LEAD3_COVERAGE_MEAN_MAX,
    jaccard_max: float = JACCARD_MAX,
    max_loss5_rate: float | None = LOSS5_RATE_MAX,
    extra_atoms: list[str] | None = None,
    existing_day_sets: list[set[str]] | None = None,
    existing_archetypes: set[str] | None = None,
    ortho_only: bool = True,
) -> list[dict[str, Any]]:
    """Greedy discovery · every lane includes lead3 · unique archetype_tag in strict."""
    candidates = sweep_lead3_combos_3d(
        panel,
        mean_min=mean_min,
        win_min=win_min,
        n_min=n_min,
        ortho_only=ortho_only,
        max_extra=5 if tier == "strict" else 4,
        max_loss5_rate=max_loss5_rate if tier == "strict" else None,
    )
    if mean_max < 999:
        candidates = [c for c in candidates if c["mean_3d"] <= mean_max]

    selected: list[dict[str, Any]] = []
    day_sets: list[set[str]] = list(existing_day_sets or [])
    used_archetypes: set[str] = set(existing_archetypes or [])
    prefix = "L" if tier == "strict" else "LC"

    for cand in candidates:
        if len(selected) >= max_lanes:
            break
        arch = cand.get("archetype_tag") or infer_archetype_tag(cand["rule_atoms"])
        if tier == "strict" and arch in used_archetypes:
            continue
        ds = cand["day_set"]
        if day_sets and max_jaccard_vs(ds, day_sets) > jaccard_max:
            continue
        lane_id = f"{prefix}{len(selected) + 1:02d}"
        atoms_s = cand["rule_atoms"]
        selected.append(
            {
                "id": lane_id,
                "tier": tier,
                "rule_atoms": atoms_s,
                "label": "+".join(atoms_s),
                "description": f"lead3≥3% · {'+'.join(a for a in atoms_s if a != 'lead3')}",
                "regime_tag": _infer_regime_tag(atoms_s),
                "archetype_tag": arch,
                "discovery": {
                    "n": cand["n"],
                    "win_3d": cand["win_3d"],
                    "mean_3d": cand["mean_3d"],
                    "loss5_rate": cand["loss5_rate"],
                    "loss8_rate": cand["loss8_rate"],
                    "min_ret_3d": cand["min_ret_3d"],
                    "flip_leading_3d": cand["flip_leading_3d"],
                    "calendar_days": cand["calendar_days"],
                },
            }
        )
        day_sets.append(ds)
        if tier == "strict":
            used_archetypes.add(arch)
    return selected


def _passes_strict_thresholds(
    row: dict[str, Any],
    *,
    mean_min: float = LEAD3_STRICT_MEAN_MIN,
    win_min: float = LEAD3_STRICT_WIN_MIN,
    n_min: int = LEAD3_STRICT_N_MIN,
    jaccard_max: float = JACCARD_MAX,
    reference_day_sets: list[set[str]] | None = None,
    max_loss5_rate: float | None = LOSS5_RATE_MAX,
) -> tuple[bool, list[str]]:
    """Check lane candidate vs pass criteria · return (pass, reasons)."""
    reasons: list[str] = []
    if row["mean_3d"] < mean_min:
        reasons.append(f"mean_3d {row['mean_3d']:.2f}% < {mean_min}%")
    if row["win_3d"] < win_min:
        reasons.append(f"win_3d {row['win_3d']:.1%} < {win_min:.0%}")
    if row["n"] < n_min:
        reasons.append(f"n {row['n']} < {n_min}")
    if max_loss5_rate is not None and row.get("loss5_rate", 0) > max_loss5_rate:
        reasons.append(f"loss5_rate {row['loss5_rate']:.1%} > {max_loss5_rate:.0%}")
    if reference_day_sets:
        j = max_jaccard_vs(row["day_set"], reference_day_sets)
        if j > jaccard_max:
            reasons.append(f"Jaccard {j:.3f} > {jaccard_max}")
    return len(reasons) == 0, reasons


def _passes_coverage_thresholds(
    row: dict[str, Any],
    *,
    mean_min: float = H4_COVERAGE_MEAN_MIN,
    win_min: float = H4_COVERAGE_WIN_MIN,
    n_min: int = H4_COVERAGE_N_MIN,
    jaccard_max: float = H4_COVERAGE_JACCARD_MAX,
    reference_day_sets: list[set[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Coverage tier · relaxed mean/n · higher n · wider Jaccard vs strict union."""
    reasons: list[str] = []
    if row["mean_3d"] < mean_min:
        reasons.append(f"mean_3d {row['mean_3d']:.2f}% < {mean_min}%")
    if row["win_3d"] < win_min:
        reasons.append(f"win_3d {row['win_3d']:.1%} < {win_min:.0%}")
    if row["n"] < n_min:
        reasons.append(f"n {row['n']} < {n_min}")
    if reference_day_sets:
        j = max_jaccard_vs(row["day_set"], reference_day_sets)
        if j > jaccard_max:
            reasons.append(f"Jaccard {j:.3f} > {jaccard_max}")
    return len(reasons) == 0, reasons


def _evaluate_h4_combo(
    precomputed: dict[str, pd.Series],
    panel: pd.DataFrame,
    combo: tuple[str, ...],
    *,
    archetype_tag: str,
    ref_day_sets: list[set[str]],
    used_strict_archetypes: set[str],
    existing_atoms: set[tuple[str, ...]],
) -> dict[str, Any] | None:
    """Targeted H4 combo · strict + coverage pass flags."""
    row = _combo_stats_row(precomputed, panel, combo)
    if row is None:
        return None
    arch = archetype_tag or row["archetype_tag"]
    ok_strict, fail_strict = _passes_strict_thresholds(
        row,
        reference_day_sets=ref_day_sets,
        max_loss5_rate=LOSS5_RATE_MAX,
    )
    ok_cov, fail_cov = _passes_coverage_thresholds(
        row,
        reference_day_sets=ref_day_sets,
    )
    atoms_t = tuple(row["rule_atoms"])
    return {
        **{k: v for k, v in row.items() if k != "day_set"},
        "combo": list(combo),
        "archetype_tag": arch,
        "pass_strict": ok_strict and arch not in used_strict_archetypes,
        "pass_coverage": ok_cov and atoms_t not in existing_atoms,
        "strict_fail_reasons": fail_strict,
        "coverage_fail_reasons": fail_cov,
        "archetype_available": arch not in used_strict_archetypes,
        "already_registered": atoms_t in existing_atoms,
    }


def _sweep_h4_hypotheses(
    panel: pd.DataFrame,
    *,
    strict_defs: list[dict[str, Any]],
    coverage_defs: list[dict[str, Any]],
    ref_day_sets: list[set[str]],
    used_archetypes: set[str],
) -> dict[str, Any]:
    """H4a st7p_coil · H4b high_ret_lift + variants · strict or coverage tier."""
    pre = _precompute_atom_columns(panel, ["lead3", *LEAD3_EXTRA_ATOMS])
    existing_atoms = {
        tuple(ld["rule_atoms"])
        for ld in strict_defs + coverage_defs
    }

    def _eval_family(
        combos: tuple[tuple[str, ...], ...],
        default_arch: str,
        hypothesis_id: str,
        label: str,
    ) -> dict[str, Any]:
        ranked: list[dict[str, Any]] = []
        for combo in combos:
            arch = infer_archetype_tag(combo) if default_arch == "auto" else default_arch
            ev = _evaluate_h4_combo(
                pre, panel, combo,
                archetype_tag=arch,
                ref_day_sets=ref_day_sets,
                used_strict_archetypes=used_archetypes,
                existing_atoms=existing_atoms,
            )
            if ev:
                ranked.append(ev)
        strict_pass = [c for c in ranked if c["pass_strict"] and not c.get("already_registered")]
        cov_pass = [c for c in ranked if c["pass_coverage"] and not c.get("already_registered")]
        best_strict = max(strict_pass, key=lambda c: (c["mean_3d"], c["win_3d"], c["n"]), default=None)
        best_cov = max(cov_pass, key=lambda c: (c["mean_3d"], c["win_3d"], c["n"]), default=None)
        return {
            "hypothesis": label,
            "hypothesis_id": hypothesis_id,
            "n_candidates": len(ranked),
            "n_strict_pass": len(strict_pass),
            "n_coverage_pass": len(cov_pass),
            "best_strict": best_strict,
            "best_coverage": best_cov if best_strict is None else None,
            "candidates": ranked,
        }

    h4a = _eval_family(
        H4A_ST7P_COIL_COMBOS,
        "st7p_coil",
        "H4a",
        "lead3+st7p+disp+flat (+variants)",
    )
    h4b = _eval_family(
        H4B_HIGH_RET_COMBOS,
        "auto",
        "H4b",
        "lead3+disp+hist+q35 (+variants)",
    )
    return {"h4a_st7p_coil": h4a, "h4b_high_ret_lift": h4b}


def sweep_hypothesis_lanes(
    panel: pd.DataFrame,
    strict_defs: list[dict[str, Any]],
    coverage_defs: list[dict[str, Any]] | None = None,
    *,
    mean_min: float = LEAD3_STRICT_MEAN_MIN,
    win_min: float = LEAD3_STRICT_WIN_MIN,
    n_min: int = LEAD3_STRICT_N_MIN,
    jaccard_max: float = JACCARD_MAX,
    max_loss5_rate: float | None = LOSS5_RATE_MAX,
) -> dict[str, Any]:
    """H1a plag · H2a b1 · H3a st7p · H4a/H4b targeted combos."""
    pre = _precompute_atom_columns(panel, ["lead3", *LEAD3_EXTRA_ATOMS])
    coverage_defs = coverage_defs or []
    ref_day_sets = [
        calendar_day_set(panel[lane_mask(panel, ld["rule_atoms"])])
        for ld in strict_defs
        if str(ld.get("tier", "strict")) == "strict"
    ]
    used_archetypes = {
        ld.get("archetype_tag") or infer_archetype_tag(ld["rule_atoms"])
        for ld in strict_defs
        if str(ld.get("tier", "strict")) == "strict"
    }

    # H1a · force plag in combo (relaxed sweep for reporting · strict pass filter below)
    h1a_all = sweep_lead3_combos_3d(
        panel,
        mean_min=-99.0,
        win_min=0.0,
        n_min=3,
        ortho_only=True,
        require_atoms={"plag"},
        max_loss5_rate=None,
    )
    h1a_ranked: list[dict[str, Any]] = []
    for cand in h1a_all:
        arch = cand["archetype_tag"]
        ok, fail = _passes_strict_thresholds(
            cand,
            mean_min=mean_min,
            win_min=win_min,
            n_min=n_min,
            jaccard_max=jaccard_max,
            reference_day_sets=ref_day_sets,
            max_loss5_rate=max_loss5_rate,
        )
        h1a_ranked.append(
            {
                **{k: v for k, v in cand.items() if k != "day_set"},
                "pass": ok,
                "fail_reasons": fail,
                "archetype_available": arch not in used_archetypes,
            }
        )
    h1a_pass = [c for c in h1a_ranked if c["pass"] and c["archetype_available"]]

    # H2a · validate lead3+b1+disp+hist+q35
    h2a_row = _combo_stats_row(pre, panel, H2A_B1_COMBO)
    h2a_result: dict[str, Any] = {"combo": list(H2A_B1_COMBO), "pass": False}
    if h2a_row:
        ok, fail = _passes_strict_thresholds(
            h2a_row,
            mean_min=mean_min,
            win_min=win_min,
            n_min=n_min,
            jaccard_max=jaccard_max,
            reference_day_sets=ref_day_sets,
            max_loss5_rate=max_loss5_rate,
        )
        arch = h2a_row["archetype_tag"]
        h2a_result = {
            **{k: v for k, v in h2a_row.items() if k != "day_set"},
            "combo": list(H2A_B1_COMBO),
            "pass": ok and arch not in used_archetypes,
            "fail_reasons": fail,
            "archetype_tag": arch,
            "archetype_available": arch not in used_archetypes,
            "label": ARCHETYPE_LABELS.get(arch, arch),
        }

    return {
        "h1a_plag_sweep": {
            "hypothesis": "lead3+plag+… ortho combos",
            "n_candidates": len(h1a_ranked),
            "n_pass": len(h1a_pass),
            "top_pass": h1a_pass[:5],
            "top_all": h1a_ranked[:10],
        },
        "h2a_b1_moderate": {
            "hypothesis": "+".join(H2A_B1_COMBO),
            **h2a_result,
        },
        ** _sweep_h3a_late_streak(
            panel,
            ref_day_sets=ref_day_sets,
            used_archetypes=used_archetypes,
            mean_min=mean_min,
            win_min=win_min,
            n_min=n_min,
            jaccard_max=jaccard_max,
            max_loss5_rate=max_loss5_rate,
        ),
        ** _sweep_h4_hypotheses(
            panel,
            strict_defs=strict_defs,
            coverage_defs=coverage_defs,
            ref_day_sets=ref_day_sets,
            used_archetypes=used_archetypes,
        ),
    }


def _sweep_h3a_late_streak(
    panel: pd.DataFrame,
    *,
    ref_day_sets: list[set[str]],
    used_archetypes: set[str],
    mean_min: float = LEAD3_STRICT_MEAN_MIN,
    win_min: float = LEAD3_STRICT_WIN_MIN,
    n_min: int = LEAD3_STRICT_N_MIN,
    jaccard_max: float = JACCARD_MAX,
    max_loss5_rate: float | None = LOSS5_RATE_MAX,
) -> dict[str, Any]:
    """H3a · st7p (lead3_late) ortho sweep vs L01–L03."""
    h3a_all = sweep_lead3_combos_3d(
        panel,
        mean_min=-99.0,
        win_min=0.0,
        n_min=3,
        ortho_only=True,
        require_atoms=H3A_FORCE_ATOMS,
        max_loss5_rate=None,
    )
    h3a_ranked: list[dict[str, Any]] = []
    for cand in h3a_all:
        arch = cand["archetype_tag"]
        ok, fail = _passes_strict_thresholds(
            cand,
            mean_min=mean_min,
            win_min=win_min,
            n_min=n_min,
            jaccard_max=jaccard_max,
            reference_day_sets=ref_day_sets,
            max_loss5_rate=max_loss5_rate,
        )
        h3a_ranked.append(
            {
                **{k: v for k, v in cand.items() if k != "day_set"},
                "pass": ok,
                "fail_reasons": fail,
                "archetype_available": arch not in used_archetypes,
            }
        )
    h3a_pass = [c for c in h3a_ranked if c["pass"] and c["archetype_available"]]
    return {
        "h3a_late_streak": {
            "hypothesis": "lead3+st7p+… ortho combos (lead3_late sub-pool)",
            "n_candidates": len(h3a_ranked),
            "n_pass": len(h3a_pass),
            "top_pass": h3a_pass[:5],
            "top_all": h3a_ranked[:10],
        },
    }


def _backtrack_case_signal_days(
    panel: pd.DataFrame,
    parent_panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    sid: str,
    streak_start: str,
    *,
    lookback_sessions: int = CASE_BACKTRACK_SESSIONS,
) -> dict[str, Any]:
    """Search up to N sessions before streak_start for pre-release / lead3 signal D."""
    arch_by_lane = {
        str(ld["id"]): ld.get("archetype_tag") or infer_archetype_tag(ld["rule_atoms"])
        for ld in lane_defs
    }
    sid_pr = parent_panel[parent_panel["sid"] == sid].sort_values("date")
    window = sid_pr[sid_pr["date"] <= streak_start].tail(lookback_sessions + 1)
    candidates: list[dict[str, Any]] = []
    for _, row in window.iterrows():
        d = str(row["date"])
        in_pr = True
        sub_lead3 = panel[(panel["sid"] == sid) & (panel["date"] == d)]
        in_lead3 = not sub_lead3.empty
        ret_3d = round(float(row["ret_3d"]), 4) if pd.notna(row.get("ret_3d")) else None
        min_ret = round(float(row["min_ret_3d"]), 4) if pd.notna(row.get("min_ret_3d")) else None
        matched: list[str] = []
        matched_arch: list[str] = []
        if in_lead3 and len(sub_lead3):
            idx = sub_lead3.index[0]
            matched = [
                str(ld["id"])
                for ld in lane_defs
                if bool(lane_mask(panel, ld["rule_atoms"]).loc[idx])
            ]
            matched_arch = [arch_by_lane.get(m, "other") for m in matched]
        candidates.append(
            {
                "signal_date": d,
                "in_pre_release_pool": in_pr,
                "in_lead3_pool": in_lead3,
                "ret_3d": ret_3d,
                "min_ret_3d": min_ret,
                "tail_loss5": min_ret is not None and min_ret <= LOSS5_PCT,
                "matched_lanes": matched,
                "matched_archetypes": matched_arch,
                "n_lanes": len(matched),
            }
        )

    lead3_cands = [c for c in candidates if c["in_lead3_pool"]]
    hit_cands = [c for c in lead3_cands if c["n_lanes"]]
    pool = hit_cands or lead3_cands or candidates

    def _score(c: dict[str, Any]) -> tuple[int, float, str]:
        ret = c["ret_3d"] if c["ret_3d"] is not None else -999.0
        return (c["n_lanes"], ret, c["signal_date"])

    best = max(pool, key=_score) if pool else None
    any_tail = any(c.get("tail_loss5") for c in candidates)
    return {
        "lookback_sessions": lookback_sessions,
        "candidates": candidates,
        "best": best,
        "any_tail_loss5": any_tail,
    }


def analyze_case_lane_feedback(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    conn: Any | None = None,
    *,
    parent_panel: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """June Leading case table · lane hit rate · archetype coverage · missed archetypes."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    pr_panel = parent_panel if parent_panel is not None else panel
    if parent_panel is None and "flip_leading_3d" not in pr_panel.columns:
        pr_panel, _ = build_pre_release_funnel_panel_3d(conn)

    base = analyze_june2026_leading_streak_cases(pr_panel, lane_defs, conn=conn)
    cases = base["june2026_leading_streak_cases"]
    summary = dict(base["summary"])

    arch_by_lane = {
        str(ld["id"]): ld.get("archetype_tag") or infer_archetype_tag(ld["rule_atoms"])
        for ld in lane_defs
    }
    arch_hits: dict[str, int] = {}
    bt_arch_hits: dict[str, int] = {}
    per_case: list[dict[str, Any]] = []
    for c in cases:
        sid, sig_d = str(c["sid"]), str(c.get("signal_date", c.get("streak_start", "")))
        streak_start = str(c.get("streak_start", sig_d))
        in_lead3 = not panel[
            (panel["sid"] == sid) & (panel["date"] == sig_d)
        ].empty
        matched: list[str] = []
        if in_lead3:
            sub = panel[(panel["sid"] == sid) & (panel["date"] == sig_d)]
            if len(sub):
                idx = sub.index[0]
                matched = [
                    str(ld["id"])
                    for ld in lane_defs
                    if bool(lane_mask(panel, ld["rule_atoms"]).loc[idx])
                ]
        matched_arch = [arch_by_lane.get(m, "other") for m in matched]
        for a in matched_arch:
            arch_hits[a] = arch_hits.get(a, 0) + 1

        backtrack = _backtrack_case_signal_days(
            panel, pr_panel, lane_defs, sid, streak_start,
        )
        best_bt = backtrack.get("best")
        bt_matched = best_bt.get("matched_lanes", []) if best_bt else []
        bt_arch = best_bt.get("matched_archetypes", []) if best_bt else []
        for a in bt_arch:
            bt_arch_hits[a] = bt_arch_hits.get(a, 0) + 1

        per_case.append(
            {
                **c,
                "in_lead3_pool": in_lead3,
                "matched_lanes": matched,
                "matched_archetypes": matched_arch,
                "backtrack": backtrack,
                "backtrack_best_date": best_bt.get("signal_date") if best_bt else None,
                "backtrack_ret_3d": best_bt.get("ret_3d") if best_bt else None,
                "backtrack_matched_lanes": bt_matched,
                "backtrack_matched_archetypes": bt_arch,
                "backtrack_hit": bool(bt_matched),
                "tail_loss5_flag": backtrack.get("any_tail_loss5", False),
            }
        )

    in_pool_cases = [c for c in per_case if c.get("in_pre_release_pool")]
    in_lead3_cases = [c for c in per_case if c.get("in_lead3_pool")]
    hit_cases = [c for c in in_lead3_cases if c.get("matched_lanes")]
    bt_hit_cases = [c for c in per_case if c.get("backtrack_hit")]
    in_lead3_bt = [c for c in per_case if c.get("backtrack", {}).get("best", {}) and c["backtrack"]["best"].get("in_lead3_pool")]
    missed = [c for c in in_lead3_cases if not c.get("matched_lanes")]
    tail_flagged = [c for c in per_case if c.get("tail_loss5_flag")]

    all_archetypes = set(ARCHETYPE_LABELS) | set(arch_by_lane.values())
    covered_archetypes = set(arch_hits)
    missed_archetypes = sorted(all_archetypes - covered_archetypes - {"other"})

    summary.update(
        {
            "n_in_lead3_pool": len(in_lead3_cases),
            "n_hit": len(hit_cases),
            "hit_rate": round(len(hit_cases) / len(in_lead3_cases), 4) if in_lead3_cases else 0.0,
            "n_backtrack_hit": len(bt_hit_cases),
            "backtrack_hit_rate": round(len(bt_hit_cases) / len(in_pool_cases), 4) if in_pool_cases else 0.0,
            "n_backtrack_lead3": len(in_lead3_bt),
            "backtrack_hit_rate_lead3": round(len(bt_hit_cases) / len(in_lead3_bt), 4) if in_lead3_bt else 0.0,
            "hit_rate_vs_pr_pool": round(len(hit_cases) / len(in_pool_cases), 4) if in_pool_cases else 0.0,
            "archetype_hits": arch_hits,
            "backtrack_archetype_hits": bt_arch_hits,
            "missed_archetypes": missed_archetypes,
            "n_missed_cases": len(missed),
            "n_tail_loss5_flagged": len(tail_flagged),
        }
    )
    return {
        "cases": per_case,
        "missed_cases": missed,
        "summary": summary,
    }


def mine_case_signal_facts(
    panel: pd.DataFrame,
    case_feedback: dict[str, Any],
    *,
    min_streak_pct: float = 20.0,
    min_ret_3d: float = HIGH_RET_3D_PCT,
) -> dict[str, Any]:
    """Signal-day atom profiles: June high-streak cases + lead3 ret_3d≥15% events."""
    pre = _precompute_atom_columns(panel, ["lead3", *LEAD3_EXTRA_ATOMS])
    atoms_to_check = [a for a in LEAD3_EXTRA_ATOMS if a in pre]
    base_n = len(panel)
    base_rates = {
        a: float(pre[a].sum()) / base_n if base_n else 0.0
        for a in atoms_to_check
    }

    high_cases = [
        c
        for c in case_feedback.get("cases", [])
        if c.get("in_lead3_pool")
        and float(c.get("streak_total_pct", 0)) >= min_streak_pct
        and c.get("signal_date")
    ]
    atom_hits: dict[str, int] = {a: 0 for a in atoms_to_check}
    rows: list[dict[str, Any]] = []
    for c in high_cases:
        sid, d = str(c["sid"]), str(c["signal_date"])
        sub = panel[(panel["sid"] == sid) & (panel["date"] == d)]
        if sub.empty:
            continue
        idx = sub.index[0]
        hit_atoms = []
        for a in atoms_to_check:
            if bool(pre[a].loc[idx]):
                atom_hits[a] += 1
                hit_atoms.append(a)
        rows.append(
            {
                "sid": sid,
                "name": c.get("name"),
                "signal_date": d,
                "streak_total_pct": c.get("streak_total_pct"),
                "ret_3d": c.get("ret_3d"),
                "signal_atoms": hit_atoms,
            }
        )

    n = len(rows)
    atom_rates = {
        a: round(atom_hits[a] / n, 4) if n else 0.0
        for a in atoms_to_check
    }
    top_atoms = sorted(atom_rates.items(), key=lambda x: -x[1])[:8]

    # Pool scan: ret_3d ≥ min_ret_3d within lead3
    high_ret = panel[panel["ret_3d"] >= min_ret_3d].copy()
    hr_hits: dict[str, int] = {a: 0 for a in atoms_to_check}
    hr_rows: list[dict[str, Any]] = []
    for idx in high_ret.index:
        hit_atoms = [a for a in atoms_to_check if bool(pre[a].loc[idx])]
        for a in hit_atoms:
            hr_hits[a] += 1
        hr_rows.append(
            {
                "sid": str(high_ret.at[idx, "sid"]),
                "date": str(high_ret.at[idx, "date"]),
                "ret_3d": round(float(high_ret.at[idx, "ret_3d"]), 2),
                "signal_atoms": hit_atoms,
            }
        )
    hn = len(high_ret)
    hr_rates = {
        a: round(hr_hits[a] / hn, 4) if hn else 0.0
        for a in atoms_to_check
    }
    hr_lift = {
        a: round(hr_rates[a] - base_rates.get(a, 0.0), 4)
        for a in atoms_to_check
    }
    hr_top = sorted(hr_lift.items(), key=lambda x: -x[1])[:10]
    hr_top_atoms = [{"atom": a, "rate": hr_rates[a], "lift_vs_pool": lift} for a, lift in hr_top if lift > 0]

    suggested: list[dict[str, Any]] = []
    top3 = [a for a, _ in hr_top[:3] if hr_lift[a] > 0]
    if n >= 2 and atom_rates.get("st7p", 0) >= 0.5 and atom_rates.get("disp", 0) >= 0.5:
        suggested.append(
            {
                "combo": ["lead3", "st7p", "disp", "flat"],
                "archetype_tag": "st7p_coil",
                "rationale": (
                    f"June high-streak cases: st7p/disp/flat hit "
                    f"{atom_rates.get('st7p', 0):.0%}/{atom_rates.get('disp', 0):.0%}/"
                    f"{atom_rates.get('flat', 0):.0%}"
                ),
            }
        )
    if "st7p" in hr_lift and hr_lift["st7p"] > 0.05:
        suggested.append(
            {
                "combo": ["lead3", "st7p", "disp", "hist"],
                "archetype_tag": "late_streak",
                "rationale": f"st7p lift +{hr_lift['st7p']:.1%} vs pool on ret≥{min_ret_3d}% events",
            }
        )
    if len(top3) >= 2:
        combo = ["lead3", *sorted(set(top3) - {"lead3"})][:5]
        suggested.append(
            {
                "combo": combo,
                "archetype_tag": infer_archetype_tag(combo),
                "rationale": f"top lift atoms on ret≥{min_ret_3d}%: {', '.join(top3[:3])}",
            }
        )

    # dedupe by combo tuple · keep first 2
    seen_combos: set[tuple[str, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for s in suggested:
        key = tuple(s["combo"])
        if key in seen_combos:
            continue
        seen_combos.add(key)
        deduped.append(s)

    return {
        "min_streak_pct": min_streak_pct,
        "n_cases_analyzed": n,
        "atom_hit_rates": atom_rates,
        "top_atoms": [{"atom": a, "rate": r} for a, r in top_atoms],
        "case_rows": rows,
        "high_ret_scan": {
            "min_ret_3d": min_ret_3d,
            "n_events": hn,
            "pool_baseline_atom_rates": {a: round(base_rates[a], 4) for a in atoms_to_check},
            "atom_hit_rates": hr_rates,
            "atom_lift_vs_pool": hr_lift,
            "top_atoms": hr_top_atoms,
            "sample_events": hr_rows[:15],
        },
        "suggested_hypotheses": deduped[:2],
    }


def _ensure_lane_metadata(ld: dict[str, Any]) -> dict[str, Any]:
    out = dict(ld)
    out.setdefault("archetype_tag", infer_archetype_tag(ld["rule_atoms"]))
    return out


def _append_hypothesis_lanes(
    strict_defs: list[dict[str, Any]],
    coverage_defs: list[dict[str, Any]],
    hypothesis: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Register passing H1a–H4 lanes · return (strict, coverage, newly_added)."""
    defs = [_ensure_lane_metadata(ld) for ld in strict_defs]
    cov_defs = [_ensure_lane_metadata(ld) for ld in coverage_defs]
    existing_atoms = {tuple(ld["rule_atoms"]) for ld in defs + cov_defs}
    existing_arch = {ld["archetype_tag"] for ld in defs if ld.get("tier", "strict") == "strict"}
    added: list[dict[str, Any]] = []

    def _next_strict_id() -> str:
        nums = [int(ld["id"][1:]) for ld in defs if str(ld["id"]).startswith("L") and ld["id"][1:].isdigit()]
        return f"L{max(nums or [0]) + 1:02d}"

    def _next_coverage_id() -> str:
        nums = [
            int(ld["id"][2:])
            for ld in cov_defs
            if str(ld["id"]).startswith("LC") and ld["id"][2:].isdigit()
        ]
        return f"LC{max(nums or [0]) + 1:02d}"

    def _register_strict(cand: dict[str, Any], tag: str) -> None:
        nonlocal defs, existing_atoms, existing_arch
        atoms_t = tuple(cand["rule_atoms"])
        if atoms_t in existing_atoms:
            return
        arch = cand.get("archetype_tag", "other")
        if arch in existing_arch:
            return
        lane_id = _next_strict_id()
        atoms = list(cand["rule_atoms"])
        new_ld = {
            "id": lane_id,
            "tier": "strict",
            "rule_atoms": atoms,
            "label": "+".join(atoms),
            "description": f"{tag} · {ARCHETYPE_LABELS.get(arch, arch)}",
            "regime_tag": _infer_regime_tag(atoms),
            "archetype_tag": arch,
        }
        defs.append(new_ld)
        added.append(new_ld)
        existing_atoms.add(atoms_t)
        existing_arch.add(arch)

    def _register_coverage(cand: dict[str, Any], tag: str) -> None:
        nonlocal cov_defs, existing_atoms
        atoms_t = tuple(cand["rule_atoms"])
        if atoms_t in existing_atoms:
            return
        arch = cand.get("archetype_tag", "other")
        lane_id = _next_coverage_id()
        atoms = list(cand["rule_atoms"])
        new_ld = {
            "id": lane_id,
            "tier": "coverage",
            "rule_atoms": atoms,
            "label": "+".join(atoms),
            "description": f"{tag} · {ARCHETYPE_LABELS.get(arch, arch)}",
            "regime_tag": _infer_regime_tag(atoms),
            "archetype_tag": arch,
        }
        cov_defs.append(new_ld)
        added.append(new_ld)
        existing_atoms.add(atoms_t)

    def _register_h4_family(h4: dict[str, Any], tag: str) -> None:
        best = h4.get("best_strict")
        if best:
            _register_strict(best, tag)
            return
        best_cov = h4.get("best_coverage")
        if best_cov:
            _register_coverage(best_cov, tag)

    h2a = hypothesis.get("h2a_b1_moderate", {})
    if h2a.get("pass") and tuple(h2a.get("combo", [])) not in existing_atoms:
        arch = h2a.get("archetype_tag", "b1_moderate")
        if arch not in existing_arch:
            lane_id = _next_strict_id()
            atoms = list(h2a["combo"])
            new_ld = {
                "id": lane_id,
                "tier": "strict",
                "rule_atoms": atoms,
                "label": "+".join(atoms),
                "description": f"H2a · {ARCHETYPE_LABELS.get(arch, arch)}",
                "regime_tag": _infer_regime_tag(atoms),
                "archetype_tag": arch,
            }
            defs.append(new_ld)
            added.append(new_ld)
            existing_atoms.add(tuple(atoms))
            existing_arch.add(arch)

    for cand in hypothesis.get("h1a_plag_sweep", {}).get("top_pass", []):
        atoms_t = tuple(cand["rule_atoms"])
        if atoms_t in existing_atoms:
            continue
        arch = cand.get("archetype_tag", "plag_flip")
        if arch in existing_arch:
            continue
        lane_id = _next_strict_id()
        atoms = list(cand["rule_atoms"])
        new_ld = {
            "id": lane_id,
            "tier": "strict",
            "rule_atoms": atoms,
            "label": "+".join(atoms),
            "description": f"H1a · {ARCHETYPE_LABELS.get(arch, arch)}",
            "regime_tag": _infer_regime_tag(atoms),
            "archetype_tag": arch,
        }
        defs.append(new_ld)
        added.append(new_ld)
        existing_atoms.add(atoms_t)
        existing_arch.add(arch)
        break  # at most one new plag lane per run

    for cand in hypothesis.get("h3a_late_streak", {}).get("top_pass", []):
        atoms_t = tuple(cand["rule_atoms"])
        if atoms_t in existing_atoms:
            continue
        arch = cand.get("archetype_tag", "late_streak")
        if arch in existing_arch:
            continue
        lane_id = _next_strict_id()
        atoms = list(cand["rule_atoms"])
        new_ld = {
            "id": lane_id,
            "tier": "strict",
            "rule_atoms": atoms,
            "label": "+".join(atoms),
            "description": f"H3a · {ARCHETYPE_LABELS.get(arch, arch)}",
            "regime_tag": _infer_regime_tag(atoms),
            "archetype_tag": arch,
        }
        defs.append(new_ld)
        added.append(new_ld)
        existing_atoms.add(atoms_t)
        existing_arch.add(arch)
        break  # at most one new late-streak lane per run

    _register_h4_family(hypothesis.get("h4a_st7p_coil", {}), "H4a")
    _register_h4_family(hypothesis.get("h4b_high_ret_lift", {}), "H4b")

    return defs, cov_defs, added


def evaluate_lead3_lanes_3d(
    panel: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    run_discovery: bool = True,
    conn: Any | None = None,
) -> dict[str, Any]:
    strict_defs = [_ensure_lane_metadata(ld) for ld in list(cfg.get("lanes", {}).get("strict", []))]
    coverage_defs = [_ensure_lane_metadata(ld) for ld in list(cfg.get("lanes", {}).get("coverage", []))]

    if run_discovery and cfg.get("discovery", {}).get("enabled", True):
        disc = cfg.get("discovery", {})
        if not strict_defs or disc.get("rediscover_strict", False):
            strict_defs = discover_lead3_lanes_3d(
                panel,
                tier="strict",
                max_lanes=int(disc.get("max_strict", 15)),
                win_min=float(disc.get("strict_win_min", LEAD3_STRICT_WIN_MIN)),
                mean_min=float(disc.get("strict_mean_min", LEAD3_STRICT_MEAN_MIN)),
                n_min=int(disc.get("strict_n_min", LEAD3_STRICT_N_MIN)),
                jaccard_max=float(disc.get("jaccard_max", JACCARD_MAX)),
                max_loss5_rate=float(disc.get("max_loss5_rate", LOSS5_RATE_MAX)),
                ortho_only=bool(disc.get("ortho_only", True)),
            )
        strict_day_sets = []
        for ld in strict_defs:
            sub = panel[lane_mask(panel, ld["rule_atoms"])]
            strict_day_sets.append(calendar_day_set(sub))
        discovered_cov = discover_lead3_lanes_3d(
            panel,
            tier="coverage",
            max_lanes=int(disc.get("max_coverage", 12)),
            win_min=float(disc.get("coverage_win_min", LEAD3_COVERAGE_WIN_MIN)),
            mean_min=float(disc.get("coverage_mean_min", LEAD3_COVERAGE_MEAN_MIN)),
            mean_max=float(disc.get("coverage_mean_max", LEAD3_COVERAGE_MEAN_MAX)),
            n_min=int(disc.get("coverage_n_min", LEAD3_COVERAGE_N_MIN)),
            jaccard_max=float(disc.get("jaccard_max_coverage", JACCARD_MAX_COVERAGE)),
            existing_day_sets=strict_day_sets,
            existing_archetypes={
                ld.get("archetype_tag") or infer_archetype_tag(ld["rule_atoms"])
                for ld in strict_defs
            },
            ortho_only=bool(disc.get("coverage_ortho_only", False)),
        )
        if discovered_cov:
            coverage_defs = discovered_cov

    hypothesis = sweep_hypothesis_lanes(panel, strict_defs, coverage_defs)
    if cfg.get("hypothesis", {}).get("register_passing", True):
        strict_defs, coverage_defs, hypothesis_added = _append_hypothesis_lanes(
            strict_defs, coverage_defs, hypothesis,
        )
    else:
        hypothesis_added = []

    all_defs = strict_defs + coverage_defs
    lane_stats = [_lane_stats_with_loss(panel, ld) for ld in all_defs]

    flip_rates: dict[str, float] = {}
    for ld in all_defs:
        sub = panel[lane_mask(panel, ld["rule_atoms"])]
        flip_rates[str(ld["id"])] = round(float(sub["flip_leading_3d"].mean()), 4) if len(sub) else 0.0

    factor_lift = analyze_lead3_factor_lift(panel)
    factor_phi = analyze_lead3_factor_phi(panel)[:15]
    sweep_hits = sweep_lead3_combos_3d(panel)[:20]
    pr_panel, _ = build_pre_release_funnel_panel_3d(conn)
    case_feedback = analyze_case_lane_feedback(
        panel, all_defs, conn=conn, parent_panel=pr_panel,
    )
    case_facts = mine_case_signal_facts(panel, case_feedback)

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
            "sub_pool": "lead3",
        },
        "pool_baseline": _lead3_baseline(panel),
        "strict_lanes": [s for s in lane_stats if s["tier"] == "strict"],
        "coverage_lanes": [s for s in lane_stats if s["tier"] == "coverage"],
        "flip_leading_3d_by_lane": flip_rates,
        "union_strict": asdict(_union_stats_3d(panel, strict_defs, tier="strict")),
        "union_all": asdict(_union_stats_3d(panel, all_defs, tier="strict+coverage")),
        "jaccard_matrix": _jaccard_matrix(panel, all_defs),
        "strict_defs": strict_defs,
        "coverage_defs": coverage_defs,
        "hypothesis": hypothesis,
        "hypothesis_added_lanes": hypothesis_added,
        "case_feedback": case_feedback,
        "case_signal_facts": case_facts,
        "factor_lift": factor_lift,
        "factor_phi_top": factor_phi,
        "sweep_top_combos": [{k: v for k, v in r.items() if k != "day_set"} for r in sweep_hits],
        "pass_threshold": {
            "mean_3d_min": LEAD3_STRICT_MEAN_MIN,
            "win_3d_min": LEAD3_STRICT_WIN_MIN,
            "n_min": LEAD3_STRICT_N_MIN,
            "jaccard_max": JACCARD_MAX,
            "loss5_rate_max": LOSS5_RATE_MAX,
        },
    }


def render_lead3_funnel_markdown(result: dict[str, Any], cfg: dict[str, Any]) -> str:
    meta = result["meta"]
    base = result["pool_baseline"]
    lines = [
        "# RRG lead3 funnel lanes · 3-day hold backtest",
        "",
        "> **Sub-pool lead3（PIT）**: `improving` ∧ `RV∈[98,100)` ∧ `improve_streak≥4` · pre-release 母池交集",
        f"> **Outcome**: D→D+3 `ret_3d` · 輔助 `flip_leading_3d`（D+3 象限=Leading · 非進場條件）",
        "",
        f"- Pool: **{meta['pool_n']}** lead3 stock-days · {meta['date_start']} → {meta['date_end']}",
        f"- Span: **{meta['years_span']:.1f}** years · Generated: {meta['generated_on']}",
        "",
        "## A. Executive summary",
        "",
        f"| Layer | win_3d | mean_3d | flip→Leading@D+3 | n |",
        f"|-------|--------|---------|------------------|---|",
        f"| **lead3 母池** | {base['win_3d']:.1%} | {base['mean_3d']:+.2f}% | "
        f"{base['flip_leading_3d_rate']:.1%} | {base['n']} |",
    ]
    us = result["union_strict"]
    ua = result["union_all"]
    lines.append(
        f"| **Strict union** | {us['day_win_3d']:.1%} | {us['day_mean_3d']:+.2f}% | — | "
        f"{us['n_calendar_days']} days |"
    )
    lines.append(
        f"| **Strict+coverage union** | {ua['day_win_3d']:.1%} | {ua['day_mean_3d']:+.2f}% | — | "
        f"{ua['n_calendar_days']} days |"
    )
    target_lo, target_hi = TARGET_UNION_DAYS_PER_YEAR
    hit = target_lo <= ua["days_per_year"] <= target_hi
    lines.extend(
        [
            "",
            f"Union calendar coverage: **{ua['days_per_year']:.1f}** days/yr "
            f"(target {target_lo:.0f}–{target_hi:.0f}) · {'✅' if hit else '⚠️ 未達標'}",
            "",
            "> 均報酬為 **3 日累積 %** · **過關門檻 mean_3d ≥ +3.0%** · 正交維度每條 lane 每維 ≤1 atom。",
            "",
            "## B. Factor lift · 單因子 vs lead3 母池",
            "",
            "| 維度 | atom | n | win_3d | mean_3d | lift |",
            "|------|------|---|--------|---------|------|",
        ]
    )
    for r in result.get("factor_lift", [])[:12]:
        lines.append(
            f"| {r['dimension']} | {r['atom']} | {r['n']} | {r['win_3d']:.1%} | "
            f"{r['mean_3d']:+.2f}% | {r['lift_mean']:+.2f}% |"
        )
    lines.extend(
        [
            "",
            "## B2. Factor phi · 高相關對（避免同維堆疊）",
            "",
            "| atom_a | atom_b | phi | 同維 |",
            "|--------|--------|-----|------|",
        ]
    )
    for r in result.get("factor_phi_top", [])[:10]:
        flag = "⚠️" if r["same_dimension"] and abs(r["phi"]) > 0.25 else ""
        lines.append(
            f"| {r['atom_a']} | {r['atom_b']} | {r['phi']:+.3f} | {flag} {r['dim_a']}/{r['dim_b']} |"
        )
    lines.extend(
        [
            "",
            "## Summary · strict tier (mean_3d ≥ 3%)",
            "",
            "| ID | archetype | rule | n | days | d/yr | win_3d | mean_3d | flip@D+3 | P(≤−5%) | P(≤−8%) | min_ret | 白話 |",
            "|----|-----------|------|---|------|------|--------|---------|----------|--------|--------|---------|------|",
        ]
    )
    flip_map = result.get("flip_leading_3d_by_lane", {})
    strict_cfg = {ld["id"]: ld for ld in cfg.get("lanes", {}).get("strict", [])}
    for s in result["strict_lanes"]:
        atoms = "+".join(s["rule_atoms"])
        desc = strict_cfg.get(s["lane_id"], {}).get("description", "")[:28]
        flip = flip_map.get(s["lane_id"], 0.0)
        arch = s.get("archetype_tag", "")
        lines.append(
            f"| {s['lane_id']} | {arch} | {atoms} | {s['n_events']} | {s['n_calendar_days']} | "
            f"{s['days_per_year']:.1f} | {s['win_3d']:.1%} | {s['mean_3d']:+.2f}% | "
            f"{flip:.1%} | {s['loss5_rate']:.1%} | {s['loss8_rate']:.1%} | "
            f"{s.get('min_ret_3d', 0):+.1f}% | {desc} |"
        )

    lines.extend(
        [
            "",
            f"**Strict union**: {us['n_calendar_days']} calendar days · "
            f"{us['days_per_year']:.1f}/yr · day win **{us['day_win_3d']:.1%}** · "
            f"day mean **{us['day_mean_3d']:+.3f}%**",
            "",
            "## Summary · coverage tier",
            "",
            "| ID | archetype | rule | n | days | d/yr | win_3d | mean_3d | flip@D+3 | P(≤−5%) | min_ret |",
            "|----|-----------|------|---|------|------|--------|---------|----------|--------|---------|",
        ]
    )
    for s in result["coverage_lanes"]:
        atoms = "+".join(s["rule_atoms"])
        flip = flip_map.get(s["lane_id"], 0.0)
        arch = s.get("archetype_tag", "")
        lines.append(
            f"| {s['lane_id']} | {arch} | {atoms} | {s['n_events']} | {s['n_calendar_days']} | "
            f"{s['days_per_year']:.1f} | {s['win_3d']:.1%} | {s['mean_3d']:+.2f}% | "
            f"{flip:.1%} | {s['loss5_rate']:.1%} | {s.get('min_ret_3d', 0):+.1f}% |"
        )

    lines.extend(
        [
            "",
            f"**Strict + coverage union**: {ua['n_calendar_days']} calendar days · "
            f"{ua['days_per_year']:.1f}/yr · day win **{ua['day_win_3d']:.1%}** · "
            f"day mean **{ua['day_mean_3d']:+.3f}%**",
            "",
        ]
    )

    for tier_key, title in [("strict_lanes", "Strict"), ("coverage_lanes", "Coverage")]:
        lines.append(f"## Year-by-year · {title}")
        lines.append("")
        for s in result[tier_key]:
            flip = flip_map.get(s["lane_id"], 0.0)
            lines.append(f"### {s['lane_id']} · {s['label']} · flip@D+3 {flip:.1%}")
            lines.append("")
            lines.append("| Year | n | days | win_3d | mean_3d | day_win | day_mean |")
            lines.append("|------|---|------|--------|---------|---------|----------|")
            for yr in sorted(s["year_table"]):
                y = s["year_table"][yr]
                lines.append(
                    f"| {yr} | {y['n']} | {y['days']} | {y['win_3d']:.1%} | "
                    f"{y['mean_3d']:+.2f}% | {y['day_win_3d']:.1%} | {y['day_mean_3d']:+.2f}% |"
                )
            lines.append("")

    hyp = result.get("hypothesis", {})
    h1a = hyp.get("h1a_plag_sweep", {})
    h2a = hyp.get("h2a_b1_moderate", {})
    lines.extend(
        [
            "",
            "## E. Hypothesis sweeps",
            "",
            f"### H1a · lead3+plag+… ({h1a.get('n_pass', 0)} pass / {h1a.get('n_candidates', 0)} candidates)",
            "",
            "| pass | rule | n | win_3d | mean_3d | loss5 | archetype |",
            "|------|------|---|--------|---------|-------|-----------|",
        ]
    )
    for r in h1a.get("top_all", [])[:8]:
        flag = "✅" if r.get("pass") and r.get("archetype_available") else "—"
        atoms = "+".join(r.get("rule_atoms", []))
        lines.append(
            f"| {flag} | {atoms} | {r.get('n')} | {r.get('win_3d', 0):.1%} | "
            f"{r.get('mean_3d', 0):+.2f}% | {r.get('loss5_rate', 0):.1%} | {r.get('archetype_tag', '')} |"
        )
    h2a_pass = "✅ PASS" if h2a.get("pass") else "❌ FAIL"
    lines.extend(
        [
            "",
            f"### H2a · moderate bench (`{'+'.join(H2A_B1_COMBO)}`) · {h2a_pass}",
            "",
            f"- n={h2a.get('n', '—')} · win_3d={h2a.get('win_3d', 0):.1%} · "
            f"mean_3d={h2a.get('mean_3d', 0):+.2f}% · loss5={h2a.get('loss5_rate', 0):.1%} · "
            f"min_ret={h2a.get('min_ret_3d', 0):+.1f}%",
        ]
    )
    if h2a.get("fail_reasons"):
        lines.append(f"- Fail: {', '.join(h2a['fail_reasons'])}")
    h3a = hyp.get("h3a_late_streak", {})
    lines.extend(
        [
            "",
            f"### H3a · lead3+st7p+… ({h3a.get('n_pass', 0)} pass / {h3a.get('n_candidates', 0)} candidates)",
            "",
            "| pass | rule | n | win_3d | mean_3d | loss5 | archetype |",
            "|------|------|---|--------|---------|-------|-----------|",
        ]
    )
    for r in h3a.get("top_all", [])[:8]:
        flag = "✅" if r.get("pass") and r.get("archetype_available") else "—"
        atoms = "+".join(r.get("rule_atoms", []))
        lines.append(
            f"| {flag} | {atoms} | {r.get('n')} | {r.get('win_3d', 0):.1%} | "
            f"{r.get('mean_3d', 0):+.2f}% | {r.get('loss5_rate', 0):.1%} | {r.get('archetype_tag', '')} |"
        )

    def _h4_section(h4: dict[str, Any], title: str) -> list[str]:
        out: list[str] = [
            "",
            f"### {title} ({h4.get('n_strict_pass', 0)} strict / "
            f"{h4.get('n_coverage_pass', 0)} coverage / {h4.get('n_candidates', 0)} candidates)",
            "",
            "| tier | rule | n | win_3d | mean_3d | loss5 | archetype |",
            "|------|------|---|--------|---------|-------|-----------|",
        ]
        for r in h4.get("candidates", []):
            if r.get("pass_strict"):
                tier = "strict ✅"
            elif r.get("pass_coverage"):
                tier = "coverage ✅"
            else:
                tier = "—"
            atoms = "+".join(r.get("rule_atoms", r.get("combo", [])))
            out.append(
                f"| {tier} | {atoms} | {r.get('n')} | {r.get('win_3d', 0):.1%} | "
                f"{r.get('mean_3d', 0):+.2f}% | {r.get('loss5_rate', 0):.1%} | "
                f"{r.get('archetype_tag', '')} |"
            )
        best_s = h4.get("best_strict")
        best_c = h4.get("best_coverage")
        if best_s:
            atoms = "+".join(best_s.get("rule_atoms", []))
            out.append(
                f"\n**Best strict:** `{atoms}` · mean={best_s.get('mean_3d', 0):+.2f}% · "
                f"win={best_s.get('win_3d', 0):.1%} · n={best_s.get('n')}"
            )
        elif best_c:
            atoms = "+".join(best_c.get("rule_atoms", []))
            out.append(
                f"\n**Best coverage:** `{atoms}` · mean={best_c.get('mean_3d', 0):+.2f}% · "
                f"win={best_c.get('win_3d', 0):.1%} · n={best_c.get('n')}"
            )
        else:
            out.append("\n**No pass** (strict or coverage)")
        return out

    h4a = hyp.get("h4a_st7p_coil", {})
    h4b = hyp.get("h4b_high_ret_lift", {})
    lines.extend(_h4_section(h4a, "H4a · st7p_coil"))
    lines.extend(_h4_section(h4b, "H4b · high_ret_lift"))

    added = result.get("hypothesis_added_lanes", [])
    if added:
        lines.append("")
        lines.append("**Registered this run:**")
        for ld in added:
            lines.append(f"- `{ld['id']}` {ld['label']} · archetype `{ld.get('archetype_tag')}`")

    cf = result.get("case_feedback", {}).get("summary", {})
    lines.extend(
        [
            "",
            "## F. June Leading case feedback",
            "",
            f"- Cases: **{cf.get('n_cases', 0)}** · pre-release pool: **{cf.get('n_in_pre_release_pool', 0)}** · lead3 pool: **{cf.get('n_in_lead3_pool', 0)}**",
            f"- Lane hit (lead3): **{cf.get('n_hit', 0)}** / {cf.get('n_in_lead3_pool', 0)} "
            f"({cf.get('hit_rate', 0):.1%})",
            f"- Backtrack hit (≤{CASE_BACKTRACK_SESSIONS}d): **{cf.get('n_backtrack_hit', 0)}** / "
            f"{cf.get('n_in_pre_release_pool', 0)} ({cf.get('backtrack_hit_rate', 0):.1%})",
            f"- Tail loss5 flagged (min_ret_3d≤−5% in window): **{cf.get('n_tail_loss5_flagged', 0)}**",
            f"- Missed archetypes (next hypothesis): **{', '.join(cf.get('missed_archetypes', [])) or '—'}**",
            f"- Archetype hits: {cf.get('archetype_hits', {})}",
            f"- Backtrack archetype hits: {cf.get('backtrack_archetype_hits', {})}",
            "",
        ]
    )
    facts = result.get("case_signal_facts", {})
    if facts.get("top_atoms"):
        lines.append(f"### Case fact mining (streak≥{facts.get('min_streak_pct', 20)}% · n={facts.get('n_cases_analyzed', 0)})")
        lines.append("")
        lines.append("| atom | hit rate |")
        lines.append("|------|----------|")
        for t in facts["top_atoms"]:
            lines.append(f"| {t['atom']} | {t['rate']:.1%} |")
        lines.append("")
    hr = facts.get("high_ret_scan", {})
    if hr.get("n_events"):
        lines.append(f"### High-ret scan (ret_3d≥{hr.get('min_ret_3d', 15)}% · n={hr.get('n_events', 0)})")
        lines.append("")
        lines.append("| atom | hit rate | lift vs pool |")
        lines.append("|------|----------|--------------|")
        for t in hr.get("top_atoms", [])[:8]:
            lines.append(
                f"| {t['atom']} | {t['rate']:.1%} | {t.get('lift_vs_pool', 0):+.1%} |"
            )
        lines.append("")
    if facts.get("suggested_hypotheses"):
        lines.append("**Suggested combos (from mining):**")
        for s in facts["suggested_hypotheses"]:
            lines.append(f"- `{'+'.join(s['combo'])}` · `{s['archetype_tag']}` — {s['rationale']}")
        lines.append("")

    lines.extend(
        [
            "",
            "## C. Hypotheses（mean_3d ≥ 3% sweep 後）",
            "",
            "1. **單因子 lift 不足**：lead3 母池 mean 僅 +0.2% · 必須 **多維 AND** 才過 +3%（b2 lift +0.57% 為最高單因子）。",
            "2. **正交微策略核心配方**：`b2`（台指）× `disp↓`（軌跡）× `hist+`（史績）× `q35`（準 burst）× `st46`（中 streak）。",
            "3. **次配方（S2 蓄力）**：`b2` × `dip` × `hist+` × `s2` · mean ~+3.2% · flip 率較低但可復刻。",
            "4. **base 路徑（非 b2）**：`base+disp+hist+q35` · 台指非強勢日仍可达 +4% · 與 b2 路徑 Jaccard 低。",
            "5. **稀疏性**：11 年僅 ~19 組 mean≥3% · 正交 greedy 約 **3 strict** · 這是微策略常態非參數未設好。",
            "",
            "## D. PIT checklist",
            "",
            "- [x] lead3 全在 D 收盤可算（RV · streak · improving）",
            "- [x] flip_leading_3d 僅作 outcome 診斷 · 非進場",
            "- [x] outcome = D→D+3 交易日",
            "- [x] 獨立 yaml / 模組 / 報告",
        ]
    )
    return "\n".join(lines)


def run_lead3_funnel_3d_backtest(
    conn: Any | None = None,
    *,
    config_path: Path | None = None,
    run_discovery: bool = True,
) -> dict[str, Any]:
    cfg = load_lead3_funnel_config(config_path)
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_lead3_funnel_panel_3d(conn)
    result = evaluate_lead3_lanes_3d(panel, cfg, run_discovery=run_discovery, conn=conn)
    result["meta"] = {**meta, **result["meta"]}
    result["config"] = {"version": cfg.get("version"), "model_id": cfg.get("model_id")}
    return result


def write_lead3_lanes_to_config(
    strict_defs: list[dict[str, Any]],
    coverage_defs: list[dict[str, Any]],
    path: Path | None = None,
) -> None:
    p = path or CONFIG_PATH
    cfg = load_lead3_funnel_config(p)

    def _clean(defs: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
        out = []
        for ld in defs:
            out.append(
                {
                    "id": ld["id"],
                    "tier": tier,
                    "rule_atoms": ld["rule_atoms"],
                    "label": ld.get("label", "+".join(ld["rule_atoms"])),
                    "description": ld.get("description", ""),
                    "regime_tag": ld.get("regime_tag"),
                    "archetype_tag": ld.get("archetype_tag") or infer_archetype_tag(ld["rule_atoms"]),
                }
            )
        return out

    cfg.setdefault("lanes", {})["strict"] = _clean(strict_defs, "strict")
    cfg.setdefault("lanes", {})["coverage"] = _clean(coverage_defs, "coverage")
    cfg.setdefault("discovery", {})["enabled"] = False
    p.write_text(
        yaml.dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
