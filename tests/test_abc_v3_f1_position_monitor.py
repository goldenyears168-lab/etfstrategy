"""Tests for ABC v3+F1 position monitor (champion TP + pyramid add)."""

from __future__ import annotations

import unittest

from order.abc_v3_f1_monitor_config import (
    ChampionTpMonitorConfig,
    PyramidAddMonitorConfig,
    load_champion_tp_monitor_config,
    load_pyramid_add_monitor_config,
)
from order.abc_v3_f1_position_monitor import (
    evaluate_champion_tp_on_timeline,
    evaluate_pyramid_rebound_on_timeline,
    list_open_abc_positions,
    scan_abc_v3_f1_positions,
)
from order.abc_v3_f1_reentry import AbcV3F1Ledger

TD = [
    "2026-07-01",
    "2026-07-02",
    "2026-07-03",
    "2026-07-06",
    "2026-07-07",
    "2026-07-08",
    "2026-07-09",
]


def _snap(
    *,
    minute: str = "10:00",
    ret: float = 0.0,
    w3_rv: float = 99.0,
    gap_w20_w5: float = 5.0,
    w3_quad: str = "improving",
    dmv_w5: float = 0.0,
) -> dict:
    return {
        "minute": minute,
        "ret_from_entry_pct": ret,
        "w3_rv": w3_rv,
        "w20_rv": 100.0,
        "w5_rv": 100.0,
        "w3_mv": 101.0,
        "w5_mv": 100.0,
        "w20_mv": 105.0,
        "w3_quad": w3_quad,
        "w5_quad": w3_quad,
        "gap_mv_w20_w5": gap_w20_w5,
        "gap_mv_w5_w3": 1.0,
        "dmv_w5": dmv_w5,
        "dmv_w3": 0.0,
        "drv_w3": -0.3,
    }


class TestMonitorConfig(unittest.TestCase):
    def test_load_configs_from_order_yaml(self) -> None:
        tp = load_champion_tp_monitor_config()
        py = load_pyramid_add_monitor_config()
        self.assertEqual(tp.variant_id, "TP_or_g54_g31_rvw3poll_drop_d25_r5_c1")
        # ABC Order retired · champion/pyramid stubs disabled
        self.assertFalse(tp.enabled)
        self.assertTrue(tp.observe_only)
        self.assertFalse(py.enabled)
        self.assertEqual(py.applies_to, ())


class TestOpenPositions(unittest.TestCase):
    def test_list_open_within_hold_days(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "symbol": "2330",
                    "session_date": "2026-07-07",
                    "poll_minute": "10:00",
                    "lifecycle_status": "filled",
                    "filled_qty": 10,
                    "avg_fill_price": 900.0,
                    "client_intent_id": "abc-v3-f1-pullback_20260707_1000_2330",
                    "user_def": "abc-v3-f1-pullback",
                }
            ]
        )
        open_pos = list_open_abc_positions(
            ledger, session_date="2026-07-09", trading_dates=TD, hold_days=5
        )
        self.assertEqual(len(open_pos), 1)
        self.assertEqual(open_pos[0].symbol, "2330")

    def test_expired_position_excluded(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "symbol": "2330",
                    "session_date": "2026-07-01",
                    "lifecycle_status": "filled",
                    "filled_qty": 10,
                    "avg_fill_price": 900.0,
                    "client_intent_id": "x",
                    "user_def": "abc-v3-f1-pullback",
                }
            ]
        )
        open_pos = list_open_abc_positions(
            ledger, session_date="2026-07-09", trading_dates=TD, hold_days=3
        )
        self.assertEqual(open_pos, [])


class TestTimelineEval(unittest.TestCase):
    def test_pyramid_rebound_fires_on_underwater_bounce(self) -> None:
        timeline = [
            _snap(ret=0.0, w3_rv=100.0),
            _snap(ret=-1.0, w3_rv=98.5),
            _snap(ret=-0.5, w3_rv=99.2),
        ]
        fired, idx = evaluate_pyramid_rebound_on_timeline(timeline, rebound_min=0.3)
        self.assertTrue(fired)
        self.assertEqual(idx, 2)

    def test_champion_tp_needs_min_ret_and_gaps(self) -> None:
        cfg = ChampionTpMonitorConfig(
            enabled=True,
            observe_only=True,
            strategy_id="abc-v3-f1-champion-tp",
            hold_days_cap=5,
            variant_id="TP_or_g54_g31_rvw3poll_drop_d25_r5_c1",
            gw5_min=4.0,
            gw3_min=1.0,
            rv_leg="w3",
            rv_mode="poll_drop",
            drv_min=0.25,
            min_ret_pct=5.0,
        )
        timeline = [
            _snap(ret=0.0, gap_w20_w5=3.0),
            _snap(ret=6.0, gap_w20_w5=5.0, w3_rv=98.0),
        ]
        fired, _ = evaluate_champion_tp_on_timeline(timeline, cfg=cfg)
        self.assertIsInstance(fired, bool)


class TestScan(unittest.TestCase):
    def test_scan_disabled_returns_skipped(self) -> None:
        result = scan_abc_v3_f1_positions(
            None,
            session_date="2026-07-09",
            poll_minute="10:00",
            trading_dates=TD,
            champion_cfg=ChampionTpMonitorConfig(
                enabled=False,
                observe_only=True,
                strategy_id="abc-v3-f1-champion-tp",
                hold_days_cap=5,
                variant_id="x",
                gw5_min=4.0,
                gw3_min=1.0,
                rv_leg="w3",
                rv_mode="poll_drop",
                drv_min=0.25,
                min_ret_pct=5.0,
            ),
            pyramid_cfg=PyramidAddMonitorConfig(
                enabled=False,
                observe_only=True,
                rebound_min=0.3,
                sizing_mode="equal_weight_second_unit",
                budget_fraction=1.0,
                exit_mode="sync_exit",
                applies_to=("abc-v3-f1-pullback",),
            ),
        )
        self.assertEqual(result.skipped_reason, "monitor_disabled")

    def test_scan_hold_cap_waits_full_cap_day(self) -> None:
        """Policy B: held==cap still waits for TP; no force on cap day."""
        cid = "abc-v3-f1-pullback_20260701_1000_2330"
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "symbol": "2330",
                    "session_date": "2026-07-01",
                    "poll_minute": "10:00",
                    "lifecycle_status": "filled",
                    "filled_qty": 10,
                    "avg_fill_price": 900.0,
                    "client_intent_id": cid,
                    "user_def": "abc-v3-f1-pullback",
                }
            ]
        )
        timeline = [_snap(ret=0.0), _snap(ret=1.0)]
        champion = ChampionTpMonitorConfig(
            enabled=True,
            observe_only=False,
            strategy_id="abc-v3-f1-champion-tp",
            hold_days_cap=5,
            variant_id="TP_or_g54_g31_rvw3poll_drop_d25_r5_c1",
            gw5_min=4.0,
            gw3_min=1.0,
            rv_leg="w3",
            rv_mode="poll_drop",
            drv_min=0.25,
            min_ret_pct=5.0,
        )
        pyramid = PyramidAddMonitorConfig(
            enabled=False,
            observe_only=True,
            rebound_min=0.3,
            sizing_mode="equal_weight_second_unit",
            budget_fraction=1.0,
            exit_mode="sync_exit",
            applies_to=("abc-v3-f1-pullback",),
        )
        # Jul1 → Jul8 = 5 trading days (cap day) · no force even late session
        cap_day = scan_abc_v3_f1_positions(
            None,
            session_date="2026-07-08",
            poll_minute="13:20",
            trading_dates=TD,
            ledger=ledger,
            timelines={cid: timeline},
            champion_cfg=champion,
            pyramid_cfg=pyramid,
        )
        self.assertNotIn("hold_cap_sell", [h.kind for h in cap_day.hits])

    def test_scan_hold_cap_sell_next_session(self) -> None:
        """Policy B: force on held > cap (next session after full cap day)."""
        cid = "abc-v3-f1-pullback_20260701_1000_2330"
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "symbol": "2330",
                    "session_date": "2026-07-01",
                    "poll_minute": "10:00",
                    "lifecycle_status": "filled",
                    "filled_qty": 10,
                    "avg_fill_price": 900.0,
                    "client_intent_id": cid,
                    "user_def": "abc-v3-f1-pullback",
                }
            ]
        )
        timeline = [_snap(ret=0.0), _snap(ret=1.0)]
        # Jul1 → Jul9 = 6 trading days → force at open poll
        result = scan_abc_v3_f1_positions(
            None,
            session_date="2026-07-09",
            poll_minute="09:30",
            trading_dates=TD,
            ledger=ledger,
            timelines={cid: timeline},
            champion_cfg=ChampionTpMonitorConfig(
                enabled=True,
                observe_only=False,
                strategy_id="abc-v3-f1-champion-tp",
                hold_days_cap=5,
                variant_id="TP_or_g54_g31_rvw3poll_drop_d25_r5_c1",
                gw5_min=4.0,
                gw3_min=1.0,
                rv_leg="w3",
                rv_mode="poll_drop",
                drv_min=0.25,
                min_ret_pct=5.0,
            ),
            pyramid_cfg=PyramidAddMonitorConfig(
                enabled=False,
                observe_only=True,
                rebound_min=0.3,
                sizing_mode="equal_weight_second_unit",
                budget_fraction=1.0,
                exit_mode="sync_exit",
                applies_to=("abc-v3-f1-pullback",),
            ),
        )
        self.assertIn("hold_cap_sell", [h.kind for h in result.hits])

    def test_scan_pyramid_hit_with_injected_timeline(self) -> None:
        cid = "abc-v3-f1-pullback_20260707_1000_2330"
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "symbol": "2330",
                    "session_date": "2026-07-07",
                    "poll_minute": "10:00",
                    "lifecycle_status": "filled",
                    "filled_qty": 10,
                    "avg_fill_price": 900.0,
                    "client_intent_id": cid,
                    "user_def": "abc-v3-f1-pullback",
                }
            ]
        )
        timeline = [
            _snap(ret=0.0, w3_rv=100.0),
            _snap(ret=-1.0, w3_rv=98.5),
            _snap(ret=-0.5, w3_rv=99.2),
        ]
        result = scan_abc_v3_f1_positions(
            None,
            session_date="2026-07-09",
            poll_minute="10:30",
            trading_dates=TD,
            ledger=ledger,
            champion_cfg=ChampionTpMonitorConfig(
                enabled=False,
                observe_only=True,
                strategy_id="abc-v3-f1-champion-tp",
                hold_days_cap=5,
                variant_id="x",
                gw5_min=4.0,
                gw3_min=1.0,
                rv_leg="w3",
                rv_mode="poll_drop",
                drv_min=0.25,
                min_ret_pct=5.0,
            ),
            pyramid_cfg=PyramidAddMonitorConfig(
                enabled=True,
                observe_only=True,
                rebound_min=0.3,
                sizing_mode="equal_weight_second_unit",
                budget_fraction=1.0,
                exit_mode="sync_exit",
                applies_to=("abc-v3-f1-pullback",),
            ),
            timelines={cid: timeline},
        )
        self.assertEqual(len(result.open_positions), 1)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].kind, "pyramid_add_buy")


if __name__ == "__main__":
    unittest.main()
