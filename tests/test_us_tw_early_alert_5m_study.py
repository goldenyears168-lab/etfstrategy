"""Minimal tests for early-alert 5m helpers."""

from __future__ import annotations

import unittest
from datetime import timedelta

import pandas as pd

from research.backtest.us_tw_early_alert_5m_study import (
    build_session_calendar,
    case_study_day,
    classify_early,
    day_label_and_features,
)


class TestEarlyAlert5m(unittest.TestCase):
    def test_calendar_and_day_features(self) -> None:
        idx = pd.date_range("2026-07-14 09:00", periods=12, freq="5min", tz="Asia/Taipei")
        tw = pd.DataFrame(
            {
                "open": [100.0] + [99.0] * 11,
                "high": [100.5] * 12,
                "low": [98.0] * 5 + [95.0] * 7,
                "close": [99.5, 98.8, 98.5, 98.2, 98.0, 97.0, 96.5, 96.0, 95.5, 95.2, 95.5, 96.0],
                "volume": [1] * 12,
            },
            index=idx,
        )
        # NQ ET bars covering TW session (09:00 TW = 21:00 prior ET in summer ≈ UTC+8 vs EDT)
        # Use Taiwan-local then convert: easier to build ET index matching slice_us_during_tw
        nq_idx = pd.date_range("2026-07-13 21:00", periods=12, freq="5min", tz="America/New_York")
        nq = pd.DataFrame(
            {
                "open": [20000.0] * 12,
                "high": [20010.0] * 12,
                "low": [19980.0] * 12,
                "close": [20000.0 - i * 2 for i in range(12)],
                "volume": [1] * 12,
            },
            index=nq_idx,
        )
        cal = build_session_calendar(tw)
        self.assertIn("2026-07-14", cal)
        row = day_label_and_features(
            "2026-07-14", tw_5m=tw, nq_5m=nq, event_threshold=3.0
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("spread_10m", row)
        self.assertTrue(pd.notna(row["spread_10m"]))

    def test_classify_early(self) -> None:
        accepted = {
            "oos": {"n_events": 6, "precision": 0.4, "recall": 0.4, "false_alarm_rate": 0.05, "n_triggers": 5},
            "full": {},
        }
        self.assertEqual(classify_early(accepted), "accepted")
        weak = {
            "oos": {"n_events": 2, "precision": 0.3, "recall": 0.5, "false_alarm_rate": 0.2, "n_triggers": 4},
            "full": {"n_events": 8, "precision": 0.25, "recall": 0.4, "false_alarm_rate": 0.15, "n_triggers": 8},
        }
        self.assertEqual(classify_early(weak), "weak")

    def test_case_study_missing(self) -> None:
        feat = pd.DataFrame([{"date": "2026-01-01", "is_event": False}])
        out = case_study_day(feat, "2026-07-14")
        self.assertFalse(out["present"])


if __name__ == "__main__":
    unittest.main()
