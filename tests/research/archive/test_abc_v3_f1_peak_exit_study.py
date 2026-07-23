"""Tests for ABC v3+f1 peak exit study."""

from __future__ import annotations

from research.backtest.archive.abc_v3_f1_peak_exit_study import (
    _dist_rows,
    _mean,
    _mv_declining,
    _pct,
    _wma_combo,
    profit_gated_exit_rule_specs,
    render_abc_v3_f1_peak_exit_study_md,
    simulate_exit_rule,
    simulate_profit_gated_exit_rule,
    snap_peak_features,
    summarize_peak_patterns,
)


def test_pct_and_mean_helpers():
    assert _pct(1, 4) == 25.0
    assert _mean([1.0, 3.0]) == 2.0
    assert _mean([]) is None


def test_snap_peak_features_empty():
    assert snap_peak_features(None) == {}


def test_wma_combo():
    assert _wma_combo({"w20q": "leading", "w5q": "leading", "w3q": "leading"}) == "leading/leading/leading"


def test_summarize_peak_patterns():
    rows = [
        {
            "net_pct": 8.0,
            "mfe_pct": 10.0,
            "giveback_pct": 2.0,
            "peak_is_exit": False,
            "features": {"spread_class": "pullback", "w3_rv": 99.0},
            "peak_feats": {"spread_class": "mixed", "w3_rv": 102.5, "w20_rv": 104.0, "w5_rv": 103.0},
            "peak_spread_transition": "pullback→mixed",
            "peak_wma_combo": "leading/leading/leading",
            "w3_rv_delta": 3.5,
            "w20_rv_delta": 2.0,
        },
        {
            "net_pct": -2.0,
            "mfe_pct": 1.0,
            "giveback_pct": 3.0,
            "peak_is_exit": True,
            "features": {"spread_class": "pullback", "w3_rv": 99.5},
            "peak_feats": {"spread_class": "pullback", "w3_rv": 100.0, "w20_rv": 102.0, "w5_rv": 100.5},
            "peak_spread_transition": "pullback→pullback",
            "peak_wma_combo": "leading/weakening/lagging",
            "w3_rv_delta": 0.5,
            "w20_rv_delta": 0.2,
        },
    ]
    summary = summarize_peak_patterns(rows)
    assert summary["n"] == 2
    assert summary["big_win_n"] == 1
    assert summary["delta_big_win"]["w3_rv_delta"] == 3.5
    keys = {row["key"] for row in summary["spread_transitions_all"]}
    assert keys == {"pullback→mixed", "pullback→pullback"}


def test_dist_rows():
    rows = [{"x": 1}, {"x": 1}, {"x": 2}]
    out = _dist_rows(rows, lambda r: r["x"])
    assert out[0]["key"] == 1
    assert out[0]["n"] == 2


def test_simulate_exit_rule_first_hit():
    frames = [
        {"date": "2026-01-02", "minute": "09:30"},
        {"date": "2026-01-02", "minute": "10:00"},
        {"date": "2026-01-05", "minute": "13:30"},
    ]
    stock_maps = {
        "2330": [
            {"spread_class": "pullback", "w3": {"rs_ratio": 99.0}},
            {"spread_class": "mixed", "w3": {"rs_ratio": 103.5}},
            {"spread_class": "pullback", "w3": {"rs_ratio": 101.0}},
        ]
    }

    class _Conn:
        pass

    rows = [
        {
            "stock_id": "2330",
            "entry_frame_idx": 0,
            "exit_frame_idx": 2,
            "entry_px": 100.0,
            "exit_px": 110.0,
            "net_pct": 9.415,
            "features": {"spread_class": "pullback", "w3_rv": 99.0},
        }
    ]

    def _price(_cache, _conn, _sid, date, minute):
        return {"2026-01-02|09:30": 100.0, "2026-01-02|10:00": 108.0, "2026-01-05|13:30": 110.0}[
            f"{date}|{minute}"
        ]

    import research.backtest.archive.abc_v3_f1_peak_exit_study as mod

    orig = mod._price_at
    mod._price_at = lambda cache, conn, sid, d, m: _price(cache, conn, sid, d, m)
    try:
        result = simulate_exit_rule(
            _Conn(),
            rows,
            frames=frames,
            stock_maps=stock_maps,
            rule_id="pullback_to_mixed",
            predicate=lambda p, fe: fe.get("spread_class") == "pullback"
            and p.get("spread_class") == "mixed",
        )
    finally:
        mod._price_at = orig

    assert result["trigger_rate_pct"] == 100.0
    assert result["mean_net_pct"] == 7.42


def test_render_peak_exit_md():
    payload = {
        "window": {"date_start": "2024-01-02", "date_end": "2026-07-08", "n_trading_days": 600},
        "contract": {"matcher": "f1", "exit_baseline": "hold_3d", "peak_definition": "mfe"},
        "panel_meta": {"n_candidates": 500, "n_trades": 400},
        "summary": {
            "mean_net_pct": 2.5,
            "win_rate_pct": 54.0,
            "mean_mfe_pct": 6.8,
            "mean_giveback_pct": 3.7,
            "peak_at_exit_pct": 2.0,
            "oracle_peak_exit_mean_pct": 6.2,
            "big_win_peak_spread": [{"key": "pullback", "n": 30, "pct": 65.0}],
            "big_win_wma_combo": [{"key": "leading/leading/leading", "n": 40, "pct": 90.0}],
            "delta_big_win": {"w3_rv_delta": 3.3, "w20_rv_delta": 2.2, "peak_w20_rv": 104.6},
        },
        "exit_rules": [
            {
                "rule_id": "hold_3d_baseline",
                "trigger_rate_pct": 0.0,
                "mean_net_pct": 2.5,
                "delta_vs_baseline_pp": 0.0,
                "win_rate_pct": 54.0,
                "mean_save_vs_hold_pct": 0.0,
            }
        ],
        "top_trades": [
            {
                "stock_id": "6239",
                "entry_date": "2026-05-20",
                "entry_minute": "09:30",
                "net_pct": 32.0,
                "mfe_pct": 33.0,
                "giveback_pct": 0.0,
                "peak_spread": "mixed",
                "peak_wma_combo": "leading/leading/leading",
            }
        ],
        "verdict": {
            "summary": "test",
            "reasons": ["reason"],
            "recommend_rules": ["w3_delta_gte_3"],
            "avoid_rules": ["spread_extension"],
        },
    }
    md = render_abc_v3_f1_peak_exit_study_md(payload)
    assert "ABC v3+f1 · 价峰 WMA" in md
    assert "6239" in md
    assert "w3_delta_gte_3" in md


def test_mv_declining_consecutive():
    points = [
        {"w3": {"rs_momentum": 101.0}},
        {"w3": {"rs_momentum": 100.5}},
        {"w3": {"rs_momentum": 100.0}},
    ]
    assert _mv_declining(points, 2, "w3", consecutive=1) is True
    assert _mv_declining(points, 2, "w3", consecutive=2) is True
    assert _mv_declining(points, 1, "w3", consecutive=2) is False


def test_profit_gated_specs_include_user_hypothesis():
    ids = {s["id"] for s in profit_gated_exit_rule_specs()}
    assert "profit5_w3_or_w5_mv_d1" in ids
    assert "profit5_w3_or_w5_mv_d2" in ids


def test_simulate_profit_gated_exit_rule():
    frames = [
        {"date": "2026-01-02", "minute": "09:30"},
        {"date": "2026-01-02", "minute": "10:00"},
        {"date": "2026-01-02", "minute": "10:30"},
        {"date": "2026-01-05", "minute": "13:30"},
    ]
    stock_maps = {
        "2330": [
            {"w3": {"rs_momentum": 100.0}, "w5": {"rs_momentum": 101.0}},
            {"w3": {"rs_momentum": 101.0}, "w5": {"rs_momentum": 101.0}},
            {"w3": {"rs_momentum": 100.5}, "w5": {"rs_momentum": 100.8}},
            {"w3": {"rs_momentum": 99.0}, "w5": {"rs_momentum": 99.0}},
        ]
    }

    class _Conn:
        pass

    rows = [
        {
            "stock_id": "2330",
            "entry_frame_idx": 0,
            "exit_frame_idx": 3,
            "entry_px": 100.0,
            "exit_px": 112.0,
            "net_pct": 11.415,
            "features": {"spread_class": "pullback"},
        }
    ]

    import research.backtest.archive.abc_v3_f1_peak_exit_study as mod

    orig = mod._price_at
    prices = {
        ("2330", "2026-01-02", "09:30"): 100.0,
        ("2330", "2026-01-02", "10:00"): 104.0,
        ("2330", "2026-01-02", "10:30"): 106.0,
        ("2330", "2026-01-05", "13:30"): 112.0,
    }
    mod._price_at = lambda cache, conn, sid, d, m: prices.get((sid, d, m))
    try:
        result = simulate_profit_gated_exit_rule(
            _Conn(),
            rows,
            frames=frames,
            stock_maps=stock_maps,
            rule_id="profit5_w3_mv_d1",
            profit_min_gross_pct=5.0,
            legs=("w3",),
            decline_polls=1,
        )
    finally:
        mod._price_at = orig

    assert result["trigger_rate_pct"] == 100.0
    assert result["mean_net_pct"] == 5.42
