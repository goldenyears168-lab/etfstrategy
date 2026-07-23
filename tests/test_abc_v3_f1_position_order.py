"""Tests for ABC v3+F1 position order · champion TP + pyramid dry-run."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from order.abc_v3_f1_monitor_config import (
    ChampionTpMonitorConfig,
    PyramidAddMonitorConfig,
)
from order.abc_v3_f1_position_order import (
    exit_done_for_parent,
    position_monitor_enabled,
    process_abc_v3_f1_position_orders,
)
from order.abc_v3_f1_reentry import AbcV3F1Ledger


class TestAbcPositionOrder(unittest.TestCase):
    def test_monitor_disabled_when_abc_retired(self) -> None:
        # order.yaml ABC champion/pyramid enabled:false · Order retired
        self.assertFalse(position_monitor_enabled())

    def test_exit_done_for_parent(self) -> None:
        ledger = AbcV3F1Ledger(
            entries=[
                {
                    "client_intent_id": "abc_exit_1",
                    "exit_parent_cid": "abc_entry_1",
                    "order_kind": "champion_tp_sell",
                    "lifecycle_status": "filled",
                }
            ]
        )
        self.assertTrue(exit_done_for_parent(ledger, "abc_entry_1"))
        self.assertFalse(exit_done_for_parent(ledger, "abc_other"))

    def test_process_retired_without_force_legacy(self) -> None:
        out = process_abc_v3_f1_position_orders(
            session_date="2026-07-09",
            poll_minute="10:30",
            conn=None,
            dry_run=True,
        )
        self.assertEqual(out.get("status"), "retired")
        self.assertIn("abc_order_removed", str(out.get("reason") or ""))

    @patch.dict(
        os.environ,
        {"ABC_V3_F1_CHAMPION_TP_ENABLED": "1", "ABC_V3_F1_ORDER_FORCE_LEGACY": "1"},
        clear=False,
    )
    @patch("order.abc_v3_f1_position_order.load_champion_tp_monitor_config")
    @patch("order.abc_v3_f1_position_order.load_pyramid_add_monitor_config")
    @patch("order.abc_v3_f1_position_order.scan_abc_v3_f1_positions")
    def test_dry_run_hits(self, mock_scan, mock_py, mock_tp) -> None:
        from order.abc_v3_f1_position_monitor import OpenAbcPosition, PositionMonitorHit, PositionMonitorResult

        mock_tp.return_value = ChampionTpMonitorConfig(
            enabled=True,
            observe_only=False,
            strategy_id="abc-v3-f1-champion-tp",
            hold_days_cap=5,
            variant_id="x",
            gw5_min=4.0,
            gw3_min=1.0,
            rv_leg="w3",
            rv_mode="poll_drop",
            drv_min=0.25,
            min_ret_pct=5.0,
        )
        mock_py.return_value = PyramidAddMonitorConfig(
            enabled=False,
            observe_only=True,
            rebound_min=0.3,
            sizing_mode="equal_weight_second_unit",
            budget_fraction=1.0,
            exit_mode="sync_exit",
            applies_to=("abc-v3-f1-pullback",),
        )
        pos = OpenAbcPosition(
            symbol="2330",
            entry_session_date="2026-07-07",
            entry_poll_minute="10:00",
            entry_price=900.0,
            quantity_shares=10,
            client_intent_id="abc-v3-f1-pullback_20260707_1000_2330",
            strategy_id="abc-v3-f1-pullback",
        )
        mock_scan.return_value = PositionMonitorResult(
            session_date="2026-07-09",
            poll_minute="10:30",
            open_positions=[pos],
            hits=[
                PositionMonitorHit(
                    kind="champion_tp_sell",
                    position=pos,
                    poll_minute="10:30",
                    reason="champion_tp test",
                )
            ],
        )
        out = process_abc_v3_f1_position_orders(
            session_date="2026-07-09",
            poll_minute="10:30",
            conn=None,
            dry_run=True,
        )
        self.assertEqual(out["hits_n"], 1)
        self.assertEqual(out["results"][0]["status"], "dry_run")
        self.assertEqual(out["results"][0]["kind"], "champion_tp_sell")


if __name__ == "__main__":
    unittest.main()
