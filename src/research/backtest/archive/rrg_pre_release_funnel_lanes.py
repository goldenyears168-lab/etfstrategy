"""RRG pre-release funnel lanes · strict + coverage tier backtest (PIT).

Universe: pre-release pool from lifecycle events with lagging pre-flip gate.
Outcome: nxt_ret (D close → D+1 close). Lane = conjunction of feature atoms.
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

from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, build_lifecycle_events
from research.backtest.rrg_improving_s3rv_executable import MIN_SID_HIST, enrich_lifecycle_events
from stock_db import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "rrg_pre_release_funnel_lanes.yaml"

# Coverage discovery thresholds
COVERAGE_WIN_MIN = 0.55
COVERAGE_MEAN_MIN = 1.0
COVERAGE_MEAN_MAX = 3.0
COVERAGE_N_MIN = 20
JACCARD_MAX_STRICT = 0.35
JACCARD_MAX_COVERAGE = 0.35
TARGET_UNION_DAYS_PER_YEAR = (50.0, 80.0)


@dataclass(frozen=True)
class LaneStats:
    lane_id: str
    tier: str
    label: str
    rule_atoms: tuple[str, ...]
    n_events: int
    n_calendar_days: int
    days_per_year: float
    win_rate: float
    mean_pct: float
    median_pct: float
    loss5_rate: float
    year_table: dict[str, dict[str, float | int]]


@dataclass(frozen=True)
class UnionStats:
    tier: str
    n_calendar_days: int
    days_per_year: float
    day_win_rate: float
    day_mean_pct: float
    lane_ids: tuple[str, ...]


def load_funnel_lanes_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw


def _attach_sid_burst_hist(df: pd.DataFrame) -> pd.DataFrame:
    """PIT sid burst history on every row (prior s3_burst nxt_ret per sid)."""
    prior: dict[str, list[float]] = {}
    hist_n, hist_ok = [], []
    for _, row in df.sort_values(["sid", "date"]).iterrows():
        sid = str(row["sid"])
        prev = prior.get(sid, [])
        if len(prev) >= MIN_SID_HIST:
            hist_n.append(len(prev))
            hist_ok.append(float(np.mean(prev)) > 0)
        else:
            hist_n.append(len(prev))
            hist_ok.append(False)
        if bool(row["s3_burst"]):
            prior.setdefault(sid, []).append(float(row["nxt_ret"]))
    out = df.copy()
    out["sid_hist_n"] = hist_n
    out["sid_hist_ok"] = hist_ok
    return out


def _atom_mask(panel: pd.DataFrame, atom: str) -> pd.Series:
    """Evaluate a single feature atom (PIT columns on signal-day)."""
    s = panel["improve_streak"]
    if atom == "b2":
        return panel["bench_today_ret"] >= 2.0
    if atom == "b1":
        return panel["bench_today_ret"] >= 1.0
    if atom == "b0":
        return panel["bench_today_ret"] >= 0.0
    if atom == "disp":
        return panel["disp_contract"]
    if atom == "base":
        return panel["base_setup"]
    if atom == "st46":
        return s.between(4, 6)
    if atom == "st13":
        return s.between(1, 3)
    if atom == "st1":
        return s == 1
    if atom == "st7p":
        return s >= 7
    if atom == "hist":
        return panel["sid_hist_ok"]
    if atom == "plag":
        return panel["prev_end_q"] == "lagging"
    if atom == "elag":
        return panel["end_q"] == "lagging"
    if atom == "dip":
        return panel["sig_ret"].between(-2.0, 0.0)
    if atom == "dip_deep":
        return (panel["sig_ret"] >= -4.0) & (panel["sig_ret"] < -1.0)
    if atom == "flat":
        return panel["sig_ret"].between(-1.0, 1.0)
    if atom == "q35":
        return panel["sig_ret"].between(3.0, 5.0, inclusive="left")
    if atom == "q45":
        return panel["sig_ret"].between(4.0, 5.0, inclusive="left")
    if atom == "ex":
        return panel["excess"].between(-2.0, -0.3)
    if atom == "s2":
        return panel["s2_setup_core"]
    if atom == "rv98":
        return panel["s4_rv_98_99"]
    if atom == "rv98100":
        rv = panel["rv"] if "rv" in panel.columns else pd.Series(np.nan, index=panel.index)
        return rv.notna() & (rv >= 98.0) & (rv < 100.0)
    if atom == "no23":
        return ~s.between(2, 3)
    if atom == "gap1":
        return panel["session_gap_days"] == 1
    if atom == "s0":
        return panel["s0_fresh_flip"]
    if atom == "imp":
        return panel["end_q"] == "improving"
    if atom == "lead3":
        rv = panel["rv"] if "rv" in panel.columns else pd.Series(np.nan, index=panel.index)
        return (
            (panel["end_q"] == "improving")
            & rv.notna()
            & (rv >= 98.0)
            & (rv < 100.0)
            & (panel["improve_streak"] >= 4)
        )
    if atom == "lead3_late":
        return _atom_mask(panel, "lead3") & _atom_mask(panel, "st7p")
    raise KeyError(f"Unknown atom: {atom}")


def lane_mask(panel: pd.DataFrame, rule_atoms: list[str] | tuple[str, ...]) -> pd.Series:
    mask = pd.Series(True, index=panel.index)
    for atom in rule_atoms:
        mask &= _atom_mask(panel, atom)
    return mask


def build_pre_release_funnel_panel(
    conn: Any | None = None,
    *,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pre-release pool with PIT feature columns for lane evaluation."""
    if events is not None:
        df, meta = events, {}
    else:
        df, meta = enrich_lifecycle_events(conn)
    if df.empty:
        return df, meta

    panel = df.sort_values(["sid", "date"]).copy()
    panel["prev_end_q"] = panel.groupby("sid")["end_q"].shift(1).fillna("")
    panel = _attach_sid_burst_hist(panel)

    pool = (
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
    out = panel[pool].copy()
    out["nxt_loss5"] = out["nxt_ret"] <= -BURST_PCT
    return out, meta


def _calendar_day_returns(sub: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight mean nxt_ret per signal calendar day."""
    if sub.empty:
        return pd.DataFrame(columns=["date", "day_ret", "day_win"])
    g = sub.groupby("date", as_index=False).agg(day_ret=("nxt_ret", "mean"))
    g["day_win"] = g["day_ret"] > 0
    return g


def _years_span(dates: pd.Series | list[str]) -> float:
    if len(dates) == 0:
        return 1.0
    d0 = pd.Timestamp(min(dates))
    d1 = pd.Timestamp(max(dates))
    return max((d1 - d0).days / 365.25, 1 / 365.25)


def _lane_stats(
    panel: pd.DataFrame,
    lane_id: str,
    tier: str,
    label: str,
    rule_atoms: list[str] | tuple[str, ...],
) -> LaneStats:
    sub = panel[lane_mask(panel, rule_atoms)]
    cal = _calendar_day_returns(sub)
    years = _years_span(panel["date"])
    yr: dict[str, dict[str, float | int]] = {}
    for y, g in sub.groupby(sub["date"].str[:4]):
        cal_y = _calendar_day_returns(g)
        yr[str(y)] = {
            "n": len(g),
            "days": int(g["date"].nunique()),
            "win": round(float((g["nxt_ret"] > 0).mean()), 4) if len(g) else 0.0,
            "mean": round(float(g["nxt_ret"].mean()), 4) if len(g) else 0.0,
            "day_win": round(float(cal_y["day_win"].mean()), 4) if len(cal_y) else 0.0,
            "day_mean": round(float(cal_y["day_ret"].mean()), 4) if len(cal_y) else 0.0,
        }
    return LaneStats(
        lane_id=lane_id,
        tier=tier,
        label=label,
        rule_atoms=tuple(rule_atoms),
        n_events=len(sub),
        n_calendar_days=int(sub["date"].nunique()) if len(sub) else 0,
        days_per_year=round(sub["date"].nunique() / years, 2) if len(sub) else 0.0,
        win_rate=round(float((sub["nxt_ret"] > 0).mean()), 4) if len(sub) else 0.0,
        mean_pct=round(float(sub["nxt_ret"].mean()), 4) if len(sub) else 0.0,
        median_pct=round(float(sub["nxt_ret"].median()), 4) if len(sub) else 0.0,
        loss5_rate=round(float(sub["nxt_loss5"].mean()), 4) if len(sub) else 0.0,
        year_table=yr,
    )


def calendar_day_set(sub: pd.DataFrame) -> set[str]:
    return set(sub["date"].astype(str).unique())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    u = a | b
    if not u:
        return 0.0
    return len(a & b) / len(u)


def max_jaccard_vs(days: set[str], others: list[set[str]]) -> float:
    if not others:
        return 0.0
    return max(jaccard(days, o) for o in others)


def _union_stats(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
    *,
    tier: str,
) -> UnionStats:
    ids: list[str] = []
    parts: list[pd.DataFrame] = []
    for ld in lane_defs:
        sub = panel[lane_mask(panel, ld["rule_atoms"])]
        if sub.empty:
            continue
        ids.append(str(ld["id"]))
        parts.append(sub)
    if not parts:
        return UnionStats(tier, 0, 0.0, 0.0, 0.0, tuple())
    union = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["date", "sid"])
    cal = _calendar_day_returns(union)
    years = _years_span(panel["date"])
    return UnionStats(
        tier=tier,
        n_calendar_days=int(union["date"].nunique()),
        days_per_year=round(union["date"].nunique() / years, 2),
        day_win_rate=round(float(cal["day_win"].mean()), 4) if len(cal) else 0.0,
        day_mean_pct=round(float(cal["day_ret"].mean()), 4) if len(cal) else 0.0,
        lane_ids=tuple(ids),
    )


def _jaccard_matrix(
    panel: pd.DataFrame,
    lane_defs: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    day_sets: dict[str, set[str]] = {}
    for ld in lane_defs:
        sub = panel[lane_mask(panel, ld["rule_atoms"])]
        day_sets[str(ld["id"])] = calendar_day_set(sub)
    ids = sorted(day_sets)
    out: dict[str, dict[str, float]] = {}
    for i in ids:
        out[i] = {}
        for j in ids:
            out[i][j] = round(jaccard(day_sets[i], day_sets[j]), 4)
    return out


def _precompute_atom_columns(panel: pd.DataFrame, atoms: list[str]) -> dict[str, pd.Series]:
    return {a: _atom_mask(panel, a) for a in atoms}


def _combo_mask(precomputed: dict[str, pd.Series], combo: tuple[str, ...]) -> pd.Series:
    mask = pd.Series(True, index=next(iter(precomputed.values())).index)
    for atom in combo:
        mask &= precomputed[atom]
    return mask


def _candidate_combos(search_atoms: list[str], min_size: int = 2, max_size: int = 6) -> list[tuple[str, ...]]:
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, ...]] = []
    for k in range(min_size, max_size + 1):
        for combo in itertools.combinations(search_atoms, k):
            key = tuple(sorted(combo))
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def discover_coverage_lanes(
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
    jaccard_max_vs_coverage: float = 0.40,
    min_combo_size: int = 2,
    max_combo_size: int = 6,
) -> list[dict[str, Any]]:
    """Greedy search for coverage-tier lanes · low overlap with strict union."""
    if search_atoms is None:
        search_atoms = [
            "b0", "b1", "b2", "disp", "base", "st46", "st13", "st1", "st7p",
            "hist", "plag", "elag", "dip", "flat", "q35", "q45", "ex", "s2",
            "rv98", "no23", "gap1", "s0", "imp",
        ]

    precomputed = _precompute_atom_columns(panel, search_atoms)
    nxt = panel["nxt_ret"].to_numpy(dtype=float)
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
        {"phase": "extended", "n_min": max(15, n_min - 5), "mean_min": max(0.8, mean_min - 0.2)},
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
            rets = nxt[idx]
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
        refs_strict = strict_day_sets
        refs_cov = coverage_day_sets
        if max_jaccard_vs(ds, refs_strict) > jaccard_max:
            continue
        cov_j_max = jaccard_max if cand["phase"] == "primary" else max(jaccard_max_vs_coverage, jaccard_max)
        if refs_cov and max_jaccard_vs(ds, refs_cov) > cov_j_max:
            continue
        incremental = ds - coverage_union
        union_dpy = len(coverage_union | ds) / years
        if not incremental and len(selected) >= 20 and cand["phase"] == "extended":
            pass  # allow extended tier overlap for registry breadth
        elif not incremental and union_dpy < target_lo:
            continue
        elif not incremental and len(selected) >= 20:
            continue
        lane_id = f"C{len(selected) + 1:02d}"
        atoms = cand["rule_atoms"]
        label = "+".join(atoms)
        selected.append(
            {
                "id": lane_id,
                "tier": "coverage",
                "rule_atoms": atoms,
                "label": label,
                "description": (
                    f"Coverage ({cand['phase']}) · {label} · pre-burst signal lane"
                ),
                "regime_tag": _infer_regime_tag(atoms),
                "discovery": {
                    "phase": cand["phase"],
                    "n": cand["n"],
                    "win": round(cand["win"], 4),
                    "mean": round(cand["mean"], 4),
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


def _infer_regime_tag(atoms: list[str]) -> str | None:
    if "s0" in atoms:
        return "fresh_flip"
    if "elag" in atoms or "plag" in atoms:
        return "lagging_track"
    if "st7p" in atoms:
        return "late_streak"
    if "s2" in atoms:
        return "s2_core"
    if "rv98" in atoms:
        return "rv98_edge"
    if "lead3" in atoms:
        return "lead3_flip"
    if "q35" in atoms or "q45" in atoms:
        return "quasi_burst"
    return None


def evaluate_lanes(
    panel: pd.DataFrame,
    cfg: dict[str, Any],
    *,
    run_discovery: bool = True,
) -> dict[str, Any]:
    strict_defs = list(cfg.get("lanes", {}).get("strict", []))
    coverage_defs = list(cfg.get("lanes", {}).get("coverage", []))

    if run_discovery and cfg.get("discovery", {}).get("enabled", True):
        disc = cfg.get("discovery", {})
        discovered = discover_coverage_lanes(
            panel,
            strict_defs,
            search_atoms=disc.get("search_atoms"),
            max_lanes=int(disc.get("max_lanes", 30)),
            win_min=float(disc.get("win_min", COVERAGE_WIN_MIN)),
            mean_min=float(disc.get("mean_min", COVERAGE_MEAN_MIN)),
            mean_max=float(disc.get("mean_max", COVERAGE_MEAN_MAX)),
            n_min=int(disc.get("n_min", COVERAGE_N_MIN)),
            jaccard_max=float(disc.get("jaccard_max", JACCARD_MAX_COVERAGE)),
            jaccard_max_vs_coverage=float(disc.get("jaccard_max_vs_coverage", 0.40)),
            min_combo_size=int(disc.get("min_combo_size", 2)),
            max_combo_size=int(disc.get("max_combo_size", 6)),
        )
        if discovered:
            coverage_defs = discovered

    all_defs = strict_defs + coverage_defs
    lane_stats = [
        _lane_stats(
            panel,
            str(ld["id"]),
            str(ld.get("tier", "strict")),
            str(ld.get("label", ld["id"])),
            ld["rule_atoms"],
        )
        for ld in all_defs
    ]

    strict_union = _union_stats(panel, strict_defs, tier="strict")
    full_union = _union_stats(panel, all_defs, tier="strict+coverage")
    jaccard_mx = _jaccard_matrix(panel, all_defs)

    years = _years_span(panel["date"])
    return {
        "meta": {
            "generated_on": date.today().isoformat(),
            "pool_n": len(panel),
            "years_span": round(years, 3),
            "date_start": str(panel["date"].min()) if len(panel) else "",
            "date_end": str(panel["date"].max()) if len(panel) else "",
        },
        "strict_lanes": [asdict(s) for s in lane_stats if s.tier == "strict"],
        "coverage_lanes": [asdict(s) for s in lane_stats if s.tier == "coverage"],
        "union_strict": asdict(strict_union),
        "union_all": asdict(full_union),
        "jaccard_matrix": jaccard_mx,
        "coverage_defs": coverage_defs,
    }


def render_funnel_markdown(result: dict[str, Any], cfg: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = [
        "# RRG pre-release funnel lanes backtest",
        "",
        f"- Pool: **{meta['pool_n']}** stock-days · {meta['date_start']} → {meta['date_end']}",
        f"- Span: **{meta['years_span']:.1f}** years · Generated: {meta['generated_on']}",
        "",
        "## Summary · strict tier (R01–R30)",
        "",
        "| ID | rule | n | days | d/yr | win | mean | median | P(≤−5%) |",
        "|----|------|---|------|------|-----|------|--------|--------|",
    ]
    for s in result["strict_lanes"]:
        atoms = "+".join(s["rule_atoms"])
        lines.append(
            f"| {s['lane_id']} | {atoms} | {s['n_events']} | {s['n_calendar_days']} | "
            f"{s['days_per_year']:.1f} | {s['win_rate']:.1%} | {s['mean_pct']:+.2f}% | "
            f"{s['median_pct']:+.2f}% | {s['loss5_rate']:.1%} |"
        )

    us = result["union_strict"]
    lines.extend(
        [
            "",
            f"**Strict union**: {us['n_calendar_days']} calendar days · "
            f"{us['days_per_year']:.1f}/yr · day win **{us['day_win_rate']:.1%}** · "
            f"day mean **{us['day_mean_pct']:+.3f}%**",
            "",
            "## Summary · coverage tier",
            "",
            "| ID | rule | n | days | d/yr | win | mean | median | P(≤−5%) |",
            "|----|------|---|------|------|-----|------|--------|--------|",
        ]
    )
    for s in result["coverage_lanes"]:
        atoms = "+".join(s["rule_atoms"])
        lines.append(
            f"| {s['lane_id']} | {atoms} | {s['n_events']} | {s['n_calendar_days']} | "
            f"{s['days_per_year']:.1f} | {s['win_rate']:.1%} | {s['mean_pct']:+.2f}% | "
            f"{s['median_pct']:+.2f}% | {s['loss5_rate']:.1%} |"
        )

    ua = result["union_all"]
    lines.extend(
        [
            "",
            f"**Strict + coverage union**: {ua['n_calendar_days']} calendar days · "
            f"{ua['days_per_year']:.1f}/yr · day win **{ua['day_win_rate']:.1%}** · "
            f"day mean **{ua['day_mean_pct']:+.3f}%**",
            "",
            f"Target coverage band: {TARGET_UNION_DAYS_PER_YEAR[0]:.0f}–"
            f"{TARGET_UNION_DAYS_PER_YEAR[1]:.0f} days/yr",
            "",
        ]
    )

    for tier_key, title in [("strict_lanes", "Strict"), ("coverage_lanes", "Coverage")]:
        lines.append(f"## Year-by-year · {title}")
        lines.append("")
        for s in result[tier_key]:
            lines.append(f"### {s['lane_id']} · {s['label']}")
            lines.append("")
            lines.append("| Year | n | days | win | mean | day_win | day_mean |")
            lines.append("|------|---|------|-----|------|---------|----------|")
            for yr in sorted(s["year_table"]):
                y = s["year_table"][yr]
                lines.append(
                    f"| {yr} | {y['n']} | {y['days']} | {y['win']:.1%} | "
                    f"{y['mean']:+.2f}% | {y['day_win']:.1%} | {y['day_mean']:+.2f}% |"
                )
            lines.append("")

    return "\n".join(lines)


def run_funnel_backtest(
    conn: Any | None = None,
    *,
    config_path: Path | None = None,
    run_discovery: bool = True,
) -> dict[str, Any]:
    cfg = load_funnel_lanes_config(config_path)
    panel, meta = build_pre_release_funnel_panel(conn)
    result = evaluate_lanes(panel, cfg, run_discovery=run_discovery)
    result["meta"] = {**meta, **result["meta"]}
    result["config"] = {
        "version": cfg.get("version"),
        "model_id": cfg.get("model_id"),
    }
    return result


def write_validated_coverage_to_config(
    coverage_defs: list[dict[str, Any]],
    path: Path | None = None,
) -> None:
    """Merge discovered coverage lanes into YAML SSOT (strip discovery metadata)."""
    p = path or CONFIG_PATH
    cfg = load_funnel_lanes_config(p)
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
    p.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding="utf-8")
