"""Minimal unit tests for US–TW divergence sell-gate calc helpers."""

from __future__ import annotations

import unittest

import pandas as pd

from research.backtest.us_tw_divergence_sell_gate_study import (
    GateRule,
    _triggered,
    augment_daily_with_twii_1h,
    classify_hypothesis,
    session_intraday_max_dd_from_peak,
)


class TestSessionDrawdown(unittest.TestCase):
    def test_peak_to_trough_basic(self) -> None:
        idx = pd.date_range("2026-01-02 09:00", periods=4, freq="h", tz="Asia/Taipei")
        df = pd.DataFrame(
            {
                "open": [100.0, 101.0, 99.0, 97.0],
                "high": [100.5, 102.0, 99.5, 97.5],
                "low": [99.5, 100.0, 96.0, 95.0],
                "close": [100.0, 100.5, 96.5, 96.0],
            },
            index=idx,
        )
        out = session_intraday_max_dd_from_peak(df)
        # peak high 102 → trough low 95 = 6.86%
        self.assertAlmostEqual(out["peak_to_trough_pct"], (102 - 95) / 102 * 100, places=3)
        self.assertGreater(out["open_to_low_pct"], 0)

    def test_empty(self) -> None:
        out = session_intraday_max_dd_from_peak(pd.DataFrame())
        self.assertTrue(pd.isna(out["peak_to_trough_pct"]))


class TestGateHelpers(unittest.TestCase):
    def test_triggered(self) -> None:
        self.assertTrue(_triggered(-1.5, "lt", -1.0))
        self.assertFalse(_triggered(-0.5, "lt", -1.0))
        self.assertTrue(_triggered(2.0, "gt", 1.5))
        self.assertFalse(_triggered(float("nan"), "lt", -1.0))

    def test_classify(self) -> None:
        accepted = {
            "oos": {
                "n_events": 8,
                "n_triggers": 10,
                "precision": 0.4,
                "recall": 0.4,
                "false_alarm_rate": 0.04,
            }
        }
        self.assertEqual(classify_hypothesis(accepted), "accepted")
        weak = {
            "oos": {
                "n_events": 8,
                "n_triggers": 20,
                "precision": 0.25,
                "recall": 0.35,
                "false_alarm_rate": 0.08,
            }
        }
        self.assertEqual(classify_hypothesis(weak), "weak")
        rejected = {
            "oos": {
                "n_events": 8,
                "n_triggers": 40,
                "precision": 0.05,
                "recall": 0.1,
                "false_alarm_rate": 0.4,
            }
        }
        self.assertEqual(classify_hypothesis(rejected), "rejected")

    def test_augment_daily_fills_missing_session(self) -> None:
        daily = pd.DataFrame(
            [
                {
                    "date": "2026-07-13",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1.0,
                }
            ]
        )
        idx = pd.date_range("2026-07-14 09:00", periods=4, freq="h", tz="Asia/Taipei")
        tw = pd.DataFrame(
            {
                "open": [100.0, 99.0, 97.0, 98.0],
                "high": [100.5, 99.2, 97.5, 98.5],
                "low": [99.0, 96.0, 95.0, 97.5],
                "close": [99.5, 97.0, 96.5, 98.2],
                "volume": [1, 1, 1, 1],
            },
            index=idx,
        )
        out, added = augment_daily_with_twii_1h(
            daily, tw, date_start="2026-07-01", date_end="2026-07-14"
        )
        self.assertEqual(added, ["2026-07-14"])
        self.assertIn("2026-07-14", set(out["date"].astype(str)))
        row = out[out["date"] == "2026-07-14"].iloc[0]
        self.assertAlmostEqual(float(row["high"]), 100.5)
        self.assertAlmostEqual(float(row["low"]), 95.0)


if __name__ == "__main__":
    unittest.main()
