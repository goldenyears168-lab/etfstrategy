#!/usr/bin/env python3
"""Unit tests for TF-2 trough-find pure metric helpers."""

from __future__ import annotations

import math

import pandas as pd

from research.backtest.detach_gate_tf2_trough_find_study import (
    aggregate_tf2,
    first_dspread_flip_idx,
    first_improve_idx,
    lead_minutes,
    poll_hhmm_to_minutes,
    post_event_trough_idx,
)


def test_poll_and_lead_minutes():
    assert poll_hhmm_to_minutes("09:40") == 9 * 60 + 40
    assert poll_hhmm_to_minutes("11:15") == 11 * 60 + 15
    assert lead_minutes("09:40", "11:15") == 95.0
    assert lead_minutes("11:15", "09:40") == -95.0
    assert lead_minutes("10:00", "10:00") == 0.0
    assert lead_minutes(None, "10:00") is None


def test_first_improve_and_flip():
    poll = pd.DataFrame(
        {
            "poll": ["09:40", "09:45", "09:50", "09:55", "10:00"],
            "gap_pulse": ["WORSEN", "FLAT", "IMPROVE", "WORSEN", "IMPROVE"],
            "d_spread_30m": [-0.2, -0.1, 0.08, -0.05, 0.1],
            "tw_from_open": [-0.6, -0.8, -1.0, -1.2, -0.9],
            "spread_30m": [-0.7, -0.8, -0.7, -0.75, -0.6],
        }
    )
    assert first_improve_idx(poll, 0) == 2
    assert first_improve_idx(poll, 3) == 4
    assert first_dspread_flip_idx(poll, 0) == 2
    assert post_event_trough_idx(poll, 0) == 3


def test_aggregate_before_trough():
    rows = [
        {
            "has_event": True,
            "meaningful_trough": True,
            "improve_poll": "10:00",
            "improve_lead_min": 30.0,
            "improve_before_trough": True,
            "improve_after_trough": False,
            "improve_headroom_vs_trough_pct": 0.2,
            "improve_buy_near_trough": True,
        },
        {
            "has_event": True,
            "meaningful_trough": True,
            "improve_poll": "11:30",
            "improve_lead_min": -20.0,
            "improve_before_trough": False,
            "improve_after_trough": True,
            "improve_headroom_vs_trough_pct": 0.5,
            "improve_buy_near_trough": False,
        },
        {
            "has_event": True,
            "meaningful_trough": False,
            "improve_poll": "10:05",
            "improve_lead_min": 5.0,
            "improve_before_trough": True,
            "improve_after_trough": False,
            "improve_headroom_vs_trough_pct": 0.1,
            "improve_buy_near_trough": True,
        },
        {"has_event": False},
    ]
    agg = aggregate_tf2(rows, signal="improve")
    assert agg["n_event_days"] == 3
    assert agg["n_mt_with_signal"] == 2
    assert agg["n_before"] == 1
    assert agg["n_after"] == 1
    assert agg["median_lead_min_before"] == 30.0
    assert math.isclose(agg["fraction_signal_before_trough"], 0.5)
    assert math.isclose(agg["fpr_signal_after_trough"], 0.5)
    assert math.isclose(agg["fpr_improve_no_meaningful_trough"], 1.0)
