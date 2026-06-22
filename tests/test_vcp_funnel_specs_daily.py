"""Tests for VCP funnel specs daily intraday/close cycles."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from vcp_funnel_specs_daily import _close_screen_ready, run_close_cycle


class VcpFunnelSpecsDailyTests(unittest.TestCase):
    def test_close_screen_ready_requires_min_bars(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE stock_daily_bars (
                stock_id TEXT,
                trade_date TEXT,
                source TEXT
            )
            """
        )
        for i in range(49):
            conn.execute(
                "INSERT INTO stock_daily_bars VALUES (?, '2026-06-22', 'finmind')",
                (f"{i:04d}",),
            )
        self.assertFalse(_close_screen_ready(conn, "2026-06-22", min_bars=50))
        conn.execute(
            "INSERT INTO stock_daily_bars VALUES ('9999', '2026-06-22', 'finmind')"
        )
        self.assertTrue(_close_screen_ready(conn, "2026-06-22", min_bars=50))
        conn.close()

    @patch("vcp_funnel_specs_daily.write_spec_briefs", return_value=[])
    @patch("vcp_funnel_specs_daily.run_close_funnel_screen", return_value=(None, {}))
    def test_run_close_cycle_skips_without_bars(
        self,
        _mock_screen,
        _mock_brief,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        paths, as_of = run_close_cycle(conn, as_of_date="2026-06-22")
        self.assertEqual(paths, [])
        self.assertIsNone(as_of)
        conn.close()


if __name__ == "__main__":
    unittest.main()
