"""Unit tests for Detach Gate 5m sell-gate evaluator helpers."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from order.us_tw_5m_sell_gate import ensure_nq_5m_for_session, evaluate_session_poll


class TestEnsureNq5m(unittest.TestCase):
    def test_skips_when_local_covers_session(self) -> None:
        with patch(
            "order.us_tw_5m_sell_gate.local_5m_max_trade_date",
            return_value="2026-07-16",
        ) as mx:
            out = ensure_nq_5m_for_session("2026-07-16")
        self.assertEqual(out["status"], "fresh")
        self.assertEqual(out["bars_upserted"], 0)
        mx.assert_called_once()

    def test_refreshes_when_stale(self) -> None:
        fake_df = pd.DataFrame({"Close": [1.0]})
        fake_conn = MagicMock()
        with (
            patch(
                "order.us_tw_5m_sell_gate.local_5m_max_trade_date",
                side_effect=["2026-07-15", "2026-07-16"],
            ),
            patch(
                "yahoo_chart_sync.fetch_yahoo_intraday_df",
                return_value=fake_df,
            ),
            patch(
                "yahoo_chart_sync.yahoo_intraday_rows_to_db",
                return_value=[{"trade_date": "2026-07-16", "minute": "09:00:00"}],
            ),
            patch("stock_db.connect", return_value=fake_conn),
            patch("stock_db.kbar.upsert_yahoo_5m_rows", return_value=12) as upsert,
        ):
            out = ensure_nq_5m_for_session("2026-07-16")
        self.assertEqual(out["status"], "refreshed")
        self.assertEqual(out["bars_upserted"], 12)
        upsert.assert_called_once()
        fake_conn.commit.assert_called_once()

    def test_force_refreshes_even_when_fresh(self) -> None:
        fake_df = pd.DataFrame({"Close": [1.0]})
        fake_conn = MagicMock()
        with (
            patch(
                "order.us_tw_5m_sell_gate.local_5m_max_trade_date",
                side_effect=["2026-07-16", "2026-07-16"],
            ),
            patch(
                "yahoo_chart_sync.fetch_yahoo_intraday_df",
                return_value=fake_df,
            ),
            patch(
                "yahoo_chart_sync.yahoo_intraday_rows_to_db",
                return_value=[{"trade_date": "2026-07-16"}],
            ),
            patch("stock_db.connect", return_value=fake_conn),
            patch("stock_db.kbar.upsert_yahoo_5m_rows", return_value=3),
        ):
            out = ensure_nq_5m_for_session("2026-07-16", force=True)
        self.assertEqual(out["status"], "refreshed")
        self.assertEqual(out["bars_upserted"], 3)


class TestEvaluateRefreshFlag(unittest.TestCase):
    def test_refresh_nq_false_skips_ensure(self) -> None:
        empty = pd.DataFrame()
        with (
            patch("order.us_tw_5m_sell_gate.ensure_nq_5m_for_session") as ensure,
            patch(
                "order.us_tw_5m_sell_gate.fetch_yahoo_5m_panel",
                return_value=empty,
            ),
            patch(
                "order.us_tw_5m_sell_gate.build_poll_frame_5m",
                return_value=None,
            ),
        ):
            out = evaluate_session_poll(
                session_date="2026-07-16",
                end=date(2026, 7, 16),
                refresh_nq=False,
            )
        ensure.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "insufficient_5m_bars")
        self.assertIsNone(out.get("nq_refresh"))


if __name__ == "__main__":
    unittest.main()
