"""Tests for C18acc kinematic combo + gap expansion study."""

from __future__ import annotations

from research.backtest.archive.c18acc_kinematic_combo_gap_study import (
    LayerSpec,
    evaluate_leg_gap_threshold,
    evaluate_leg_layered,
    run_c18acc_kinematic_combo_gap_study,
)


def _snap(**kw) -> dict:
    base = {
        "ret_from_entry_pct": 5.0,
        "spread_class": "aligned",
        "spread_dist": 4.0,
        "gap_mv_w20_w5": 2.0,
        "gap_mv_w20_w3": 2.5,
        "gap_mv_w5_w3": 0.5,
        "w20_mv": 1.0,
        "w5_mv": 0.5,
        "w3_mv": 0.4,
        "dmv_w20": 0.0,
        "dmv_w5": 0.0,
        "dmv_w3": 0.0,
    }
    base.update(kw)
    return base


def _leg(timeline: list[dict], **kw) -> dict:
    return {
        "return_pct": 4.0,
        "outcome": "win",
        "peak_ret_pct": 10.0,
        "peak_frame_idx": len(timeline) - 1,
        "entry_t0": timeline[0],
        "timeline": timeline,
        **kw,
    }


def test_r7_priority_uses_r7_when_both_fire():
    t0 = _snap(ret_from_entry_pct=0, spread_class="pullback", spread_dist=5.0, gap_mv_w20_w5=1.0)
    t1 = _snap(
        ret_from_entry_pct=8.0,
        spread_class="pullback",
        spread_dist=9.0,
        gap_mv_w20_w5=6.5,
    )
    t2 = _snap(
        ret_from_entry_pct=10.0,
        spread_class="pullback",
        spread_dist=9.0,
        gap_mv_w20_w5=7.0,
    )
    leg = _leg([t0, t1, t2])
    r = evaluate_leg_layered(leg, LayerSpec(mode="R7_priority"))
    assert r["fired"]
    assert r["source"] == "R7"
    assert r["exit_ret_pct"] == 10.0


def test_r2_then_r7_requires_r2_before_r7():
    t0 = _snap(ret_from_entry_pct=0, spread_class="pullback", spread_dist=5.0, gap_mv_w20_w5=0.5)
    t1 = _snap(
        ret_from_entry_pct=7.0,
        spread_class="pullback",
        spread_dist=9.0,
        gap_mv_w20_w5=6.2,
    )
    t2 = _snap(
        ret_from_entry_pct=9.0,
        spread_class="pullback",
        spread_dist=9.0,
        gap_mv_w20_w5=6.5,
    )
    leg = _leg([t0, t1, t2])
    r = evaluate_leg_layered(leg, LayerSpec(mode="R2_then_R7"))
    assert r["fired"]
    assert r["exit_ret_pct"] == 9.0


def test_gap_abs_threshold_fires():
    t0 = _snap(gap_mv_w20_w5=0.5, ret_from_entry_pct=0)
    t1 = _snap(gap_mv_w20_w5=6.2, ret_from_entry_pct=7.0)
    t2 = _snap(gap_mv_w20_w5=6.3, ret_from_entry_pct=8.0)
    leg = _leg([t0, t1, t2])
    r = evaluate_leg_gap_threshold(leg, pair="w20_w5", mode="abs", threshold=6.0, consecutive=2)
    assert r["fired"]
    assert r["exit_ret_pct"] == 8.0


def test_run_combo_gap_on_minimal_cache():
    cache = {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": "2025-01-02",
        "date_end": "2025-06-01",
        "n_slots": 99,
        "poll": "30m",
        "legs": [
            _leg(
                [
                    _snap(ret_from_entry_pct=0, gap_mv_w20_w5=0.5),
                    _snap(
                        ret_from_entry_pct=7.0,
                        gap_mv_w20_w5=6.5,
                        spread_class="pullback",
                        spread_dist=9.0,
                    ),
                    _snap(ret_from_entry_pct=10.0, gap_mv_w20_w5=7.0),
                ]
            )
        ],
    }
    payload = run_c18acc_kinematic_combo_gap_study(cache)
    assert payload["schema"] == "c18acc_kinematic_combo_gap-v1"
    assert payload["n_legs"] == 1
    assert len(payload["layer_combo"]["layer_summaries"]) >= 5
