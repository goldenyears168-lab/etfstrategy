"""Tests for US ES/NQ overnight futures snapshots."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from us_futures_overnight import (
    overnight_pct,
    prior_us_rth_close_date,
    tw_poll_to_et,
)

TZ_ET = ZoneInfo("America/New_York")


class TestUsFuturesOvernight(unittest.TestCase):
    def test_tw_poll_to_et(self) -> None:
        dt = tw_poll_to_et("2026-03-10", "09:30")
        self.assertEqual(dt.tzinfo, TZ_ET)

    def test_prior_rth_close_monday_evening(self) -> None:
        cal = ["2026-03-06", "2026-03-09", "2026-03-10"]
        dt = datetime(2026, 3, 9, 20, 30, tzinfo=TZ_ET)
        self.assertEqual(prior_us_rth_close_date(dt, cal), "2026-03-09")

    def test_prior_rth_close_tuesday_morning(self) -> None:
        cal = ["2026-03-09", "2026-03-10"]
        dt = datetime(2026, 3, 10, 10, 0, tzinfo=TZ_ET)
        self.assertEqual(prior_us_rth_close_date(dt, cal), "2026-03-09")

    def test_overnight_pct(self) -> None:
        self.assertAlmostEqual(overnight_pct(101.0, 100.0), 1.0)


if __name__ == "__main__":
    unittest.main()
