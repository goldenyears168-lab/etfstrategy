"""Tests for view-ready screen_status helpers."""

from __future__ import annotations

import unittest

from snapshot_screen_status import (
    copytrade_screen_status,
    rrg_screen_status,
    vcp_screen_status,
)


class SnapshotScreenStatusTests(unittest.TestCase):
    def test_copytrade_active(self) -> None:
        status = copytrade_screen_status(2)
        self.assertEqual(status["kind"], "active")
        self.assertIn("2", status["text_zh"])

    def test_rrg_intraday_slots(self) -> None:
        status = rrg_screen_status(
            intraday=True,
            mono_count=0,
            fresh_count=0,
            slots_label="0/3",
        )
        self.assertEqual(status["kind"], "empty")
        self.assertIn("盤中預估", status["text_zh"])

    def test_vcp_empty(self) -> None:
        status = vcp_screen_status(0)
        self.assertEqual(status["kind"], "empty")
        self.assertEqual(status["text_zh"], "今日無候選")


if __name__ == "__main__":
    unittest.main()
