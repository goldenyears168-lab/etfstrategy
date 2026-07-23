"""Tests for C18acc structural pyramid 3-slot resim."""

from __future__ import annotations

from research.backtest.c18acc_structural_pyramid_slot_resim import (
    evaluate_pyramid_slot_resim,
    overlay_pyramid_on_periods,
    underwater_rebound_condition,
)


def _snap(**kw) -> dict:
    base = {
        "ret_from_entry_pct": 0.0,
        "trade_date": "2025-01-02",
        "minute": "10:00:00",
        "px": 100.0,
        "w5_quad": "leading",
        "dmv_w5": -0.1,
        "gap_mv_w20_w5": 5.0,
        "w3_rv": 100.0,
    }
    base.update(kw)
    return base


def test_overlay_rewrites_blended_when_triggered():
    t0 = _snap(ret_from_entry_pct=0.0, w3_rv=102.0)
    t1 = _snap(ret_from_entry_pct=-2.0, w3_rv=100.5, trade_date="2025-01-03")
    t2 = _snap(ret_from_entry_pct=-1.0, w3_rv=101.0, trade_date="2025-01-04")
    t3 = _snap(ret_from_entry_pct=6.0, w3_rv=101.5, trade_date="2025-01-05")
    periods = [
        {
            "entry_date": "2025-01-02",
            "stock_id": "2330",
            "slot": 0,
            "return_pct": 6.0,
            "bench_return_pct": 1.0,
        }
    ]
    legs = [
        {
            "leg_id": "2025-01-02|2330|0",
            "return_pct": 6.0,
            "entry_t0": t0,
            "timeline": [t0, t1, t2, t3],
        }
    ]
    out, meta = overlay_pyramid_on_periods(periods, legs, underwater_rebound_condition())
    assert meta["n_triggered"] == 1
    assert out[0]["pyramid_triggered"] is True
    assert out[0]["return_pct"] != 6.0
    assert out[0]["return_pct"] == out[0]["pyramid_blended_sync_ret_pct"]


def test_evaluate_adopt_gate():
    baseline = {
        "full": {"mean_excess_pct": 5.0, "n_legs": 100, "n_triggered": 0},
        "is": {"mean_excess_pct": 4.0, "n_legs": 70, "n_triggered": 0},
        "oos": {"mean_excess_pct": 6.0, "n_legs": 30, "n_triggered": 0},
    }
    pyramid = {
        "full": {"mean_excess_pct": 5.5, "n_legs": 100, "n_triggered": 40},
        "is": {"mean_excess_pct": 4.2, "n_legs": 70, "n_triggered": 25},
        "oos": {"mean_excess_pct": 6.5, "n_legs": 30, "n_triggered": 15},
    }
    v = evaluate_pyramid_slot_resim(baseline, pyramid, trigger_meta={"n_triggered": 40})
    assert v["recommend_adopt"] is True
    assert v["delta_oos_excess_pp"] == 0.5


def test_evaluate_hold_when_oos_flat():
    baseline = {
        "full": {"mean_excess_pct": 5.0, "n_legs": 100, "n_triggered": 0},
        "is": {"mean_excess_pct": 4.0, "n_legs": 70, "n_triggered": 0},
        "oos": {"mean_excess_pct": 6.0, "n_legs": 30, "n_triggered": 0},
    }
    pyramid = {
        "full": {"mean_excess_pct": 5.1, "n_legs": 100, "n_triggered": 40},
        "is": {"mean_excess_pct": 4.1, "n_legs": 70, "n_triggered": 25},
        "oos": {"mean_excess_pct": 6.1, "n_legs": 30, "n_triggered": 15},
    }
    v = evaluate_pyramid_slot_resim(baseline, pyramid, trigger_meta={"n_triggered": 40})
    assert v["recommend_adopt"] is False


def test_render_slot_count_compare_smoke():
    from research.backtest.c18acc_structural_pyramid_slot_resim import (
        render_c18acc_slot_count_compare_md,
    )

    md = render_c18acc_slot_count_compare_md(
        {
            "note": "Δ = n_slots=4 − n_slots=3",
            "stack": "test",
            "total_capital": 100_000.0,
            "slot_counts": [3, 4],
            "by_slots": {
                "3": {
                    "baseline_slices": {
                        "full": {"n_legs": 10, "mean_return_pct": 1.0, "mean_excess_pct": 0.5},
                        "is": {"n_legs": 6, "mean_return_pct": 1.0, "mean_excess_pct": 0.5},
                        "oos": {"n_legs": 4, "mean_return_pct": 1.0, "mean_excess_pct": 0.5},
                    },
                    "sim_summary": {"swaps_total": 1, "slot_util_pct": 80},
                    "nav": {
                        "full": {
                            "total_return_pct": 10,
                            "cagr_pct": 5,
                            "max_drawdown_pct": 2,
                            "avg_utilization_pct": 80,
                        },
                        "oos": {
                            "total_return_pct": 4,
                            "cagr_pct": 3,
                            "max_drawdown_pct": 1,
                            "avg_utilization_pct": 70,
                        },
                    },
                },
                "4": {
                    "baseline_slices": {
                        "full": {"n_legs": 12, "mean_return_pct": 0.9, "mean_excess_pct": 0.4},
                        "is": {"n_legs": 7, "mean_return_pct": 0.9, "mean_excess_pct": 0.4},
                        "oos": {"n_legs": 5, "mean_return_pct": 0.9, "mean_excess_pct": 0.4},
                    },
                    "sim_summary": {"swaps_total": 2, "slot_util_pct": 75},
                    "nav": {
                        "full": {
                            "total_return_pct": 11,
                            "cagr_pct": 6,
                            "max_drawdown_pct": 2,
                            "avg_utilization_pct": 75,
                        },
                        "oos": {
                            "total_return_pct": 5,
                            "cagr_pct": 4,
                            "max_drawdown_pct": 1,
                            "avg_utilization_pct": 72,
                        },
                    },
                },
            },
            "delta": {
                "delta_full_excess_pp": -0.1,
                "delta_full_n": 2,
                "delta_is_excess_pp": -0.1,
                "delta_is_n": 1,
                "delta_oos_excess_pp": -0.1,
                "delta_oos_n": 1,
                "delta_full_nav_total_pp": 1.0,
                "delta_full_util_pp": -5.0,
            },
        }
    )
    assert "槽位數比較" in md
    assert "| 3 |" in md
    assert "| 4 |" in md
