"""Tests for stock_db.order_holdings and intraday exit gate."""

from __future__ import annotations

import sqlite3
import unittest

from order.intraday_exit_gate import (
    append_gate_daily_log,
    evaluate_portfolio_gate,
    evaluate_portfolio_gate_with_sync,
    gate_evaluation_final,
    gate_watch_stock_ids,
    persist_portfolio_gate,
)
from order.morning_holdings_brief import HoldingBriefRow, MorningBrief, brief_to_snapshot_rows, sync_morning_brief_to_db
from stock_db.order_holdings import load_order_holdings_snapshot, upsert_order_holdings_snapshot, OrderHoldingSnapshotRow


class TestOrderHoldingsSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE order_holdings_snapshot (
                snapshot_date TEXT NOT NULL,
                stock_id TEXT NOT NULL,
                stock_name TEXT,
                shares INTEGER NOT NULL,
                avg_cost REAL,
                unrealized_pnl INTEGER,
                prev_close REAL,
                bar_date TEXT,
                rrg_quadrant TEXT,
                rrg_session TEXT,
                structure_tier TEXT,
                gate_2pct REAL,
                trigger_s1b REAL,
                extension_spike REAL,
                notes_json TEXT,
                source TEXT NOT NULL DEFAULT 'fubon',
                synced_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_date, stock_id)
            );
            CREATE TABLE order_intraday_exit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                phase TEXT NOT NULL,
                stock_id TEXT,
                event TEXT NOT NULL,
                detail_json TEXT,
                dry_run INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE stock_kbar_1m (
                stock_id TEXT, trade_date TEXT, minute TEXT, close REAL,
                PRIMARY KEY (stock_id, trade_date, minute)
            );
            CREATE TABLE stock_daily_bars (
                stock_id TEXT, trade_date TEXT, close REAL,
                PRIMARY KEY (stock_id, trade_date)
            );
            INSERT INTO stock_daily_bars VALUES ('2330', '2026-06-26', 2390.0);
            CREATE TABLE vcp_screen_scores_v2 (
                stock_id TEXT, as_of_date TEXT, stop_loss REAL,
                PRIMARY KEY (stock_id, as_of_date)
            );
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_upsert_and_load(self) -> None:
        row = OrderHoldingSnapshotRow(
            snapshot_date="2026-06-29",
            stock_id="2327",
            stock_name="國巨*",
            shares=20,
            avg_cost=775.1,
            unrealized_pnl=4710,
            prev_close=1015.0,
            bar_date="2026-06-26",
            rrg_quadrant="weakening",
            rrg_session="2026-06-26",
            structure_tier="weak",
            gate_2pct=994.7,
            trigger_s1b=984.55,
            extension_spike=1055.6,
            notes=("CORE4",),
            source="fubon",
            synced_at="2026-06-29T08:50:00+08:00",
        )
        self.assertEqual(upsert_order_holdings_snapshot(self.conn, [row]), 1)
        self.assertEqual(load_order_holdings_snapshot(self.conn, "2026-06-29"), {"2327": 20})

    def test_brief_sync_roundtrip(self) -> None:
        brief = MorningBrief(
            generated_at="2026-06-29T08:50:00+08:00",
            session_date="2026-06-29",
            account_label="test",
            cash_available=100,
            bar_as_of="2026-06-26",
            rrg_as_of="2026-06-26",
            tx_gap_pct=1.0,
            tx_price=48000.0,
            tw_spot_prev_close=47000.0,
            te_gap_pct=None,
            morning_risk_captured_at="2026-06-29T08:50:00+08:00",
            morning_warnings=(),
            gate_2330_995=2328.0,
            core4_gate_rows=(),
            holdings=(
                HoldingBriefRow(
                    stock_id="2327",
                    stock_name="國巨*",
                    shares=20,
                    cost_price=775.1,
                    unrealized_pnl=4710,
                    prev_close=1015.0,
                    bar_date="2026-06-26",
                    rrg_quadrant="weakening",
                    rrg_seg=0.7,
                    rrg_session="2026-06-26",
                    structure_tier="weak",
                    gate_2pct=994.7,
                    trigger_s1b=984.55,
                    extension_spike=1055.6,
                ),
            ),
            holdings_source="fubon",
            morning_risk_source="live",
        )
        self.assertEqual(len(brief_to_snapshot_rows(brief)), 1)
        self.assertEqual(sync_morning_brief_to_db(self.conn, brief), 1)

    def test_portfolio_gate_skip_without_kbar(self) -> None:
        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2327",
                stock_name="國巨*",
                shares=20,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1015.0,
                bar_date="2026-06-26",
                rrg_quadrant="weakening",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=994.7,
                trigger_s1b=984.55,
                extension_spike=1055.6,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        result = evaluate_portfolio_gate_with_sync(
            self.conn,
            session_date="2026-06-29",
            sync_kbar=False,
        )
        self.assertFalse(result.evaluated)
        self.assertIsNone(result.portfolio_exit_mode)
        self.assertIn("2330", result.skip_reason or "")

    def test_portfolio_gate_evaluated_with_kbar(self) -> None:
        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2327",
                stock_name="國巨*",
                shares=20,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1015.0,
                bar_date="2026-06-26",
                rrg_quadrant="weakening",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=994.7,
                trigger_s1b=984.55,
                extension_spike=1055.6,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.executemany(
            "INSERT INTO stock_kbar_1m VALUES (?, '2026-06-29', ?, ?)",
            [
                ("2330", "09:05", 2400.0),
                ("2327", "09:05", 1000.0),
                ("2449", "09:05", 310.0),
                ("3211", "09:05", 390.0),
                ("3264", "09:05", 215.0),
            ],
        )
        self.conn.commit()
        result = evaluate_portfolio_gate_with_sync(
            self.conn,
            session_date="2026-06-29",
            sync_kbar=False,
        )
        self.assertTrue(result.evaluated)
        self.assertFalse(result.portfolio_exit_mode)
        self.assertEqual(result.gate_a_count, 0)

    def test_portfolio_gate_a_counts_unheld_core4(self) -> None:
        """Playbook §3.1: A counts all CORE4 triggers, not only held names."""
        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2327",
                stock_name="國巨*",
                shares=20,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1015.0,
                bar_date="2026-06-26",
                rrg_quadrant="weakening",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=994.7,
                trigger_s1b=984.55,
                extension_spike=1055.6,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.executemany(
            "INSERT OR REPLACE INTO stock_daily_bars VALUES (?, '2026-06-26', ?)",
            [
                ("2330", 2390.0),
                ("3264", 228.0),
                ("2449", 334.5),
                ("3211", 408.0),
            ],
        )
        self.conn.executemany(
            "INSERT INTO stock_kbar_1m VALUES (?, '2026-06-29', '09:05', ?)",
            [
                ("2330", 2350.0),
                ("2327", 990.0),
                ("3264", 220.0),
                ("2449", 340.0),
                ("3211", 410.0),
            ],
        )
        self.conn.commit()
        result = evaluate_portfolio_gate_with_sync(
            self.conn,
            session_date="2026-06-29",
            sync_kbar=False,
        )
        self.assertTrue(result.evaluated)
        self.assertEqual(result.gate_a_count, 2)
        self.assertTrue(result.gate_b_triggered)
        self.assertTrue(result.portfolio_exit_mode)
        roles = {c.stock_id: c.role for c in result.checks if c.stock_id in {"2327", "3264"}}
        self.assertEqual(roles["2327"], "core4_a·held")
        self.assertEqual(roles["3264"], "core4_a")

    def test_gate_watch_stock_ids(self) -> None:
        ids = gate_watch_stock_ids()
        self.assertIn("2330", ids)
        self.assertIn("2327", ids)
        self.assertEqual(len(ids), 5)

    def test_gate_evaluation_final_and_daily_log_append(self) -> None:
        from stock_db.order_holdings import append_intraday_exit_log

        self.assertFalse(gate_evaluation_final(self.conn, "2026-06-29"))
        append_intraday_exit_log(
            self.conn,
            trade_date="2026-06-29",
            checked_at="2026-06-29T09:05:00+08:00",
            phase="gate",
            event="gate_skip",
            detail={},
        )
        self.assertFalse(gate_evaluation_final(self.conn, "2026-06-29"))
        append_intraday_exit_log(
            self.conn,
            trade_date="2026-06-29",
            checked_at="2026-06-29T09:10:00+08:00",
            phase="gate",
            event="gate_off",
            detail={},
        )
        self.assertTrue(gate_evaluation_final(self.conn, "2026-06-29"))

        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2327",
                stock_name="國巨*",
                shares=20,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1015.0,
                bar_date="2026-06-26",
                rrg_quadrant="weakening",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=994.7,
                trigger_s1b=984.55,
                extension_spike=1055.6,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.executemany(
            "INSERT INTO stock_kbar_1m VALUES (?, '2026-06-29', '09:05', ?)",
            [("2330", 2400.0), ("2327", 1000.0), ("2449", 310.0), ("3211", 390.0), ("3264", 215.0)],
        )
        self.conn.commit()
        result = evaluate_portfolio_gate_with_sync(
            self.conn, session_date="2026-06-29", sync_kbar=False
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "gate.log"
            append_gate_daily_log(result, path=log_path)
            first = log_path.read_text(encoding="utf-8")
            append_gate_daily_log(result, path=log_path)
            second = log_path.read_text(encoding="utf-8")
        self.assertIn("09:05 組合閘門", first)
        self.assertGreater(len(second), len(first))
        persist_portfolio_gate(self.conn, result)
        self.assertTrue(gate_evaluation_final(self.conn, "2026-06-29"))

    def test_portfolio_gate_off_without_prices_legacy(self) -> None:
        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2327",
                stock_name="國巨*",
                shares=20,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1015.0,
                bar_date="2026-06-26",
                rrg_quadrant="weakening",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=994.7,
                trigger_s1b=984.55,
                extension_spike=1055.6,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        result = evaluate_portfolio_gate(self.conn, session_date="2026-06-29")
        self.assertFalse(result.portfolio_exit_mode)
        self.assertTrue(result.checks)

    def test_structural_s1b_without_gate(self) -> None:
        from order.intraday_structural_exit import scan_structural_sell_signals

        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2327",
                stock_name="國巨*",
                shares=20,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1015.0,
                bar_date="2026-06-26",
                rrg_quadrant="weakening",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=994.7,
                trigger_s1b=984.55,
                extension_spike=1055.6,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.execute(
            "INSERT INTO stock_kbar_1m VALUES ('2327', '2026-06-29', '09:10', 980.0)"
        )
        self.conn.commit()
        signals, ctx = scan_structural_sell_signals(
            self.conn,
            session_date="2026-06-29",
            poll_minute="09:10",
            holdings={"2327": 20},
        )
        self.assertFalse(ctx.gate_ran)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].exit_mode, "trigger_s1b")
        self.assertEqual(signals[0].stock_id, "2327")

    def test_structural_s2_requires_gate_on(self) -> None:
        from order.intraday_structural_exit import scan_structural_sell_signals
        from stock_db.order_holdings import append_intraday_exit_log

        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="2449",
                stock_name="京元電",
                shares=10,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=100.0,
                bar_date="2026-06-26",
                rrg_quadrant="lagging",
                rrg_session="2026-06-26",
                structure_tier="weak",
                gate_2pct=98.0,
                trigger_s1b=97.0,
                extension_spike=104.0,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.execute(
            "INSERT INTO stock_kbar_1m VALUES ('2449', '2026-06-29', '09:10', 96.0)"
        )
        self.conn.commit()

        off_sigs, _ = scan_structural_sell_signals(
            self.conn,
            session_date="2026-06-29",
            poll_minute="09:10",
            holdings={"2449": 10},
        )
        self.assertEqual(len(off_sigs), 1)
        self.assertEqual(off_sigs[0].exit_mode, "watch_s0")

        append_intraday_exit_log(
            self.conn,
            trade_date="2026-06-29",
            checked_at="2026-06-29T09:05:00+08:00",
            phase="gate",
            event="gate_on",
            detail={"portfolio_exit_mode": True},
        )
        on_sigs, ctx = scan_structural_sell_signals(
            self.conn,
            session_date="2026-06-29",
            poll_minute="09:10",
            holdings={"2449": 10},
        )
        self.assertTrue(ctx.gate_ran)
        self.assertTrue(ctx.portfolio_exit_mode)
        self.assertEqual(len(on_sigs), 1)
        self.assertEqual(on_sigs[0].exit_mode, "trigger_s2")

    def test_s0_watch_on_strong_minus_3pct(self) -> None:
        from order.intraday_structural_exit import scan_holdings_exit_signals

        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="3008",
                stock_name="大立光",
                shares=5,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=1000.0,
                bar_date="2026-06-26",
                rrg_quadrant="leading",
                rrg_session="2026-06-26",
                structure_tier="strong",
                gate_2pct=980.0,
                trigger_s1b=970.0,
                extension_spike=None,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.execute(
            "INSERT INTO stock_kbar_1m VALUES ('3008', '2026-06-29', '09:15', 965.0)"
        )
        self.conn.commit()
        signals, ctx = scan_holdings_exit_signals(
            self.conn,
            session_date="2026-06-29",
            poll_minute="09:15",
            holdings={"3008": 5},
        )
        self.assertFalse(ctx.holdings_stress)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].exit_mode, "watch_s0")
        self.assertEqual(signals[0].stock_id, "3008")

    def test_s2_lite_when_holdings_stress(self) -> None:
        from order.intraday_structural_exit import scan_holdings_exit_signals

        def _row(stock_id: str, name: str, prev: float, tier: str, quad: str) -> OrderHoldingSnapshotRow:
            return OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id=stock_id,
                stock_name=name,
                shares=10,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=prev,
                bar_date="2026-06-26",
                rrg_quadrant=quad,
                rrg_session="2026-06-26",
                structure_tier=tier,
                gate_2pct=round(prev * 0.98, 2),
                trigger_s1b=round(prev * 0.97, 2),
                extension_spike=None,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )

        rows = [
            _row("3008", "大立光", 1000.0, "strong", "leading"),
            _row("5536", "聖暉", 500.0, "strong", "leading"),
            _row("6223", "旺矽", 2000.0, "strong", "leading"),
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.executemany(
            "INSERT INTO stock_kbar_1m VALUES (?, '2026-06-29', '09:20', ?)",
            [
                ("3008", 965.0),
                ("5536", 480.0),
                ("6223", 1920.0),
            ],
        )
        self.conn.commit()
        signals, ctx = scan_holdings_exit_signals(
            self.conn,
            session_date="2026-06-29",
            poll_minute="09:20",
            holdings={"3008": 5, "5536": 10, "6223": 3},
        )
        self.assertTrue(ctx.holdings_stress)
        self.assertEqual(ctx.holdings_stress_count, 3)
        modes = {s.stock_id: s.exit_mode for s in signals}
        self.assertEqual(modes["3008"], "trigger_s2_lite")
        self.assertEqual(modes["5536"], "trigger_s2_lite")
        self.assertEqual(modes["6223"], "trigger_s2_lite")

    def test_s1a_vcp_stop(self) -> None:
        from order.intraday_structural_exit import scan_holdings_exit_signals

        rows = [
            OrderHoldingSnapshotRow(
                snapshot_date="2026-06-29",
                stock_id="6488",
                stock_name="環球晶",
                shares=10,
                avg_cost=None,
                unrealized_pnl=0,
                prev_close=500.0,
                bar_date="2026-06-26",
                rrg_quadrant="improving",
                rrg_session="2026-06-26",
                structure_tier="strong",
                gate_2pct=490.0,
                trigger_s1b=485.0,
                extension_spike=None,
                notes=(),
                source="fubon",
                synced_at="2026-06-29T08:50:00+08:00",
            )
        ]
        upsert_order_holdings_snapshot(self.conn, rows)
        self.conn.execute(
            "INSERT INTO vcp_screen_scores_v2 VALUES ('6488', '2026-06-26', 495.0)"
        )
        self.conn.execute(
            "INSERT INTO stock_kbar_1m VALUES ('6488', '2026-06-29', '09:10', 490.0)"
        )
        self.conn.commit()
        signals, _ = scan_holdings_exit_signals(
            self.conn,
            session_date="2026-06-29",
            poll_minute="09:10",
            holdings={"6488": 10},
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].exit_mode, "trigger_s1a")


if __name__ == "__main__":
    unittest.main()
