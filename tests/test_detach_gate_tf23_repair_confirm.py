#!/usr/bin/env python3
"""Unit tests for TF-2∩TF-3 repair-confirm pure helpers."""

from __future__ import annotations

import math

import pandas as pd

from research.backtest.detach_gate_tf23_repair_confirm_study import (
    VariantSpec,
    aggregate_variant,
    before_cutoff,
    find_signal_idx,
    first_tf3_idx,
    tf3_bar_ok,
)


def _poll() -> pd.DataFrame:
    # Event at 0; IMPROVE at 1; TF-3 bar at 3 (both↑, TW outperforms by 0.12)
    return pd.DataFrame(
        {
            "poll": ["09:40", "09:45", "09:50", "10:00", "10:30", "12:40"],
            "gap_pulse": ["WORSEN", "IMPROVE", "FLAT", "FLAT", "FLAT", "FLAT"],
            "tw_from_open": [-1.0, -1.2, -1.5, -1.4, -1.1, -0.9],
            "nq_from_open": [-0.4, -0.5, -0.6, -0.5, -0.3, -0.2],
            "tw_bar_roc": [float("nan"), -0.2, -0.3, 0.10, 0.30, 0.20],
            "nq_bar_roc": [float("nan"), -0.1, -0.1, 0.05, 0.20, 0.15],
            "sync_pulse": ["OPPOSE", "FOLLOW", "QUIET", "FOLLOW", "FOLLOW", "FOLLOW"],
            "spread_30m": [-0.7, -0.8, -0.9, -0.85, -0.7, -0.6],
            "d_spread_30m": [float("nan"), -0.1, -0.1, 0.05, 0.15, 0.1],
        }
    )


def test_tf3_bar_ok_delta_units():
    row = _poll().loc[3]
    assert tf3_bar_ok(row, delta=0.05) is True  # 0.10 >= 0.05 + 0.05
    assert tf3_bar_ok(row, delta=0.06) is False
    row_down = _poll().loc[2]
    assert tf3_bar_ok(row_down, delta=0.05) is False


def test_first_tf3_and_cutoff():
    poll = _poll()
    assert first_tf3_idx(poll, start_idx=0, delta=0.05) == 3
    assert first_tf3_idx(poll, start_idx=0, delta=0.05, require_follow=True) == 3
    assert first_tf3_idx(poll, start_idx=0, delta=0.05, cutoff="10:00") is None
    assert first_tf3_idx(poll, start_idx=0, delta=0.05, cutoff="10:01") == 3
    assert before_cutoff("12:40", "12:30") is False
    assert before_cutoff("12:25", "12:30") is True


def test_find_signal_arm_and_cooldown():
    poll = _poll()
    a = VariantSpec(id="A_tf2", family="A", label="IMPROVE")
    assert find_signal_idx(poll, event_idx=0, variant=a) == 1

    b = VariantSpec(id="B", family="B", label="TF-3", delta=0.05)
    assert find_signal_idx(poll, event_idx=0, variant=b) == 3

    c = VariantSpec(id="C", family="C", label="arm", delta=0.05, arm_improve=True)
    assert find_signal_idx(poll, event_idx=0, variant=c) == 3

    d = VariantSpec(
        id="D",
        family="D",
        label="cool",
        delta=0.05,
        arm_improve=True,
        cooldown_min=20,
        cooldown_from="improve",
    )
    # IMPROVE@09:45 +20m => need ≥10:05; first TF-3 at 10:00 fails, 10:30 ok
    assert find_signal_idx(poll, event_idx=0, variant=d) == 4

    g = VariantSpec(
        id="G",
        family="G",
        label="cut",
        delta=0.05,
        cutoff="12:30",
    )
    # Last bar 12:40 blocked; still fires at 10:00
    assert find_signal_idx(poll, event_idx=0, variant=g) == 3


def test_aggregate_variant_fpr():
    rows = [
        {
            "has_event": True,
            "meaningful_trough": True,
            "signals": {
                "X": {
                    "poll": "10:00",
                    "lead_min": 30.0,
                    "before_trough": True,
                    "after_trough": False,
                    "headroom_vs_trough_pct": 0.2,
                    "buy_near_trough": True,
                    "abs_entry_vs_trough_pct": 0.2,
                }
            },
        },
        {
            "has_event": True,
            "meaningful_trough": True,
            "signals": {
                "X": {
                    "poll": "11:30",
                    "lead_min": -15.0,
                    "before_trough": False,
                    "after_trough": True,
                    "headroom_vs_trough_pct": 0.4,
                    "buy_near_trough": False,
                    "abs_entry_vs_trough_pct": 0.4,
                }
            },
        },
        {
            "has_event": True,
            "meaningful_trough": False,
            "signals": {
                "X": {
                    "poll": "10:05",
                    "lead_min": 5.0,
                    "before_trough": True,
                    "after_trough": False,
                    "headroom_vs_trough_pct": 0.1,
                    "buy_near_trough": True,
                    "abs_entry_vs_trough_pct": 0.1,
                }
            },
        },
        {"has_event": False, "signals": {}},
    ]
    agg = aggregate_variant(rows, "X")
    assert agg["n_event_days"] == 3
    assert agg["n_mt_with_signal"] == 2
    assert agg["n_before"] == 1
    assert agg["n_after"] == 1
    assert agg["median_lead_min_before"] == 30.0
    assert agg["median_lag_min_after"] == 15.0
    assert math.isclose(agg["fraction_before_trough"], 0.5)
    assert math.isclose(agg["fpr_sig_on_no_mt"], 1.0)
