"""Tests for C18acc position monitor (underwater_rebound pyramid add)."""

from __future__ import annotations

import unittest

from order.c18acc_monitor_config import load_c18acc_pyramid_add_monitor_config
from order.c18acc_position_monitor import (
    is_kinematic_poll,
    list_open_c18_positions,
    scan_c18acc_pyramid_adds,
    slot_parent_cid,
)
from order.c18acc_pyramid_ledger import C18accPyramidLedger

TD = [
    "2026-07-01",
    "2026-07-02",
    "2026-07-03",
    "2026-07-06",
    "2026-07-07",
    "2026-07-08",
    "2026-07-09",
]


def _snap(**kw) -> dict:
    base = {
        "minute": "10:00",
        "ret_from_entry_pct": 0.0,
        "w3_rv": 99.0,
        "w20_rv": 100.0,
        "w5_rv": 100.0,
        "w3_mv": 101.0,
        "w5_mv": 100.0,
        "w20_mv": 105.0,
        "w3_quad": "improving",
        "w5_quad": "improving",
        "gap_mv_w20_w5": 5.0,
        "gap_mv_w5_w3": 1.0,
        "dmv_w5": 0.0,
        "dmv_w3": 0.0,
        "drv_w3": -0.3,
    }
    base.update(kw)
    return base


class TestC18MonitorConfig(unittest.TestCase):
    def test_load_config_yaml_enabled(self) -> None:
        cfg = load_c18acc_pyramid_add_monitor_config()
        self.assertTrue(cfg.enabled)
        self.assertFalse(cfg.observe_only)
        self.assertEqual(cfg.rebound_min, 0.3)
        self.assertEqual(cfg.poll_interval_minutes, 30)


class TestC18OpenPositions(unittest.TestCase):
    def test_list_open_within_max_hold(self) -> None:
        state = {
            "slots": [
                {
                    "slot": 0,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "entry_date": "2026-07-07",
                    "entry_px": 900.0,
                    "entry_poll_minute": "09:35",
                }
            ]
        }
        open_pos = list_open_c18_positions(
            state, session_date="2026-07-09", trading_dates=TD, max_hold_days=10
        )
        self.assertEqual(len(open_pos), 1)
        self.assertEqual(open_pos[0].symbol, "2330")
        self.assertEqual(open_pos[0].entry_poll_minute, "09:35")

    def test_expired_slot_excluded(self) -> None:
        long_td = [
            "2026-06-20",
            "2026-06-23",
            "2026-06-24",
            "2026-06-25",
            "2026-06-26",
            "2026-06-27",
            "2026-06-30",
            "2026-07-01",
            "2026-07-02",
            "2026-07-03",
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
            "2026-07-09",
        ]
        state = {
            "slots": [
                {
                    "slot": 0,
                    "stock_id": "2330",
                    "entry_date": "2026-06-20",
                    "entry_px": 900.0,
                }
            ]
        }
        open_pos = list_open_c18_positions(
            state, session_date="2026-07-09", trading_dates=long_td, max_hold_days=10
        )
        self.assertEqual(open_pos, [])


class TestKinematicPoll(unittest.TestCase):
    def test_30m_grid(self) -> None:
        self.assertTrue(is_kinematic_poll("09:30"))
        self.assertTrue(is_kinematic_poll("10:00"))
        self.assertFalse(is_kinematic_poll("09:35"))
        self.assertFalse(is_kinematic_poll("10:05"))


class TestScan(unittest.TestCase):
    def test_scan_disabled(self) -> None:
        from order.c18acc_monitor_config import C18accPyramidAddMonitorConfig

        result = scan_c18acc_pyramid_adds(
            None,
            state={"slots": []},
            session_date="2026-07-09",
            poll_minute="10:30",
            trading_dates=TD,
            pyramid_cfg=C18accPyramidAddMonitorConfig(
                enabled=False,
                observe_only=True,
                rebound_min=0.3,
                sizing_mode="equal_weight_second_unit",
                budget_fraction=1.0,
                exit_mode="sync_exit",
                strategy_id="rrg-mono-swap-accel",
                max_hold_days=10,
                poll_interval_minutes=30,
            ),
        )
        self.assertEqual(result.skipped_reason, "monitor_disabled")

    def test_scan_pyramid_hit_with_injected_timeline(self) -> None:
        from order.c18acc_monitor_config import C18accPyramidAddMonitorConfig

        pos = {
            "slot": 1,
            "stock_id": "2330",
            "stock_name": "台積電",
            "entry_date": "2026-07-07",
            "entry_px": 900.0,
            "entry_poll_minute": "09:30",
        }
        parent = slot_parent_cid(pos)
        timeline = [
            _snap(ret_from_entry_pct=0.0, w3_rv=100.0),
            _snap(ret_from_entry_pct=-1.0, w3_rv=98.5),
            _snap(ret_from_entry_pct=-0.5, w3_rv=99.2),
        ]
        result = scan_c18acc_pyramid_adds(
            None,
            state={"slots": [pos]},
            session_date="2026-07-09",
            poll_minute="10:30",
            trading_dates=TD,
            pyramid_cfg=C18accPyramidAddMonitorConfig(
                enabled=True,
                observe_only=True,
                rebound_min=0.3,
                sizing_mode="equal_weight_second_unit",
                budget_fraction=1.0,
                exit_mode="sync_exit",
                strategy_id="rrg-mono-swap-accel",
                max_hold_days=10,
                poll_interval_minutes=30,
            ),
            ledger=C18accPyramidLedger(),
            timelines={parent: timeline},
        )
        self.assertEqual(len(result.open_positions), 1)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].kind, "pyramid_add_buy")


if __name__ == "__main__":
    unittest.main()
