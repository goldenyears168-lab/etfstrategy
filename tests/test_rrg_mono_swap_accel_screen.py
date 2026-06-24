"""Tests for rrg_mono_swap_accel_screen live helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from research.backtest.rrg_mono_score_swap_c import (
    CHAMPION_SCORE_SWAP_C_VARIANT_ID,
    champion_score_swap_c_config,
)
from rrg_mono_daily_brief import TOP_N, ScanRow
from rrg_mono_swap_accel_screen import (
    ALIGN_MODE,
    ScreenAction,
    ScreenResult,
    _c0_scale_blocker_clause,
    _entry_blocker_plain,
    _lead_drift_plain,
    _lot_shares,
    _parse_pool_override,
    _poll_minute,
    _poll_window_ok,
    _scan_row_from_dict,
    _scan_row_to_dict,
    _swap_allowed,
    append_poll_tick_log,
    build_intent_batch,
    champion_score_swap_c_config as screen_champion,
    load_slot_state,
    lock_pit_fresh_pool,
    render_markdown,
    save_slot_state,
)


class TestC18accLiveScreen(unittest.TestCase):
    def test_champion_config_matches_variant(self) -> None:
        cfg = champion_score_swap_c_config()
        self.assertEqual(cfg.variant_id, CHAMPION_SCORE_SWAP_C_VARIANT_ID)
        self.assertEqual(cfg.entry_leg, "C0")
        self.assertEqual(cfg.timing_mode, "poll_5m")
        self.assertEqual(screen_champion().variant_id, cfg.variant_id)

    def test_poll_minute_floors_to_5m(self) -> None:
        self.assertEqual(_poll_minute(datetime(2026, 6, 24, 9, 17)), "09:15")
        self.assertEqual(_poll_minute(datetime(2026, 6, 24, 9, 32)), "09:30")

    def test_poll_window_weekday_only(self) -> None:
        tue = datetime(2026, 6, 23, 10, 0)
        sat = datetime(2026, 6, 27, 10, 0)
        self.assertTrue(_poll_window_ok(tue))
        self.assertFalse(_poll_window_ok(sat))

    def test_swap_after_0930(self) -> None:
        self.assertFalse(_swap_allowed(datetime(2026, 6, 23, 9, 29)))
        self.assertTrue(_swap_allowed(datetime(2026, 6, 23, 9, 30)))

    def test_scan_row_roundtrip(self) -> None:
        row = ScanRow("2330", "台積電", True, True, 1.2, 1.5, [1.0], ["leading"], 100.0, 101.0, None)
        back = _scan_row_from_dict(_scan_row_to_dict(row))
        self.assertEqual(back.stock_id, "2330")
        self.assertTrue(back.fresh)

    def test_build_intent_batch_includes_align_metadata(self) -> None:
        cfg = champion_score_swap_c_config()
        result = ScreenResult(
            session_date="2026-06-24",
            polled_at="2026-06-24 09:35",
            config=cfg,
            pool_as_of="2026-06-23",
            poll_minute="09:35",
            dry_run=True,
            actions=[
                ScreenAction(
                    kind="entry",
                    stock_id="2330",
                    stock_name="台積電",
                    side="buy",
                    price=580.0,
                    quantity_shares=1000,
                    note="test",
                )
            ],
        )
        batch = build_intent_batch(result)
        assert batch is not None
        self.assertEqual(batch["metadata"]["align_mode"], ALIGN_MODE)
        self.assertEqual(batch["metadata"]["pool_as_of"], "2026-06-23")

    def test_parse_pool_override_respects_date(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "C18ACC_POOL_OVERRIDE": "3711,6488,2344,3008",
                "C18ACC_POOL_OVERRIDE_DATE": "2026-06-24",
            },
        ):
            self.assertEqual(
                _parse_pool_override("2026-06-24"),
                ["3711", "6488", "2344", "3008"],
            )
            self.assertIsNone(_parse_pool_override("2026-06-25"))

    def test_render_markdown_shows_manual_override(self) -> None:
        cfg = champion_score_swap_c_config()
        md = render_markdown(
            ScreenResult(
                session_date="2026-06-24",
                polled_at="2026-06-24 09:35",
                config=cfg,
                pool_as_of="2026-06-23",
                poll_minute="09:35",
                pool_override=["3711", "6488", "2344", "3008"],
                mono_top10=[],
                dry_run=True,
            )
        )
        self.assertIn("手動候選", md)
        self.assertIn("3711", md)

    def test_render_markdown_shows_pit_pool(self) -> None:
        cfg = champion_score_swap_c_config()
        md = render_markdown(
            ScreenResult(
                session_date="2026-06-24",
                polled_at="2026-06-24 09:35",
                config=cfg,
                pool_as_of="2026-06-23",
                poll_minute="09:35",
                dry_run=True,
            )
        )
        self.assertIn("fresh mono 全池", md)
        self.assertIn("2026-06-23", md)
        self.assertIn("backtest_pit", md)

    def test_append_poll_tick_log(self) -> None:
        cfg = champion_score_swap_c_config()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "tick.log"
            result = ScreenResult(
                session_date="2026-06-24",
                polled_at="2026-06-24 09:35",
                config=cfg,
                pool_as_of="2026-06-23",
                poll_minute="09:35",
                mono_top10=[],
                actions=[],
                entry_gate="有標的正在靠近買點",
                lead="3711 台塑化",
                lead_drift="較上輪更靠近買點",
                blocker="3711 · C0 排序 #1：confirm_bars 0/1",
            )
            append_poll_tick_log(result, log_path=log_path)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("pool_as_of=2026-06-23", text)
            self.assertIn("minute=09:35", text)
            self.assertIn("entry_gate=有標的正在靠近買點", text)
            self.assertIn("lead=3711 台塑化", text)
            self.assertIn("lead_drift=較上輪更靠近買點", text)
            self.assertIn("blocker=3711 · C0 排序 #1：confirm_bars 0/1", text)

    def test_c0_scale_blocker_clause_missing_close(self) -> None:
        clause = _c0_scale_blocker_clause(
            "3711",
            "12:50",
            close_ok=False,
            px_ok=True,
            rank=1,
        )
        self.assertIn("C0 scaled seg_last 無法計算", clause)
        self.assertIn("收盤基準價", clause)
        self.assertNotIn("lead缺", clause)

    def test_c0_scale_blocker_clause_missing_kbar_only(self) -> None:
        clause = _c0_scale_blocker_clause(
            "6488",
            "12:50",
            close_ok=True,
            px_ok=False,
            rank=2,
        )
        self.assertIn("12:50 尚無盤中 kbar", clause)
        self.assertIn("C0 排序 #2", clause)

    def test_c0_scale_blocker_clause_missing_both(self) -> None:
        clause = _c0_scale_blocker_clause(
            "3711",
            "12:50",
            close_ok=False,
            px_ok=False,
            rank=1,
        )
        self.assertIn("session 無收盤基準價", clause)
        self.assertIn("12:50 無盤中 kbar", clause)

    def test_entry_blocker_prefers_confirm_ready_candidate(self) -> None:
        import pandas as pd

        row_lead = ScanRow("3711", "日月光", True, True, 1.0, 1.0, [1.0], ["leading"], 100.0, 101.0, None)
        row_ready = ScanRow("6488", "環球晶", True, True, 0.8, 1.0, [1.0], ["leading"], 100.0, 101.0, None)

        class _Conn:
            pass

        with patch(
            "rrg_mono_swap_accel_screen._c0_scale_diagnostics",
            side_effect=[(True, False), (True, True)],
        ):
            blocker = _entry_blocker_plain(
                ranked=[row_lead, row_ready],
                confirm={"3711": 0, "6488": 41},
                need_confirm=1,
                held=set(),
                entries_today=set(),
                session="2026-06-24",
                poll_minute="12:50",
                conn=_Conn(),
                close=pd.DataFrame(),
                kbar_cache={},
                lead_row=row_lead,
                lead_rank=1,
                slots_n=0,
                has_entry_action=False,
            )
        self.assertIn("6488", blocker)
        self.assertIn("12:50 尚無盤中 kbar", blocker)

    def test_lot_shares_odd_lot_default(self) -> None:
        self.assertEqual(_lot_shares(644.0, 20000), 31)
        self.assertEqual(_lot_shares(644.0, 20000, board_lot=True), 0)

    def test_lot_shares_board_lot(self) -> None:
        self.assertEqual(_lot_shares(50.0, 20000, board_lot=True), 0)
        self.assertEqual(_lot_shares(50.0, 60000, board_lot=True), 1000)

    def test_lead_drift_plain_closer(self) -> None:
        row = ScanRow("3711", "台塑化", True, True, 1.0, 1.0, [1.0], ["leading"], 100.0, 101.0, None)
        drift = _lead_drift_plain(
            lead=row,
            rank=1,
            confirm=1,
            scaled=0.9,
            last_poll={"lead": "3711", "rank": 2, "confirm": 0, "scaled": 0.8},
        )
        self.assertEqual(drift, "較上輪更靠近買點")

    def test_slot_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slots.json"
            with patch("rrg_mono_swap_accel_screen.STATE_PATH", path):
                save_slot_state({"slots": [{"slot": 0, "stock_id": "2330"}], "history": []})
                loaded = load_slot_state()
                self.assertEqual(loaded["slots"][0]["stock_id"], "2330")


if __name__ == "__main__":
    unittest.main()
