#!/usr/bin/env python3
"""Minimal unit tests for Detach Gate trough-first readiness."""

from __future__ import annotations

import pandas as pd

from order.us_tw_5m_sell_gate import YELLOW
from research.backtest.us_tw_sell_only_live_readiness_study import (
    SellSpec,
    _confidence_verdict_5m,
    _detach_mask,
    frozen_sell_gate_spec,
    score_sell,
)


def test_frozen_spec_shape():
    s = frozen_sell_gate_spec()
    assert s.win_thr == -0.6
    assert s.confirm_polls == 1
    assert s.max_tw_from_open == -0.5
    assert s.arm_after == "09:40"


def test_frozen_red_fires_on_single_confirm():
    poll = pd.DataFrame(
        {
            "poll": ["09:35", "09:40", "09:45"],
            "spread_30m": [-0.7, -0.7, -0.2],
            "tw_from_open": [-0.6, -0.6, -0.4],
        }
    )
    m = _detach_mask(poll, frozen_sell_gate_spec())
    assert bool(m.iloc[0]) is False
    assert bool(m.iloc[1]) is True


def test_yellow_no_down_filter():
    poll = pd.DataFrame(
        {
            "poll": ["10:00", "10:05"],
            "spread_30m": [-0.7, -0.7],
            "tw_from_open": [0.2, 0.1],
        }
    )
    assert bool(_detach_mask(poll, YELLOW).iloc[1]) is True


def test_score_trough_gates():
    spec = SellSpec("x", "5m30", -0.6, 1, max_tw_from_open=-0.5, arm_after="09:40", arm_before="12:30")
    rows = []
    for i in range(8):
        rows.append(
            {
                "is_event3": False,
                "triggered": False,
                "sell_near_trough": False,
                "exit_headroom_vs_trough_pct": None,
                "edge_vs_hold_pct": 0.0,
                "false_alarm_opp_cost_pct": 0.0,
                "catastrophic_miss": False,
                "pnl_hold_close_pct": 0.1,
                "date": f"d{i}",
            }
        )
    for i, h in enumerate((1.5, 1.8, 2.1)):
        rows.append(
            {
                "is_event3": True,
                "triggered": True,
                "sell_near_trough": False,
                "exit_headroom_vs_trough_pct": h,
                "edge_vs_hold_pct": -0.2,
                "false_alarm_opp_cost_pct": 0.2,
                "catastrophic_miss": False,
                "pnl_hold_close_pct": -1.0,
                "date": f"e{i}",
            }
        )
    sc = score_sell(rows, spec)
    assert sc["objective"].startswith("trough")
    assert sc["gates"]["G5_sell_near0"] is True
    assert sc["gates"]["G6_med_headroom"] is True
    assert sc["gates"]["G7_min_headroom"] is True
    assert sc["median_exit_headroom_pct"] == 1.8


def test_verdict_trough_oos():
    v = _confidence_verdict_5m(
        {"live_ops_memo_ok": True, "order_auto_ok": False},
        is_oos={
            "oos": {
                "event_recall": 0.75,
                "catastrophic_miss_n": 0,
                "sell_near_trough_rate_on_events": 0.0,
                "median_exit_headroom_pct": 1.2,
            }
        },
    )
    assert v["level"] == "USABLE_5M"
    assert v["for_order_auto_sell_all"] is False
