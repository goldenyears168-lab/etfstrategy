"""Unit tests for buy-radar C0 event helpers."""

from __future__ import annotations

import unittest

from research.backtest.buy_radar_c0_fresh_tier2_events import (
    _layer_stats,
    _poll_minutes,
    _tag,
    render_buy_radar_c0_fresh_tier2_events_md,
)


class TestBuyRadarC0Events(unittest.TestCase):
    def test_poll_minutes(self) -> None:
        ms = _poll_minutes(start="09:05", end="09:15", step=5)
        self.assertEqual(ms, ["09:05", "09:10", "09:15"])

    def test_tag(self) -> None:
        self.assertEqual(_tag("A", {"A"}, {"A"}), "overlap")
        self.assertEqual(_tag("A", {"A"}, set()), "fresh_only")
        self.assertEqual(_tag("A", set(), {"A"}), "tier2_only")

    def test_layer_stats(self) -> None:
        evs = [
            {"fwd": {"5": 1.0}},
            {"fwd": {"5": -0.5}},
            {"fwd": {"5": None}},
        ]
        s = _layer_stats(evs, 5)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["mean_excess_pct"], 0.25)
        self.assertEqual(s["hit_rate_pct"], 50.0)

    def test_render(self) -> None:
        md = render_buy_radar_c0_fresh_tier2_events_md(
            {
                "topic": "buy-radar-c0-fresh-tier2-events",
                "date_start": "2025-01-02",
                "date_end": "2026-07-16",
                "n_trade_dates": 10,
                "pool_mode": "prior_session_PIT",
                "poll_start": "09:05",
                "confirm_bars": 1,
                "n_events": 0,
                "by_layer": {},
                "case_6505": {"found": False},
                "verdict": {"summary": "NO_EDGE", "signal_edge": "NO_EDGE"},
            }
        )
        self.assertIn("NO_EDGE", md)
        self.assertIn("6505", md)


if __name__ == "__main__":
    unittest.main()
