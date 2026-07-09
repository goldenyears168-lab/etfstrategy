"""P5 · RRG streak cohort panel · streak-level estimand for lane ML.

Estimand (analysis unit)
------------------------
One row per **(sid, d0, streak_start, streak_end)** where ``d0…d3`` are four
consecutive sessions with three up-closes (``c1>c0``, ``c2>c1``, ``c3>c2``).
Features attach at **signal_date** — the PIT pre-release signal on or before
``streak_start`` (``_find_pre_release_signal``), not at arbitrary pool stock-days.

Label (≠ pool-wide unconditional ``ret_3d``)
-------------------------------------------
``hit_strong_streak`` generalizes the June 2026 Improving→Leading streak cases:

  ``streak_total_pct >= STRONG_STREAK_PCT`` (default 15 % over D0→D3)

When ``in_pre_release_pool``, the June-type extension also requires signal-day
hold quality: ``ret_3d >= min_ret_3d_hit`` **or** ``max_ret_3d >= min_ret_3d_hit``.

Cohort spans **all months/years** in ``[year_start, year_end]`` (default 2015–today),
not June-only diagnostics.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.tree import DecisionTreeClassifier

from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_improving_s3rv_executable import enrich_lifecycle_events
from research.backtest.archive.rrg_lane_discovery_loop import (
    _load_lane_defs,
)
from research.backtest.archive.rrg_pre_release_funnel_lanes import lane_mask
from research.backtest.archive.rrg_pre_release_funnel_lanes_3d import (
    CONTINUOUS_FEATURE_COLS,
    EXTRA_FEATURE_COLS,
    _years_span,
    build_pre_release_funnel_panel_3d,
    enrich_events_3d_outcomes,
    load_funnel_lanes_3d_config,
    search_atom_ids,
    _precompute_atom_columns,
)
from stock_db import PROJECT_ROOT

STRONG_STREAK_PCT = 15.0
DEFAULT_YEAR_START = 2015
DEFAULT_TRAIN_END_YEAR = 2023
DEFAULT_OUT_DIR = PROJECT_ROOT / "reports/research/rrg-lane-foundation/artifacts"
DEFAULT_MISS_PATH = PROJECT_ROOT / "reports/research/rrg/lane_discovery_miss_list_20260701.json"
DEFAULT_POOL_PANEL_PATH = DEFAULT_OUT_DIR / "pre_release_feature_panel.parquet"

CONTINUOUS_MODEL_COLS: tuple[str, ...] = (
    "sig_ret",
    "excess",
    "bench_today_ret",
    "improve_streak",
    "rv",
    "disp_d",
    "session_gap_days",
    "sid_hist_n",
)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj


def detect_three_day_up_streaks(
    close: pd.DataFrame,
    full_dates: list[str],
    *,
    year_start: int = DEFAULT_YEAR_START,
    year_end: int | None = None,
) -> pd.DataFrame:
    """All 3-session up-close windows · no ``min_total_pct`` pre-filter."""
    year_end = year_end or date.today().year
    years = np.array([int(d[:4]) for d in full_dates])
    year_ok = (years >= year_start) & (years <= year_end)
    rows: list[dict[str, Any]] = []
    arr = close.reindex(full_dates).to_numpy(dtype=float)
    sids = [str(c) for c in close.columns]
    n_dates = len(full_dates)
    for j, sid in enumerate(sids):
        col = arr[:, j]
        if np.nanmin(col) <= 0:
            valid = col > 0
        else:
            valid = np.ones(n_dates, dtype=bool)
        up3 = (
            valid[3:]
            & valid[2:-1]
            & valid[1:-2]
            & valid[:-3]
            & (col[1:-2] > col[:-3])
            & (col[2:-1] > col[1:-2])
            & (col[3:] > col[2:-1])
            & year_ok[3:]
        )
        idxs = np.flatnonzero(up3) + 3
        if idxs.size == 0:
            continue
        c0 = col[idxs - 3]
        c3 = col[idxs]
        total_pct = (c3 / c0 - 1) * 100
        for k, i in enumerate(idxs):
            rows.append(
                {
                    "sid": sid,
                    "d0": full_dates[i - 3],
                    "streak_start": full_dates[i - 2],
                    "streak_end": full_dates[i],
                    "streak_total_pct": round(float(total_pct[k]), 4),
                }
            )
    return pd.DataFrame(rows)


def compute_hit_strong_streak(
    streak_total_pct: float,
    *,
    in_pre_release_pool: bool,
    ret_3d: float | None,
    max_ret_3d: float | None,
    strong_pct: float = STRONG_STREAK_PCT,
    min_ret_3d_hit: float = 0.0,
) -> bool:
    """June-type strong streak label on a single streak unit."""
    if streak_total_pct < strong_pct:
        return False
    if not in_pre_release_pool:
        return True
    if ret_3d is not None and float(ret_3d) >= min_ret_3d_hit:
        return True
    if max_ret_3d is not None and float(max_ret_3d) >= min_ret_3d_hit:
        return True
    return False


def _signal_feature_row(
    full_idx: pd.DataFrame,
    panel_idx: pd.DataFrame,
    sid: str,
    signal_d: str | None,
) -> pd.Series | None:
    if not signal_d:
        return None
    key = (sid, signal_d)
    if key in panel_idx.index:
        row = panel_idx.loc[key]
        return row.iloc[-1] if isinstance(row, pd.DataFrame) else row
    if key in full_idx.index:
        row = full_idx.loc[key]
        return row.iloc[-1] if isinstance(row, pd.DataFrame) else row
    return None


def _attach_atoms(feat_df: pd.DataFrame, atom_ids: list[str]) -> pd.DataFrame:
    if feat_df.empty:
        return feat_df
    out = feat_df.copy()
    for atom_id, mask in _precompute_atom_columns(feat_df, atom_ids).items():
        out[f"atom_{atom_id}"] = mask.astype(np.int8)
    return out


def _build_lane_lookup(
    panel: pd.DataFrame,
    lane_masks: dict[str, pd.Series],
) -> dict[tuple[str, str], list[str]]:
    lookup: dict[tuple[str, str], list[str]] = {}
    for lid, mask in lane_masks.items():
        sub = panel.loc[mask, ["sid", "date"]]
        for sid, sd in zip(sub["sid"].astype(str), sub["date"].astype(str)):
            lookup.setdefault((sid, sd), []).append(lid)
    return lookup


def _resolve_pre_release_signal_indexed(
    panel_dates: dict[str, pd.Series],
    full_sorted_dates: list[str],
    full_rows_by_date: dict[str, pd.Series],
    interval_start: str,
    *,
    lookback_sessions: int = 5,
) -> tuple[str | None, pd.Series | None]:
    from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT

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

    if interval_start in panel_dates:
        return interval_start, panel_dates[interval_start]

    prior = [d for d in full_sorted_dates if d < interval_start][-lookback_sessions:]
    for d in reversed(prior):
        if d in panel_dates:
            return d, panel_dates[d]
        row = full_rows_by_date.get(d)
        if row is not None and _pool_ok(row):
            continue
    return None, None


def _build_signal_map(
    keys: pd.DataFrame,
    panel: pd.DataFrame,
    full: pd.DataFrame,
) -> dict[tuple[str, str], tuple[str | None, pd.Series | None]]:
    panel_by_sid = {
        str(s): {str(r["date"]): r for r in g.to_dict("records")}
        for s, g in panel.groupby("sid", sort=False)
    }
    full_by_sid = {
        str(s): g.sort_values("date")
        for s, g in full.groupby("sid", sort=False)
    }
    signal_map: dict[tuple[str, str], tuple[str | None, pd.Series | None]] = {}
    for sid, grp in keys.groupby(keys["sid"].astype(str), sort=False):
        panel_dates = panel_by_sid.get(sid, {})
        full_sid = full_by_sid.get(sid, pd.DataFrame())
        if full_sid.empty:
            full_sorted: list[str] = []
            full_rows: dict[str, pd.Series] = {}
        else:
            full_sorted = full_sid["date"].astype(str).tolist()
            full_rows = {str(r["date"]): r for _, r in full_sid.iterrows()}
        panel_series = {d: pd.Series(v) for d, v in panel_dates.items()}
        for streak_start in grp["streak_start"].astype(str):
            signal_map[(sid, streak_start)] = _resolve_pre_release_signal_indexed(
                panel_series,
                full_sorted,
                full_rows,
                streak_start,
            )
    return signal_map


def _enrich_streak_rows(
    streaks: pd.DataFrame,
    panel: pd.DataFrame,
    full: pd.DataFrame,
    lane_masks: dict[str, pd.Series],
    *,
    strong_pct: float,
    min_ret_3d_hit: float,
) -> pd.DataFrame:
    lane_lookup = _build_lane_lookup(panel, lane_masks)
    keys = streaks[["sid", "streak_start"]].drop_duplicates()
    signal_map = _build_signal_map(keys, panel, full)

    rows: list[dict[str, Any]] = []
    for st in streaks.itertuples(index=False):
        sid = str(st.sid)
        signal_d, pr_row = signal_map.get((sid, str(st.streak_start)), (None, None))
        in_pool = pr_row is not None
        ret_3d = float(pr_row["ret_3d"]) if in_pool else None
        max_ret_3d = float(pr_row.get("max_ret_3d", ret_3d)) if in_pool else None
        min_ret_3d = float(pr_row.get("min_ret_3d", ret_3d)) if in_pool else None
        hit = compute_hit_strong_streak(
            float(st.streak_total_pct),
            in_pre_release_pool=in_pool,
            ret_3d=ret_3d,
            max_ret_3d=max_ret_3d,
            strong_pct=strong_pct,
            min_ret_3d_hit=min_ret_3d_hit,
        )
        matched: list[str] = []
        if in_pool and signal_d:
            matched = list(lane_lookup.get((sid, str(signal_d)), []))
        rows.append(
            {
                "sid": sid,
                "d0": st.d0,
                "streak_start": st.streak_start,
                "streak_end": st.streak_end,
                "streak_total_pct": st.streak_total_pct,
                "signal_date": signal_d,
                "signal_year": int(str(signal_d)[:4]) if signal_d else None,
                "in_pre_release_pool": in_pool,
                "ret_3d": round(ret_3d, 4) if ret_3d is not None else None,
                "max_ret_3d": round(max_ret_3d, 4) if max_ret_3d is not None else None,
                "min_ret_3d": round(min_ret_3d, 4) if min_ret_3d is not None else None,
                "hit_strong_streak": bool(hit),
                "matched_lanes": matched,
            }
        )
    return pd.DataFrame(rows)


def _join_signal_features(
    out: pd.DataFrame,
    full: pd.DataFrame,
    panel: pd.DataFrame,
    atom_ids: list[str],
) -> pd.DataFrame:
    keys = out.loc[out["signal_date"].notna(), ["sid", "signal_date"]].drop_duplicates()
    if keys.empty:
        return out

    full_idx = full.set_index(["sid", "date"])
    panel_idx = panel.set_index(["sid", "date"], drop=False)
    feat_rows: list[pd.Series] = []
    feat_keys: list[tuple[str, str]] = []
    for sid, sd in keys.itertuples(index=False):
        row = _signal_feature_row(full_idx, panel_idx, str(sid), str(sd))
        if row is not None:
            feat_rows.append(row)
            feat_keys.append((str(sid), str(sd)))
    if not feat_rows:
        return out

    feat_df = pd.DataFrame(feat_rows)
    feat_df["sid"] = [k[0] for k in feat_keys]
    feat_df["date"] = [k[1] for k in feat_keys]
    feat_df = _attach_atoms(feat_df, atom_ids)
    cont_cols = [c for c in CONTINUOUS_FEATURE_COLS if c in feat_df.columns]
    extra_cols = [c for c in EXTRA_FEATURE_COLS if c in feat_df.columns]
    atom_cols = [f"atom_{a}" for a in atom_ids if f"atom_{a}" in feat_df.columns]
    join_cols = ["sid", "date", *cont_cols, *extra_cols, *atom_cols]
    feat_df = feat_df[join_cols].rename(columns={"date": "signal_date"})
    return out.merge(feat_df, on=["sid", "signal_date"], how="left")


def build_streak_cohort_panel(
    conn: Any | None = None,
    *,
    year_start: int = DEFAULT_YEAR_START,
    year_end: int | None = None,
    strong_pct: float = STRONG_STREAK_PCT,
    min_ret_3d_hit: float = 0.0,
    lane_tiers: tuple[str, ...] = ("strict", "coverage"),
    cfg: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Streak-level feature panel · all years · continuous + optional atom columns."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    cfg = cfg or load_funnel_lanes_3d_config()
    year_end = year_end or date.today().year

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    streaks = detect_three_day_up_streaks(
        close, full_dates, year_start=year_start, year_end=year_end
    )
    if streaks.empty:
        return streaks, {"n_rows": 0, "estimand": "streak_cohort"}

    base, _ = enrich_lifecycle_events(conn)
    full, _ = enrich_events_3d_outcomes(conn, events=base)
    full = full.sort_values(["sid", "date"]).copy()
    full["prev_end_q"] = full.groupby("sid")["end_q"].shift(1).fillna("")
    panel, _ = build_pre_release_funnel_panel_3d(conn=conn, events=full)

    lane_defs = _load_lane_defs(tiers=lane_tiers)
    lane_masks = {str(ld["id"]): lane_mask(panel, ld["rule_atoms"]) for ld in lane_defs}
    atom_ids = search_atom_ids(cfg)

    out = _enrich_streak_rows(
        streaks,
        panel,
        full,
        lane_masks,
        strong_pct=strong_pct,
        min_ret_3d_hit=min_ret_3d_hit,
    )
    out = _join_signal_features(out, full, panel, atom_ids)

    meta: dict[str, Any] = {
        "estimand": "streak_cohort",
        "analysis_unit": "(sid, d0, streak_start, streak_end)",
        "feature_anchor": "signal_date",
        "label": "hit_strong_streak",
        "strong_streak_pct": strong_pct,
        "min_ret_3d_hit": min_ret_3d_hit,
        "year_start": year_start,
        "year_end": year_end,
        "n_rows": len(out),
        "n_hit_strong_streak": int(out["hit_strong_streak"].sum()),
        "hit_rate": round(float(out["hit_strong_streak"].mean()), 6) if len(out) else 0.0,
        "n_in_pre_release_pool": int(out["in_pre_release_pool"].sum()),
        "pool_hit_rate": round(
            float(out.loc[out["in_pre_release_pool"], "hit_strong_streak"].mean()), 6
        )
        if out["in_pre_release_pool"].any()
        else 0.0,
        "years_span": round(_years_span(out["streak_end"]), 4),
        "atom_ids": atom_ids,
        "continuous_columns": [c for c in CONTINUOUS_MODEL_COLS if c in out.columns],
        "config_version": cfg.get("version"),
    }
    return out, meta


def export_streak_cohort_panel(
    out_path: Path | None = None,
    *,
    conn: Any | None = None,
    fmt: str = "parquet",
    **build_kw: Any,
) -> dict[str, Any]:
    out_path = Path(out_path or DEFAULT_OUT_DIR / "streak_cohort_panel.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df, meta = build_streak_cohort_panel(conn=conn, **build_kw)
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
    meta_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    payload["meta_path"] = str(meta_path.relative_to(PROJECT_ROOT))
    return payload


def _load_miss_keys(miss_path: Path | None) -> list[tuple[str, str]]:
    mp = miss_path or DEFAULT_MISS_PATH
    if not mp.is_file():
        return []
    raw = json.loads(mp.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("misses") or raw.get("rows") or []
    keys: list[tuple[str, str]] = []
    for row in rows:
        sid = str(row.get("sid") or "")
        sd = str(row.get("signal_date") or "")
        if sid and sd:
            keys.append((sid, sd))
    return keys


def _pool_baseline_label_rate(
    pool_path: Path | None,
    cohort: pd.DataFrame,
    *,
    strong_pct: float,
) -> dict[str, Any]:
    pp = pool_path or DEFAULT_POOL_PANEL_PATH
    if not pp.is_file():
        return {"available": False, "reason": f"pool panel missing: {pp}"}
    pool = pd.read_parquet(pp)
    pool_n = len(pool)
    strong_signals = cohort.loc[cohort["hit_strong_streak"] & cohort["signal_date"].notna()]
    signal_days = strong_signals.drop_duplicates(subset=["sid", "signal_date"])
    pool_keys = set(zip(pool["sid"].astype(str), pool["date"].astype(str)))
    cohort_strong_keys = set(
        zip(signal_days["sid"].astype(str), signal_days["signal_date"].astype(str))
    )
    overlap = cohort_strong_keys & pool_keys
    return {
        "available": True,
        "pool_n_stock_days": pool_n,
        "cohort_n_streak_units": len(cohort),
        "cohort_hit_rate": round(float(cohort["hit_strong_streak"].mean()), 6),
        "pool_baseline_strong_signal_days": len(overlap),
        "pool_baseline_rate": round(len(overlap) / pool_n, 8) if pool_n else 0.0,
        "streak_units_per_pool_day": round(len(cohort) / pool_n, 6) if pool_n else 0.0,
        "strong_pct_threshold": strong_pct,
    }


def _fit_binary_model(
    train: pd.DataFrame,
    test: pd.DataFrame,
    feature_cols: list[str],
    *,
    model_kind: str,
    random_state: int = 42,
) -> dict[str, Any]:
    cols = [c for c in feature_cols if c in train.columns and c in test.columns]
    if not cols:
        return {"model": model_kind, "features": feature_cols, "status": "no_features"}

    tr = train.dropna(subset=cols + ["hit_strong_streak"])
    te = test.dropna(subset=cols + ["hit_strong_streak"])
    if len(tr) < 30 or len(te) < 10 or tr["hit_strong_streak"].nunique() < 2:
        return {
            "model": model_kind,
            "features": cols,
            "status": "insufficient_data",
            "n_train": len(tr),
            "n_test": len(te),
        }

    x_tr = tr[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y_tr = tr["hit_strong_streak"].astype(int)
    x_te = te[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y_te = te["hit_strong_streak"].astype(int)

    if model_kind == "logistic":
        clf: Any = LogisticRegression(max_iter=500, random_state=random_state)
    else:
        clf = DecisionTreeClassifier(
            max_depth=4, min_samples_leaf=15, random_state=random_state
        )
    clf.fit(x_tr, y_tr)
    pred = clf.predict(x_te)
    prob = clf.predict_proba(x_te)[:, 1] if hasattr(clf, "predict_proba") else pred.astype(float)

    out: dict[str, Any] = {
        "model": model_kind,
        "features": cols,
        "status": "ok",
        "n_train": len(tr),
        "n_test": len(te),
        "train_pos_rate": round(float(y_tr.mean()), 4),
        "test_pos_rate": round(float(y_te.mean()), 4),
        "accuracy": round(float(accuracy_score(y_te, pred)), 4),
    }
    try:
        out["auc"] = round(float(roc_auc_score(y_te, prob)), 4)
    except ValueError:
        out["auc"] = None
    return out


def analyze_streak_cohort(
    cohort: pd.DataFrame,
    *,
    train_end_year: int = DEFAULT_TRAIN_END_YEAR,
    miss_path: Path | None = None,
    pool_panel_path: Path | None = None,
    strong_pct: float = STRONG_STREAK_PCT,
) -> dict[str, Any]:
    """Lightweight P5 analysis · baseline · miss recall · IS/OOS binary models."""
    df = cohort.copy()
    if "signal_year" not in df.columns and "signal_date" in df.columns:
        df["signal_year"] = pd.to_numeric(df["signal_date"].astype(str).str[:4], errors="coerce")

    miss_keys = _load_miss_keys(miss_path)
    cohort_keys = {
        (str(r["sid"]), str(r["signal_date"]))
        for _, r in df.iterrows()
        if r.get("signal_date") and not pd.isna(r["signal_date"])
    }
    miss_hits = [k for k in miss_keys if k in cohort_keys]
    miss_hit_strong = [
        k
        for k in miss_keys
        if k in cohort_keys
        and bool(
            df[
                (df["sid"].astype(str) == k[0])
                & (df["signal_date"].astype(str) == k[1])
            ]["hit_strong_streak"].iloc[0]
        )
    ]

    baseline = _pool_baseline_label_rate(pool_panel_path, df, strong_pct=strong_pct)

    train = df[df["signal_year"] <= train_end_year]
    test = df[df["signal_year"] > train_end_year]
    atom_cols = sorted(c for c in df.columns if c.startswith("atom_"))
    cont_cols = [c for c in CONTINUOUS_MODEL_COLS if c in df.columns]

    models: dict[str, Any] = {}
    for feat_set, cols in (
        ("continuous", cont_cols),
        ("atoms", atom_cols),
        ("both", cont_cols + atom_cols),
    ):
        models[feat_set] = {
            "logistic": _fit_binary_model(train, test, cols, model_kind="logistic"),
            "decision_tree": _fit_binary_model(train, test, cols, model_kind="decision_tree"),
        }

    cont_auc = models.get("continuous", {}).get("logistic", {}).get("auc")
    atom_auc = models.get("atoms", {}).get("logistic", {}).get("auc")
    continuous_beats_atoms = (
        cont_auc is not None and atom_auc is not None and cont_auc > atom_auc
    )

    return _json_safe(
        {
            "generated_on": date.today().isoformat(),
            "estimand": "streak_cohort",
            "n_streak_units": len(df),
            "hit_strong_streak_rate": round(float(df["hit_strong_streak"].mean()), 6),
            "in_pool_rate": round(float(df["in_pre_release_pool"].mean()), 6),
            "pool_subset_hit_rate": round(
                float(df.loc[df["in_pre_release_pool"], "hit_strong_streak"].mean()), 6
            )
            if df["in_pre_release_pool"].any()
            else 0.0,
            "baseline_vs_pool": baseline,
            "miss_list": {
                "n_miss_keys": len(miss_keys),
                "n_in_cohort": len(miss_hits),
                "recall_in_cohort": round(len(miss_hits) / len(miss_keys), 4)
                if miss_keys
                else None,
                "n_hit_strong_streak": len(miss_hit_strong),
                "recall_hit_strong": round(len(miss_hit_strong) / len(miss_keys), 4)
                if miss_keys
                else None,
            },
            "is_oos_split": {
                "train_end_year": train_end_year,
                "n_train": len(train),
                "n_test": len(test),
            },
            "models": models,
            "continuous_beats_atoms_logistic_auc": continuous_beats_atoms,
            "verdict": (
                "continuous_features_beat_atoms"
                if continuous_beats_atoms
                else "atoms_not_worse_or_inconclusive"
            ),
        }
    )


def write_streak_cohort_analysis(
    cohort: pd.DataFrame,
    out_dir: Path | None = None,
    *,
    analysis: dict[str, Any] | None = None,
    **analyze_kw: Any,
) -> dict[str, str]:
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    analysis = analysis or analyze_streak_cohort(cohort, **analyze_kw)

    json_path = out_dir / f"streak_cohort_analysis_{today}.json"
    json_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    bl = analysis.get("baseline_vs_pool") or {}
    miss = analysis.get("miss_list") or {}
    lines = [
        "# RRG streak cohort analysis (P5)",
        "",
        f"Generated: {analysis.get('generated_on')}",
        "",
        "## Estimand",
        "- Unit: `(sid, d0, streak_start, streak_end)` · features at `signal_date`",
        "- Label: `hit_strong_streak` (≥15% 3-session streak · June-type generalization)",
        "",
        "## Cohort",
        f"- Streak units: **{analysis.get('n_streak_units')}**",
        f"- Hit rate: **{analysis.get('hit_strong_streak_rate')}**",
        f"- In pre-release pool: **{analysis.get('in_pool_rate')}**",
        "",
        "## vs pool baseline",
        f"- Pool stock-days: {bl.get('pool_n_stock_days', 'n/a')}",
        f"- Strong-signal days in pool / pool_n: **{bl.get('pool_baseline_rate', 'n/a')}**",
        f"- Cohort hit rate: **{bl.get('cohort_hit_rate', analysis.get('hit_strong_streak_rate'))}**",
        "",
        "## Miss-list recall (June holdout diagnostic)",
        f"- Miss keys: {miss.get('n_miss_keys')} · in cohort: **{miss.get('n_in_cohort')}** "
        f"(recall {miss.get('recall_in_cohort')})",
        f"- Hit strong streak: **{miss.get('n_hit_strong_streak')}** "
        f"(recall {miss.get('recall_hit_strong')})",
        "",
        "## IS/OOS models (hit_strong_streak)",
    ]
    split = analysis.get("is_oos_split") or {}
    lines.append(
        f"Train ≤{split.get('train_end_year')}: n={split.get('n_train')} · "
        f"OOS: n={split.get('n_test')}"
    )
    for feat_set, runs in (analysis.get("models") or {}).items():
        lines.append(f"\n### {feat_set}")
        for mk, res in runs.items():
            lines.append(
                f"- {mk}: auc={res.get('auc')} acc={res.get('accuracy')} "
                f"status={res.get('status')}"
            )
    lines.extend(
        [
            "",
            f"## Verdict",
            f"- **{analysis.get('verdict')}**",
        ]
    )
    md_path = out_dir / f"streak_cohort_analysis_{today}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path.relative_to(PROJECT_ROOT)), "md": str(md_path.relative_to(PROJECT_ROOT))}
