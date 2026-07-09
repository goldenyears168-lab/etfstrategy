"""Signal-day co-occurrence · monthly success cohort · loss exclusion gates (PIT).

Used by rrg-lane-discovery-loop: find recurring success patterns across all months,
derive exclusion rules from loss-heavy tags, emit sweep seeds.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import LOSS5_PCT, LOSS8_PCT

# Tag → dimension (one tag per dimension in lane seeds)
TAG_DIMENSION: dict[str, str] = {
    "bench_neg": "bench",
    "b0": "bench",
    "b1": "bench",
    "b2": "bench",
    "b2p": "bench",
    "q_improving": "quadrant",
    "q_lagging": "quadrant",
    "q_leading": "quadrant",
    "q_weakening": "quadrant",
    "prev_improving": "prev_q",
    "prev_lagging": "prev_q",
    "prev_leading": "prev_q",
    "prev_weakening": "prev_q",
    "lagv": "track",
    "dip_deep": "price",
    "dip": "price",
    "flat": "price",
    "micro_up": "price",
    "chase": "price",
    "rv_lt95": "rv",
    "rv95_98": "rv",
    "rv98_99": "rv",
    "rv99_100": "rv",
    "st0": "streak",
    "st1_3": "streak",
    "st4_6": "streak",
    "st7p": "streak",
    "disp": "rrg_shape",
    "ex": "excess",
    "hist": "memory",
    "gap1": "session",
}

EXCLUSION_TAGS_DEFAULT = frozenset({"bench_neg", "chase", "st1_3"})


def encode_signal_tags(row: pd.Series) -> frozenset[str]:
    """Discrete PIT tags for one signal-day row."""
    tags: set[str] = set()
    bench = float(row.get("bench_today_ret") or 0)
    if bench < 0:
        tags.add("bench_neg")
    elif bench < 1.0:
        tags.add("b0")
    elif bench < 2.0:
        tags.add("b1")
    elif bench < 2.5:
        tags.add("b2")
    else:
        tags.add("b2p")

    eq = str(row.get("end_q") or "")
    pq = str(row.get("prev_end_q") or "")
    if eq:
        tags.add(f"q_{eq}")
    if pq:
        tags.add(f"prev_{pq}")
    if eq == "lagging" and pq == "lagging":
        tags.add("lagv")

    sig = float(row.get("sig_ret") or 0)
    if -4.0 <= sig < -1.0:
        tags.add("dip_deep")
    elif -2.0 <= sig <= 0.0:
        tags.add("dip")
    elif -1.0 <= sig <= 1.0:
        tags.add("flat")
    elif 0.0 < sig < 3.0:
        tags.add("micro_up")
    elif sig >= 3.0:
        tags.add("chase")

    rv = float(row.get("rv") or np.nan)
    if not np.isnan(rv):
        if rv < 95:
            tags.add("rv_lt95")
        elif rv < 98:
            tags.add("rv95_98")
        elif rv < 99:
            tags.add("rv98_99")
        else:
            tags.add("rv99_100")

    st = int(row.get("improve_streak") or 0)
    if st == 0:
        tags.add("st0")
    elif st <= 3:
        tags.add("st1_3")
    elif st <= 6:
        tags.add("st4_6")
    else:
        tags.add("st7p")

    if bool(row.get("disp_contract")):
        tags.add("disp")
    ex = float(row.get("excess") or 0)
    if -2.0 <= ex <= -0.3:
        tags.add("ex")
    if bool(row.get("sid_hist_ok")):
        tags.add("hist")
    if int(row.get("session_gap_days") or 1) == 1:
        tags.add("gap1")
    return frozenset(tags)


def tag_mask(panel: pd.DataFrame, tag: str) -> pd.Series:
    """Boolean mask for a single discrete tag."""
    if tag == "bench_neg":
        return panel["bench_today_ret"] < 0
    if tag == "b0":
        return (panel["bench_today_ret"] >= 0) & (panel["bench_today_ret"] < 1)
    if tag == "b1":
        return (panel["bench_today_ret"] >= 1) & (panel["bench_today_ret"] < 2)
    if tag == "b2":
        return (panel["bench_today_ret"] >= 2) & (panel["bench_today_ret"] < 2.5)
    if tag == "b2p":
        return panel["bench_today_ret"] >= 2.5
    if tag.startswith("q_"):
        return panel["end_q"] == tag[2:]
    if tag.startswith("prev_"):
        return panel["prev_end_q"] == tag[5:]
    if tag == "lagv":
        return (panel["end_q"] == "lagging") & (panel["prev_end_q"] == "lagging")
    if tag == "dip_deep":
        return (panel["sig_ret"] >= -4) & (panel["sig_ret"] < -1)
    if tag == "dip":
        return panel["sig_ret"].between(-2, 0)
    if tag == "flat":
        return panel["sig_ret"].between(-1, 1)
    if tag == "micro_up":
        return (panel["sig_ret"] > 0) & (panel["sig_ret"] < 3)
    if tag == "chase":
        return panel["sig_ret"] >= 3
    if tag == "rv_lt95":
        return panel["rv"] < 95
    if tag == "rv95_98":
        return panel["rv"].between(95, 98, inclusive="left")
    if tag == "rv98_99":
        return panel["rv"].between(98, 99, inclusive="left")
    if tag == "rv99_100":
        return panel["rv"] >= 99
    if tag == "st0":
        return panel["improve_streak"] == 0
    if tag == "st1_3":
        return panel["improve_streak"].between(1, 3)
    if tag == "st4_6":
        return panel["improve_streak"].between(4, 6)
    if tag == "st7p":
        return panel["improve_streak"] >= 7
    if tag == "disp":
        return panel["disp_contract"]
    if tag == "ex":
        return panel["excess"].between(-2, -0.3)
    if tag == "hist":
        return panel["sid_hist_ok"]
    if tag == "gap1":
        return panel["session_gap_days"] == 1
    raise KeyError(f"Unknown tag: {tag}")


def apply_exclusion_mask(
    base: pd.Series,
    panel: pd.DataFrame,
    exclude_tags: frozenset[str] | list[str],
) -> pd.Series:
    """Keep rows that do NOT hit any exclusion tag."""
    m = base.copy()
    for tag in exclude_tags:
        m &= ~tag_mask(panel, tag)
    return m


def label_outcome_row(row: pd.Series, *, success_min: float, exclude_extreme_pct: float) -> str:
    """success | loss | neutral | extreme — PIT labels from forward ret_3d only."""
    r3 = float(row.get("ret_3d") or 0)
    min_r = row.get("min_ret_3d")
    min_r = float(min_r) if min_r is not None and not (isinstance(min_r, float) and np.isnan(min_r)) else r3

    if abs(r3) > exclude_extreme_pct or abs(min_r) > exclude_extreme_pct:
        return "extreme"
    if r3 <= LOSS5_PCT or min_r <= LOSS8_PCT:
        return "loss"
    if r3 >= success_min:
        return "success"
    return "neutral"


def _outcome_series(
    panel: pd.DataFrame,
    *,
    success_min: float,
    exclude_extreme_pct: float,
) -> pd.Series:
    r3 = panel["ret_3d"].astype(float)
    min_r = panel["min_ret_3d"].astype(float) if "min_ret_3d" in panel.columns else r3
    label = pd.Series("neutral", index=panel.index, dtype="object")
    extreme = (r3.abs() > exclude_extreme_pct) | (min_r.abs() > exclude_extreme_pct)
    loss = (r3 <= LOSS5_PCT) | (min_r <= LOSS8_PCT)
    label.loc[extreme] = "extreme"
    label.loc[~extreme & loss] = "loss"
    label.loc[~extreme & ~loss & (r3 >= success_min)] = "success"
    return label


def _tag_matrix(panel: pd.DataFrame) -> pd.DataFrame:
    """Vectorized tag membership · one column per discrete tag."""
    tags = sorted(TAG_DIMENSION)
    return pd.DataFrame({t: tag_mask(panel, t) for t in tags}, index=panel.index)


def build_tagged_cohort(
    panel: pd.DataFrame,
    *,
    success_min: float = 3.0,
    exclude_extreme_pct: float = 25.0,
) -> pd.DataFrame:
    """All pre-release pool rows with tags, outcome label, YYYY-MM month."""
    mat = _tag_matrix(panel)
    tag_cols = list(mat.columns)
    tag_values = mat.to_numpy(dtype=bool)
    labels = _outcome_series(
        panel, success_min=success_min, exclude_extreme_pct=exclude_extreme_pct
    )
    dates = panel["date"].astype(str)
    rows: list[dict[str, Any]] = []
    for i, idx in enumerate(panel.index):
        active = [tag_cols[j] for j in np.flatnonzero(tag_values[i])]
        tag_set = frozenset(active)
        rows.append(
            {
                "idx": idx,
                "sid": str(panel.at[idx, "sid"]),
                "date": dates.iloc[i],
                "month": dates.iloc[i][:7],
                "outcome_label": labels.iloc[i],
                "ret_3d": float(panel.at[idx, "ret_3d"]),
                "min_ret_3d": float(panel.at[idx, "min_ret_3d"])
                if "min_ret_3d" in panel.columns
                else float(panel.at[idx, "ret_3d"]),
                "tags": sorted(tag_set),
                "tag_str": "+".join(sorted(tag_set)),
            }
        )
    return pd.DataFrame(rows)


def _orthogonal_pairs(tags: frozenset[str]) -> list[tuple[str, str]]:
    """Tag pairs from different dimensions (for co-occurrence)."""
    dims: dict[str, list[str]] = defaultdict(list)
    for t in tags:
        dim = TAG_DIMENSION.get(t)
        if dim:
            dims[dim].append(t)
    reps = [sorted(v)[0] for v in dims.values() if v]
    return [(a, b) for a, b in combinations(sorted(reps), 2)]


def analyze_tag_cooccurrence(
    cohort: pd.DataFrame,
    *,
    min_recurrence: int = 2,
    min_lift: float = 1.3,
    min_months: int = 1,
) -> list[dict[str, Any]]:
    """Cross-month success co-occurrence · orthogonal tag pairs."""
    pool = cohort[cohort["outcome_label"].isin(["success", "neutral", "loss"])]
    success = cohort[cohort["outcome_label"] == "success"]
    if success.empty:
        return []

    pair_success: Counter[tuple[str, str]] = Counter()
    pair_months: dict[tuple[str, str], set[str]] = defaultdict(set)
    for _, row in success.iterrows():
        tag_set = frozenset(row["tags"])
        for pair in _orthogonal_pairs(tag_set):
            pair_success[pair] += 1
            pair_months[pair].add(str(row["month"]))

    pair_pool: Counter[tuple[str, str]] = Counter()
    for _, row in pool.iterrows():
        tag_set = frozenset(row["tags"])
        for pair in _orthogonal_pairs(tag_set):
            pair_pool[pair] += 1

    n_succ = len(success)
    n_pool = max(len(pool), 1)
    results: list[dict[str, Any]] = []
    for pair, cnt in pair_success.items():
        if cnt < min_recurrence:
            continue
        months = pair_months[pair]
        if len(months) < min_months:
            continue
        pool_cnt = pair_pool.get(pair, 0)
        succ_rate = cnt / n_succ
        pool_rate = pool_cnt / n_pool if pool_cnt else 0
        lift = succ_rate / pool_rate if pool_rate > 0 else 0.0
        if lift < min_lift:
            continue
        results.append(
            {
                "tag_a": pair[0],
                "tag_b": pair[1],
                "n_success": cnt,
                "n_pool": pool_cnt,
                "lift": round(lift, 3),
                "succ_share": round(succ_rate, 4),
                "months": sorted(months),
                "n_months": len(months),
                "pattern": f"{pair[0]}+{pair[1]}",
            }
        )
    results.sort(key=lambda x: (-x["n_success"], -x["lift"], -x["n_months"]))
    return results


def analyze_monthly_success_profile(cohort: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-month success / loss counts and top tags."""
    profiles: list[dict[str, Any]] = []
    for month, sub in cohort.groupby("month"):
        succ = sub[sub["outcome_label"] == "success"]
        loss = sub[sub["outcome_label"] == "loss"]
        tag_ctr: Counter[str] = Counter()
        for tags in succ["tags"]:
            tag_ctr.update(tags)
        profiles.append(
            {
                "month": str(month),
                "n_total": len(sub),
                "n_success": len(succ),
                "n_loss": len(loss),
                "success_rate": round(len(succ) / len(sub), 4) if len(sub) else 0,
                "mean_ret_3d_success": round(float(succ["ret_3d"].mean()), 2) if len(succ) else None,
                "top_tags_success": [t for t, _ in tag_ctr.most_common(8)],
            }
        )
    profiles.sort(key=lambda x: x["month"])
    return profiles


def discover_loss_exclusion_tags(
    panel: pd.DataFrame,
    *,
    loss_lift_mult: float = 1.8,
    min_tag_n: int = 30,
    extra_exclude: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Tags whose loss5 or path-loss8 rate exceeds baseline × mult → exclusion candidates."""
    base_loss5 = float((panel["ret_3d"] <= LOSS5_PCT).mean())
    min_col = panel["min_ret_3d"] if "min_ret_3d" in panel.columns else panel["ret_3d"]
    base_loss8 = float((min_col <= LOSS8_PCT).mean())

    candidates: list[dict[str, Any]] = []
    all_tags = set(TAG_DIMENSION)
    for tag in sorted(all_tags):
        try:
            m = tag_mask(panel, tag)
        except KeyError:
            continue
        n = int(m.sum())
        if n < min_tag_n:
            continue
        sub = panel.loc[m]
        l5 = float((sub["ret_3d"] <= LOSS5_PCT).mean())
        l8 = float((sub["min_ret_3d"] <= LOSS8_PCT).mean()) if "min_ret_3d" in sub.columns else l5
        lift5 = l5 / base_loss5 if base_loss5 > 0 else 0
        lift8 = l8 / base_loss8 if base_loss8 > 0 else 0
        exclude = lift5 >= loss_lift_mult or lift8 >= loss_lift_mult
        if extra_exclude and tag in extra_exclude:
            exclude = True
        candidates.append(
            {
                "tag": tag,
                "dimension": TAG_DIMENSION.get(tag, ""),
                "n": n,
                "loss5_rate": round(l5, 4),
                "loss8_path_rate": round(l8, 4),
                "lift5_vs_pool": round(lift5, 3),
                "lift8_vs_pool": round(lift8, 3),
                "exclude": exclude,
            }
        )
    candidates.sort(key=lambda x: (-x["lift5_vs_pool"], -x["loss5_rate"]))
    return candidates


def active_exclusion_tags(
    exclusion_analysis: list[dict[str, Any]],
    *,
    force: frozenset[str] | None = None,
) -> list[str]:
    tags = [r["tag"] for r in exclusion_analysis if r.get("exclude")]
    if force:
        tags = sorted(set(tags) | set(force))
    return tags


def pattern_to_seed(pattern: str, seed_id: str) -> dict[str, Any]:
    """Map co-occurrence pattern (tag_a+tag_b+...) to evaluate_seed params."""
    tags = pattern.split("+")
    params: dict[str, Any] = {"seed_id": seed_id, "pattern": pattern, "require_gap1": True}
    for tag in tags:
        if tag in ("b0", "b1", "b2", "b2p"):
            params["bench_min"] = {"b0": 0, "b1": 1, "b2": 2, "b2p": 2.5}[tag]
        elif tag == "bench_neg":
            params["bench_max"] = 0
        elif tag == "lagv":
            params["require_elag_plag"] = True
        elif tag == "q_improving":
            params["end_q"] = "improving"
        elif tag == "q_lagging":
            params["end_q"] = "lagging"
        elif tag == "q_leading":
            params["end_q"] = "leading"
        elif tag == "q_weakening":
            params["end_q"] = "weakening"
        elif tag == "prev_improving":
            params["prev_end_q"] = "improving"
        elif tag == "prev_lagging":
            params["prev_end_q"] = "lagging"
        elif tag == "prev_leading":
            params["prev_end_q"] = "leading"
        elif tag == "prev_weakening":
            params["prev_end_q"] = "weakening"
        elif tag == "dip_deep":
            params["sig_lo"], params["sig_hi"] = -4, -1
        elif tag == "dip":
            params["sig_lo"], params["sig_hi"] = -2, 0
        elif tag == "flat":
            params["sig_lo"], params["sig_hi"] = -1, 1
        elif tag == "ex":
            params["require_excess_band"] = True
        elif tag == "rv_lt95":
            params["rv_lo"], params["rv_hi"] = 0, 95
        elif tag == "rv95_98":
            params["rv_lo"], params["rv_hi"] = 95, 98
        elif tag == "rv98_99":
            params["rv_lo"], params["rv_hi"] = 98, 99
        elif tag == "rv99_100":
            params["rv_lo"], params["rv_hi"] = 99, 100
        elif tag == "st7p":
            params["streak_lo"] = 7
        elif tag == "st4_6":
            params["streak_lo"], params["streak_hi"] = 4, 6
        elif tag == "st1_3":
            params["streak_lo"], params["streak_hi"] = 1, 3
        elif tag == "st0":
            params["streak_lo"], params["streak_hi"] = 0, 0
        elif tag == "disp":
            params["require_disp"] = True
        elif tag == "hist":
            params["require_hist"] = True
    return params


def seeds_from_cooccurrence(
    patterns: list[dict[str, Any]],
    *,
    max_seeds: int = 12,
    prefix: str = "CO",
    skip_tags: frozenset[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    skip = set(skip_tags or ())
    for p in patterns:
        pat = str(p.get("pattern") or "")
        if not pat:
            continue
        tags = set(pat.split("+"))
        if tags & skip:
            continue
        seeds.append(pattern_to_seed(pat, f"{prefix}{len(seeds) + 1:02d}"))
        if len(seeds) >= max_seeds:
            break
    return seeds


def run_cooccurrence_analysis(
    panel: pd.DataFrame,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full pipeline: cohort → monthly profile → co-occurrence → exclusion → seeds."""
    cfg = cfg or {}
    cohort = build_tagged_cohort(
        panel,
        success_min=float(cfg.get("success_min_ret_3d", 3.0)),
        exclude_extreme_pct=float(cfg.get("exclude_extreme_pct", 25.0)),
    )
    monthly = analyze_monthly_success_profile(cohort)
    cooc = analyze_tag_cooccurrence(
        cohort,
        min_recurrence=int(cfg.get("min_recurrence", 2)),
        min_lift=float(cfg.get("min_lift", 1.3)),
        min_months=int(cfg.get("min_months", 1)),
    )
    triples: list[dict[str, Any]] = []
    if int(cfg.get("min_recurrence_triple", 0)) >= 2:
        triples = _analyze_triple_cooccurrence(
            cohort,
            min_recurrence=int(cfg.get("min_recurrence_triple", 2)),
            min_lift=float(cfg.get("min_lift", 1.3)),
        )

    excl = discover_loss_exclusion_tags(
        panel,
        loss_lift_mult=float(cfg.get("loss_lift_mult", 1.8)),
        min_tag_n=int(cfg.get("min_tag_n_exclusion", 30)),
        extra_exclude=frozenset(cfg.get("force_exclude_tags") or EXCLUSION_TAGS_DEFAULT),
    )
    excl_tags = active_exclusion_tags(
        excl,
        force=frozenset(cfg.get("force_exclude_tags") or EXCLUSION_TAGS_DEFAULT),
    )
    pattern_seeds = seeds_from_cooccurrence(
        cooc + triples,
        max_seeds=int(cfg.get("max_pattern_seeds", 12)),
        skip_tags=excl_tags,
    )

    return {
        "cohort_summary": {
            "n_total": len(cohort),
            "n_success": int((cohort["outcome_label"] == "success").sum()),
            "n_loss": int((cohort["outcome_label"] == "loss").sum()),
            "n_extreme": int((cohort["outcome_label"] == "extreme").sum()),
            "n_neutral": int((cohort["outcome_label"] == "neutral").sum()),
            "n_months": cohort["month"].nunique(),
        },
        "monthly_profiles": monthly,
        "cooccurrence_pairs": cooc[:50],
        "cooccurrence_triples": triples[:30],
        "loss_exclusion_tags": excl,
        "active_exclusion_tags": excl_tags,
        "pattern_seeds": pattern_seeds,
    }


def _analyze_triple_cooccurrence(
    cohort: pd.DataFrame,
    *,
    min_recurrence: int = 2,
    min_lift: float = 1.3,
) -> list[dict[str, Any]]:
    """3-tag patterns from different dimensions in success cohort."""
    pool = cohort[cohort["outcome_label"].isin(["success", "neutral", "loss"])]
    success = cohort[cohort["outcome_label"] == "success"]
    if success.empty:
        return []

    def _reps(tags: frozenset[str]) -> list[str]:
        dims: dict[str, list[str]] = defaultdict(list)
        for t in tags:
            d = TAG_DIMENSION.get(t)
            if d:
                dims[d].append(t)
        return [sorted(v)[0] for v in dims.values() if v]

    ctr_s: Counter[tuple[str, ...]] = Counter()
    ctr_p: Counter[tuple[str, ...]] = Counter()
    for _, row in success.iterrows():
        reps = sorted(_reps(frozenset(row["tags"])))
        if len(reps) < 3:
            continue
        for trip in combinations(reps, 3):
            ctr_s[trip] += 1
    for _, row in pool.iterrows():
        reps = sorted(_reps(frozenset(row["tags"])))
        if len(reps) < 3:
            continue
        for trip in combinations(reps, 3):
            ctr_p[trip] += 1

    n_succ, n_pool = len(success), max(len(pool), 1)
    out: list[dict[str, Any]] = []
    for trip, cnt in ctr_s.items():
        if cnt < min_recurrence:
            continue
        pc = ctr_p.get(trip, 0)
        lift = (cnt / n_succ) / (pc / n_pool) if pc else 0
        if lift < min_lift:
            continue
        out.append(
            {
                "pattern": "+".join(trip),
                "n_success": cnt,
                "n_pool": pc,
                "lift": round(lift, 3),
                "tag_a": trip[0],
                "tag_b": trip[1],
            }
        )
    out.sort(key=lambda x: (-x["n_success"], -x["lift"]))
    return out
