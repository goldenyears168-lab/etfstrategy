"""Tests for market probe strategy research helpers."""

from research.backtest.market_probe_strategy_research import (
    ProbeDayRecord,
    _pearson,
    _quality_verdict,
    day_type_taxonomy,
)


def test_pearson_perfect():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pearson(xs, xs) == 1.0


def test_day_type_taxonomy_counts():
    probes = [
        ProbeDayRecord(
            probe_date="2026-01-02",
            signal_date="2026-01-03",
            axis_z={"momentum": 1.0},
            axis_raw={},
            axis_n={"momentum": 80},
            axis_sufficient={"momentum": True},
            vol_decile=8,
            vol_regime="high_vol",
            market_type_id="momentum_day",
            market_type_label="動能日",
            dominant_axis="momentum",
            axis_quartile={"momentum": 4},
        ),
        ProbeDayRecord(
            probe_date="2026-01-03",
            signal_date="2026-01-04",
            axis_z={"momentum": -1.0},
            axis_raw={},
            axis_n={"momentum": 90},
            axis_sufficient={"momentum": True},
            vol_decile=3,
            vol_regime="low_vol",
            market_type_id="neutral",
            market_type_label="中性",
            dominant_axis="mean_reversion",
            axis_quartile={"momentum": 1},
        ),
    ]
    tax = day_type_taxonomy(probes)
    assert tax["market_type_counts"]["momentum_day"] == 1
    assert tax["vol_regime_counts"]["high_vol"] == 1


def test_quality_verdict_stable():
    stability = [
        {"axis_id": "momentum", "pct_sufficient": 80, "mean_n_signals": 90, "lag1_autocorr_z": 0.2},
        {"axis_id": "volatility", "pct_sufficient": 100, "mean_n_signals": 0, "lag1_autocorr_z": None},
    ]
    v = _quality_verdict(stability, probes=[1, 2, 3], min_n=50)
    assert v["stable_enough_for_routing"] is True
