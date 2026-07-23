"""Tests for C18acc kinematic exit sweep rule evaluators."""

from __future__ import annotations

from research.backtest.archive.c18acc_kinematic_exit_sweep import (
    RuleParams,
    _evaluate_leg,
    _pred_r1,
    _pred_r4,
    _pred_r7,
    run_c18acc_kinematic_exit_sweep,
    run_c18acc_kinematic_gap_sweep,
)


def _snap(
    *,
    dmv_w20: float = 0.0,
    dmv_w5: float = -0.3,
    dmv_w3: float = -0.35,
    w20_mv: float = 1.2,
    w5_mv: float = 0.8,
    w3_mv: float = 0.5,
    gap_rv_w5_w20: float = -5.0,
    parallel: bool = False,
    spread_class: str = "aligned",
    spread_dist: float = 4.0,
    ret_from_entry_pct: float = 6.0,
) -> dict:
    return {
        "dmv_w20": dmv_w20,
        "dmv_w5": dmv_w5,
        "dmv_w3": dmv_w3,
        "w20_mv": w20_mv,
        "w5_mv": w5_mv,
        "w3_mv": w3_mv,
        "gap_rv_w5_w20": gap_rv_w5_w20,
        "parallel_3leg_dmv_deepening": parallel,
        "spread_class": spread_class,
        "spread_dist": spread_dist,
        "ret_from_entry_pct": ret_from_entry_pct,
    }


def test_r1_fires_on_relative_fatigue():
    entry = _snap(dmv_w20=0, dmv_w5=0, dmv_w3=0, gap_rv_w5_w20=-3.0)
    snap = _snap(dmv_w20=-0.05, dmv_w5=-0.3, dmv_w3=-0.4, gap_rv_w5_w20=-6.0)
    params = RuleParams("R1", 0.25, 0.1, 1, 8.0, False)
    assert _pred_r1(snap, entry, [entry, snap], params)


def test_r1_rejects_w20_weak():
    entry = _snap(gap_rv_w5_w20=-3.0)
    snap = _snap(dmv_w20=-0.3, dmv_w5=-0.4, dmv_w3=-0.5)
    params = RuleParams("R1", 0.25, 0.1, 1, 8.0, False)
    assert not _pred_r1(snap, entry, [entry, snap], params)


def test_r4_requires_parallel_deepening():
    entry = _snap(gap_rv_w5_w20=-2.0)
    s1 = _snap(parallel=True, gap_rv_w5_w20=-5.0)
    s2 = _snap(parallel=True, gap_rv_w5_w20=-7.0)
    params = RuleParams("R4", 0.25, 0.1, 2, 8.0, True)
    assert _pred_r4(s2, entry, [entry, s1, s2], params)


def test_evaluate_leg_on_synthetic_timeline():
    entry = _snap(dmv_w20=0, dmv_w5=0, dmv_w3=0, gap_rv_w5_w20=-2.0)
    trigger = _snap(dmv_w20=-0.05, dmv_w5=-0.3, dmv_w3=-0.35, gap_rv_w5_w20=-5.0)
    leg = {
        "return_pct": 4.0,
        "outcome": "win",
        "peak_ret_pct": 8.0,
        "peak_frame_idx": 2,
        "entry_t0": entry,
        "timeline": [entry, trigger, _snap(ret_from_entry_pct=8.0)],
    }
    params = RuleParams("R1", 0.25, 0.1, 1, 8.0, False)
    result = _evaluate_leg(leg, params)
    assert result["fired"]
    assert result["exit_ret_pct"] == 6.0
    assert result["at_or_before_peak"]


def test_r7_fires_on_gap_threshold():
    entry = _snap(gap_rv_w5_w20=-3.0)
    entry["gap_mv_w20_w5"] = 0.5
    snap = _snap(gap_rv_w5_w20=-6.0)
    snap["gap_mv_w20_w5"] = 6.2
    params = RuleParams(
        "R7",
        0.0,
        0.1,
        1,
        0.0,
        False,
        gap_w20_w5_min=6.0,
    )
    assert _pred_r7(snap, entry, [entry, snap], params)


def test_r7_rejects_small_gap():
    entry = _snap()
    entry["gap_mv_w20_w5"] = 1.0
    snap = _snap()
    snap["gap_mv_w20_w5"] = 4.0
    params = RuleParams("R7", 0.0, 0.1, 1, 0.0, False, gap_w20_w5_min=6.0)
    assert not _pred_r7(snap, entry, [entry, snap], params)


def test_run_sweep_on_minimal_cache():
    cache = {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": "2025-01-02",
        "date_end": "2025-06-01",
        "n_slots": 3,
        "legs": [
            {
                "leg_id": "a|2330|0",
                "return_pct": 5.0,
                "outcome": "win",
                "peak_ret_pct": 10.0,
                "peak_frame_idx": 2,
                "entry_t0": _snap(dmv_w20=0, dmv_w5=0, dmv_w3=0),
                "timeline": [
                    _snap(dmv_w20=0, dmv_w5=0, dmv_w3=0),
                    _snap(dmv_w20=-0.05, dmv_w5=-0.3, dmv_w3=-0.35),
                    _snap(ret_from_entry_pct=10.0),
                ],
            }
        ],
    }
    payload = run_c18acc_kinematic_exit_sweep(cache)
    assert payload["n_legs"] == 1
    assert payload["n_variants"] > 0
    assert payload["best_by_family"]["R1"]["n_fired"] >= 1


def test_run_gap_sweep_on_minimal_cache():
    cache = {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": "2025-01-02",
        "date_end": "2025-06-01",
        "n_slots": 99,
        "legs": [
            {
                "leg_id": "a|2330|0",
                "return_pct": 5.0,
                "outcome": "win",
                "peak_ret_pct": 10.0,
                "peak_frame_idx": 2,
                "entry_t0": {**_snap(), "gap_mv_w20_w5": 0.5},
                "timeline": [
                    {**_snap(), "gap_mv_w20_w5": 0.5},
                    {**_snap(), "gap_mv_w20_w5": 6.5, "spread_class": "pullback", "spread_dist": 9.0},
                    _snap(ret_from_entry_pct=10.0),
                ],
            }
        ],
    }
    payload = run_c18acc_kinematic_gap_sweep(cache)
    assert payload["n_legs"] == 1
    assert payload["schema"] == "c18acc_kinematic_gap_sweep-v1"
    assert payload["n_variants"] > 0
