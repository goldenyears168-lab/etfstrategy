"""RRG pre-release pool Top-K ranking · LightGBM + walk-forward (P3 · H-ML-3).

Within daily pre-release pool, rank by ŷ(ret_3d) · evaluate OOS Rank IC and Top-K mean excess.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from rank_stats import icir, spearman_correlation
from research.backtest.rrg_lane_rulefit import (
    DEFAULT_MISS_PATH,
    DEFAULT_OUT_DIR,
    DEFAULT_PANEL_PATH,
    atom_columns,
    load_feature_panel,
)
from research.backtest.tw100_walk_forward import _ic_stats
from research_config import load_research_splits
from stock_db import PROJECT_ROOT

DEFAULT_SPLIT_ID = "walk_forward_3y1y"
HML3_IC_MIN = 0.03
HML3_TOPK_EXCESS_MIN_PP = 0.5


@dataclass(frozen=True)
class YearWalkForwardFold:
    fold_id: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_years: tuple[str, ...]
    test_years: tuple[str, ...]


@dataclass(frozen=True)
class PoolTopkConfig:
    panel_path: Path = DEFAULT_PANEL_PATH
    split_spec_id: str = DEFAULT_SPLIT_ID
    top_k: int = 3
    min_cross_section: int = 15
    valid_ratio: float = 0.2
    num_boost_round: int = 500
    early_stopping_rounds: int = 30
    learning_rate: float = 0.05
    num_leaves: int = 31
    max_depth: int = 5
    min_data_in_leaf: int = 20
    max_folds: int | None = None


def _import_lightgbm():
    try:
        return importlib.import_module("lightgbm")
    except ImportError as exc:
        raise ImportError(
            "lightgbm required for P3 pool Top-K · install: .venv/bin/python -m pip install 'lightgbm>=4.0'"
        ) from exc


def build_year_walk_forward_folds(
    trading_dates: list[str],
    *,
    train_years: int = 3,
    test_years: int = 1,
    max_folds: int | None = None,
) -> list[YearWalkForwardFold]:
    """Rolling year-based WFA · e.g. train 2015–2017 test 2018."""
    if not trading_dates:
        return []
    dates = sorted(set(trading_dates))
    years = sorted({d[:4] for d in dates})
    folds: list[YearWalkForwardFold] = []
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
            YearWalkForwardFold(
                fold_id=fold_id,
                train_start=train_dates[0],
                train_end=train_dates[-1],
                test_start=test_dates[0],
                test_end=test_dates[-1],
                train_years=tuple(train_y),
                test_years=tuple(test_y),
            )
        )
        fold_id += 1
        if max_folds is not None and fold_id >= max_folds:
            break
    return folds


def daily_rank_ic(
    df: pd.DataFrame,
    *,
    pred_col: str = "pred",
    label_col: str = "ret_3d",
    min_cross_section: int = 15,
) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for dt, grp in df.groupby("date"):
        sub = grp[[pred_col, label_col]].dropna()
        if len(sub) < min_cross_section:
            continue
        ic = spearman_correlation(sub[pred_col].tolist(), sub[label_col].tolist())
        if ic is not None:
            out.append((str(dt), float(ic)))
    return out


def daily_topk_stats(
    df: pd.DataFrame,
    *,
    pred_col: str = "pred",
    label_col: str = "ret_3d",
    k: int = 3,
    min_cross_section: int = 15,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dt, grp in df.groupby("date"):
        sub = grp.dropna(subset=[pred_col, label_col])
        if len(sub) < min_cross_section:
            continue
        pool_mean = float(sub[label_col].mean())
        top = sub.nlargest(min(k, len(sub)), pred_col)
        top_mean = float(top[label_col].mean())
        rows.append(
            {
                "date": str(dt),
                "n_pool": len(sub),
                "pool_mean_3d": round(pool_mean, 4),
                "topk_mean_3d": round(top_mean, 4),
                "excess_pp": round(top_mean - pool_mean, 4),
                "top_sids": top["sid"].astype(str).tolist(),
            }
        )
    return rows


def _train_valid_split(train_df: pd.DataFrame, valid_ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = sorted(train_df["date"].unique())
    valid_n = max(20, int(len(dates) * valid_ratio))
    if valid_n >= len(dates):
        valid_n = max(1, len(dates) // 5)
    valid_dates = set(dates[-valid_n:])
    valid = train_df[train_df["date"].isin(valid_dates)]
    tr = train_df[~train_df["date"].isin(valid_dates)]
    if tr.empty:
        tr = train_df.iloc[: max(1, len(train_df) - len(valid))]
        valid = train_df.iloc[len(tr) :]
    return tr, valid


def _fit_lgbm(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: list[str],
    cfg: PoolTopkConfig,
):
    lgb = _import_lightgbm()
    x_train = train_df[feature_cols].to_numpy(dtype=np.float32)
    y_train = train_df["ret_3d"].to_numpy(dtype=np.float32)
    x_valid = valid_df[feature_cols].to_numpy(dtype=np.float32)
    y_valid = valid_df["ret_3d"].to_numpy(dtype=np.float32)
    train_set = lgb.Dataset(x_train, label=y_train)
    valid_set = lgb.Dataset(x_valid, label=y_valid)
    params = {
        "objective": "regression",
        "metric": "l2",
        "learning_rate": cfg.learning_rate,
        "num_leaves": cfg.num_leaves,
        "max_depth": cfg.max_depth,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "verbosity": -1,
        "num_threads": 4,
    }
    callbacks = [lgb.log_evaluation(period=0)]
    if cfg.early_stopping_rounds > 0:
        callbacks.insert(0, lgb.early_stopping(cfg.early_stopping_rounds))
    return lgb.train(
        params,
        train_set,
        num_boost_round=cfg.num_boost_round,
        valid_sets=[valid_set],
        valid_names=["valid"],
        callbacks=callbacks,
    )


def _predict_df(df: pd.DataFrame, model, feature_cols: list[str]) -> pd.Series:
    x = df[feature_cols].to_numpy(dtype=np.float32)
    return pd.Series(model.predict(x), index=df.index)


def _fit_predict_fold(
    panel: pd.DataFrame,
    fold: YearWalkForwardFold,
    feature_cols: list[str],
    cfg: PoolTopkConfig,
) -> dict[str, Any]:
    train_all = panel[(panel["date"] >= fold.train_start) & (panel["date"] <= fold.train_end)].copy()
    test = panel[(panel["date"] >= fold.test_start) & (panel["date"] <= fold.test_end)].copy()
    if len(train_all) < 500 or len(test) < 100:
        return {"fold_id": fold.fold_id, "skipped": True, "reason": "insufficient_rows"}

    tr, va = _train_valid_split(train_all, cfg.valid_ratio)
    model = _fit_lgbm(tr, va, feature_cols, cfg)

    va = va.copy()
    test = test.copy()
    va["pred"] = _predict_df(va, model, feature_cols)
    test["pred"] = _predict_df(test, model, feature_cols)

    va_ic = daily_rank_ic(va, min_cross_section=cfg.min_cross_section)
    test_ic = daily_rank_ic(test, min_cross_section=cfg.min_cross_section)
    test_topk = daily_topk_stats(
        test, k=cfg.top_k, min_cross_section=cfg.min_cross_section
    )
    excess_vals = [r["excess_pp"] for r in test_topk]

    return {
        "fold_id": fold.fold_id,
        "train_years": list(fold.train_years),
        "test_years": list(fold.test_years),
        "train_start": fold.train_start,
        "train_end": fold.train_end,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "n_train": len(train_all),
        "n_test": len(test),
        "best_iteration": int(getattr(model, "best_iteration", 0) or 0),
        "valid_ic": _ic_stats([v for _, v in va_ic]),
        "test_ic": _ic_stats([v for _, v in test_ic]),
        "test_topk": {
            "k": cfg.top_k,
            "n_days": len(test_topk),
            "mean_excess_pp": round(float(np.mean(excess_vals)), 4) if excess_vals else None,
            "median_excess_pp": round(float(np.median(excess_vals)), 4) if excess_vals else None,
        },
        "test_ic_daily": [{"date": d, "ic": round(v, 6)} for d, v in test_ic],
        "test_topk_daily": test_topk,
    }


def _aggregate_fold_metrics(folds: list[dict[str, Any]]) -> dict[str, Any]:
    active = [f for f in folds if not f.get("skipped")]
    ic_vals: list[float] = []
    excess_vals: list[float] = []
    for f in active:
        for row in f.get("test_ic_daily") or []:
            ic_vals.append(float(row["ic"]))
        tk = f.get("test_topk") or {}
        if tk.get("mean_excess_pp") is not None and tk.get("n_days", 0) > 0:
            for row in f.get("test_topk_daily") or []:
                excess_vals.append(float(row["excess_pp"]))

    ic_mean = round(float(np.mean(ic_vals)), 6) if ic_vals else None
    excess_mean = round(float(np.mean(excess_vals)), 4) if excess_vals else None
    return {
        "n_folds": len(active),
        "oos_ic_mean": ic_mean,
        "oos_icir": round(icir(ic_vals), 6) if ic_vals and icir(ic_vals) is not None else None,
        "oos_ic_n_days": len(ic_vals),
        "oos_topk_excess_mean_pp": excess_mean,
        "oos_topk_n_days": len(excess_vals),
    }


def _evaluate_hml3(agg: dict[str, Any]) -> dict[str, Any]:
    ic = agg.get("oos_ic_mean")
    ex = agg.get("oos_topk_excess_mean_pp")
    pass_ic = ic is not None and ic > HML3_IC_MIN
    pass_ex = ex is not None and ex > HML3_TOPK_EXCESS_MIN_PP
    return {
        "hypothesis_id": "H-ML-3",
        "pass_ic": pass_ic,
        "pass_topk_excess": pass_ex,
        "pass_all": pass_ic and pass_ex,
        "thresholds": {"rank_ic_min": HML3_IC_MIN, "topk_excess_pp_min": HML3_TOPK_EXCESS_MIN_PP},
        "observed": {"rank_ic_mean": ic, "topk_excess_pp_mean": ex},
    }


def _holdout_2026h1(
    panel: pd.DataFrame,
    feature_cols: list[str],
    cfg: PoolTopkConfig,
    *,
    train_end: str = "2025-12-31",
) -> dict[str, Any]:
    holdout = panel[panel["date"] >= "2026-01-01"].copy()
    train = panel[panel["date"] <= train_end].copy()
    if len(holdout) < 50 or len(train) < 500:
        return {"skipped": True, "reason": "insufficient_holdout_rows"}
    tr, va = _train_valid_split(train, cfg.valid_ratio)
    model = _fit_lgbm(tr, va, feature_cols, cfg)
    holdout["pred"] = _predict_df(holdout, model, feature_cols)
    ic_series = daily_rank_ic(holdout, min_cross_section=cfg.min_cross_section)
    topk = daily_topk_stats(holdout, k=cfg.top_k, min_cross_section=cfg.min_cross_section)
    excess = [r["excess_pp"] for r in topk]
    return {
        "holdout": "oos_2026h1",
        "n_test": len(holdout),
        "test_ic": _ic_stats([v for _, v in ic_series]),
        "test_topk_mean_excess_pp": round(float(np.mean(excess)), 4) if excess else None,
        "test_topk_n_days": len(topk),
    }


def _miss_topk_recall(
    panel: pd.DataFrame,
    miss_rows: list[dict[str, Any]],
    *,
    k: int = 3,
) -> dict[str, Any]:
    """Share of miss cases appearing in daily Top-K on signal_date."""
    if not miss_rows:
        return {"n_miss": 0, "n_in_topk": 0, "recall": None}
    hits = 0
    for row in miss_rows:
        sd, sid = str(row["signal_date"]), str(row["sid"])
        day = panel[panel["date"] == sd]
        if day.empty or "pred" not in day.columns:
            continue
        top_sids = day.nlargest(min(k, len(day)), "pred")["sid"].astype(str).tolist()
        if sid in top_sids:
            hits += 1
    n = len(miss_rows)
    return {"n_miss": n, "n_in_topk": hits, "recall": round(hits / n, 4) if n else None}


def run_pool_topk_wfa(
    cfg: PoolTopkConfig | None = None,
    *,
    miss_path: Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = cfg or PoolTopkConfig()
    panel = load_feature_panel(cfg.panel_path)
    feature_cols = atom_columns(panel)
    if not feature_cols:
        raise ValueError("panel has no atom_* features")

    split = load_research_splits().get(cfg.split_spec_id)
    if split is None:
        raise ValueError(f"unknown split_spec: {cfg.split_spec_id}")
    train_years = int(split.train_years or 3)
    test_years = int(split.test_years or 1)

    dates = sorted(panel["date"].astype(str).unique().tolist())
    folds_spec = build_year_walk_forward_folds(
        dates, train_years=train_years, test_years=test_years, max_folds=cfg.max_folds
    )
    if not folds_spec:
        raise RuntimeError("no WFA folds generated")

    fold_results = [_fit_predict_fold(panel, fold, feature_cols, cfg) for fold in folds_spec]
    agg = _aggregate_fold_metrics(fold_results)
    hml3 = _evaluate_hml3(agg)
    holdout = _holdout_2026h1(panel, feature_cols, cfg)

    miss_rows: list[dict[str, Any]] = []
    mp = miss_path or DEFAULT_MISS_PATH
    if mp.is_file():
        miss_rows = json.loads(mp.read_text(encoding="utf-8"))

    # Full-sample model for miss Top-K recall (diagnostic only · not OOS)
    tr, va = _train_valid_split(panel[panel["date"] <= "2024-12-31"], cfg.valid_ratio)
    if len(tr) >= 500:
        full_model = _fit_lgbm(tr, va, feature_cols, cfg)
        scored = panel[panel["date"] <= "2024-12-31"].copy()
        scored["pred"] = _predict_df(scored, full_model, feature_cols)
        miss_topk = _miss_topk_recall(scored, miss_rows, k=cfg.top_k)
        june_scored = panel[panel["date"] >= "2026-01-01"].copy()
        if len(june_scored) >= 10:
            june_scored["pred"] = _predict_df(june_scored, full_model, feature_cols)
            miss_topk_2026 = _miss_topk_recall(june_scored, miss_rows, k=cfg.top_k)
        else:
            miss_topk_2026 = {"skipped": True}
    else:
        miss_topk = miss_topk_2026 = {"skipped": True}

    today = date.today().isoformat()
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"pool_topk_wfa_{today.replace('-', '')}.json"

    payload: dict[str, Any] = {
        "meta": {
            "generated_on": today,
            "panel_path": str(cfg.panel_path.relative_to(PROJECT_ROOT)),
            "split_spec_id": cfg.split_spec_id,
            "n_rows": len(panel),
            "n_features": len(feature_cols),
            "top_k": cfg.top_k,
            "min_cross_section": cfg.min_cross_section,
            "label": "ret_3d",
        },
        "aggregate_oos": agg,
        "h_ml_3": hml3,
        "folds": fold_results,
        "holdout_2026h1": holdout,
        "miss_topk_recall_is": miss_topk,
        "miss_topk_recall_2026_scored": miss_topk_2026,
        "miss_list_atom_recall": {
            "note": "P2 baseline · atom conjunction",
            "best_combo": "hist+no23+gap1",
            "n_hit": 6,
            "n_miss": len(miss_rows),
        },
    }
    out_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / f"pool_topk_wfa_{today.replace('-', '')}.md"
    md_path.write_text(_render_md(payload), encoding="utf-8")
    payload["artifacts"] = {
        "json": str(out_path.relative_to(PROJECT_ROOT)),
        "report_md": str(md_path.relative_to(PROJECT_ROOT)),
    }
    return payload


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj


def _render_md(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    agg = payload["aggregate_oos"]
    h = payload["h_ml_3"]
    lines = [
        "# RRG Pre-release Pool Top-K WFA (P3)",
        "",
        f"- **Generated**: {meta['generated_on']}",
        f"- **Panel**: {meta['n_rows']} rows · {meta['n_features']} atom features",
        f"- **Split**: `{meta['split_spec_id']}` · Top-{meta['top_k']}",
        "",
        "## H-ML-3 verdict",
        "",
        f"- Rank IC OOS mean: **{agg.get('oos_ic_mean')}** (threshold > {HML3_IC_MIN}) · "
        f"{'PASS' if h.get('pass_ic') else 'FAIL'}",
        f"- Top-{meta['top_k']} excess vs pool: **{agg.get('oos_topk_excess_mean_pp')} pp** "
        f"(threshold > {HML3_TOPK_EXCESS_MIN_PP}) · {'PASS' if h.get('pass_topk_excess') else 'FAIL'}",
        f"- **Overall**: {'PASS' if h.get('pass_all') else 'FAIL'}",
        "",
        "## Folds",
        "",
        "| fold | test years | IC mean | Top-K excess pp | n_test |",
        "|------|------------|---------|-----------------|--------|",
    ]
    for f in payload.get("folds") or []:
        if f.get("skipped"):
            continue
        ic_m = (f.get("test_ic") or {}).get("mean")
        ex = (f.get("test_topk") or {}).get("mean_excess_pp")
        lines.append(
            f"| {f.get('fold_id')} | {','.join(f.get('test_years') or [])} | "
            f"{ic_m} | {ex} | {f.get('n_test')} |"
        )

    ho = payload.get("holdout_2026h1") or {}
    if not ho.get("skipped"):
        lines.extend(
            [
                "",
                "## 2026H1 hold-out",
                "",
                f"- IC mean: {(ho.get('test_ic') or {}).get('mean')}",
                f"- Top-K excess: {ho.get('test_topk_mean_excess_pp')} pp",
            ]
        )

    lines.extend(["", "## Miss-list Top-K recall (diagnostic)", ""])
    for key in ("miss_topk_recall_is", "miss_topk_recall_2026_scored"):
        r = payload.get(key) or {}
        if not r.get("skipped"):
            lines.append(f"- `{key}`: {r.get('n_in_topk', 0)}/{r.get('n_miss', 0)}")

    return "\n".join(lines)
