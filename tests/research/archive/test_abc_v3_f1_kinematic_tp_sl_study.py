"""Tests for ABC v3+f1 TP/SL split study."""

from __future__ import annotations

from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import (
    StopLossParams,
    TakeProfitParams,
    TpSlCombo,
    evaluate_leg_tp_sl,
    run_abc_f1_tp_sl_study,
)


def _snap(**kw) -> dict:
    base = {
        "ret_from_entry_pct": 5.0,
        "trade_date": "2025-01-02",
        "gap_mv_w20_w5": 7.0,
        "gap_mv_w5_w3": 2.5,
        "w3_rv": 102.0,
        "w5_rv": 101.0,
        "drv_w3": -0.4,
        "drv_w5": -0.3,
        "dmv_w5": -0.4,
        "w5_quad": "weakening",
    }
    base.update(kw)
    return base


def test_tp_fires_before_hold_end():
    t0 = _snap(ret_from_entry_pct=0, gap_mv_w20_w5=1.0, drv_w3=0.0, w3_rv=103.0)
    t1 = _snap(ret_from_entry_pct=11.0, gap_mv_w20_w5=7.0, drv_w3=-0.4, w3_rv=102.0)
    leg = {
        "return_pct": 8.0,
        "peak_ret_pct": 12.0,
        "peak_frame_idx": 1,
        "entry_t0": t0,
        "timeline": [t0, t1],
    }
    combo = TpSlCombo(
        tp=TakeProfitParams(min_ret_pct=10.0, gw5_min=6.0),
        sl=StopLossParams(arm_ret_max_pct=5.0, dmv_w5_min=0.35, drv_w5_min=0.25),
    )
    r = evaluate_leg_tp_sl(leg, combo)
    assert r["fired"]
    assert r["source"] == "TP"
    assert r["exit_ret_pct"] == 11.0


def test_sl_fires_on_weak_leg():
    t0 = _snap(ret_from_entry_pct=0, dmv_w5=0.0, w5_quad="leading")
    t1 = _snap(ret_from_entry_pct=-2.0, dmv_w5=-0.5, w5_quad="lagging", drv_w5=-0.3)
    leg = {
        "return_pct": -4.0,
        "peak_ret_pct": 1.0,
        "peak_frame_idx": 0,
        "entry_t0": t0,
        "timeline": [t0, t1],
    }
    combo = TpSlCombo(
        tp=TakeProfitParams(min_ret_pct=10.0, gw5_min=6.0),
        sl=StopLossParams(
            dmv_w5_min=0.35,
            drv_w5_min=0.25,
            arm_ret_max_pct=3.0,
            require_w5_lagging=True,
        ),
    )
    r = evaluate_leg_tp_sl(leg, combo)
    assert r["fired"]
    assert r["source"] == "SL"


def test_trail_tp_fires_on_giveback():
    t0 = _snap(ret_from_entry_pct=0)
    t1 = _snap(ret_from_entry_pct=12.0)
    t2 = _snap(ret_from_entry_pct=9.5)
    leg = {
        "return_pct": 8.0,
        "peak_ret_pct": 12.0,
        "peak_frame_idx": 1,
        "entry_t0": t0,
        "timeline": [t0, t1, t2],
    }
    combo = TpSlCombo(
        tp=TakeProfitParams(
            tp_mode="trail_giveback",
            trail_giveback_pp=2.0,
            min_ret_pct=8.0,
        ),
        sl=StopLossParams(sl_mode="underwater", dmv_w5_min=0.35, drv_w5_min=0.25),
    )
    r = evaluate_leg_tp_sl(leg, combo)
    assert r["fired"] and r["source"] == "TP"
    assert r["exit_ret_pct"] == 9.5

    legs = [
        {
            "return_pct": 5.0,
            "peak_ret_pct": 10.0,
            "peak_frame_idx": 1,
            "entry_t0": _snap(ret_from_entry_pct=0),
            "timeline": [_snap(ret_from_entry_pct=0), _snap(ret_from_entry_pct=5.0)],
        }
    ]
    payload = run_abc_f1_tp_sl_study(legs, target_pct=8.0)
    assert payload["schema"] == "abc_v3_f1_kinematic_tp_sl-v1"
