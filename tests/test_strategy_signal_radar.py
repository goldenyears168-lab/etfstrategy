"""Tests for advisory signal radar · dedup · markdown · buy without slots."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from rrg_mono_daily_brief import ScanRow
from strategy_signal_radar import (
    BuySignal,
    SellSignal,
    dedup_key,
    filter_new_signals,
    format_radar_markdown,
    mark_notified,
    _try_c0_entry_advisory,
)


class TestSignalRadarDedup(unittest.TestCase):
    def test_dedup_same_symbol_reason_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dedup = Path(tmp) / "dedup.json"
            with patch("strategy_signal_radar.DEDUP_PATH", dedup):
                session = "2026-06-28"
                sig = BuySignal("2330", "台積電", reason="C0 scale", price=100.0, poll_minute="09:15")
                _, new1 = filter_new_signals("buy", [sig], session_date=session)
                self.assertEqual(len(new1), 1)
                mark_notified("buy", new1, session_date=session)
                _, new2 = filter_new_signals("buy", [sig], session_date=session)
                self.assertEqual(len(new2), 0)

    def test_dedup_resets_next_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dedup = Path(tmp) / "dedup.json"
            with patch("strategy_signal_radar.DEDUP_PATH", dedup):
                sig = SellSignal("2454", "聯發科", reason="combo", price=900.0, exit_mode="combo_spike")
                mark_notified("sell", [sig], session_date="2026-06-27")
                _, new = filter_new_signals("sell", [sig], session_date="2026-06-28")
                self.assertEqual(len(new), 1)

    def test_dedup_key_format(self) -> None:
        self.assertEqual(dedup_key("buy", "2330", "c0_entry"), "buy:2330:c0_entry")


class TestSignalRadarMarkdown(unittest.TestCase):
    def test_format_includes_symbol_action_price(self) -> None:
        buy = [
            BuySignal(
                "2330",
                "台積電",
                reason="C0 scale @ 09:15 confirm=1",
                price=580.0,
                poll_minute="09:15",
                pool_id="rrg-fresh-mono",
            )
        ]
        sell = [SellSignal("2454", "聯發科", reason="combo_spike", price=900.0, exit_mode="combo_spike")]
        md = format_radar_markdown(buy=buy, sell=sell, session_date="2026-06-28", poll_minute="09:15")
        self.assertIn("2330", md)
        self.assertIn("**buy**", md)
        self.assertIn("580.00", md)
        self.assertIn("RRG fresh mono", md)
        self.assertIn("2454", md)
        self.assertIn("**sell**", md)

    def test_format_buy_merges_same_symbol_pools(self) -> None:
        buy = [
            BuySignal(
                "5536",
                "聖暉*",
                reason="C0 scale @ 09:05 confirm=1",
                price=1365.0,
                pool_id="rrg-fresh-mono",
            ),
            BuySignal(
                "5536",
                "聖暉*",
                reason="C0 scale @ 09:05 confirm=1",
                price=1365.0,
                pool_id="rrg-mono-tier2",
            ),
        ]
        md = format_radar_markdown(buy=buy, session_date="2026-06-26", poll_minute="09:05", side="buy")
        self.assertEqual(md.count("5536"), 1)
        self.assertIn("RRG fresh mono + RRG mono tier2", md)
        self.assertIn("多軌重疊", md)

    def test_format_buy_log_skip(self) -> None:
        from strategy_signal_radar import BuyRadarResult, format_buy_radar_log

        result = BuyRadarResult(
            session_date="2026-06-26",
            polled_at="2026-06-26T09:00:00",
            poll_minute="09:00",
            skip_reason="outside entry window",
        )
        text = format_buy_radar_log(result)
        self.assertIn("【買入觀測】", text)
        self.assertIn("略過", text)
        self.assertIn("寄信：否", text)

    def test_format_buy_log_signal(self) -> None:
        from strategy_signal_radar import BuyRadarResult, format_buy_radar_log

        sig = BuySignal(
            "3711",
            "日月光投控",
            reason="C0 scale @ 09:15 confirm=1",
            price=638.0,
            poll_minute="09:15",
            pool_id="rrg-fresh-mono",
        )
        result = BuyRadarResult(
            session_date="2026-06-26",
            polled_at="2026-06-26T09:15:00",
            poll_minute="09:15",
            pool_as_of="2026-06-25",
            pool_n=12,
            signals=[sig],
            new_signals=[sig],
        )
        text = format_buy_radar_log(result)
        self.assertIn("買進 3711", text)
        self.assertIn("638.00", text)
        self.assertIn("RRG fresh mono", text)
        self.assertIn("寄信：是", text)

    def test_load_buy_observation_config(self) -> None:
        from buy_observation import load_buy_observation_config

        _, specs = load_buy_observation_config()
        self.assertGreaterEqual(len(specs), 8)
        ids = {s.id for s in specs}
        self.assertIn("rrg-fresh-mono", ids)
        self.assertIn("rrg-mono-tier2", ids)
        self.assertIn("rrg-improving-watch-setup", ids)
        self.assertIn("dual-wma-lead-pullback", ids)
        lpb = next(s for s in specs if s.id == "dual-wma-lead-pullback")
        self.assertEqual(lpb.source, "dual_wma_lead_pullback")
        self.assertTrue(lpb.observe_only)
        self.assertFalse(lpb.notify)

    def test_format_buy_only(self) -> None:
        md = format_radar_markdown(
            buy=[BuySignal("2330", "台積電", price=1.0, reason="test")],
            session_date="2026-06-28",
            poll_minute="09:00",
            side="buy",
        )
        self.assertIn("Buy signals", md)
        self.assertNotIn("Sell signals", md)


class TestBuyWithoutSlots(unittest.TestCase):
    def test_c0_entry_advisory_no_slot_cap(self) -> None:
        conn = MagicMock()
        pool = [
            ScanRow("2330", "台積電", True, True, 1.2, 1.5, [1.0], ["leading"], 100.0, 101.0, None),
            ScanRow("2454", "聯發科", True, True, 1.1, 1.4, [1.0], ["leading"], 99.0, 100.0, None),
        ]
        state: dict = {"entry_confirm": {}}
        c0_cfg = MagicMock()
        c0_cfg.confirm_bars = 1

        with patch("strategy_signal_radar.rank_shortlist_scale", return_value=pool):
            with patch("strategy_signal_radar._kbar_px_at", return_value=580.0):
                row, px, reason = _try_c0_entry_advisory(
                    conn,
                    confirm_state=state,
                    pool=pool,
                    session="2026-06-28",
                    poll_minute="09:15",
                    close=MagicMock(),
                    kbar_cache={},
                    c0_cfg=c0_cfg,
                    held_ids=set(),
                )
        self.assertIsNotNone(row)
        self.assertEqual(row.stock_id, "2330")
        self.assertEqual(px, 580.0)
        self.assertIn("C0 scale", reason)

    def test_c0_entry_skips_held(self) -> None:
        conn = MagicMock()
        pool = [ScanRow("2330", "台積電", True, True, 1.2, 1.5, [1.0], ["leading"], 100.0, 101.0, None)]
        state: dict = {"entry_confirm": {"2330": 5}}
        c0_cfg = MagicMock()
        c0_cfg.confirm_bars = 1

        with patch("strategy_signal_radar.rank_shortlist_scale", return_value=pool):
            row, px, _ = _try_c0_entry_advisory(
                conn,
                confirm_state=state,
                pool=pool,
                session="2026-06-28",
                poll_minute="09:15",
                close=MagicMock(),
                kbar_cache={},
                c0_cfg=c0_cfg,
                held_ids={"2330"},
            )
        self.assertIsNone(row)
        self.assertIsNone(px)


class TestSellUniverseOverlay(unittest.TestCase):
    def test_split_held_and_watch(self) -> None:
        from strategy_signal_radar import SellSignal, split_universe_sell_signals
        from c18acc_extension_overlay import ExtensionAlert

        alerts = [
            ExtensionAlert(
                stock_id="3711",
                stock_name="日月光投控",
                minute="09:45",
                price=679.0,
                heat_score=1,
                zone="",
                exit_mode="combo_spike",
                ext_prev_pct=5.9,
                flags=[],
                note="combo",
            ),
            ExtensionAlert(
                stock_id="3008",
                stock_name="大立光",
                minute="10:00",
                price=2500.0,
                heat_score=1,
                zone="",
                exit_mode="combo_spike",
                ext_prev_pct=4.0,
                flags=[],
                note="combo",
            ),
        ]
        members = {"vip_a": {"3711": 50}}
        universe, held, watch = split_universe_sell_signals(
            alerts,
            "09:45",
            holdings={"3711": 100},
            member_holdings=members,
        )
        self.assertEqual(len(universe), 2)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].stock_id, "3711")
        self.assertEqual(held[0].quantity, 100)
        self.assertEqual(held[0].member_holders, ("vip_a",))
        self.assertEqual(len(watch), 1)
        self.assertEqual(watch[0].stock_id, "3008")

    def test_format_sell_log_held_overlay(self) -> None:
        from strategy_signal_radar import SellRadarResult, SellSignal, format_sell_radar_log

        held = SellSignal(
            "3711",
            "日月光投控",
            reason="X3 combo_spike",
            price=679.0,
            exit_mode="combo_spike",
            in_holdings=True,
            quantity=100,
        )
        watch = SellSignal(
            "3008",
            "大立光",
            reason="X3 combo_spike",
            price=2500.0,
            exit_mode="combo_spike",
            in_holdings=False,
        )
        result = SellRadarResult(
            session_date="2026-06-26",
            polled_at="2026-06-26T09:46:00",
            poll_minute="09:45",
            universe_n=149,
            universe_kbar_n=149,
            holdings_n=11,
            signals=[held, watch],
            extension_held_signals=[held],
            held_signals=[held],
            watch_signals=[watch],
            new_signals=[held],
        )
        text = format_sell_radar_log(result)
        self.assertIn("持倉交集", text)
        self.assertIn("3711", text)
        self.assertIn("×100", text)
        self.assertIn("寄信：是", text)

    def test_replay_fast_sell_matches_live_poll(self) -> None:
        import os
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import patch

        from stock_db import DEFAULT_DB_PATH, connect
        from strategy_signal_radar import (
            clear_signal_radar_replay_caches,
            run_sell_signal_radar,
        )

        conn = connect(DEFAULT_DB_PATH)
        session = "2026-06-26"
        now = datetime(2026, 6, 26, 9, 46, tzinfo=ZoneInfo("Asia/Taipei"))
        try:
            with patch.dict(os.environ, {"SIGNAL_RADAR_REPLAY_FAST": "0"}, clear=False):
                clear_signal_radar_replay_caches()
                slow = run_sell_signal_radar(conn, session_date=session, now=now, mark_dedup=False)
            with patch.dict(os.environ, {"SIGNAL_RADAR_REPLAY_FAST": "1"}, clear=False):
                clear_signal_radar_replay_caches()
                fast = run_sell_signal_radar(conn, session_date=session, now=now, mark_dedup=False)
        finally:
            conn.close()
            clear_signal_radar_replay_caches()

        slow_ids = sorted(s.stock_id for s in slow.signals)
        fast_ids = sorted(s.stock_id for s in fast.signals)
        self.assertEqual(slow_ids, fast_ids)

    def test_extension_alerts_to_sell_signals(self) -> None:
        from c18acc_extension_overlay import ExtensionAlert
        from strategy_signal_radar import extension_alerts_to_sell_signals

        alerts = [
            ExtensionAlert(
                stock_id="3189",
                stock_name="景碩科技",
                minute="09:42",
                price=879.0,
                heat_score=1,
                zone="",
                exit_mode="combo_spike",
                ext_prev_pct=8.3,
                flags=[],
                note="combo",
            ),
        ]
        sigs = extension_alerts_to_sell_signals(alerts, "09:45")
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].stock_id, "3189")
        self.assertFalse(sigs[0].in_holdings)


if __name__ == "__main__":
    unittest.main()
