"""Unit tests for C18acc old vs live gap attribution helpers."""

from __future__ import annotations

import unittest

from research.backtest.c18acc_old_vs_live_gap_attribution import (
    _attribution_verdict,
    _equal_slot_compound_pct,
    _is_cut_date,
    render_old_vs_live_gap_attribution_md,
)


class TestOldVsLiveGapAttribution(unittest.TestCase):
    def test_is_cut_date_70_30(self) -> None:
        dates = [f"2025-{m:02d}-01" for m in range(1, 11)]  # 10 months
        cut = _is_cut_date(dates, ratio=0.7)
        self.assertEqual(cut, dates[6])  # index 6 of 10

    def test_equal_slot_compound(self) -> None:
        periods = [
            {"slot": 0, "entry_date": "2025-01-02", "return_pct": 10.0},
            {"slot": 0, "entry_date": "2025-02-01", "return_pct": 10.0},
            {"slot": 1, "entry_date": "2025-01-02", "return_pct": 0.0},
        ]
        # slot0: 1.1*1.1-1 = 21%; slot1: 0%; mean = 10.5%
        self.assertAlmostEqual(_equal_slot_compound_pct(periods), 10.5, places=3)

    def test_verdict_primary_ntb(self) -> None:
        def _row(vid: str, factor: str | None, excess: float, compound: float) -> dict:
            return {
                "spec": {"factor": factor, "no_trade_before": "13:00" if vid != "A1" else "09:30"},
                "slices": {
                    "full": {
                        "mean_excess_pct": excess,
                        "compound_slot_pct": compound,
                        "n_legs": 100,
                        "n_big_wins": 10,
                    },
                    "oos": {"mean_excess_pct": excess - 0.5, "compound_slot_pct": compound * 0.3},
                    "is": {"mean_excess_pct": excess + 0.2},
                },
                "big_win_keys": [["2330", "2025-01-02"], ["2454", "2025-03-01"]]
                if vid == "B_old"
                else [["2330", "2025-01-02"]],
            }

        rows = {
            "B_old": _row("B_old", None, 7.0, 1400.0),
            "B_live": _row("B_live", None, 3.0, 400.0),
            "A1": _row("A1", "ntb", 6.0, 1100.0),  # +3pp vs live
            "A2": _row("A2", "chase_gate", 3.2, 420.0),
            "A3": _row("A3", "entry_sort", 3.1, 410.0),
            "A4": _row("A4", "pool", 5.0, 900.0),
        }
        v = _attribution_verdict(rows)
        self.assertEqual(v["primary_recoverable_factor"], "ntb")
        self.assertEqual(v["phase1_recommendation"], "RUN_PROVISIONAL_ALLDAY")
        self.assertEqual(v["phase1_open_window"], "09:30")
        self.assertEqual(v["big_win_coverage"]["missing_in_live"], 1)

    def test_render_md(self) -> None:
        payload = {
            "topic": "c18acc-old-vs-live-gap-attribution",
            "date_start": "2025-01-02",
            "date_end": "2026-07-16",
            "n_trade_dates": 370,
            "is_end": "2025-12-01",
            "oos_start": "2025-12-02",
            "variants": {
                vid: {
                    "spec": {"role": "x", "factor": None},
                    "slices": {
                        "full": {
                            "n_legs": 1,
                            "mean_excess_pct": 1.0,
                            "compound_slot_pct": 10.0,
                            "n_big_wins": 0,
                        },
                        "oos": {"mean_excess_pct": 1.0},
                    },
                }
                for vid in ("B_old", "B_live", "A1", "A2", "A3", "A4")
            },
            "verdict": {
                "summary": "test",
                "gap_old_minus_live_full_excess_pp": 1.0,
                "gap_old_minus_live_oos_excess_pp": 1.0,
                "gap_old_minus_live_full_compound_pp": 100.0,
                "factor_effects": [],
                "residual_full_excess_pp_after_A1_A2_A3": 0.0,
                "primary_recoverable_factor": "ntb",
                "phase1_recommendation": "RUN_PROVISIONAL_ALLDAY",
                "phase1_open_window": "09:30",
                "big_win_coverage": {
                    "old_n_big_wins": 1,
                    "live_n_big_wins": 0,
                    "still_in_live": 0,
                    "missing_in_live": 1,
                    "missing_keys": [],
                },
            },
        }
        md = render_old_vs_live_gap_attribution_md(payload)
        self.assertIn("Phase 0", md)
        self.assertIn("RUN_PROVISIONAL_ALLDAY", md)


if __name__ == "__main__":
    unittest.main()
