"""TW100 walk-forward skeleton · Research layer (504 train + 100 test + 7 embargo)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from rank_stats import icir, spearman_correlation
from research.backtest.archive.tw100_ml_backtest import (
    Tw100MlBacktestConfig,
    daily_rank_ic_series,
    forward_label,
    load_close_panel,
    momentum_signal,
)
from research_config import SplitSpec, load_research_splits
from stock_universe import TW100_UNIVERSE_ID, load_universe_constituents

DEFAULT_SPLIT_ID = "tw100_wfa_504_100"


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: int
    train_start: str
    train_end: str
    embargo_start: str
    embargo_end: str
    test_start: str
    test_end: str


@dataclass(frozen=True)
class Tw100WalkForwardConfig:
    snapshot_date: str = "2026-06-26"
    start_date: str = "2015-01-01"
    end_date: str | None = None
    split_spec_id: str = DEFAULT_SPLIT_ID
    mom_window: int = 20
    min_cross_section: int = 30
    max_folds: int | None = None


def resolve_wfa_split(split_spec_id: str = DEFAULT_SPLIT_ID) -> SplitSpec:
    spec = load_research_splits().get(split_spec_id)
    if spec is None:
        raise ValueError(f"unknown split_spec: {split_spec_id}")
    if spec.method != "walk_forward" or spec.train_days is None or spec.test_days is None:
        raise ValueError(f"split {split_spec_id} is not a day-based walk_forward spec")
    return spec


def build_walk_forward_folds(
    trading_dates: list[str],
    *,
    train_days: int,
    test_days: int,
    embargo_days: int = 0,
    roll_days: int | None = None,
    max_folds: int | None = None,
) -> list[WalkForwardFold]:
    """Rolling walk-forward on sorted trading calendar."""
    if not trading_dates:
        return []
    dates = sorted(trading_dates)
    step = roll_days if roll_days is not None else test_days
    folds: list[WalkForwardFold] = []
    cursor = 0
    fold_id = 0
    while True:
        train_start_i = cursor
        train_end_i = train_start_i + train_days - 1
        embargo_end_i = train_end_i + embargo_days
        test_start_i = embargo_end_i + 1
        test_end_i = test_start_i + test_days - 1
        if test_end_i >= len(dates):
            break
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                train_start=dates[train_start_i],
                train_end=dates[train_end_i],
                embargo_start=dates[train_end_i + 1] if train_end_i + 1 < len(dates) else dates[train_end_i],
                embargo_end=dates[embargo_end_i],
                test_start=dates[test_start_i],
                test_end=dates[test_end_i],
            )
        )
        fold_id += 1
        if max_folds is not None and fold_id >= max_folds:
            break
        cursor += step

    return folds


def _ic_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n_days": 0, "mean": None, "icir": None}
    return {
        "n_days": len(values),
        "mean": round(sum(values) / len(values), 6),
        "icir": round(icir(values), 6) if icir(values) is not None else None,
    }


def run_tw100_walk_forward_backtest(
    conn: sqlite3.Connection,
    cfg: Tw100WalkForwardConfig,
    *,
    signal_fn: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    split = resolve_wfa_split(cfg.split_spec_id)
    train_days = int(split.train_days or 504)
    test_days = int(split.test_days or 100)
    embargo_days = int(split.embargo_days or 0)
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

    trading_dates = [str(d) for d in panel.index.tolist()]
    folds = build_walk_forward_folds(
        trading_dates,
        train_days=train_days,
        test_days=test_days,
        embargo_days=embargo_days,
        max_folds=cfg.max_folds,
    )
    if not folds:
        raise RuntimeError(
            f"no WFA folds (need {train_days + embargo_days + test_days}+ trading days; got {len(trading_dates)})"
        )

    sig_fn = signal_fn or (lambda p: momentum_signal(p, cfg.mom_window))
    signal = sig_fn(panel)
    label = forward_label(panel, horizon=2)
    full_ic = daily_rank_ic_series(signal, label, min_cross_section=cfg.min_cross_section)
    ic_by_date = dict(full_ic)

    fold_rows: list[dict[str, Any]] = []
    oos_ic_all: list[float] = []
    for fold in folds:
        test_dates = [d for d in trading_dates if fold.test_start <= d <= fold.test_end]
        train_dates = [d for d in trading_dates if fold.train_start <= d <= fold.train_end]
        test_ic = [ic_by_date[d] for d in test_dates if d in ic_by_date]
        train_ic = [ic_by_date[d] for d in train_dates if d in ic_by_date]
        oos_ic_all.extend(test_ic)
        fold_rows.append(
            {
                "fold_id": fold.fold_id,
                "train_start": fold.train_start,
                "train_end": fold.train_end,
                "embargo_start": fold.embargo_start,
                "embargo_end": fold.embargo_end,
                "test_start": fold.test_start,
                "test_end": fold.test_end,
                "train_ic": _ic_stats(train_ic),
                "test_ic": _ic_stats(test_ic),
            }
        )

    agg_test = _ic_stats(oos_ic_all)
    passed_oos = agg_test["n_days"] >= min_oos_n if min_oos_n else True

    return {
        "schema": "tw100_walk_forward-v1",
        "layer": "research",
        "universe_id": TW100_UNIVERSE_ID,
        "snapshot_date": cfg.snapshot_date,
        "start_date": cfg.start_date,
        "end_date": end,
        "split_spec_id": cfg.split_spec_id,
        "train_days": train_days,
        "test_days": test_days,
        "embargo_days": embargo_days,
        "min_oos_n": min_oos_n,
        "signal": "momentum_baseline",
        "mom_window": cfg.mom_window,
        "label": "forward_2d_return_t1_t3",
        "n_folds": len(folds),
        "n_trading_days": len(trading_dates),
        "aggregate_test_ic": agg_test,
        "oos_gate_passed": passed_oos,
        "folds": fold_rows,
        "notes": [
            "Skeleton WFA: signal fixed (momentum); no per-fold retrain yet.",
            "Embargo dates excluded from train/test IC aggregation.",
            "Replace signal_fn with Alpha158/LightGBM scores when wired.",
        ],
    }


def write_walk_forward_artifact(payload: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path
