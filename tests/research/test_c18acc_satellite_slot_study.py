"""Unit tests for C18acc satellite slot study helpers."""

from __future__ import annotations

from research.backtest.c18acc_satellite_slot_study import (
    evaluate_satellite_verdict,
    marginal_satellite_periods,
    render_c18acc_satellite_slot_md,
)


def test_marginal_satellite_periods_excludes_core_keys():
    core = [
        {"stock_id": "2330", "entry_date": "2025-01-02", "return_pct": 1.0},
        {"stock_id": "2317", "entry_date": "2025-01-03", "return_pct": 2.0},
    ]
    expanded = [
        {"stock_id": "2330", "entry_date": "2025-01-02", "return_pct": 1.0},
        {"stock_id": "2454", "entry_date": "2025-01-02", "return_pct": 3.0},
        {"stock_id": "2317", "entry_date": "2025-01-03", "return_pct": 2.0},
        {"stock_id": "2382", "entry_date": "2025-01-04", "return_pct": 4.0},
        {"stock_id": "2454", "entry_date": "2025-01-02", "return_pct": 9.0},  # dup
    ]
    sat = marginal_satellite_periods(expanded, core)
    keys = {(p["stock_id"], p["entry_date"]) for p in sat}
    assert keys == {("2454", "2025-01-02"), ("2382", "2025-01-04")}
    assert all(p.get("sleeve") == "satellite" for p in sat)


def test_evaluate_keep_when_phase0_fails():
    payload = {
        "phase0": {
            "hypothesis_checks": {
                "H-SAT-D0-1_pass": False,
                "H-SAT-D0-2_pass": True,
                "H-SAT-D0-3_pass": True,
            }
        },
        "variants": {
            "B0": {
                "nav_windows": {
                    "oos": {
                        "total_return_pct": 10.0,
                        "max_drawdown_pct": -5.0,
                        "sharpe_ratio": 1.0,
                    },
                    "is": {"total_return_pct": 8.0},
                },
                "core_excess": {"oos": {"mean_excess_pct": 7.0}},
            },
            "S1_CORE3_SAT1": {
                "nav_windows": {
                    "oos": {
                        "total_return_pct": 11.0,
                        "max_drawdown_pct": -4.0,
                        "sharpe_ratio": 1.1,
                    },
                    "is": {"total_return_pct": 8.5},
                },
                "core_excess": {"oos": {"mean_excess_pct": 7.0}},
            },
            "S2_CORE3_SAT2": {
                "nav_windows": {"oos": {}, "is": {}},
                "core_excess": {"oos": {}},
            },
            "S3_EQ4": {
                "nav_windows": {"oos": {}, "is": {}},
                "core_excess": {"oos": {}},
            },
            "S4_SAT_IN_SWAP": {
                "nav_windows": {"oos": {}, "is": {}},
                "core_excess": {"oos": {}},
            },
        },
    }
    v = evaluate_satellite_verdict(payload)
    assert v["verdict"] == "KEEP_CORE_ONLY"
    assert v["phase0_ok"] is False


def test_evaluate_go_sat1_when_gates_pass():
    payload = {
        "phase0": {
            "hypothesis_checks": {
                "H-SAT-D0-1_pass": True,
                "H-SAT-D0-2_pass": True,
                "H-SAT-D0-3_pass": True,
            }
        },
        "variants": {
            "B0": {
                "nav_windows": {
                    "oos": {
                        "total_return_pct": 10.0,
                        "max_drawdown_pct": -5.0,
                        "sharpe_ratio": 1.0,
                    },
                    "is": {"total_return_pct": 8.0},
                },
                "core_excess": {"oos": {"mean_excess_pct": 7.0}},
            },
            "S1_CORE3_SAT1": {
                "nav_windows": {
                    "oos": {
                        "total_return_pct": 11.0,
                        "max_drawdown_pct": -4.5,
                        "sharpe_ratio": 1.05,
                    },
                    "is": {"total_return_pct": 8.2},
                },
                "core_excess": {"oos": {"mean_excess_pct": 7.0}},
            },
            "S2_CORE3_SAT2": {
                "nav_windows": {"oos": {}, "is": {}},
                "core_excess": {"oos": {}},
            },
            "S3_EQ4": {
                "nav_windows": {
                    "oos": {
                        "total_return_pct": 9.0,
                        "max_drawdown_pct": -6.0,
                        "sharpe_ratio": 0.9,
                    },
                    "is": {"total_return_pct": 7.0},
                },
                "core_excess": {"oos": {"mean_excess_pct": 6.0}},
            },
            "S4_SAT_IN_SWAP": {
                "nav_windows": {"oos": {}, "is": {}},
                "core_excess": {"oos": {}},
            },
        },
    }
    v = evaluate_satellite_verdict(payload)
    assert v["verdict"] == "GO_SAT1_OBSERVE"
    assert v["H-SAT-1"]["pass"] is True


def test_render_md_smoke():
    payload = {
        "verdict": {"verdict": "KEEP_CORE_ONLY", "summary": "KEEP_CORE_ONLY · test"},
        "date_start": "2025-01-02",
        "date_end": "2026-07-09",
        "is_end": "2025-12-31",
        "oos_start": "2026-01-02",
        "economics": {
            "nav_book_twd": 100000,
            "core_budget_twd": 20000,
            "sat_budget_twd": 5000,
        },
        "phase0": {
            "pool_stats": {"days_ge4": 10, "n_trade_dates": 100, "pct_ge4": 10.0},
            "hypothesis_checks": {
                "H-SAT-D0-1_pass": True,
                "H-SAT-D0-2_pass": False,
                "H-SAT-D0-2_sat1_oos_mean_excess_pct": -1.0,
                "H-SAT-D0-2_sat1_oos_n": 5,
                "H-SAT-D0-3_corr": 0.9,
                "H-SAT-D0-3_pass": False,
            },
        },
        "variants": {},
        "method_notes": ["note"],
        "plan_artifact": "reports/research/rrg/20260715_c18acc_satellite_slot_research_plan.md",
    }
    md = render_c18acc_satellite_slot_md(payload)
    assert "KEEP_CORE_ONLY" in md
    assert "Phase 0" in md
