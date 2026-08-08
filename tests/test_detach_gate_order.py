"""Unit tests for Detach Gate（台美脫鉤閘門）Order layer half-flatten."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from order.detach_gate_order import (
    half_client_intent_id,
    half_qty,
    load_ledger,
    mark_order_notified,
    mark_signal_notified,
    run_half_flatten,
    save_ledger,
    session_already_flattened,
    session_dry_run_done,
    session_live_attempted,
)


class TestHalfQty(unittest.TestCase):
    def test_floor_half(self) -> None:
        self.assertEqual(half_qty(0), 0)
        self.assertEqual(half_qty(1), 0)
        self.assertEqual(half_qty(2), 1)
        self.assertEqual(half_qty(3), 1)
        self.assertEqual(half_qty(100), 50)
        self.assertEqual(half_qty(101), 50)


class TestLedgerIdempotency(unittest.TestCase):
    def test_session_already_flattened(self) -> None:
        ledger = {"schema": "detach-gate-order-v1", "sessions": {}}
        self.assertFalse(session_already_flattened(ledger, "2026-07-15"))
        ledger["sessions"]["2026-07-15"] = {"half_flatten_done": True}
        self.assertTrue(session_already_flattened(ledger, "2026-07-15"))

    def test_session_dry_run_done(self) -> None:
        ledger: dict = {"schema": "detach-gate-order-v1", "sessions": {}}
        self.assertFalse(session_dry_run_done(ledger, "2026-07-15"))
        ledger["sessions"]["2026-07-15"] = {"half_flatten_dry_run": True}
        self.assertTrue(session_dry_run_done(ledger, "2026-07-15"))
        self.assertFalse(session_already_flattened(ledger, "2026-07-15"))

    def test_session_live_attempted(self) -> None:
        ledger: dict = {"schema": "detach-gate-order-v1", "sessions": {}}
        self.assertFalse(session_live_attempted(ledger, "2026-07-15"))
        ledger["sessions"]["2026-07-15"] = {"half_flatten_attempted": True}
        self.assertTrue(session_live_attempted(ledger, "2026-07-15"))
        ledger["sessions"]["2026-07-16"] = {"half_flatten_done": True}
        self.assertTrue(session_live_attempted(ledger, "2026-07-16"))

    def test_mark_signal_notified_once(self) -> None:
        ledger: dict = {"schema": "detach-gate-order-v1", "sessions": {}}
        self.assertTrue(
            mark_signal_notified(ledger, "2026-07-15", level="RED", poll="09:40")
        )
        self.assertFalse(
            mark_signal_notified(ledger, "2026-07-15", level="RED_LATCHED", poll="09:45")
        )
        sess = ledger["sessions"]["2026-07-15"]
        self.assertTrue(sess["signal_notified"])
        self.assertEqual(sess["signal_level"], "RED")

    def test_mark_order_notified_once(self) -> None:
        ledger: dict = {"schema": "detach-gate-order-v1", "sessions": {}}
        self.assertTrue(mark_order_notified(ledger, "2026-07-15"))
        self.assertFalse(mark_order_notified(ledger, "2026-07-15"))
        self.assertTrue(ledger["sessions"]["2026-07-15"]["order_notified"])

    def test_load_save_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "detach_gate_ledger.json"
            ledger = {"schema": "detach-gate-order-v1", "sessions": {"2026-07-15": {"x": 1}}}
            save_ledger(ledger, path=path)
            loaded = load_ledger(path=path)
            self.assertEqual(loaded["sessions"]["2026-07-15"]["x"], 1)


class TestClientIntentId(unittest.TestCase):
    def test_stable_and_matches_user_def_shape(self) -> None:
        cid = half_client_intent_id(
            session_date="2026-07-15", symbol="2330", poll_minute="09:40"
        )
        self.assertEqual(cid, "dg_20260715_0940_2330")


class TestDryRunPath(unittest.TestCase):
    @patch("order.detach_gate_order.live_submit_block_reason", return_value=None)
    def test_dry_run_does_not_mark_half_flatten_done(self, _block: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger({"schema": "detach-gate-order-v1", "sessions": {}}, path=ledger_path)

            with (
                patch("order.detach_gate_order.LEDGER_PATH", ledger_path),
                patch("order.fubon_subprocess.host_needs_fubon_venv", return_value=False),
                patch("order.fubon_session.connect_fubon", return_value=object()),
                patch(
                    "order.fubon_orders.holdings_shares_by_symbol",
                    return_value={"2330": 100, "2454": 1},
                ),
                patch("order.chase_runner.chase_bid_price", return_value=500.0),
                patch(
                    "order.fubon_orders.place_resolved_order",
                    side_effect=AssertionError("dry_run must not place_order"),
                ),
            ):
                out = run_half_flatten(
                    session_date="2026-07-15",
                    dry_run=True,
                    auto_submit=True,
                    order_enabled=True,
                )

            self.assertEqual(out["status"], "dry_run")
            self.assertTrue(out["dry_run"])
            by_sym = {r["symbol"]: r for r in out["orders"]}
            self.assertEqual(by_sym["2330"]["sell_qty"], 50)
            self.assertEqual(by_sym["2330"]["status"], "dry_run")
            self.assertEqual(by_sym["2330"]["bid_price"], 500.0)
            self.assertEqual(by_sym["2454"]["status"], "skipped")
            # user_def == client_intent_id (lifecycle match)
            resolved = by_sym["2330"]["resolved"]
            self.assertEqual(resolved["user_def"], by_sym["2330"]["client_intent_id"])
            self.assertEqual(resolved["side"], "sell")

            raw = json.loads(ledger_path.read_text(encoding="utf-8"))
            sess = raw["sessions"]["2026-07-15"]
            self.assertTrue(sess.get("half_flatten_dry_run"))
            self.assertFalse(sess.get("half_flatten_done", False))

    def test_dry_run_second_call_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger(
                {
                    "schema": "detach-gate-order-v1",
                    "sessions": {"2026-07-15": {"half_flatten_dry_run": True}},
                },
                path=ledger_path,
            )
            with patch("order.detach_gate_order.LEDGER_PATH", ledger_path):
                out = run_half_flatten(
                    session_date="2026-07-15",
                    dry_run=True,
                    order_enabled=True,
                )
            self.assertEqual(out["status"], "already_dry_run")

    def test_already_done_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger(
                {
                    "schema": "detach-gate-order-v1",
                    "sessions": {"2026-07-15": {"half_flatten_done": True}},
                },
                path=ledger_path,
            )
            with patch("order.detach_gate_order.LEDGER_PATH", ledger_path):
                out = run_half_flatten(
                    session_date="2026-07-15",
                    dry_run=False,
                    order_enabled=True,
                )
            self.assertEqual(out["status"], "already_done")

    def test_live_reject_latches_attempted_no_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger({"schema": "detach-gate-order-v1", "sessions": {}}, path=ledger_path)

            with (
                patch("order.detach_gate_order.live_submit_block_reason", return_value=None),
                patch("order.detach_gate_order.assert_live_submit_allowed", return_value=None),
                patch("order.detach_gate_order.LEDGER_PATH", ledger_path),
                patch("order.fubon_subprocess.host_needs_fubon_venv", return_value=False),
                patch("order.fubon_session.connect_fubon", return_value=object()),
                patch(
                    "order.fubon_orders.holdings_shares_by_symbol",
                    return_value={"2330": 100},
                ),
                patch("order.chase_runner.chase_bid_price", return_value=500.0),
                patch(
                    "order.oms_lifecycle.reconcile_before_submit",
                    return_value=None,
                ),
                patch(
                    "order.fubon_orders.place_resolved_order",
                    return_value={"is_success": False, "message": "集保庫存不足"},
                ) as place,
            ):
                out1 = run_half_flatten(
                    session_date="2026-07-15",
                    dry_run=False,
                    auto_submit=True,
                    order_enabled=True,
                )
                out2 = run_half_flatten(
                    session_date="2026-07-15",
                    dry_run=False,
                    auto_submit=True,
                    order_enabled=True,
                )

            self.assertEqual(out1["status"], "no_orders")
            self.assertEqual(out2["status"], "already_attempted")
            self.assertEqual(place.call_count, 1)

    def test_broker_exception_on_one_symbol_does_not_crash_batch_or_lose_others(self) -> None:
        """2026-08-08 code review 修正驗證：其中一檔的 place_resolved_order 拋例外時，
        不能讓整個 run_half_flatten() 崩潰、也不能讓其他檔的處理跟著消失——每檔獨立
        記錄，例外的那檔標成 exception，其餘照常繼續。"""
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger({"schema": "detach-gate-order-v1", "sessions": {}}, path=ledger_path)

            def place_side_effect(session, resolved):
                if resolved.symbol == "2330":
                    raise RuntimeError("broker connection dropped mid-order")
                return {"is_success": True, "order_no": "O123"}

            with (
                patch("order.detach_gate_order.live_submit_block_reason", return_value=None),
                patch("order.detach_gate_order.assert_live_submit_allowed", return_value=None),
                patch("order.detach_gate_order.LEDGER_PATH", ledger_path),
                patch("order.fubon_subprocess.host_needs_fubon_venv", return_value=False),
                patch("order.fubon_session.connect_fubon", return_value=object()),
                patch(
                    "order.fubon_orders.holdings_shares_by_symbol",
                    return_value={"2330": 100, "2454": 100},
                ),
                patch("order.chase_runner.chase_bid_price", return_value=500.0),
                patch(
                    "order.oms_lifecycle.reconcile_before_submit",
                    return_value=None,
                ),
                patch(
                    "order.fubon_orders.place_resolved_order",
                    side_effect=place_side_effect,
                ),
                patch(
                    "order.oms_lifecycle.poll_order_lifecycle",
                    return_value={"lifecycle_status": "filled"},
                ),
                patch(
                    "order.oms_lifecycle.outer_status_from_lifecycle",
                    return_value="filled",
                ),
            ):
                out = run_half_flatten(
                    session_date="2026-07-16",
                    dry_run=False,
                    auto_submit=True,
                    order_enabled=True,
                )

            # both symbols got a row -- the exception on 2330 didn't wipe out 2454
            by_symbol = {r["symbol"]: r for r in out["orders"]}
            self.assertEqual(set(by_symbol), {"2330", "2454"})
            self.assertEqual(by_symbol["2330"]["status"], "exception")
            self.assertIn("broker connection dropped", by_symbol["2330"]["error"])
            self.assertEqual(by_symbol["2454"]["status"], "filled")
            # batch-level status still reflects a real fill happened
            self.assertEqual(out["status"], "submitted")

    def test_attempted_latched_before_any_order_placed_blocks_retry_even_on_crash(self) -> None:
        """attempted 旗標必須在送出任何一張賣單「之前」就存檔——就算這唯一一檔的下單
        整個拋例外，下一次呼叫也必須被 already_attempted 擋下，不能重新讀縮水後的
        持倉再賣一次（半倉停損被觸發兩次的核心風險）。"""
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger({"schema": "detach-gate-order-v1", "sessions": {}}, path=ledger_path)

            with (
                patch("order.detach_gate_order.live_submit_block_reason", return_value=None),
                patch("order.detach_gate_order.assert_live_submit_allowed", return_value=None),
                patch("order.detach_gate_order.LEDGER_PATH", ledger_path),
                patch("order.fubon_subprocess.host_needs_fubon_venv", return_value=False),
                patch("order.fubon_session.connect_fubon", return_value=object()),
                patch(
                    "order.fubon_orders.holdings_shares_by_symbol",
                    return_value={"2330": 100},
                ),
                patch("order.chase_runner.chase_bid_price", return_value=500.0),
                patch(
                    "order.oms_lifecycle.reconcile_before_submit",
                    return_value=None,
                ),
                patch(
                    "order.fubon_orders.place_resolved_order",
                    side_effect=RuntimeError("broker down"),
                ),
            ):
                out1 = run_half_flatten(
                    session_date="2026-07-17",
                    dry_run=False,
                    auto_submit=True,
                    order_enabled=True,
                )
                out2 = run_half_flatten(
                    session_date="2026-07-17",
                    dry_run=False,
                    auto_submit=True,
                    order_enabled=True,
                )

            self.assertEqual(out1["orders"][0]["status"], "exception")
            self.assertEqual(out2["status"], "already_attempted")  # not re-run
            raw = json.loads(ledger_path.read_text(encoding="utf-8"))
            sess = raw["sessions"]["2026-07-17"]
            self.assertTrue(sess.get("half_flatten_attempted"))
            self.assertFalse(sess.get("half_flatten_done", False))

    def test_order_disabled(self) -> None:
        out = run_half_flatten(
            session_date="2026-07-15",
            dry_run=True,
            order_enabled=False,
        )
        self.assertEqual(out["status"], "disabled")


class TestOrderMasterSwitch(unittest.TestCase):
    """ORDER_MASTER_ENABLED gates env-derived auto_submit; place_resolved_order
    also refuses when MASTER=0 (last-line choke · 2026-08-04). Explicit
    dry_run=/auto_submit= still affect preview/status, but cannot reach broker.
    """

    def _run_with_env(self, env: dict[str, str]) -> dict:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            save_ledger({"schema": "detach-gate-order-v1", "sessions": {}}, path=ledger_path)
            with (
                patch.dict("os.environ", env, clear=False),
                patch("order.detach_gate_order.LEDGER_PATH", ledger_path),
                patch("order.fubon_subprocess.host_needs_fubon_venv", return_value=False),
                patch("order.fubon_session.connect_fubon", return_value=object()),
                patch(
                    "order.fubon_orders.holdings_shares_by_symbol",
                    return_value={"2330": 100},
                ),
                patch("order.chase_runner.chase_bid_price", return_value=500.0),
                patch(
                    "order.fubon_orders.place_resolved_order",
                    side_effect=AssertionError("must not place_order when master switch off"),
                ),
            ):
                if "ORDER_MASTER_ENABLED" not in env:
                    os.environ.pop("ORDER_MASTER_ENABLED", None)
                return run_half_flatten(session_date="2026-07-15", force=True)

    def test_master_switch_off_forces_auto_submit_false(self) -> None:
        out = self._run_with_env(
            {
                "ORDER_DETACH_GATE_ORDER_ENABLED": "1",
                "ORDER_DETACH_GATE_AUTO_SUBMIT": "1",
                "ORDER_DETACH_GATE_DRY_RUN": "1",
            }
        )
        self.assertFalse(out["auto_submit"])

    def test_master_switch_on_respects_individual_flag(self) -> None:
        out = self._run_with_env(
            {
                "ORDER_DETACH_GATE_ORDER_ENABLED": "1",
                "ORDER_DETACH_GATE_AUTO_SUBMIT": "1",
                "ORDER_DETACH_GATE_DRY_RUN": "1",
                "ORDER_MASTER_ENABLED": "1",
            }
        )
        self.assertTrue(out["auto_submit"])


if __name__ == "__main__":
    unittest.main()
