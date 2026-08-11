"""Tests for live Fubon submit hard gates."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from order.live_submit_guard import (
    live_submit_block_reason,
    today_session_date,
    under_test_runner,
)


class TestLiveSubmitGuard(unittest.TestCase):
    def test_under_pytest(self) -> None:
        self.assertTrue(under_test_runner())
        reason = live_submit_block_reason(session_date=today_session_date())
        self.assertIsNotNone(reason)
        self.assertIn("live_submit_blocked", str(reason))

    def test_unittest_in_argv_is_blocked(self) -> None:
        """coverage run -m unittest must never reach the broker (2026-08-04)."""
        with patch.object(sys, "argv", ["coverage", "run", "-m", "unittest", "discover"]):
            with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": ""}, clear=False):
                os.environ.pop("PYTEST_CURRENT_TEST", None)
                self.assertTrue(under_test_runner())

    def test_backdated_session(self) -> None:
        with patch.dict(
            os.environ,
            {"ORDER_LIVE_FORBIDDEN": "0", "ORDER_MASTER_ENABLED": "1"},
            clear=False,
        ):
            with patch("order.live_submit_guard.under_test_runner", return_value=False):
                reason = live_submit_block_reason(session_date="2026-07-09")
        self.assertIsNotNone(reason)
        self.assertIn("session_date", str(reason))

    def test_master_off_blocks_even_when_not_under_test(self) -> None:
        with patch("order.live_submit_guard.under_test_runner", return_value=False):
            with patch.dict(os.environ, {"ORDER_MASTER_ENABLED": "0"}, clear=False):
                reason = live_submit_block_reason(session_date=today_session_date())
        self.assertIsNotNone(reason)
        self.assertIn("ORDER_MASTER_ENABLED=0", str(reason))

    def test_today_session_date_matches_trading_day_str(self) -> None:
        """2026-08-11/12 live: today_session_date() used a naive calendar
        strftime, independent of tmf_channel_order.py's day computation
        (fixed the same night to use trading_day_str()). Once that fix
        landed, every order placed during 00:00-04:59 correctly carried
        session_date=<previous day>, but this guard still compared against
        the old naive "today" and rejected all of them as backdated,
        immediately after un-killing the sleeve. Must delegate to the same
        single source of truth."""
        from order.tmf_channel_ledger import trading_day_str

        self.assertEqual(today_session_date(), trading_day_str())

    def test_today_allowed_when_master_on_and_not_under_test(self) -> None:
        with patch("order.live_submit_guard.under_test_runner", return_value=False):
            with patch.dict(
                os.environ,
                {
                    "ORDER_ALLOW_BACKDATED_SESSION": "0",
                    "ORDER_MASTER_ENABLED": "1",
                    "ORDER_LIVE_FORBIDDEN": "0",
                },
                clear=False,
            ):
                self.assertIsNone(live_submit_block_reason(session_date=today_session_date()))


if __name__ == "__main__":
    unittest.main()
