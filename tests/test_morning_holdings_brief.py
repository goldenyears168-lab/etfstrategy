"""Tests for order.morning_holdings_brief (no live Fubon / FinMind)."""

from __future__ import annotations

import sqlite3
import unittest

from order.morning_holdings_brief import (
    enrich_holdings,
    format_morning_brief,
    structure_tier,
    MorningBrief,
    HoldingBriefRow,
)


class TestMorningHoldingsBrief(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE stock_daily_bars (
                stock_id TEXT, trade_date TEXT, close REAL
            );
            CREATE TABLE rrg_universe_scores (
                session_date TEXT, screen_kind TEXT, stock_id TEXT,
                stock_name TEXT, quadrant TEXT, seg_last REAL
            );
            CREATE TABLE etf_holdings (
                stock_id TEXT, stock_name TEXT, snapshot_date TEXT
            );
            INSERT INTO stock_daily_bars VALUES
                ('2327', '2026-06-25', 1015.0),
                ('2449', '2026-06-25', 308.0),
                ('2330', '2026-06-25', 2340.0);
            INSERT INTO rrg_universe_scores VALUES
                ('2026-06-25', 'close', '2327', '國巨*', 'weakening', 0.701),
                ('2026-06-25', 'close', '2449', '京元電子', 'improving', 0.526);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_structure_tier(self) -> None:
        self.assertEqual(structure_tier("weakening"), "weak")
        self.assertEqual(structure_tier("leading"), "strong")
        self.assertEqual(structure_tier("neutral"), "neutral")
        self.assertEqual(structure_tier("leading", vcp_state="Overextended"), "weak")

    def test_enrich_holdings_gate_and_s1b(self) -> None:
        rows = enrich_holdings(
            self.conn,
            [
                {"stock_no": "2327", "tradable_qty": 0, "odd": {"tradable_qty": 20}},
                {"stock_no": "2449", "tradable_qty": 0, "odd": {"tradable_qty": 88}},
            ],
            as_of_session="2026-06-26",
            pnl_by_symbol={"2327": (4710, 775.1), "2449": (-2625, 336.48)},
        )
        by_id = {r.stock_id: r for r in rows}
        self.assertEqual(by_id["2327"].structure_tier, "weak")
        self.assertAlmostEqual(by_id["2327"].gate_2pct, 1015.0 * 0.98)
        self.assertAlmostEqual(by_id["2327"].trigger_s1b, 1015.0 * 0.97)
        self.assertEqual(by_id["2449"].structure_tier, "strong")
        self.assertIsNone(by_id["2449"].trigger_s1b)
        self.assertIn("CORE4", by_id["2327"].notes)

    def test_format_includes_gate_section(self) -> None:
        row = HoldingBriefRow(
            stock_id="2327",
            stock_name="國巨*",
            shares=20,
            cost_price=775.1,
            unrealized_pnl=4710,
            prev_close=1015.0,
            bar_date="2026-06-26",
            rrg_quadrant="weakening",
            rrg_seg=0.701,
            rrg_session="2026-06-26",
            structure_tier="weak",
            gate_2pct=994.7,
            trigger_s1b=984.55,
            notes=("CORE4",),
        )
        brief = MorningBrief(
            generated_at="2026-06-29T08:50:00+08:00",
            session_date="2026-06-29",
            account_label="test",
            cash_available=111916,
            bar_as_of="2026-06-26",
            rrg_as_of="2026-06-26",
            tx_gap_pct=0.5,
            tx_price=48000.0,
            tw_spot_prev_close=47741.51,
            te_gap_pct=None,
            morning_risk_captured_at="2026-06-29T08:50:00+08:00",
            morning_warnings=(),
            gate_2330_995=2328.3,
            core4_gate_rows=(row,),
            holdings=(row,),
        )
        text = format_morning_brief(brief)
        self.assertIn("09:05 組合閘門", text)
        self.assertIn("2327", text)
        self.assertIn("984.55", text)


if __name__ == "__main__":
    unittest.main()
