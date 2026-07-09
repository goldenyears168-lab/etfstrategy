"""RRG pre-release lane discovery · interpretable tree rules (P2).

Trains sklearn DecisionTreeClassifier on atom_* features · label ret_3d ≥ threshold.
Distills leaf paths (positive atoms only) into lane candidates · gates + Jaccard vs R01–R30.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier, _tree

from research.backtest.archive.rrg_lane_discovery_loop import (
    _expand_seed_grid,
    evaluate_seed,
    jaccard,
    load_discovery_topic,
)
from research.backtest.archive.rrg_pre_release_funnel_lanes import calendar_day_set, lane_mask
from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (
    LOSS5_PCT,
    LOSS8_PCT,
    _years_span,
    build_pre_release_funnel_panel_3d,
    load_funnel_lanes_3d_config,
)
from stock_db import PROJECT_ROOT

DEFAULT_PANEL_PATH = (
    PROJECT_ROOT / "reports/research/rrg-lane-foundation/artifacts/pre_release_feature_panel.parquet"
)
DEFAULT_MISS_PATH = PROJECT_ROOT / "reports/research/rrg/lane_discovery_miss_list_20260701.json"
DEFAULT_OUT_DIR = PROJECT_ROOT / "reports/research/rrg-lane-foundation/artifacts"


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, frozenset):
        return sorted(obj)
    return obj


def load_feature_panel(path: Path | None = None) -> pd.DataFrame:
    p = path or DEFAULT_PANEL_PATH
    if not p.is_file():
        raise FileNotFoundError(f"feature panel not found: {p}")
    return pd.read_parquet(p)


def atom_columns(df: pd.DataFrame) -> list[str]:
    return sorted(c for c in df.columns if c.startswith("atom_"))


def mask_from_rule_atoms(df: pd.DataFrame, rule_atoms: list[str] | tuple[str, ...]) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for atom_id in rule_atoms:
        col = f"atom_{atom_id}"
        if col not in df.columns:
            return pd.Series(False, index=df.index)
        m &= df[col] == 1
    return m


def extract_tree_rules(
    clf: DecisionTreeClassifier,
    feature_names: list[str],
    *,
    min_leaf_n: int = 15,
    min_pos_rate: float = 0.5,
    min_atoms: int = 2,
    max_atoms: int = 6,
) -> list[dict[str, Any]]:
    """Leaf paths requiring atom==1 only → rule_atoms conjunctions."""
    tree = clf.tree_
    rules: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()

    def walk(node_id: int, required: list[str], neg_atom_path: bool) -> None:
        if tree.feature[node_id] == _tree.TREE_UNDEFINED:
            if neg_atom_path or len(required) < min_atoms:
                return
            n = int(tree.n_node_samples[node_id])
            counts = tree.value[node_id][0]
            total = float(counts.sum())
            if total <= 0 or n < min_leaf_n:
                return
            pos_rate = float(counts[1] / total)
            if pos_rate < min_pos_rate:
                return
            key = frozenset(required)
            if key in seen or len(key) > max_atoms:
                return
            seen.add(key)
            rules.append(
                {
                    "rule_atoms": tuple(sorted(key)),
                    "leaf_n_train": n,
                    "train_pos_rate": round(pos_rate, 4),
                }
            )
            return

        feat_idx = int(tree.feature[node_id])
        atom_id = feature_names[feat_idx].replace("atom_", "", 1)
        left, right = int(tree.children_left[node_id]), int(tree.children_right[node_id])
        walk(left, required, True)
        walk(right, required + [atom_id], neg_atom_path)

    walk(0, [], False)
    return rules


def _lane_day_sets(panel: pd.DataFrame, lane_defs: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Calendar-day sets per lane · uses atom_* columns when present."""
    out: dict[str, set[str]] = {}
    use_atoms = any(c.startswith("atom_") for c in panel.columns)
    for ld in lane_defs:
        lid = str(ld["id"])
        if use_atoms:
            sub = panel[mask_from_rule_atoms(panel, ld["rule_atoms"])]
        else:
            sub = panel[lane_mask(panel, ld["rule_atoms"])]
        out[lid] = calendar_day_set(sub)
    return out


def _max_jaccard(days: set[str], others: list[set[str]]) -> float:
    if not days or not others:
        return 0.0
    return max(jaccard(days, o) for o in others)


def _stats_on_mask(sub: pd.DataFrame, years: float) -> dict[str, Any]:
    if sub.empty:
        return {
            "n": 0,
            "n_calendar_days": 0,
            "ev_yr": 0.0,
            "win_3d": 0.0,
            "mean_3d": 0.0,
            "loss5_rate": 0.0,
            "loss8_rate": 0.0,
            "loss8_path_rate": 0.0,
        }
    rets = sub["ret_3d"].to_numpy(dtype=float)
    min_rets = sub["min_ret_3d"].to_numpy(dtype=float) if "min_ret_3d" in sub.columns else rets
    return {
        "n": len(sub),
        "n_calendar_days": int(sub["date"].nunique()),
        "ev_yr": round(len(sub) / years, 2),
        "win_3d": round(float((rets > 0).mean()), 4),
        "mean_3d": round(float(rets.mean()), 2),
        "loss5_rate": round(float((rets <= LOSS5_PCT).mean()), 4),
        "loss8_rate": round(float((rets <= LOSS8_PCT).mean()), 4),
        "loss8_path_rate": round(float((min_rets <= LOSS8_PCT).mean()), 4),
    }


def evaluate_lane_candidate(
    df: pd.DataFrame,
    rule_atoms: list[str] | tuple[str, ...],
    *,
    gates: dict[str, Any],
    strict_day_sets: list[set[str]],
    lane_defs: list[dict[str, Any]],
    years: float,
    split_label: str = "full",
    apply_gates: bool = True,
) -> dict[str, Any]:
    m = mask_from_rule_atoms(df, rule_atoms)
    sub = df[m]
    stats = _stats_on_mask(sub, years)
    days = calendar_day_set(sub) if len(sub) else set()
    j_max = _max_jaccard(days, strict_day_sets)

    mean_min = float(gates.get("mean_3d_min", 3.0))
    win_min = float(gates.get("win_3d_min", 0.58))
    loss5_max = float(gates.get("loss5_rate_max", 0.15))
    loss8_max = float(gates.get("loss8_rate_max", 0.08))
    n_min = int(gates.get("n_min", 8))
    jaccard_max = float(gates.get("jaccard_max", 0.32))

    rejections: list[str] = []
    if apply_gates:
        if len(rule_atoms) < int(gates.get("min_combo_size", 2)):
            rejections.append("too_few_atoms")
        if len(rule_atoms) > int(gates.get("max_combo_size", 6)):
            rejections.append("too_many_atoms")

        for ld in lane_defs:
            if tuple(sorted(ld["rule_atoms"])) == tuple(sorted(rule_atoms)):
                rejections.append(f"duplicate_lane:{ld['id']}")

        if stats["n"] < n_min:
            rejections.append("n_below_min")
        if stats["mean_3d"] < mean_min:
            rejections.append("mean_3d_below_min")
        if stats["win_3d"] < win_min:
            rejections.append("win_3d_below_min")
        if stats["loss5_rate"] > loss5_max or stats["loss8_path_rate"] > loss8_max:
            rejections.append("loss_rate_above_max")
        if j_max > jaccard_max:
            rejections.append(f"jaccard_above_max:{j_max:.3f}")

    pass_gates = len(rejections) == 0
    return {
        "split": split_label,
        "rule_atoms": list(rule_atoms),
        "label": "+".join(rule_atoms),
        **stats,
        "jaccard_max_vs_strict": round(j_max, 4),
        "rejections": rejections,
        "pass_gates": pass_gates,
    }


def miss_list_recall(
    df: pd.DataFrame,
    rule_atoms: list[str] | tuple[str, ...],
    miss_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not miss_rows:
        return {"n_miss": 0, "n_hit": 0, "recall": None}
    hits = 0
    for row in miss_rows:
        sid, sd = str(row["sid"]), str(row["signal_date"])
        sub = df[(df["sid"] == sid) & (df["date"] == sd)]
        if sub.empty:
            continue
        if bool(mask_from_rule_atoms(sub, rule_atoms).iloc[0]):
            hits += 1
    n = len(miss_rows)
    return {"n_miss": n, "n_hit": hits, "recall": round(hits / n, 4) if n else None}


def miss_profile_combos(
    df: pd.DataFrame,
    miss_rows: list[dict[str, Any]],
    *,
    min_freq: float = 0.5,
    combo_sizes: tuple[int, ...] = (3, 4),
) -> list[tuple[str, ...]]:
    """Enumerate atom combos frequent on miss-list signal-days (H-ML-2 driver)."""
    import itertools

    if not miss_rows:
        return []
    profiles: list[pd.Series] = []
    for row in miss_rows:
        sub = df[(df["sid"] == str(row["sid"])) & (df["date"] == str(row["signal_date"]))]
        if len(sub):
            profiles.append(sub.iloc[0])
    if not profiles:
        return []

    mdf = pd.DataFrame(profiles)
    freq_atoms: list[str] = []
    for col in atom_columns(df):
        if col in mdf.columns and float(mdf[col].mean()) >= min_freq:
            freq_atoms.append(col.replace("atom_", "", 1))

    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, ...]] = []
    for k in combo_sizes:
        for combo in itertools.combinations(sorted(freq_atoms), k):
            key = tuple(sorted(combo))
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _fit_tree_rules(
    train: pd.DataFrame,
    atom_cols: list[str],
    *,
    label_threshold: float,
    max_depth: int,
    min_samples_leaf: int,
    min_pos_rate: float,
    min_atoms: int,
    max_atoms: int,
    random_state: int = 42,
) -> list[dict[str, Any]]:
    y = (train["ret_3d"].to_numpy(dtype=float) >= label_threshold).astype(np.int8)
    if int(y.sum()) < min_samples_leaf:
        return []
    X = train[atom_cols].to_numpy(dtype=np.int8)
    clf = DecisionTreeClassifier(
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        class_weight="balanced",
        random_state=random_state,
    )
    clf.fit(X, y)
    return extract_tree_rules(
        clf,
        atom_cols,
        min_leaf_n=min_samples_leaf,
        min_pos_rate=min_pos_rate,
        min_atoms=min_atoms,
        max_atoms=max_atoms,
    )


def _build_candidate(
    df: pd.DataFrame,
    train: pd.DataFrame,
    oos: pd.DataFrame,
    rule_atoms: tuple[str, ...],
    *,
    source: str,
    candidate_idx: int,
    gates: dict[str, Any],
    strict_day_sets: list[set[str]],
    all_lane_defs: list[dict[str, Any]],
    miss_rows: list[dict[str, Any]],
    years_full: float,
    years_is: float,
    years_oos: float,
    train_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full = evaluate_lane_candidate(
        df, rule_atoms, gates=gates, strict_day_sets=strict_day_sets,
        lane_defs=all_lane_defs, years=years_full, split_label="full",
    )
    is_eval = evaluate_lane_candidate(
        train, rule_atoms, gates=gates, strict_day_sets=strict_day_sets,
        lane_defs=all_lane_defs, years=years_is, split_label="is", apply_gates=False,
    )
    oos_eval = (
        evaluate_lane_candidate(
            oos, rule_atoms, gates=gates, strict_day_sets=strict_day_sets,
            lane_defs=all_lane_defs, years=years_oos, split_label="oos", apply_gates=False,
        )
        if len(oos)
        else {"split": "oos", "n": 0}
    )
    sub = df[mask_from_rule_atoms(df, rule_atoms)]
    cand: dict[str, Any] = {
        "candidate_id": f"RF{candidate_idx:02d}",
        "tier": "discovery",
        "source": source,
        "rule_atoms": list(rule_atoms),
        **{k: v for k, v in full.items() if k not in ("rule_atoms", "split")},
        "is_stats": {k: v for k, v in is_eval.items() if k not in ("rule_atoms", "label", "rejections", "pass_gates")},
        "oos_stats": {k: v for k, v in oos_eval.items() if k not in ("rule_atoms", "label", "rejections", "pass_gates")},
        "miss_recall": miss_list_recall(df, rule_atoms, miss_rows),
        "calendar_days": sorted(calendar_day_set(sub)) if len(sub) else [],
    }
    if train_meta:
        cand.update(train_meta)
    return cand


def greedy_select_candidates(
    candidates: list[dict[str, Any]],
    *,
    jaccard_max: float = 0.32,
) -> list[dict[str, Any]]:
    """Pick non-overlapping lanes by mean_3d · low Jaccard on calendar days."""
    pool = [c for c in candidates if c.get("pass_gates")]
    pool.sort(key=lambda x: (-float(x.get("mean_3d", 0)), -int(x.get("n", 0))))
    selected: list[dict[str, Any]] = []
    day_sets: list[set[str]] = []

    for cand in pool:
        days = set(cand.get("calendar_days") or [])
        if not days:
            continue
        if any(jaccard(days, ds) > jaccard_max for ds in day_sets):
            continue
        day_sets.append(days)
        selected.append(cand)
    return selected


def run_rulefit_discovery(
    *,
    panel_path: Path | None = None,
    miss_path: Path | None = None,
    out_dir: Path | None = None,
    train_end: str = "2024-12-31",
    label_threshold: float = 3.0,
    max_depth: int = 4,
    min_samples_leaf: int = 15,
    min_pos_rate: float = 0.45,
) -> dict[str, Any]:
    topic = load_discovery_topic()
    sweep = topic.get("sweep") or {}
    gates = dict(sweep.get("gates") or {})
    gates.setdefault("jaccard_max", float(sweep.get("jaccard_max", 0.32)))
    cfg = load_funnel_lanes_3d_config()
    discovery = cfg.get("discovery") or {}
    gates.setdefault("min_combo_size", int(discovery.get("min_combo_size", 2)))
    gates.setdefault("max_combo_size", int(discovery.get("max_combo_size", 6)))

    df = load_feature_panel(panel_path)
    atom_cols = atom_columns(df)
    if not atom_cols:
        raise ValueError("feature panel has no atom_* columns")

    strict_defs = list((cfg.get("lanes") or {}).get("strict") or [])
    strict_day_sets = list(_lane_day_sets(df, strict_defs).values())
    all_lane_defs = strict_defs + list((cfg.get("lanes") or {}).get("coverage") or [])

    miss_rows: list[dict[str, Any]] = []
    mp = miss_path or DEFAULT_MISS_PATH
    if mp.is_file():
        miss_rows = json.loads(mp.read_text(encoding="utf-8"))

    train = df[df["date"] <= train_end].copy()
    oos = df[df["date"] > train_end].copy()
    years_full = _years_span(df["date"])
    years_is = _years_span(train["date"]) if len(train) else years_full
    years_oos = _years_span(oos["date"]) if len(oos) else 1.0

    min_atoms = int(gates["min_combo_size"])
    max_atoms = int(gates["max_combo_size"])

    rule_specs: list[tuple[tuple[str, ...], str, dict[str, Any] | None]] = []
    seen_atoms: set[tuple[str, ...]] = set()

    def _add_rules(rules: list[dict[str, Any]], source: str) -> None:
        for rule in rules:
            key = tuple(sorted(rule["rule_atoms"]))
            if key not in seen_atoms:
                seen_atoms.add(key)
                meta = {k: v for k, v in rule.items() if k != "rule_atoms"}
                rule_specs.append((key, source, meta))

    _add_rules(
        _fit_tree_rules(
            train, atom_cols, label_threshold=label_threshold,
            max_depth=max_depth, min_samples_leaf=min_samples_leaf,
            min_pos_rate=min_pos_rate, min_atoms=min_atoms, max_atoms=max_atoms,
        ),
        "decision_tree_ret3",
    )
    _add_rules(
        _fit_tree_rules(
            train, atom_cols, label_threshold=5.0,
            max_depth=max_depth, min_samples_leaf=min_samples_leaf,
            min_pos_rate=0.35, min_atoms=min_atoms, max_atoms=max_atoms,
            random_state=7,
        ),
        "decision_tree_ret5",
    )
    for combo in miss_profile_combos(df, miss_rows):
        key = tuple(sorted(combo))
        if key not in seen_atoms:
            seen_atoms.add(key)
            rule_specs.append((key, "miss_profile", None))

    candidates: list[dict[str, Any]] = []
    for i, (atoms, source, train_meta) in enumerate(rule_specs, 1):
        candidates.append(
            _build_candidate(
                df, train, oos, atoms, source=source, candidate_idx=i,
                gates=gates, strict_day_sets=strict_day_sets, all_lane_defs=all_lane_defs,
                miss_rows=miss_rows, years_full=years_full, years_is=years_is, years_oos=years_oos,
                train_meta=train_meta,
            )
        )

    passed = [c for c in candidates if c.get("pass_gates")]
    rejected = [c for c in candidates if not c.get("pass_gates")]
    rejected.sort(
        key=lambda c: (
            -(c.get("miss_recall") or {}).get("n_hit", 0),
            -float(c.get("mean_3d", 0)),
        )
    )
    greedy = greedy_select_candidates(passed, jaccard_max=float(gates["jaccard_max"]))
    for j, g in enumerate(greedy, 1):
        g["greedy_id"] = f"G{j:02d}"

    # H-DL seed baseline on live panel
    panel_db, _ = build_pre_release_funnel_panel_3d()
    seed_results: list[dict[str, Any]] = []
    for params in _expand_seed_grid(sweep):
        seed_results.append(evaluate_seed(panel_db, params, gates=gates))
    seed_df = pd.DataFrame(seed_results)
    seed_top = seed_df.sort_values("mean_3d", ascending=False).head(10).to_dict(orient="records") if len(seed_df) else []

    today = date.today().isoformat()
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"rulefit_lanes_{today.replace('-', '')}.json"

    payload: dict[str, Any] = {
        "meta": {
            "generated_on": today,
            "method": "DecisionTreeClassifier",
            "panel_path": str((panel_path or DEFAULT_PANEL_PATH).relative_to(PROJECT_ROOT)),
            "n_rows": len(df),
            "n_train": len(train),
            "n_oos": len(oos),
            "train_end": train_end,
            "label": f"ret_3d>={label_threshold}",
            "max_depth": max_depth,
            "min_samples_leaf": min_samples_leaf,
            "atom_features": len(atom_cols),
            "n_raw_rules": len(rule_specs),
            "n_pass_gates": len(passed),
            "n_greedy_selected": len(greedy),
            "gates": gates,
            "sources": {
                "decision_tree_ret3": sum(1 for _, s, _ in rule_specs if s == "decision_tree_ret3"),
                "decision_tree_ret5": sum(1 for _, s, _ in rule_specs if s == "decision_tree_ret5"),
                "miss_profile": sum(1 for _, s, _ in rule_specs if s == "miss_profile"),
            },
        },
        "top_miss_recall_rejected": rejected[:10],
        "greedy_selected": greedy,
        "candidates_passed": passed,
        "candidates_rejected": rejected,
        "hdl_seed_top10": seed_top,
        "miss_list_path": str(mp.relative_to(PROJECT_ROOT)) if mp.is_file() else None,
    }

    out_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / f"rulefit_lanes_{today.replace('-', '')}.md"
    md_path.write_text(_render_md(payload), encoding="utf-8")
    payload["artifacts"] = {
        "json": str(out_path.relative_to(PROJECT_ROOT)),
        "report_md": str(md_path.relative_to(PROJECT_ROOT)),
    }
    return payload


def _render_md(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    lines = [
        "# RRG Lane RuleFit Discovery (P2)",
        "",
        f"- **Generated**: {meta['generated_on']}",
        f"- **Panel**: {meta['n_rows']} rows · train≤{meta['train_end']} n={meta['n_train']}",
        f"- **Label**: {meta['label']} · max_depth={meta['max_depth']} · min_leaf={meta['min_samples_leaf']}",
        f"- **Raw rules**: {meta['n_raw_rules']} · pass gates: {meta['n_pass_gates']} · greedy: {meta['n_greedy_selected']}",
        "",
        "## Greedy selected",
        "",
    ]
    if payload.get("greedy_selected"):
        lines.append("| id | atoms | n | mean_3d | win_3d | jaccard | miss recall |")
        lines.append("|----|-------|---|---------|--------|---------|-------------|")
        for g in payload["greedy_selected"]:
            mr = g.get("miss_recall") or {}
            lines.append(
                f"| {g.get('greedy_id', '?')} | {g.get('label', '?')} | {g.get('n')} | "
                f"{g.get('mean_3d'):+.2f}% | {g.get('win_3d', 0):.1%} | "
                f"{g.get('jaccard_max_vs_strict', 0):.2f} | {mr.get('n_hit', 0)}/{mr.get('n_miss', 0)} |"
            )
    else:
        lines.append("（無通過 greedy 選取的 lane）")

    lines.extend(["", "## Top miss-recall rejected", ""])
    for c in (payload.get("top_miss_recall_rejected") or [])[:8]:
        mr = c.get("miss_recall") or {}
        lines.append(
            f"- `{c.get('label')}` ({c.get('source')}) · miss {mr.get('n_hit', 0)}/{mr.get('n_miss', 0)} · "
            f"mean={c.get('mean_3d'):+.2f}% · {', '.join(c.get('rejections') or [])}"
        )

    lines.extend(["", "## Top rejections (sample)", ""])
    rej = payload.get("candidates_rejected") or []
    for c in rej[:8]:
        lines.append(f"- `{c.get('label')}` · {', '.join(c.get('rejections') or [])}")

    lines.extend(["", "## H-DL seed top (reference)", ""])
    for s in (payload.get("hdl_seed_top10") or [])[:5]:
        lines.append(
            f"- `{s.get('seed_id')}` n={s.get('n')} mean={s.get('mean_3d'):+.2f}% "
            f"pass={'✅' if s.get('pass_all') else '—'}"
        )
    return "\n".join(lines)
