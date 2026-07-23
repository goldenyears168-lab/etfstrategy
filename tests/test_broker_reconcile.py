"""Broker reconcile report includes slots + holdings."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from order import broker_reconcile as br


class TestBrokerReconcile(unittest.TestCase):
    def test_format_includes_holdings_and_slots(self) -> None:
        report = {
            "as_of": "2026-07-13T12:00:00+08:00",
            "broker_holdings": {"2377": 134, "6505": 1268},
            "c18_slots": [
                {
                    "slot": 0,
                    "stock_id": "2377",
                    "stock_name": "微星",
                    "entry_date": "2026-07-13",
                    "entry_px": 148.5,
                }
            ],
            "local_abc_symbols": ["6505"],
            "local_c18_symbols": ["2377", "2454"],
            "only_in_broker": [],
            "only_in_local_ledger": ["2454"],
            "only_in_slots_not_broker": [],
            "in_broker_not_in_slots": ["6505"],
            "ok": False,
            "notes": ["test"],
        }
        text = br.format_reconcile_plain(report)
        self.assertIn("2377 · 134 股", text)
        self.assertIn("微星", text)
        self.assertIn("6505", text)
        self.assertIn("只在 ledger", text)

    def test_build_uses_slots_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "order").mkdir(parents=True)
            (root / "data").mkdir(exist_ok=True)
            slots = {
                "slots": [
                    {
                        "slot": 0,
                        "stock_id": "2377",
                        "stock_name": "微星",
                        "entry_date": "2026-07-13",
                        "entry_px": 148.5,
                    }
                ]
            }
            (root / "data" / "rrg_c18acc_slots.json").write_text(
                json.dumps(slots), encoding="utf-8"
            )
            (root / "data" / "order" / "abc_v3_f1_ledger.json").write_text(
                json.dumps({"entries": []}), encoding="utf-8"
            )
            (root / "data" / "order" / "c18acc_order_ledger.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "symbol": "2377",
                                "side": "buy",
                                "lifecycle_status": "filled",
                                "filled_qty": 134,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            class _S:
                pass

            with patch.object(br, "PROJECT_ROOT", root):
                with patch(
                    "order.fubon_orders.holdings_shares_by_symbol",
                    return_value={"2377": 134, "6505": 1268},
                ):
                    report = br.build_reconcile_report(_S())
            self.assertEqual(report["broker_holdings"]["2377"], 134)
            self.assertEqual(report["c18_slot_symbols"], ["2377"])
            self.assertIn("6505", report["in_broker_not_in_slots"])
            self.assertIn("6505", report["only_in_broker"])


if __name__ == "__main__":
    unittest.main()
