"""Tests for C18acc gap OR + RV3 decline interaction study."""

from __future__ import annotations

from research.backtest.archive.c18acc_kinematic_gap_rv3_study import (
    GapRv3Params,
    evaluate_leg_gap_rv3,
    run_c18acc_kinematic_gap_rv3_study,
    run_hold_cap_gap_rv3_study,
    truncate_leg_to_hold_days,
)


def _snap(**kw) -> dict:
    base = {
        "ret_from_entry_pct": 5.0,
        "trade_date": "2025-01-02",
        "spread_class": "pullback",
        "spread_dist": 9.0,
        "gap_mv_w20_w5": 7.0,
        "gap_mv_w5_w3": 2.5,
        "w3_rv": 102.0,
        "drv_w3": -0.4,
        "dmv_w3": -0.3,
        "w20_mv": 1.0,
    }
    base.update(kw)
    return base


def test_gap_or_and_rv3_drv_fires():
    t0 = _snap(ret_from_entry_pct=0, gap_mv_w20_w5=1.0, gap_mv_w5_w3=0.5, drv_w3=0.0, w3_rv=103.0)
    t1 = _snap(ret_from_entry_pct=11.0, drv_w3=-0.4, w3_rv=102.5)
    t2 = _snap(ret_from_entry_pct=12.0, drv_w3=-0.5, w3_rv=102.0)
    leg = {
        "return_pct": 4.0,
        "peak_ret_pct": 15.0,
        "peak_frame_idx": 2,
        "entry_t0": t0,
        "timeline": [t0, t1, t2],
    }
    params = GapRv3Params(
        gap_logic="or",
        gw5_min=6.0,
        gw3_min=2.0,
        gap_expand_min=0.0,
        rv3_mode="drv",
        drv3_min=0.35,
        dmv3_min=0.0,
        min_ret_pct=10.0,
        require_pullback=False,
        dist_min=0.0,
        consecutive=2,
    )
    r = evaluate_leg_gap_rv3(leg, params)
    assert r["fired"]
    assert r["exit_ret_pct"] == 12.0


def test_rv3_blocks_when_not_declining():
    t0 = _snap(ret_from_entry_pct=0, gap_mv_w20_w5=1.0, drv_w3=0.0)
    t1 = _snap(ret_from_entry_pct=11.0, drv_w3=0.0)
    t2 = _snap(ret_from_entry_pct=12.0, drv_w3=0.0)
    leg = {
        "return_pct": 4.0,
        "peak_ret_pct": 15.0,
        "peak_frame_idx": 2,
        "entry_t0": t0,
        "timeline": [t0, t1, t2],
    }
    params = GapRv3Params(
        gap_logic="or",
        gw5_min=6.0,
        gw3_min=2.0,
        gap_expand_min=0.0,
        rv3_mode="drv",
        drv3_min=0.25,
        dmv3_min=0.0,
        min_ret_pct=10.0,
        require_pullback=False,
        dist_min=0.0,
        consecutive=2,
    )
    r = evaluate_leg_gap_rv3(leg, params)
    assert not r["fired"]


def test_run_study_smoke():
    cache = {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": "2025-01-02",
        "date_end": "2025-06-01",
        "n_slots": 99,
        "poll": "30m",
        "legs": [
            {
                "return_pct": 5.0,
                "peak_ret_pct": 12.0,
                "peak_frame_idx": 2,
                "entry_t0": _snap(ret_from_entry_pct=0, gap_mv_w20_w5=0.5, drv_w3=0.0, w3_rv=103.0),
                "timeline": [
                    _snap(ret_from_entry_pct=0, gap_mv_w20_w5=0.5, drv_w3=0.0, w3_rv=103.0),
                    _snap(ret_from_entry_pct=11.0, gap_mv_w20_w5=7.0, drv_w3=-0.4, w3_rv=102.0),
                    _snap(ret_from_entry_pct=12.0, gap_mv_w20_w5=7.5, drv_w3=-0.5, w3_rv=101.5),
                ],
            }
        ],
    }
    payload = run_c18acc_kinematic_gap_rv3_study(cache, full_grid=False)
    assert payload["schema"] == "c18acc_kinematic_gap_rv3-v1"
    assert payload["n_variants"] > 0


def test_truncate_leg_hold_days():
    t0 = _snap(ret_from_entry_pct=0, trade_date="2025-01-02")
    t1 = _snap(ret_from_entry_pct=3.0, trade_date="2025-01-03")
    t2 = _snap(ret_from_entry_pct=6.0, trade_date="2025-01-06")
    t3 = _snap(ret_from_entry_pct=9.0, trade_date="2025-01-07")
    leg = {
        "entry_date": "2025-01-02",
        "return_pct": 12.0,
        "peak_ret_pct": 15.0,
        "peak_frame_idx": 3,
        "entry_t0": t0,
        "timeline": [t0, t1, t2, t3],
    }
    capped = truncate_leg_to_hold_days(leg, 2)
    assert capped is not None
    assert capped["return_pct"] == 3.0
    assert len(capped["timeline"]) == 2


def test_hold_cap_study_smoke():
    cache = {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": "2025-01-02",
        "date_end": "2025-06-01",
        "n_slots": 99,
        "legs": [
            {
                "entry_date": "2025-01-02",
                "return_pct": 5.0,
                "peak_ret_pct": 12.0,
                "peak_frame_idx": 2,
                "entry_t0": _snap(ret_from_entry_pct=0, trade_date="2025-01-02", gap_mv_w20_w5=0.5),
                "timeline": [
                    _snap(ret_from_entry_pct=0, trade_date="2025-01-02", gap_mv_w20_w5=0.5),
                    _snap(
                        ret_from_entry_pct=11.0,
                        trade_date="2025-01-03",
                        gap_mv_w20_w5=7.0,
                        drv_w3=-0.4,
                    ),
                    _snap(ret_from_entry_pct=12.0, trade_date="2025-01-06", gap_mv_w20_w5=7.5),
                ],
            }
        ],
    }
    payload = run_hold_cap_gap_rv3_study(cache, hold_days_list=(2,))
    assert payload["schema"] == "c18acc_kinematic_hold_cap_gap_rv3-v1"
    assert "2" in payload["by_hold_days"]
