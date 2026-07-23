#!/usr/bin/env python3
"""Unit tests for Detach Gate long-sample rebuy adoption helpers."""

from __future__ import annotations

import math

import pandas as pd

from research.backtest.detach_gate_rebuy_adoption_study import (
    GATE_FPR,
    GATE_N_MT,
    GATE_N_RED,
    _wilson_ci_lo,
    enrich_poll_1h_native,
    evaluate_gates,
    pick_champion_1h,
    three_way_verdict,
    variant_catalog_1h,
)


def test_enrich_poll_1h_native_pulses():
    poll = pd.DataFrame(
        {
            "poll": ["10:00", "11:00", "12:00"],
            "tw_from_open": [-0.5, -0.8, -0.6],
            "nq_from_open": [-0.2, -0.3, -0.1],
            "spread_30m": [-0.7, -0.9, -0.6],
            "tw_win": [-0.5, -0.3, 0.2],
            "nq_win": [-0.2, -0.1, 0.05],
        }
    )
    out = enrich_poll_1h_native(poll)
    assert "gap_pulse" in out.columns
    assert "tw_bar_roc" in out.columns
    assert "sync_pulse" in out.columns
    # uses hour-bar win when present
    assert math.isclose(float(out.loc[2, "tw_bar_roc"]), 0.2)
    assert math.isclose(float(out.loc[2, "nq_bar_roc"]), 0.05)
    # Δspread -0.7→-0.9 = -0.2 → WORSEN; -0.9→-0.6 = +0.3 → IMPROVE
    assert out.loc[1, "gap_pulse"] == "WORSEN"
    assert out.loc[2, "gap_pulse"] == "IMPROVE"
    assert out.loc[2, "sync_pulse"] == "FOLLOW"


def test_variant_catalog_1h_has_champion_proxy():
    ids = {v.id for v in variant_catalog_1h()}
    assert "D_cool_imp_K60_d0.1" in ids
    assert "A_tf2" in ids
    assert "G_D_cool_imp_K60_d0.1_cut1300" in ids


def test_wilson_and_gates_fail_small_sample():
    assert _wilson_ci_lo(0, 10) == 0.0
    lo = _wilson_ci_lo(3, 10)
    assert lo is not None and 0.0 < lo < 0.5

    agg = {
        "n_event_days": 10,  # < GATE_N_RED
        "n_meaningful_trough": 5,
        "fpr_sig_on_no_mt": 0.8,
        "median_headroom_vs_trough_pct": 1.5,
        "buy_near_trough_rate": 0.0,
        "n_before": 5,
    }
    is_oos = {
        "cut_date": "2025-01-01",
        "n_is": 5,
        "n_oos": 5,
        "is": {"fpr_sig_on_no_mt": 0.7, "median_headroom_vs_trough_pct": 1.4},
        "oos": {"fpr_sig_on_no_mt": 0.9, "median_headroom_vs_trough_pct": 1.6},
    }
    ge = evaluate_gates(
        champion_id="D_cool_imp_K60_d0.1",
        agg=agg,
        is_oos=is_oos,
        day_rows=[{}] * 10,
        case_row=None,
        panel_5m_agg={"fpr_sig_on_no_mt": 0.75, "median_headroom_vs_trough_pct": 0.82},
    )
    assert ge["gates"]["A1_sample_floor"]["pass"] is False
    assert ge["gates"]["A3_fpr_no_mt"]["pass"] is False
    assert GATE_N_RED == 40 and GATE_N_MT == 25 and GATE_FPR == 0.40

    verd = three_way_verdict(ge, agg)
    assert verd["A_manual_sop"]["decision"] == "不採納"
    assert verd["B_strategy_advisory"]["decision"] == "不採納"
    assert verd["C_order_auto_rebuy"]["decision"] == "不採納"


def test_pick_champion_prefers_k60():
    aggs = [
        {
            "variant_id": "A_tf2",
            "n_mt_with_signal": 20,
            "fpr_sig_on_no_mt": 0.1,
            "median_headroom_vs_trough_pct": 0.5,
        },
        {
            "variant_id": "D_cool_imp_K60_d0.1",
            "n_mt_with_signal": 20,
            "fpr_sig_on_no_mt": 0.5,
            "median_headroom_vs_trough_pct": 0.9,
        },
    ]
    assert pick_champion_1h(aggs) == "D_cool_imp_K60_d0.1"
