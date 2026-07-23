"""Tests for ABC v3+f1 kinematic lead-lag study."""

from __future__ import annotations

from research.backtest.archive.abc_v3_f1_kinematic_leadlag_study import (
    KinematicSnap,
    compute_lead_lag,
    mark_leg_events,
    render_abc_v3_f1_kinematic_leadlag_study_md,
    split_cohorts,
    summarize_cohort,
)


def _snap(
    i: int,
    *,
    px: float,
    ret: float,
    w3_streak: int = 0,
    w5_streak: int = 0,
    w20_streak: int = 0,
) -> KinematicSnap:
    return KinematicSnap(
        frame_idx=i,
        trade_date="2026-01-02",
        minute="09:30",
        px=px,
        ret_from_entry_pct=ret,
        w20_rv=None,
        w20_mv=100.0,
        w20_quad=None,
        w5_rv=None,
        w5_mv=100.0,
        w5_quad=None,
        w3_rv=None,
        w3_mv=100.0,
        w3_quad=None,
        spread_class=None,
        spread_dist=None,
        gap_mv_w20_w5=None,
        gap_mv_w5_w3=None,
        gap_mv_w20_w3=None,
        gap_rv_w20_w5=None,
        gap_rv_w5_w3=None,
        gap_rv_w20_w3=None,
        gap_rv_w5_w20=None,
        dmv_w20=None,
        dmv_w5=None,
        dmv_w3=None,
        drv_w20=None,
        drv_w5=None,
        drv_w3=None,
        mv_order="",
        mv_rank_w20=None,
        mv_rank_w5=None,
        mv_rank_w3=None,
        w20_mv_decline_streak=w20_streak,
        w5_mv_decline_streak=w5_streak,
        w3_mv_decline_streak=w3_streak,
        parallel_3leg_dmv_deepening=False,
    )


def test_split_cohorts_champion_failure_baseline():
    rows = [{"net_pct": x, "stock_id": str(i)} for i, x in enumerate(range(-5, 16))]
    cohorts = split_cohorts(rows, top_pct=20, bottom_pct=20, champion_min=5.0, failure_max=-2.0)
    assert len(cohorts["champion"]) >= 3
    assert len(cohorts["failure"]) >= 3
    assert len(cohorts["baseline"]) >= 1
    assert all(float(r["net_pct"]) >= 5.0 or r in cohorts["champion"] for r in cohorts["champion"])
    assert all(float(r["net_pct"]) <= -2.0 or r in cohorts["failure"] for r in cohorts["failure"])


def test_mark_leg_events_price_peak_and_mv_drops():
    timeline = [
        _snap(0, px=100.0, ret=0.0),
        _snap(1, px=108.0, ret=8.0, w3_streak=1),
        _snap(2, px=110.0, ret=10.0, w5_streak=1),
        _snap(3, px=108.0, ret=8.0, w20_streak=1),
    ]
    events = mark_leg_events(timeline)
    assert events["T_price_peak"] == 2
    assert events["T_price_peak_ret_pct"] == 10.0
    assert events["T_w3_mv_drop"] == 1
    assert events["T_w5_mv_drop"] == 2
    assert events["T_w20_mv_drop"] == 3
    assert events["mv_drop_cascade"] == "w3>w5>w20"
    assert events["T_giveback_1pct"] == 3
    assert events["T_giveback_2pct"] == 3


def test_compute_lead_lag_positive_before_peak():
    events = {
        "T_price_peak": 4,
        "mv_drop_cascade": "w3>w5>w20",
        "T_w3_mv_drop": 1,
        "T_w5_mv_drop": 2,
        "T_w20_mv_drop": 3,
        "T_giveback_1pct": 5,
        "T_giveback_2pct": 6,
    }
    ll = compute_lead_lag(events, n_snaps=7)
    assert ll["w3_lead_peak_polls"] == 3
    assert ll["w5_lead_peak_polls"] == 2
    assert ll["w20_lead_peak_polls"] == 1
    assert ll["w3_before_peak"] is True
    assert ll["w3_lead_giveback_1pct_polls"] == 4
    assert ll["cascade_is_w3_w5_w20"] is True


def test_summarize_cohort_aggregates():
    rows = [
        {
            "net_pct": 8.0,
            "events": {"mv_drop_cascade": "w3>w5>w20"},
            "lead_lag": {
                "w3_before_peak": True,
                "w3_lead_peak_polls": 2.0,
                "cascade_is_w3_first": True,
                "cascade_is_w3_w5_w20": True,
            },
        },
        {
            "net_pct": 6.0,
            "events": {"mv_drop_cascade": "w3>w20>w5"},
            "lead_lag": {
                "w3_before_peak": False,
                "w3_lead_peak_polls": -1.0,
                "cascade_is_w3_first": True,
                "cascade_is_w3_w5_w20": False,
            },
        },
    ]
    s = summarize_cohort(rows, "champion")
    assert s["n"] == 2
    assert s["mean_net_pct"] == 7.0
    assert s["pct_w3_before_peak"] == 50.0
    assert s["mean_w3_lead_peak_polls"] == 0.5
    assert s["pct_cascade_w3_w5_w20"] == 50.0


def test_render_leadlag_md():
    payload = {
        "date_start": "2024-01-02",
        "date_end": "2026-07-08",
        "ledger_source": "/tmp/20260708_c18acc_abc_v3_f1_comparison.json",
        "n_legs_analyzed": 100,
        "poll": "30m",
        "kbar_bar_minutes": 5,
        "cohort_rules": {
            "champion": "top 15%",
            "failure": "bottom 15%",
            "baseline": "middle",
        },
        "cohort_counts": {"champion": 15, "failure": 15, "baseline": 70},
        "cohort_summaries": [
            {
                "cohort": "champion",
                "n": 15,
                "mean_net_pct": 8.2,
                "pct_w3_before_peak": 70.0,
                "mean_w3_lead_peak_polls": 1.5,
                "mean_w5_lead_peak_polls": 0.8,
                "mean_w20_lead_peak_polls": 0.2,
                "pct_cascade_w3_w5_w20": 40.0,
                "mean_w3_lead_giveback_1pct_polls": 2.0,
                "mean_w3_lead_giveback_2pct_polls": 3.0,
                "mv_drop_cascade_top": [{"cascade": "w3>w5>w20", "n": 6, "pct": 40.0}],
            },
            {
                "cohort": "failure",
                "n": 15,
                "mean_net_pct": -3.1,
                "pct_w3_before_peak": 85.0,
                "mean_w3_lead_peak_polls": 2.1,
                "mean_w5_lead_peak_polls": 1.2,
                "mean_w20_lead_peak_polls": 0.5,
                "pct_cascade_w3_w5_w20": 55.0,
                "mean_w3_lead_giveback_1pct_polls": 1.0,
                "mean_w3_lead_giveback_2pct_polls": 1.5,
                "mv_drop_cascade_top": [{"cascade": "w3>w5>w20", "n": 8, "pct": 53.3}],
            },
            {"cohort": "baseline", "n": 70, "mean_net_pct": 1.2, "pct_w3_before_peak": 60.0},
        ],
    }
    md = render_abc_v3_f1_kinematic_leadlag_study_md(payload)
    assert "kinematic lead-lag" in md
    assert "champion" in md
    assert "w3>w5>w20" in md
