"""Tests for live Fubon submit hard gates."""

from __future__ import annotations

import os
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

    def test_backdated_session(self) -> None:
        with patch.dict(os.environ, {"ORDER_LIVE_FORBIDDEN": "0"}, clear=False):
            with patch("order.live_submit_guard.under_test_runner", return_value=False):
                reason = live_submit_block_reason(session_date="2026-07-09")
        self.assertIsNotNone(reason)
        self.assertIn("session_date", str(reason))

    def test_today_allowed_when_not_under_test(self) -> None:
        with patch("order.live_submit_guard.under_test_runner", return_value=False):
            with patch.dict(os.environ, {"ORDER_ALLOW_BACKDATED_SESSION": "0"}, clear=False):
                self.assertIsNone(live_submit_block_reason(session_date=today_session_date()))


if __name__ == "__main__":
    unittest.main()
