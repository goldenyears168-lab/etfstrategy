"""TW100 Alpha158 + LightGBM walk-forward · requires microsoft qlib (.venv-qlib)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from rank_stats import spearman_correlation
from research.backtest.archive.tw100_walk_forward import (
    DEFAULT_SPLIT_ID,
    WalkForwardFold,
    _ic_stats,
    build_walk_forward_folds,
    resolve_wfa_split,
    write_walk_forward_artifact,
)
from stock_db import PROJECT_ROOT

DEFAULT_QLIB_URI = PROJECT_ROOT / "data" / "qlib" / "tw100"
TW100_LABEL_EXPR = "Ref($close, -3)/Ref($close, -1) - 1"


# qlib Alpha158 LightGBM benchmark (CSI300 tuned) · workflow_config_lightgbm_Alpha158.yaml
QLIB_ALPHA158_LGBM_PRESET: dict[str, float | int] = {
    "learning_rate": 0.0421,
    "num_leaves": 210,
    "max_depth": 8,
    "colsample_bytree": 0.8879,
    "subsample": 0.8789,
    "lambda_l1": 205.6999,
    "lambda_l2": 580.9768,
    "num_boost_round": 1000,
    "early_stopping_rounds": 50,
}


@dataclass(frozen=True)
class Tw100Alpha158LgbmConfig:
    provider_uri: Path = DEFAULT_QLIB_URI
    start_date: str = "2015-01-01"
    end_date: str | None = None
    split_spec_id: str = DEFAULT_SPLIT_ID
    max_folds: int | None = None
    valid_ratio: float = 0.2
    min_cross_section: int = 30
    num_boost_round: int = int(QLIB_ALPHA158_LGBM_PRESET["num_boost_round"])
    early_stopping_rounds: int = int(QLIB_ALPHA158_LGBM_PRESET["early_stopping_rounds"])
    learning_rate: float = float(QLIB_ALPHA158_LGBM_PRESET["learning_rate"])
    num_leaves: int = int(QLIB_ALPHA158_LGBM_PRESET["num_leaves"])
    max_depth: int = int(QLIB_ALPHA158_LGBM_PRESET["max_depth"])
    colsample_bytree: float = float(QLIB_ALPHA158_LGBM_PRESET["colsample_bytree"])
    subsample: float = float(QLIB_ALPHA158_LGBM_PRESET["subsample"])
    lambda_l1: float = float(QLIB_ALPHA158_LGBM_PRESET["lambda_l1"])
    lambda_l2: float = float(QLIB_ALPHA158_LGBM_PRESET["lambda_l2"])
    min_data_in_leaf: int = 20
    min_child_samples: int = 20


def init_tw100_qlib(provider_uri: Path) -> None:
    import qlib
    from qlib.config import REG_CN

    qlib.init(provider_uri=str(provider_uri.resolve()), region=REG_CN)


def load_tw100_instruments(provider_uri: Path) -> list[str]:
    inst_file = provider_uri / "instruments" / "all.txt"
    if not inst_file.is_file():
        raise FileNotFoundError(f"missing {inst_file}")
    out: list[str] = []
    for line in inst_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(line.split("\t", 1)[0].strip())
    if not out:
        raise RuntimeError("TW100 instruments/all.txt is empty")
    return out


def load_trading_calendar(provider_uri: Path, start: str, end: str) -> list[str]:
    from qlib.data import D

    init_tw100_qlib(provider_uri)
    cal = D.calendar(start_time=start, end_time=end, freq="day")
    return [pd.Timestamp(d).strftime("%Y-%m-%d") for d in cal]


def daily_rank_ic_from_panel(
    pred: pd.Series,
    label: pd.Series,
    *,
    min_cross_section: int = 30,
) -> list[tuple[str, float]]:
    df = pd.concat([pred.rename("pred"), label.rename("label")], axis=1).dropna(how="any")
    if df.empty:
        return []
    out: list[tuple[str, float]] = []
    for dt, grp in df.groupby(level="datetime"):
        if len(grp) < min_cross_section:
            continue
        ic = spearman_correlation(grp["pred"].tolist(), grp["label"].tolist())
        if ic is not None:
            out.append((pd.Timestamp(dt).strftime("%Y-%m-%d"), ic))
    return out


def _train_valid_segments(
    fold: WalkForwardFold,
    calendar: list[str],
    *,
    valid_ratio: float,
) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    train_dates = [d for d in calendar if fold.train_start <= d <= fold.train_end]
    if len(train_dates) < 30:
        raise ValueError(f"fold {fold.fold_id}: train window too short ({len(train_dates)} days)")
    valid_n = max(20, int(len(train_dates) * valid_ratio))
    if valid_n >= len(train_dates):
        valid_n = max(1, len(train_dates) // 5)
    valid_start = train_dates[-valid_n]
    train_end = train_dates[-valid_n - 1] if len(train_dates) > valid_n else train_dates[0]
    return (
        (fold.train_start, train_end),
        (valid_start, fold.train_end),
        (fold.test_start, fold.test_end),
    )


def _import_lightgbm():
    return importlib.import_module("lightgbm")


def _prepare_xy(dataset, segment: str):
    from qlib.data.dataset.handler import DataHandlerLP

    df = dataset.prepare(segment, col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    if df.empty:
        raise ValueError(f"empty dataset segment={segment}")
    x = df["feature"]
    y = df["label"]
    if hasattr(y, "values") and y.values.ndim == 2 and y.values.shape[1] == 1:
        import numpy as np

        y = np.squeeze(y.values)
    else:
        y = y.values if hasattr(y, "values") else y
    return x.values, y


def _train_lgbm(dataset, cfg: Tw100Alpha158LgbmConfig):
    lgb = _import_lightgbm()
    x_train, y_train = _prepare_xy(dataset, "train")
    x_valid, y_valid = _prepare_xy(dataset, "valid")
    train_set = lgb.Dataset(x_train, label=y_train)
    valid_set = lgb.Dataset(x_valid, label=y_valid)
    params = {
        "objective": "regression",
        "metric": "l2",
        "learning_rate": cfg.learning_rate,
        "num_leaves": cfg.num_leaves,
        "max_depth": cfg.max_depth,
        "colsample_bytree": cfg.colsample_bytree,
        "subsample": cfg.subsample,
        "lambda_l1": cfg.lambda_l1,
        "lambda_l2": cfg.lambda_l2,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "min_child_samples": cfg.min_child_samples,
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


def _predict_lgbm(model, dataset, segment: str) -> pd.Series:
    from qlib.data.dataset.handler import DataHandlerLP

    x = dataset.prepare(segment, col_set="feature", data_key=DataHandlerLP.DK_I)
    pred = model.predict(x.values)
    return pd.Series(pred, index=x.index)


def _fit_predict_fold(
    instruments: list[str],
    fold: WalkForwardFold,
    calendar: list[str],
    cfg: Tw100Alpha158LgbmConfig,
) -> dict[str, Any]:
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP

    train_seg, valid_seg, test_seg = _train_valid_segments(
        fold, calendar, valid_ratio=cfg.valid_ratio
    )
    handler = Alpha158(
        instruments=instruments,
        start_time=fold.train_start,
        end_time=fold.test_end,
        fit_start_time=train_seg[0],
        fit_end_time=train_seg[1],
        label=([TW100_LABEL_EXPR], ["LABEL0"]),
    )
    dataset = DatasetH(
        handler,
        segments={"train": train_seg, "valid": valid_seg, "test": test_seg},
    )
    model = _train_lgbm(dataset, cfg)

    pred = _predict_lgbm(model, dataset, "test")
    test_label = dataset.prepare("test", col_set=["label"], data_key=DataHandlerLP.DK_L)["label"]
    if isinstance(test_label, pd.DataFrame):
        test_label = test_label.iloc[:, 0]
    test_ic_series = daily_rank_ic_from_panel(
        pred, test_label, min_cross_section=cfg.min_cross_section
    )

    valid_pred = _predict_lgbm(model, dataset, "valid")
    valid_label = dataset.prepare("valid", col_set=["label"], data_key=DataHandlerLP.DK_L)["label"]
    if isinstance(valid_label, pd.DataFrame):
        valid_label = valid_label.iloc[:, 0]
    valid_ic_series = daily_rank_ic_from_panel(
        valid_pred, valid_label, min_cross_section=cfg.min_cross_section
    )

    return {
        "fold_id": fold.fold_id,
        "train_start": fold.train_start,
        "train_end": fold.train_end,
        "train_segment": list(train_seg),
        "valid_segment": list(valid_seg),
        "embargo_start": fold.embargo_start,
        "embargo_end": fold.embargo_end,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "best_iteration": int(getattr(model, "best_iteration", 0) or 0),
        "valid_ic": _ic_stats([v for _, v in valid_ic_series]),
        "test_ic": _ic_stats([v for _, v in test_ic_series]),
        "test_ic_daily": [{"date": d, "ic": round(v, 6)} for d, v in test_ic_series],
    }


def run_tw100_alpha158_lgbm_wfa(cfg: Tw100Alpha158LgbmConfig) -> dict[str, Any]:
    split = resolve_wfa_split(cfg.split_spec_id)
    train_days = int(split.train_days or 504)
    test_days = int(split.test_days or 100)
    embargo_days = int(split.embargo_days or 0)
    min_oos_n = int(split.min_oos_n or 0)

    instruments = load_tw100_instruments(cfg.provider_uri)
    calendar = load_trading_calendar(
        cfg.provider_uri,
        cfg.start_date,
        cfg.end_date or "2099-12-31",
    )
    end = cfg.end_date or calendar[-1]
    calendar = [d for d in calendar if d <= end]

    folds = build_walk_forward_folds(
        calendar,
        train_days=train_days,
        test_days=test_days,
        embargo_days=embargo_days,
        max_folds=cfg.max_folds,
    )
    if not folds:
        raise RuntimeError(f"no WFA folds on calendar ({len(calendar)} days)")

    init_tw100_qlib(cfg.provider_uri)
    fold_rows: list[dict[str, Any]] = []
    oos_ic_daily: list[float] = []
    for fold in folds:
        print(f"  fold {fold.fold_id}: train {fold.train_start}..{fold.train_end} → test {fold.test_start}..{fold.test_end}")
        row = _fit_predict_fold(instruments, fold, calendar, cfg)
        daily = row.get("test_ic_daily") or []
        oos_ic_daily.extend([float(r["ic"]) for r in daily if r.get("ic") is not None])
        fold_rows.append(row)

    agg_test = _ic_stats(oos_ic_daily)
    passed_oos = agg_test["n_days"] >= min_oos_n if min_oos_n else True
    return {
        "schema": "tw100_alpha158_lgbm_wfa-v1",
        "layer": "research",
        "provider_uri": str(cfg.provider_uri.resolve()),
        "start_date": cfg.start_date,
        "end_date": end,
        "split_spec_id": cfg.split_spec_id,
        "train_days": train_days,
        "test_days": test_days,
        "embargo_days": embargo_days,
        "min_oos_n": min_oos_n,
        "model": "LightGBM",
        "features": "Alpha158",
        "label": TW100_LABEL_EXPR,
        "lgbm_params": {
            "learning_rate": cfg.learning_rate,
            "num_leaves": cfg.num_leaves,
            "max_depth": cfg.max_depth,
            "colsample_bytree": cfg.colsample_bytree,
            "subsample": cfg.subsample,
            "lambda_l1": cfg.lambda_l1,
            "lambda_l2": cfg.lambda_l2,
            "min_data_in_leaf": cfg.min_data_in_leaf,
            "min_child_samples": cfg.min_child_samples,
            "num_boost_round": cfg.num_boost_round,
            "early_stopping_rounds": cfg.early_stopping_rounds,
        },
        "best_iteration_mean": (
            sum(int(r.get("best_iteration") or 0) for r in fold_rows) / len(fold_rows)
            if fold_rows
            else None
        ),
        "n_instruments": len(instruments),
        "n_folds": len(folds),
        "n_trading_days": len(calendar),
        "aggregate_test_ic": agg_test,
        "oos_gate_passed": passed_oos,
        "folds": fold_rows,
        "notes": [
            "Per-fold retrain · valid=last 20% of train for early stopping.",
            "Default LGB params follow qlib Alpha158 benchmark (CSI300 tuned).",
            "Research layer only; not strategy SSOT.",
        ],
    }
