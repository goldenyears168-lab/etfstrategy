"""Tests for order.holdings_pulse (no live Fubon / FinMind)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from order.holdings_pulse import (
    HoldingPulseRow,
    HoldingsPulse,
    RrgPoint,
    _distance_pct,
    _flag_action_hint,
    _format_priority_watch,
    _structural_flags,
    _weak_reason,
    build_holdings_pulse,
    format_holdings_pulse,
    format_holdings_pulse_digest,
    holdings_pulse_to_dict,
    market_phase,
    trend_arrow,
)
from order.morning_holdings_brief import HoldingBriefRow


class TestHoldingsPulse(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE stock_daily_bars (
                stock_id TEXT, trade_date TEXT, close REAL, source TEXT
            );
            CREATE TABLE rrg_universe_scores (
                session_date TEXT, screen_kind TEXT, stock_id TEXT,
                stock_name TEXT, quadrant TEXT, seg_last REAL,
                rs_ratio REAL, rs_momentum REAL, trend TEXT, tick_ok INTEGER,
                data_baseline_date TEXT, synced_at TEXT
            );
            CREATE TABLE etf_holdings (
                stock_id TEXT, stock_name TEXT, snapshot_date TEXT
            );
            CREATE TABLE vcp_screen_scores_v2 (
                stock_id TEXT, as_of_date TEXT, composite_score REAL,
                execution_state TEXT, stop_loss REAL
            );
            CREATE TABLE order_intraday_exit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT, checked_at TEXT, phase TEXT,
                stock_id TEXT, event TEXT, detail_json TEXT, dry_run INTEGER
            );
            CREATE TABLE morning_risk_snapshot (
                trade_date TEXT, captured_at TEXT, tx_gap_live_pct REAL,
                tx_price REAL, te_gap_live_pct REAL, tw_spot_prev_close REAL
            );
            INSERT INTO stock_daily_bars VALUES
                ('2327', '2026-06-25', 1015.0, 'finmind'),
                ('2449', '2026-06-25', 308.0, 'finmind'),
                ('2330', '2026-06-25', 2340.0, 'finmind');
            INSERT INTO rrg_universe_scores VALUES
                ('2026-06-25', 'close', '2327', '國巨*', 'weakening', 0.701,
                 102.1, 99.2, 'down_right', 1, '2026-06-25', 't'),
                ('2026-06-25', 'close', '2449', '京元電子', 'improving', 0.526,
                 98.5, 101.3, 'up_left', 1, '2026-06-25', 't'),
                ('2026-06-29', 'intraday', '2327', '國巨*', 'lagging', 0.650,
                 99.0, 98.1, 'down_left', 1, '2026-06-25', 't'),
                ('2026-06-29', 'intraday', '2449', '京元電子', 'improving', 0.540,
                 98.8, 101.5, 'up_left', 1, '2026-06-25', 't');
            INSERT INTO vcp_screen_scores_v2 VALUES
                ('2327', '2026-06-25', 72.0, 'Coiling', 950.0);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_trend_arrow(self) -> None:
        self.assertEqual(trend_arrow("up_right"), "↗")
        self.assertEqual(trend_arrow(None), "—")

    def test_market_phase(self) -> None:
        dt = datetime(2026, 6, 29, 10, 30, tzinfo=ZoneInfo("Asia/Taipei"))
        self.assertEqual(market_phase(dt), "active")

    def test_distance_pct(self) -> None:
        self.assertAlmostEqual(_distance_pct(1000.0, 994.7, 1015.0), (1000.0 - 994.7) / 1015.0 * 100)

    def test_structural_flags(self) -> None:
        row = HoldingBriefRow(
            stock_id="2327",
            stock_name="國巨*",
            shares=20,
            cost_price=775.1,
            unrealized_pnl=4710,
            prev_close=1015.0,
            bar_date="2026-06-25",
            rrg_quadrant="weakening",
            rrg_seg=0.701,
            rrg_session="2026-06-25",
            structure_tier="weak",
            gate_2pct=994.7,
            trigger_s1b=984.55,
            extension_spike=1055.6,
            notes=("CORE4",),
        )
        flags = _structural_flags(row, current_price=980.0, poll_minute="09:30", portfolio_exit_mode=False)
        self.assertIn("≤-2%gate", flags)
        self.assertNotIn("S1b", flags)
        # gate OFF → 不因弱檔 -3% 單獨掛賣出旗標
        self.assertNotIn("S2", flags)

    def test_weak_reason_and_flag_hint(self) -> None:
        row = HoldingPulseRow(
            base=HoldingBriefRow(
                stock_id="6191",
                stock_name="精成科",
                shares=100,
                cost_price=100.0,
                unrealized_pnl=100,
                prev_close=100.0,
                bar_date="2026-06-25",
                rrg_quadrant="leading",
                rrg_seg=0.2,
                rrg_session="2026-06-25",
                structure_tier="weak",
                gate_2pct=98.0,
                trigger_s1b=97.0,
                extension_spike=104.0,
                notes=(),
            ),
            current_price=105.0,
            price_source="fubon",
            daily_pct=5.0,
            pnl_pct=5.0,
            market_value=10500.0,
            weight_pct=10.0,
            rrg_close=RrgPoint("2026-06-25", "leading", 100.0, 100.0, 0.2, "up_right", True),
            rrg_intraday=RrgPoint("2026-06-29", "leading", 100.0, 100.0, 0.3, "up_right", True),
            rrg_quad_delta=None,
            rrg_seg_delta=0.1,
            dist_gate_2pct_pct=7.0,
            dist_s1b_pct=8.0,
            dist_extension_pct=1.0,
            vcp_composite=None,
            vcp_state=None,
            hold_days=5,
            structural_flags=("ext≥4%",),
        )
        self.assertIn("tier=weak", _weak_reason(row))
        self.assertNotIn("VCP", _weak_reason(row))
        self.assertIn("守利", _flag_action_hint("ext≥4%", tier="weak", portfolio_exit_mode=False))
        self.assertIn("非自動賣出", _flag_action_hint("≤-2%gate", tier="strong", portfolio_exit_mode=False))

    def test_format_priority_watch_after_close(self) -> None:
        pulse = HoldingsPulse(
            generated_at="2026-07-03T14:00:00+08:00",
            session_date="2026-07-03",
            poll_minute="14:00",
            market_phase="after_close",
            account_label="test",
            cash_available=1000,
            total_market_value=10000.0,
            total_unrealized_pnl=-100,
            bar_as_of="2026-07-01",
            rrg_close_as_of="2026-07-01",
            rrg_intraday_as_of=None,
            tx_gap_pct=None,
            tx_price=None,
            tw_spot_prev_close=None,
            te_gap_pct=None,
            morning_risk_note=None,
            gate_2330_995=None,
            gate_2330_price=None,
            gate_2330_hit=None,
            core4_gate_hits=0,
            portfolio_exit_preview=False,
            portfolio_exit_mode=False,
            portfolio_gate_ran=True,
            holdings_stress=False,
            holdings_stress_count=0,
            rrg_intraday_tick_n=None,
            rrg_intraday_kbar_n=None,
            rrg_intraday_price_mode=None,
            rrg_intraday_error=None,
            holdings=(),
        )
        text = "\n".join(_format_priority_watch(pulse))
        self.assertIn("不等於出清指令", text)
        self.assertIn("收盤後執行", text)

    def test_format_digest_plain(self) -> None:
        pulse = HoldingsPulse(
            generated_at="2026-07-03T11:00:00+08:00",
            session_date="2026-07-03",
            poll_minute="11:00",
            market_phase="active",
            account_label="test",
            cash_available=50000,
            total_market_value=150000.0,
            total_unrealized_pnl=-1000,
            bar_as_of="2026-07-01",
            rrg_close_as_of="2026-07-01",
            rrg_intraday_as_of="2026-07-03",
            tx_gap_pct=None,
            tx_price=None,
            tw_spot_prev_close=None,
            te_gap_pct=None,
            morning_risk_note=None,
            gate_2330_995=2492.47,
            gate_2330_price=2445.0,
            gate_2330_hit=True,
            core4_gate_hits=0,
            portfolio_exit_preview=False,
            portfolio_exit_mode=False,
            portfolio_gate_ran=True,
            holdings_stress=False,
            holdings_stress_count=0,
            rrg_intraday_tick_n=100,
            rrg_intraday_kbar_n=95,
            rrg_intraday_price_mode="kbar",
            rrg_intraday_error=None,
            holdings=(),
        )
        text = format_holdings_pulse_digest(pulse)
        self.assertIn("5m K @11:00", text)
        self.assertIn("即時報價", text)

    @patch("order.holdings_pulse.fetch_fubon_snapshot")
    @patch("order.holdings_pulse.fetch_live_prices")
    def test_build_and_format(self, mock_prices, mock_fubon) -> None:
        mock_fubon.return_value = (
            {
                "_account_label": "test",
                "cash": {"available_balance": 100000},
                "holdings": [
                    {"stock_no": "2327", "tradable_qty": 0, "odd": {"tradable_qty": 20}},
                    {"stock_no": "2449", "tradable_qty": 0, "odd": {"tradable_qty": 88}},
                ],
                "unrealized_pnl": [
                    {
                        "stock_no": "2327",
                        "unrealized_profit": 4710,
                        "unrealized_loss": 0,
                        "cost_price": 775.1,
                    },
                    {
                        "stock_no": "2449",
                        "unrealized_profit": 0,
                        "unrealized_loss": 2625,
                        "cost_price": 336.48,
                    },
                ],
            },
            None,
        )
        mock_prices.return_value = (
            {"2327": 980.0, "2449": 310.0, "2330": 2300.0},
            {"2327": "fubon", "2449": "fubon", "2330": "fubon"},
            [],
        )
        pulse = build_holdings_pulse(
            self.conn,
            session_date="2026-06-29",
            use_fubon=True,
            sync_rrg_intraday=False,
            refresh_morning_risk=False,
        )
        self.assertEqual(len(pulse.holdings), 2)
        by_id = {r.base.stock_id: r for r in pulse.holdings}
        self.assertAlmostEqual(by_id["2327"].daily_pct, (980.0 - 1015.0) / 1015.0 * 100, places=2)
        self.assertEqual(by_id["2327"].rrg_close.quadrant, "weakening")
        self.assertEqual(by_id["2327"].rrg_intraday.quadrant, "lagging")
        self.assertEqual(by_id["2327"].rrg_quad_delta, "weakening→lagging")

        text = format_holdings_pulse(pulse)
        self.assertIn("持倉即時脈動", text)
        self.assertIn("組合狀態", text)
        self.assertIn("Research", text)
        self.assertIn("2327", text)
        self.assertIn("【持倉狀態】", text)
        self.assertIn("成本均價", text)
        self.assertIn("投資成本", text)
        self.assertIn("報價來源", text)
        self.assertIn("RRG收盤", text)
        self.assertIn("優先盯", text)
        self.assertIn("不等於出清指令", text)
        self.assertIn("不執行結構賣訊", text)
        self.assertNotIn("VCP", text)
        self.assertNotIn("Overextended", text)

        digest = format_holdings_pulse_digest(pulse)
        self.assertIn("【持倉狀態】", digest)
        self.assertIn("成本均價", digest)
        self.assertIn("2327", digest)
        self.assertIn("報價來源", digest)

    def test_holdings_pulse_to_dict_includes_exit_fields(self) -> None:
        base = HoldingBriefRow(
            stock_id="2327",
            stock_name="國巨*",
            shares=20,
            cost_price=909.0,
            unrealized_pnl=-363,
            prev_close=905.0,
            bar_date="2026-07-07",
            rrg_quadrant="weakening",
            rrg_seg=2.343,
            rrg_session="2026-07-07",
            structure_tier="weak",
            gate_2pct=887.0,
            trigger_s1b=877.85,
            extension_spike=941.2,
            notes=(),
        )
        row = HoldingPulseRow(
            base=base,
            current_price=896.0,
            price_source="fubon",
            daily_pct=-0.99,
            pnl_pct=-1.43,
            market_value=17920.0,
            weight_pct=9.0,
            rrg_close=RrgPoint(
                session_date="2026-07-07",
                quadrant="weakening",
                rs_ratio=106.6,
                rs_momentum=94.9,
                seg_last=2.343,
                trend="down_right",
            ),
            rrg_intraday=RrgPoint(
                session_date="2026-07-08",
                quadrant="lagging",
                rs_ratio=96.0,
                rs_momentum=98.0,
                seg_last=2.4,
                trend="down_left",
            ),
            rrg_quad_delta="weakening→lagging",
            rrg_seg_delta=0.1,
            dist_gate_2pct_pct=1.01,
            dist_s1b_pct=2.07,
            dist_extension_pct=-4.8,
            vcp_composite=None,
            vcp_state=None,
            hold_days=12,
            structural_flags=("CORE4", "結構弱"),
        )
        pulse = HoldingsPulse(
            generated_at="2026-07-08T13:15:00+08:00",
            session_date="2026-07-08",
            poll_minute="13:15",
            market_phase="active",
            account_label="test",
            cash_available=100000,
            total_market_value=198694.0,
            total_unrealized_pnl=-916,
            bar_as_of="2026-07-07",
            rrg_close_as_of="2026-07-07",
            rrg_intraday_as_of="2026-07-08",
            tx_gap_pct=1.06,
            tx_price=45963.0,
            tw_spot_prev_close=45479.11,
            te_gap_pct=None,
            morning_risk_note=None,
            gate_2330_995=2427.8,
            gate_2330_price=2445.0,
            gate_2330_hit=False,
            core4_gate_hits=1,
            portfolio_exit_preview=False,
            portfolio_exit_mode=False,
            portfolio_gate_ran=True,
            holdings_stress=False,
            holdings_stress_count=0,
            rrg_intraday_tick_n=7,
            rrg_intraday_kbar_n=7,
            rrg_intraday_price_mode="kbar",
            rrg_intraday_error=None,
            holdings=(row,),
        )
        payload = holdings_pulse_to_dict(pulse)
        h = payload["holdings"][0]
        self.assertEqual(payload["cash_available"], 100000)
        self.assertEqual(payload["total_market_value"], 198694.0)
        self.assertEqual(payload["total_unrealized_pnl"], -916)
        self.assertEqual(h["shares"], 20)
        self.assertEqual(h["cost_price"], 909.0)
        self.assertEqual(h["market_value"], 17920.0)
        self.assertEqual(h["unrealized_pnl"], -363)
        self.assertEqual(h["price_source"], "fubon")
        self.assertEqual(h["structure_tier"], "weak")
        self.assertIsNone(h["vcp_state"])
        self.assertEqual(h["hold_days"], 12)
        self.assertIn("CORE4", h["structural_flags"])
        self.assertEqual(h["rrg_close_quadrant"], "weakening")
        self.assertEqual(h["rrg_intraday_quadrant"], "lagging")
        self.assertFalse(payload["portfolio_exit_mode"])


class TestSyncIntradayRrgWorker(unittest.TestCase):
    def test_parse_rrg_sync_worker_stdout(self) -> None:
        from order.holdings_pulse import _parse_rrg_sync_worker_stdout

        self.assertEqual(
            _parse_rrg_sync_worker_stdout(
                "RRG 盤中同步 OK · rows=261 · priced=261 · kbar=15\n"
            ),
            (261, 261, 15),
        )
        self.assertIsNone(_parse_rrg_sync_worker_stdout("nope"))

    def test_sync_intraday_rrg_uses_worker_counts_without_rerun(self) -> None:
        """After main .venv worker succeeds, do not call impl in .venv-fubon."""
        import os
        from pathlib import Path
        from types import SimpleNamespace

        from order.holdings_pulse import sync_intraday_rrg

        conn = sqlite3.connect(":memory:")
        completed = SimpleNamespace(
            returncode=0,
            stdout="RRG 盤中同步 OK · rows=10 · priced=9 · kbar=3\n",
            stderr="",
        )
        main_py = Path("/proj/.venv/bin/python")
        worker_py = Path("/proj/scripts/order/sync_holdings_rrg_intraday_worker.py")

        def _is_file(self: Path) -> bool:
            return str(self) in {str(main_py), str(worker_py)}

        with (
            patch("order.holdings_pulse.PROJECT_ROOT", Path("/proj")),
            patch.object(Path, "is_file", _is_file),
            patch("subprocess.run", return_value=completed) as run_mock,
            patch("order.holdings_pulse._sync_intraday_rrg_impl") as impl_mock,
        ):
            os.environ.pop("HOLDINGS_RRG_SYNC_WORKER", None)
            n, priced, kbar, err = sync_intraday_rrg(
                conn,
                session_date="2026-07-16",
                poll_minute="10:05",
                holding_ids=["2330"],
            )

        self.assertEqual((n, priced, kbar, err), (10, 9, 3, None))
        run_mock.assert_called_once()
        impl_mock.assert_not_called()
        conn.close()


if __name__ == "__main__":
    unittest.main()
