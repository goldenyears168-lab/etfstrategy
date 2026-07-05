"""TW100 Alpha158 + LightGBM walk-forward with Top-K portfolio OOS metrics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd

from research.backtest.tw100_alpha158_lgbm_wfa import (
    TW100_LABEL_EXPR,
    Tw100Alpha158LgbmConfig,
    _ic_stats,
    _predict_lgbm,
    _train_lgbm,
    _train_valid_segments,
    daily_rank_ic_from_panel,
    init_tw100_qlib,
    load_trading_calendar,
    load_tw100_instruments,
)
from research.backtest.tw100_topk_portfolio import (
    aggregate_oos_portfolio,
    daily_topk_portfolio_from_panels,
    load_benchmark_forward_returns,
    load_index_benchmark_forward_returns,
    raw_forward_label_for_pred,
)
from research.backtest.tw100_walk_forward import build_walk_forward_folds, resolve_wfa_split


@dataclass(frozen=True)
class Tw100TopkPortfolioWfaConfig(Tw100Alpha158LgbmConfig):
    top_k: int = 10
    benchmark_id: str = "0050"
    cost_scenarios_bps: tuple[float, ...] = (0.0, 10.0, 20.0)


def _fit_predict_fold_with_portfolio(
    instruments: list[str],
    fold,
    calendar: list[str],
    cfg: Tw100TopkPortfolioWfaConfig,
    conn: sqlite3.Connection | None,
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
    raw_label = raw_forward_label_for_pred(conn, pred) if conn is not None else test_label
    port_daily = daily_topk_portfolio_from_panels(
        pred, raw_label, k=cfg.top_k, min_cross_section=cfg.min_cross_section
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
        "test_portfolio_daily": port_daily,
    }


def run_tw100_topk_portfolio_wfa(
    cfg: Tw100TopkPortfolioWfaConfig,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
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
    fold_port_daily: list[list[dict[str, Any]]] = []

    for fold in folds:
        print(
            f"  fold {fold.fold_id}: train {fold.train_start}..{fold.train_end} "
            f"→ test {fold.test_start}..{fold.test_end} · top-{cfg.top_k}"
        )
        row = _fit_predict_fold_with_portfolio(instruments, fold, calendar, cfg, conn)
        daily = row.pop("test_portfolio_daily", [])
        fold_port_daily.append(daily)
        oos_ic_daily.extend(
            [float(r["ic"]) for r in row.get("test_ic_daily", []) if r.get("ic") is not None]
        )
        fold_rows.append(row)

    bench_by_date: dict[str, float] = {}
    cfg_benchmark_note = cfg.benchmark_id
    if conn is not None:
        bench_by_date = load_benchmark_forward_returns(
            conn,
            cfg.benchmark_id,
            start=cfg.start_date,
            end=end,
        )
        port_dates = {str(row["date"]) for fold in fold_port_daily for row in fold}
        overlap = sum(1 for d in port_dates if d in bench_by_date)
        if port_dates and overlap < len(port_dates) * 0.5:
            bench_by_date = load_index_benchmark_forward_returns(
                conn, start=cfg.start_date, end=end, code="IX0001"
            )
            cfg_benchmark_note = "IX0001"

    cost_summaries: dict[str, dict[str, Any]] = {}
    for bps in cfg.cost_scenarios_bps:
        key = f"cost_{int(bps)}bps"
        cost_summaries[key] = aggregate_oos_portfolio(
            fold_port_daily,
            bench_by_date,
            cost_bps=bps,
            benchmark_id=cfg.benchmark_id,
        )

    agg_test = _ic_stats(oos_ic_daily)
    passed_oos = agg_test["n_days"] >= min_oos_n if min_oos_n else True

    return {
        "schema": "tw100_topk_portfolio_wfa-v1",
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
        "top_k": cfg.top_k,
        "benchmark_id": cfg_benchmark_note,
        "cost_scenarios_bps": list(cfg.cost_scenarios_bps),
        "n_folds": len(folds),
        "aggregate_test_ic": agg_test,
        "portfolio_oos": cost_summaries,
        "oos_gate_passed": passed_oos,
        "folds": fold_rows,
        "notes": [
            "Top-K equal-weight long · same 2-day forward label as Rank IC.",
            "Daily returns compounded with overlapping holds (research approximation).",
            "Research layer only; not strategy SSOT.",
        ],
    }
