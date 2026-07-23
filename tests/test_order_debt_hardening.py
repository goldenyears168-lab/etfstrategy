"""Tests for order-layer debt hardenings (P1/P3/P6/pytest/ops)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abc_v3_f1_intent_bridge import (
    intent_manual_submit_block_reason,
    quarantine_intent_file,
    write_intent_file,
)
from order.abc_v3_f1_lifecycle import (
    entry_counts_toward_cash,
    outer_status_from_lifecycle,
    poll_order_lifecycle,
)
from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker
from order.ops_snapshot import build_order_ops_snapshot, write_order_ops_snapshot


class TestLifecycleHardening(unittest.TestCase):
    def test_outer_status_maps_submitted_to_working(self) -> None:
        self.assertEqual(outer_status_from_lifecycle("submitted"), "working")
        self.assertEqual(outer_status_from_lifecycle("filled"), "filled")
        self.assertEqual(outer_status_from_lifecycle("ambiguous"), "ambiguous")

    def test_cash_only_on_fill(self) -> None:
        self.assertFalse(entry_counts_toward_cash({"lifecycle_status": "working", "status": "working"}))
        self.assertTrue(entry_counts_toward_cash({"lifecycle_status": "filled", "status": "filled"}))
        self.assertTrue(entry_counts_toward_cash({"lifecycle_status": "partial"}))

    def test_poll_ambiguous_when_no_row(self) -> None:
        with patch("order.abc_v3_f1_lifecycle.find_order_row", return_value=None):
            fields = poll_order_lifecycle(
                object(),
                client_intent_id="x",
                order_no="T1",
                fallback_ask=100.0,
                fallback_qty=10,
                max_attempts=1,
                interval_sec=0.0,
            )
        self.assertEqual(fields["lifecycle_status"], "ambiguous")
        self.assertEqual(fields["notional_twd"], 0.0)


class TestPytestNeverSpawnsWorker(unittest.TestCase):
    def test_host_needs_false_under_pytest(self) -> None:
        self.assertFalse(host_needs_fubon_venv())

    def test_run_worker_raises_under_pytest(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            run_fubon_order_worker({"kind": "c18acc_actions", "dry_run": False, "session_date": "2099-01-01"})
        self.assertIn("under_test_runner", str(ctx.exception))


class TestIntentSafety(unittest.TestCase):
    def test_block_failed_and_placeholder(self) -> None:
        self.assertIsNotNone(
            intent_manual_submit_block_reason({"metadata": {"submit_status": "failed"}})
        )
        self.assertIsNotNone(
            intent_manual_submit_block_reason({"metadata": {"resolve_qty_from_budget": True}})
        )
        self.assertIsNone(intent_manual_submit_block_reason({"metadata": {}, "intents": []}))

    def test_block_abc_order_removed(self) -> None:
        reason = intent_manual_submit_block_reason(
            {"strategy_id": "abc-v3-f1-pullback", "metadata": {}, "intents": []}
        )
        self.assertEqual(reason, "intent_submit_blocked:abc_order_removed")
        reason2 = intent_manual_submit_block_reason(
            {"strategy_id": "leading-dip", "metadata": {"abc_v3_f1_only": True}, "intents": []}
        )
        self.assertEqual(reason2, "intent_submit_blocked:abc_order_removed")

    def test_quarantine_moves_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            intents = Path(tmp) / "intents"
            intents.mkdir()
            with patch("abc_v3_f1_intent_bridge.INTENTS_DIR", intents):
                path = write_intent_file(
                    {
                        "schema_version": "order-intent-v1",
                        "strategy_id": "abc-v3-f1-pullback",
                        "as_of": "2026-07-13",
                        "intents": [{"symbol": "2330", "side": "buy", "quantity_shares": 1}],
                        "metadata": {},
                    },
                    session_date="2026-07-13",
                    poll_minute="12:00",
                    symbol="2330",
                )
                dest = quarantine_intent_file(path, reason="place_failed")
                self.assertIsNotNone(dest)
                assert dest is not None
                self.assertTrue(dest.is_file())
                self.assertFalse(path.exists())
                meta = json.loads(dest.read_text(encoding="utf-8"))["metadata"]
                self.assertEqual(meta["submit_status"], "failed")


class TestOpsSnapshot(unittest.TestCase):
    def test_write_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_order_ops_snapshot(out_dir=Path(tmp))
            self.assertTrue(path.is_file())
            snap = build_order_ops_snapshot()
            self.assertEqual(snap["schema"], "order-ops-snapshot-v1")
            self.assertIn("ledgers", snap)


if __name__ == "__main__":
    unittest.main()
