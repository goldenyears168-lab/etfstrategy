"""TW100 monthly cross-section WFA · next-month excess return label (Research layer)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from rank_stats import spearman_correlation
from research.backtest.tw100_chip_features import (
    CHIP_FEATURE_NAMES,
    build_chip_feature_panels,
    chip_coverage_report,
)
from research.backtest.tw100_fundamental_features import (
    FUNDAMENTAL_FEATURE_NAMES,
    build_fundamental_feature_panels,
    fundamental_coverage_report,
)
from research.backtest.tw100_ml_backtest import (
    load_close_panel,
    write_tw100_backtest_artifact,
)
from research.backtest.tw100_walk_forward import _ic_stats, write_walk_forward_artifact
from research_config import SplitSpec, load_research_splits
from stock_universe import TW100_UNIVERSE_ID, load_universe_constituents

DEFAULT_MONTHLY_SPLIT_ID = "tw100_monthly_wfa_36_6"
DEFAULT_BENCHMARK_CODE = "IX0001"
MOM_WINDOWS = (21, 63, 126, 252)
MOM_FEATURE_NAMES = [f"mom_{w}d" for w in MOM_WINDOWS]

_FEATURE_COMPONENTS: dict[str, tuple[str, ...]] = {
    "mom": ("mom",),
    "fund": ("fund",),
    "chip": ("chip",),
    "mom_fund": ("mom", "fund"),
    "mom_chip": ("mom", "chip"),
    "mom_fund_chip": ("mom", "fund", "chip"),
}


def resolve_feature_names(feature_set: str) -> list[str]:
    parts = _FEATURE_COMPONENTS.get(feature_set)
    if parts is None:
        raise ValueError(f"unknown feature_set: {feature_set}")
    names: list[str] = []
    if "mom" in parts:
        names.extend(MOM_FEATURE_NAMES)
    if "fund" in parts:
        names.extend(FUNDAMENTAL_FEATURE_NAMES)
    if "chip" in parts:
        names.extend(CHIP_FEATURE_NAMES)
    return names


def _feature_components(feature_set: str) -> tuple[str, ...]:
    parts = _FEATURE_COMPONENTS.get(feature_set)
    if parts is None:
        raise ValueError(f"unknown feature_set: {feature_set}")
    return parts


@dataclass(frozen=True)
class MonthlyWalkForwardFold:
    fold_id: int
    train_start: str
    train_end: str
    embargo_end: str
    test_start: str
    test_end: str


@dataclass(frozen=True)
class Tw100MonthlyWfaConfig:
    snapshot_date: str = "2026-06-26"
    start_date: str = "2015-01-01"
    end_date: str | None = None
    split_spec_id: str = DEFAULT_MONTHLY_SPLIT_ID
    benchmark_code: str = DEFAULT_BENCHMARK_CODE
    min_cross_section: int = 30
    max_folds: int | None = None
    valid_ratio: float = 0.2
    signal: str = "lightgbm"  # lightgbm | momentum_126d
    feature_set: str = "mom"  # mom | mom_fund | mom_chip | mom_fund_chip | fund | chip
    num_boost_round: int = 300
    early_stopping_rounds: int = 30
    feature_names: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.feature_names:
            object.__setattr__(self, "feature_names", tuple(resolve_feature_names(self.feature_set)))


def resolve_monthly_wfa_split(split_spec_id: str = DEFAULT_MONTHLY_SPLIT_ID) -> SplitSpec:
    spec = load_research_splits().get(split_spec_id)
    if spec is None:
        raise ValueError(f"unknown split_spec: {split_spec_id}")
    if spec.method != "walk_forward_monthly" or spec.train_months is None or spec.test_months is None:
        raise ValueError(f"split {split_spec_id} is not a month-based walk_forward spec")
    return spec


def month_end_trading_dates(trading_dates: list[str]) -> list[str]:
    if not trading_dates:
        return []
    s = pd.Series(trading_dates, index=trading_dates)
    idx = pd.to_datetime(s.index)
    grouped = s.groupby([idx.year, idx.month]).max()
    return sorted(str(d) for d in grouped.tolist())


def build_walk_forward_monthly_folds(
    month_ends: list[str],
    *,
    train_months: int,
    test_months: int,
    embargo_months: int = 0,
    max_folds: int | None = None,
) -> list[MonthlyWalkForwardFold]:
    if len(month_ends) < train_months + embargo_months + test_months:
        return []
    folds: list[MonthlyWalkForwardFold] = []
    cursor = 0
    fold_id = 0
    while True:
        train_start_i = cursor
        train_end_i = train_start_i + train_months - 1
        embargo_end_i = train_end_i + embargo_months
        test_start_i = embargo_end_i + 1
        test_end_i = test_start_i + test_months - 1
        if test_end_i >= len(month_ends):
            break
        folds.append(
            MonthlyWalkForwardFold(
                fold_id=fold_id,
                train_start=month_ends[train_start_i],
                train_end=month_ends[train_end_i],
                embargo_end=month_ends[embargo_end_i],
                test_start=month_ends[test_start_i],
                test_end=month_ends[test_end_i],
            )
        )
        fold_id += 1
        if max_folds is not None and fold_id >= max_folds:
            break
        cursor += test_months
    return folds


def momentum_features(panel: pd.DataFrame, windows: tuple[int, ...] = MOM_WINDOWS) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for w in windows:
        out[f"mom_{w}d"] = panel / panel.shift(w) - 1.0
    return out


def forward_month_end_return(panel: pd.DataFrame, month_ends: list[str]) -> pd.DataFrame:
    """Next month-end return at each month-end T: close(T+1m)/close(T)-1."""
    me_idx = [d for d in month_ends if d in panel.index]
    out = pd.DataFrame(index=me_idx, columns=panel.columns, dtype=float)
    for i, dt in enumerate(me_idx[:-1]):
        nxt = me_idx[i + 1]
        if dt not in panel.index or nxt not in panel.index:
            continue
        cur = panel.loc[dt]
        nxt_px = panel.loc[nxt]
        out.loc[dt] = nxt_px / cur - 1.0
    return out


def monthly_excess_label(
    stock_panel: pd.DataFrame,
    bench_series: pd.Series,
    month_ends: list[str],
) -> pd.DataFrame:
    stock_ret = forward_month_end_return(stock_panel, month_ends)
    bench_frame = bench_series.to_frame(name="_bench")
    bench_ret = forward_month_end_return(bench_frame, month_ends)["_bench"]
    excess = stock_ret.sub(bench_ret, axis=0)
    return excess


def _stack_features(
    features: dict[str, pd.DataFrame],
    label: pd.DataFrame,
    dates: list[str],
    feature_names: list[str],
) -> tuple[pd.DataFrame, pd.Series]:
    rows: list[dict[str, Any]] = []
    labels: list[float] = []
    for dt in dates:
        if dt not in label.index:
            continue
        for sid in label.columns:
            lab = label.at[dt, sid]
            if pd.isna(lab):
                continue
            feat_row: dict[str, Any] = {"date": dt, "stock_id": sid}
            skip = False
            for name in feature_names:
                frame = features.get(name)
                if frame is None or dt not in frame.index or sid not in frame.columns:
                    skip = True
                    break
                val = frame.at[dt, sid]
                if val is None or pd.isna(val):
                    skip = True
                    break
                feat_row[name] = float(val)
            if skip:
                continue
            rows.append(feat_row)
            labels.append(float(lab))
    if not rows:
        return pd.DataFrame(), pd.Series(dtype=float)
    x = pd.DataFrame(rows)
    y = pd.Series(labels, index=x.index, name="label")
    return x, y


def monthly_rank_ic_from_predictions(
    pred_df: pd.DataFrame,
    label: pd.DataFrame,
    dates: list[str],
    *,
    min_cross_section: int,
) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for dt in dates:
        if dt not in label.index:
            continue
        day_pred = pred_df[pred_df["date"] == dt]
        if len(day_pred) < min_cross_section:
            continue
        preds = []
        labs = []
        for _, row in day_pred.iterrows():
            sid = str(row["stock_id"])
            if sid not in label.columns:
                continue
            lab = label.at[dt, sid]
            if pd.isna(lab) or pd.isna(row["pred"]):
                continue
            preds.append(float(row["pred"]))
            labs.append(float(lab))
        if len(preds) < min_cross_section:
            continue
        ic = spearman_correlation(preds, labs)
        if ic is not None:
            out.append((dt, ic))
    return out


def _import_lightgbm():
    return importlib.import_module("lightgbm")


def _train_predict_lgbm(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    x_valid: pd.DataFrame,
    y_valid: pd.Series,
    cfg: Tw100MonthlyWfaConfig,
) -> Any:
    lgb = _import_lightgbm()
    feat_cols = list(cfg.feature_names)
    train_set = lgb.Dataset(x_train[feat_cols].values, label=y_train.values)
    valid_set = lgb.Dataset(x_valid[feat_cols].values, label=y_valid.values)
    params = {
        "objective": "regression",
        "metric": "l2",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": 6,
        "colsample_bytree": 0.8,
        "subsample": 0.8,
        "lambda_l1": 1.0,
        "lambda_l2": 10.0,
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


def _momentum_126d_predictions(x: pd.DataFrame) -> pd.Series:
    return x["mom_126d"]


def _fit_predict_fold(
    fold: MonthlyWalkForwardFold,
    month_ends: list[str],
    features: dict[str, pd.DataFrame],
    label: pd.DataFrame,
    cfg: Tw100MonthlyWfaConfig,
) -> dict[str, Any]:
    feature_names = list(cfg.feature_names)
    train_dates = [d for d in month_ends if fold.train_start <= d <= fold.train_end]
    test_dates = [d for d in month_ends if fold.test_start <= d <= fold.test_end]
    x_all, _ = _stack_features(features, label, train_dates, feature_names)
    if x_all.empty or len(train_dates) < 12:
        raise ValueError(f"fold {fold.fold_id}: insufficient train rows ({len(x_all)})")

    valid_n = max(6, int(len(train_dates) * cfg.valid_ratio))
    valid_dates = train_dates[-valid_n:]
    fit_dates = train_dates[:-valid_n] if len(train_dates) > valid_n else train_dates[:1]
    x_fit, y_fit = _stack_features(features, label, fit_dates, feature_names)
    x_valid, y_valid = _stack_features(features, label, valid_dates, feature_names)
    x_test, _ = _stack_features(features, label, test_dates, feature_names)

    if cfg.signal == "momentum_126d":
        valid_pred_df = x_valid[["date", "stock_id"]].copy()
        valid_pred_df["pred"] = _momentum_126d_predictions(x_valid).values
        test_pred_df = x_test[["date", "stock_id"]].copy()
        test_pred_df["pred"] = _momentum_126d_predictions(x_test).values
        best_iter = 0
    else:
        model = _train_predict_lgbm(x_fit, y_fit, x_valid, y_valid, cfg)
        valid_pred_df = x_valid[["date", "stock_id"]].copy()
        valid_pred_df["pred"] = model.predict(x_valid[feature_names].values)
        test_pred_df = x_test[["date", "stock_id"]].copy()
        test_pred_df["pred"] = model.predict(x_test[feature_names].values)
        best_iter = int(getattr(model, "best_iteration", 0) or 0)

    valid_ic = monthly_rank_ic_from_predictions(
        valid_pred_df, label, valid_dates, min_cross_section=cfg.min_cross_section
    )
    test_ic = monthly_rank_ic_from_predictions(
        test_pred_df, label, test_dates, min_cross_section=cfg.min_cross_section
    )
    return {
        "fold_id": fold.fold_id,
        "train_start": fold.train_start,
        "train_end": fold.train_end,
        "embargo_end": fold.embargo_end,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "n_train_rows": len(x_all),
        "n_test_rows": len(x_test),
        "best_iteration": best_iter,
        "valid_ic": _ic_stats([v for _, v in valid_ic]),
        "test_ic": _ic_stats([v for _, v in test_ic]),
        "test_ic_monthly": [{"date": d, "ic": round(v, 6)} for d, v in test_ic],
    }


def run_tw100_monthly_wfa(conn, cfg: Tw100MonthlyWfaConfig) -> dict[str, Any]:
    split = resolve_monthly_wfa_split(cfg.split_spec_id)
    train_months = int(split.train_months or 36)
    test_months = int(split.test_months or 6)
    embargo_months = int(split.embargo_months or 0)
    min_oos_n = int(split.min_oos_n or 0)

    watch = load_universe_constituents(conn, TW100_UNIVERSE_ID, cfg.snapshot_date)
    if not watch:
        raise RuntimeError(f"TW100 universe empty @ {cfg.snapshot_date}")
    stock_ids = [w["stock_id"] for w in watch]

    end = cfg.end_date
    if not end:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM stock_daily_bars WHERE stock_id IN ({})".format(
                ",".join("?" * len(stock_ids))
            ),
            stock_ids,
        ).fetchone()
        end = str(row[0]) if row and row[0] else cfg.start_date

    panel = load_close_panel(conn, stock_ids, start=cfg.start_date, end=end)
    if panel.empty:
        raise RuntimeError("no close panel in range")

    bench = load_benchmark_close(conn, code=cfg.benchmark_code)
    bench = bench[(bench.index >= cfg.start_date) & (bench.index <= end)]

    trading_dates = sorted(str(d) for d in panel.index.tolist())
    month_ends = month_end_trading_dates(trading_dates)
    components = _feature_components(cfg.feature_set)
    features: dict[str, pd.DataFrame] = {}
    if "mom" in components:
        features.update(momentum_features(panel))
    fund_coverage = None
    chip_coverage = None
    if "fund" in components:
        fund_panels = build_fundamental_feature_panels(
            conn,
            stock_ids,
            month_ends,
            start=cfg.start_date,
            end=end,
        )
        features.update(fund_panels)
        fund_coverage = fundamental_coverage_report(
            fund_panels,
            month_ends,
            min_cross_section=cfg.min_cross_section,
        )
    if "chip" in components:
        chip_panels = build_chip_feature_panels(
            conn,
            stock_ids,
            month_ends,
            start=cfg.start_date,
            end=end,
        )
        features.update(chip_panels)
        chip_coverage = chip_coverage_report(
            chip_panels,
            month_ends,
            min_cross_section=cfg.min_cross_section,
        )

    if cfg.feature_set in ("fund", "chip"):
        keep = resolve_feature_names(cfg.feature_set)
        features = {k: features[k] for k in keep if k in features}

    label = monthly_excess_label(panel, bench, month_ends)

    folds = build_walk_forward_monthly_folds(
        month_ends,
        train_months=train_months,
        test_months=test_months,
        embargo_months=embargo_months,
        max_folds=cfg.max_folds,
    )
    if not folds:
        raise RuntimeError(
            f"no monthly WFA folds (need {train_months + embargo_months + test_months}+ "
            f"month-ends; got {len(month_ends)})"
        )

    fold_rows: list[dict[str, Any]] = []
    oos_ic: list[float] = []
    for fold in folds:
        print(
            f"  fold {fold.fold_id}: train {fold.train_start}..{fold.train_end} "
            f"→ test {fold.test_start}..{fold.test_end}"
        )
        row = _fit_predict_fold(fold, month_ends, features, label, cfg)
        oos_ic.extend([float(r["ic"]) for r in row.get("test_ic_monthly", []) if r.get("ic") is not None])
        fold_rows.append(row)

    agg_test = _ic_stats(oos_ic)
    passed_oos = agg_test["n_days"] >= min_oos_n if min_oos_n else True
    model_name = "LightGBM" if cfg.signal == "lightgbm" else "momentum_126d"
    notes = [
        "Monthly cross-section · per-fold retrain · next month-end excess return.",
        f"Feature set: {cfg.feature_set} ({len(cfg.feature_names)} features).",
        "Research layer only; not strategy SSOT.",
    ]
    if cfg.feature_set == "mom":
        notes.insert(1, "Features: multi-horizon momentum (21/63/126/252d) at month-end.")

    return {
        "schema": "tw100_monthly_wfa-v1",
        "layer": "research",
        "universe_id": TW100_UNIVERSE_ID,
        "snapshot_date": cfg.snapshot_date,
        "start_date": cfg.start_date,
        "end_date": end,
        "split_spec_id": cfg.split_spec_id,
        "train_months": train_months,
        "test_months": test_months,
        "embargo_months": embargo_months,
        "min_oos_n": min_oos_n,
        "model": model_name,
        "feature_set": cfg.feature_set,
        "features": list(cfg.feature_names),
        "label": f"next_month_excess_vs_{cfg.benchmark_code}",
        "benchmark_code": cfg.benchmark_code,
        "rebalance": "month_end",
        "fundamental_coverage": fund_coverage,
        "chip_coverage": chip_coverage,
        "n_folds": len(folds),
        "n_month_ends": len(month_ends),
        "aggregate_test_ic": agg_test,
        "oos_gate_passed": passed_oos,
        "folds": fold_rows,
        "notes": notes,
    }


__all__ = [
    "Tw100MonthlyWfaConfig",
    "build_walk_forward_monthly_folds",
    "forward_month_end_return",
    "month_end_trading_dates",
    "monthly_excess_label",
    "resolve_feature_names",
    "run_tw100_monthly_wfa",
    "write_tw100_backtest_artifact",
    "write_walk_forward_artifact",
]
