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

    def test_observe_action_reason_key_distinct(self) -> None:
        buy = BuySignal("2330", "台積電", action="buy", pool_id="abc-v3-f1-pullback")
        obs = BuySignal("2330", "台積電", action="observe", pool_id="abc-v3-f1-pullback")
        self.assertEqual(buy.reason_key(), "abc-v3-f1-pullback:c0_entry")
        self.assertEqual(obs.reason_key(), "abc-v3-f1-pullback:observe")
        self.assertNotEqual(
            dedup_key("buy", buy.stock_id, buy.reason_key()),
            dedup_key("buy", obs.stock_id, obs.reason_key()),
        )

    def test_observe_signal_dedup_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dedup = Path(tmp) / "dedup.json"
            with patch("strategy_signal_radar.DEDUP_PATH", dedup):
                session = "2026-06-28"
                sig = BuySignal(
                    "2330", "台積電", action="observe",
                    reason="ABC v3+f1 回踩 命中", price=100.0,
                    poll_minute="09:15", pool_id="abc-v3-f1-pullback",
                )
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

    def test_mv_gap_table_sorted_positive_first(self) -> None:
        from strategy_signal_radar import _mv_gap_rows

        ranked = _mv_gap_rows(
            [
                ("8016 矽創", 100.25, 101.12),  # diff -0.87 · F fail
                ("2481 強茂", 98.95, 98.67),  # diff +0.28 · F pass
                ("6488 環球晶", 98.78, 97.65),  # diff +1.13 · F pass
                ("6499 缺MV", None, 99.0),  # 缺值略過
            ]
        )
        # 依 MV3−MV5 由大到小：+1.13 → +0.28 → -0.87；缺值列略過
        self.assertEqual([r[0] for r in ranked], ["6488 環球晶", "2481 強茂", "8016 矽創"])
        self.assertAlmostEqual(ranked[0][3], 1.13, places=2)
        self.assertTrue(ranked[0][4])  # F pass (diff positive)
        self.assertTrue(ranked[1][4])
        self.assertFalse(ranked[2][4])  # F fail (MV5 明顯高於 MV3)

    def test_format_buy_markdown_includes_mv_table(self) -> None:
        buy = [
            BuySignal(
                "8016",
                "矽創",
                action="observe",
                reason="Dual WMA 強勢回踩 命中",
                price=350.5,
                pool_id="dual-wma-lead-pullback",
                w3_mom=100.25,
                w5_mom=101.12,
            ),
            BuySignal(
                "6488",
                "環球晶",
                action="observe",
                reason="Dual WMA 強勢回踩 命中",
                price=1215.0,
                pool_id="dual-wma-lead-pullback",
                w3_mom=98.78,
                w5_mom=97.65,
            ),
        ]
        md = format_radar_markdown(buy=buy, session_date="2026-07-08", poll_minute="12:45", side="buy")
        self.assertIn("MV3−MV5 排序", md)
        self.assertIn("| 標的 | MV3 | MV5 | 差值 | F門 |", md)
        # F pass 的 6488（diff 正）應排在 F fail 的 8016（diff 負）之前
        self.assertLess(md.index("6488 環球晶"), md.index("8016 矽創"))
        self.assertIn("✅", md)
        self.assertIn("❌", md)

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
        self.assertGreaterEqual(len(specs), 5)
        ids = {s.id for s in specs}
        self.assertIn("rrg-fresh-mono", ids)
        self.assertIn("rrg-mono-tier2", ids)
        self.assertNotIn("rrg-improving-watch-setup", ids)  # Phase C disabled
        self.assertIn("abc-v3-f1-pullback", ids)
        f1 = next(s for s in specs if s.id == "abc-v3-f1-pullback")
        self.assertEqual(f1.source, "abc_v3_f1_pullback")
        self.assertTrue(f1.observe_only)
        self.assertEqual(f1.top_n, 0)
        # observe-only 命中即寄信（advisory 軌）
        self.assertTrue(f1.notify)
        self.assertIn("abc-v3-skip09-pullback", ids)
        v3 = next(s for s in specs if s.id == "abc-v3-skip09-pullback")
        self.assertEqual(v3.source, "abc_v3_skip09_pullback")
        self.assertTrue(v3.observe_only)
        self.assertTrue(v3.notify)
        self.assertNotIn("dual-wma-lead-pullback", ids)

    def test_abc_v3_skip09_source_dispatch_empty_poll(self) -> None:
        # intraday 來源在 poll_minute 為空時應早退（不觸 DB），確認 source 已註冊
        import pandas as pd
        from buy_observation import BuyObservationPoolSpec, build_observation_pool

        spec = BuyObservationPoolSpec(
            id="abc-v3-skip09-pullback",
            title="ABC v3 skip09 回踩",
            role="research",
            source="abc_v3_skip09_pullback",
            top_n=8,
            confirm_bars=1,
            notify=True,
            observe_only=True,
        )
        pool, as_of = build_observation_pool(
            None,  # type: ignore[arg-type]
            spec,
            session="2026-07-07",
            poll_minute="",
            close=pd.DataFrame(),
            bench=pd.Series(dtype=float),
            ephemeral={},
        )
        self.assertEqual(pool, [])
        self.assertEqual(as_of, "2026-07-07")

    def test_slice_top_n_zero_means_unlimited(self) -> None:
        from buy_observation import _slice_top_n

        rows = list(range(12))
        self.assertEqual(_slice_top_n(rows, 0), rows)
        self.assertEqual(_slice_top_n(rows, 8), rows[:8])

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
