"""TW100 slot Top-K portfolio walk-forward · Alpha158 + LightGBM OOS."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from research.backtest.tw100_alpha158_lgbm_wfa import (
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
from research.backtest.tw100_slot_topk_portfolio import (
    DEFAULT_TOTAL_CAPITAL,
    DEFAULT_TW_COST_MODEL,
    benchmark_periods_from_signals,
    compare_slot_vs_benchmark,
    load_tw100_close_panel,
    periods_from_pred_panel,
    run_slot_topk_simulation,
)
from research.backtest.tw100_walk_forward import build_walk_forward_folds, resolve_wfa_split


@dataclass(frozen=True)
class Tw100SlotTopkWfaConfig(Tw100Alpha158LgbmConfig):
    top_k: int = 10
    snapshot_date: str = "2026-06-26"
    benchmark_id: str = "0050"
    total_capital: float = DEFAULT_TOTAL_CAPITAL
    use_cost_model: bool = True


def _fit_predict_fold_periods(
    instruments: list[str],
    fold,
    calendar: list[str],
    cfg: Tw100SlotTopkWfaConfig,
) -> dict[str, Any]:
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP

    from research.backtest.tw100_alpha158_lgbm_wfa import TW100_LABEL_EXPR

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
    if hasattr(test_label, "iloc") and getattr(test_label, "ndim", 1) == 2:
        test_label = test_label.iloc[:, 0]
    test_ic_series = daily_rank_ic_from_panel(
        pred, test_label, min_cross_section=cfg.min_cross_section
    )
    test_dates = [d for d in calendar if fold.test_start <= d <= fold.test_end]
    periods = periods_from_pred_panel(
        pred,
        calendar,
        top_k=cfg.top_k,
        min_cross_section=cfg.min_cross_section,
    )
    periods = [p for p in periods if p["signal_date"] in set(test_dates)]

    return {
        "fold_id": fold.fold_id,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "best_iteration": int(getattr(model, "best_iteration", 0) or 0),
        "test_ic": _ic_stats([v for _, v in test_ic_series]),
        "test_ic_daily": [{"date": d, "ic": round(v, 6)} for d, v in test_ic_series],
        "n_periods": len(periods),
        "test_periods": periods,
    }


def run_tw100_slot_topk_portfolio_wfa(
    cfg: Tw100SlotTopkWfaConfig,
    conn: sqlite3.Connection,
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
    all_periods: list[dict[str, Any]] = []
    oos_ic_daily: list[float] = []

    for fold in folds:
        print(
            f"  fold {fold.fold_id}: train → test {fold.test_start}..{fold.test_end} · slot top-{cfg.top_k}"
        )
        row = _fit_predict_fold_periods(instruments, fold, calendar, cfg)
        periods = row.pop("test_periods", [])
        all_periods.extend(periods)
        oos_ic_daily.extend(
            [float(r["ic"]) for r in row.get("test_ic_daily", []) if r.get("ic") is not None]
        )
        fold_rows.append(row)

    bench_ids = [cfg.benchmark_id]
    close = load_tw100_close_panel(
        conn,
        snapshot_date=cfg.snapshot_date,
        start=cfg.start_date,
        end=end,
        extra_stock_ids=bench_ids,
    )
    cost_model = DEFAULT_TW_COST_MODEL if cfg.use_cost_model else None

    strategy_metrics = run_slot_topk_simulation(
        conn,
        close,
        calendar,
        all_periods,
        top_k=cfg.top_k,
        total_capital=cfg.total_capital,
        cost_model=cost_model,
    )

    signal_dates = sorted({p["signal_date"] for p in all_periods})
    bench_periods = benchmark_periods_from_signals(signal_dates, calendar, cfg.benchmark_id)
    benchmark_metrics = run_slot_topk_simulation(
        conn,
        close,
        calendar,
        bench_periods,
        top_k=1,
        total_capital=cfg.total_capital,
        cost_model=cost_model,
    )
    comparison = compare_slot_vs_benchmark(
        strategy_metrics, benchmark_metrics, benchmark_id=cfg.benchmark_id
    )
    if benchmark_metrics.get("n_trades", 0) == 0:
        comparison["benchmark_note"] = f"{cfg.benchmark_id} sparse in window; check close panel"

    agg_test = _ic_stats(oos_ic_daily)
    passed_oos = agg_test["n_days"] >= min_oos_n if min_oos_n else True

    return {
        "schema": "tw100_slot_topk_portfolio_wfa-v1",
        "layer": "research",
        "start_date": cfg.start_date,
        "end_date": end,
        "split_spec_id": cfg.split_spec_id,
        "model": "LightGBM",
        "features": "Alpha158",
        "top_k": cfg.top_k,
        "n_slots": cfg.top_k,
        "total_capital_ntd": cfg.total_capital,
        "hold": "T+1 entry · T+3 exit (2-day)",
        "cost_model": cost_model,
        "n_folds": len(folds),
        "n_periods": len(all_periods),
        "aggregate_test_ic": agg_test,
        "slot_portfolio_oos": strategy_metrics,
        "benchmark_slot_oos": benchmark_metrics,
        "comparison_vs_benchmark": comparison,
        "oos_gate_passed": passed_oos,
        "folds": fold_rows,
        "notes": [
            "Slot-based Top-K · non-overlapping 2-day holds · simulate_slot_portfolio.",
            "WFA OOS periods concatenated (non-overlapping test windows).",
            "Research layer only; not strategy SSOT.",
        ],
    }
