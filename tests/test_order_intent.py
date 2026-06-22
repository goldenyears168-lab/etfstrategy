"""Tests for order.intent (no Fubon SDK required)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from order.intent import (
    LEGACY_SCHEMA_VERSIONS,
    OrderIntent,
    OrderIntentBatch,
    SCHEMA_VERSION,
    load_intent_batch,
    resolve_intents,
)


class TestOrderIntent(unittest.TestCase):
    def test_delta_intent_validates(self) -> None:
        item = OrderIntent(
            symbol="2330",
            side="buy",
            quantity_shares=1000,
            price="580",
        )
        item.validate()

    def test_target_resolves_against_holdings(self) -> None:
        batch = OrderIntentBatch(
            schema_version=SCHEMA_VERSION,
            strategy_id="rrg-mono-hold7",
            as_of="2026-06-21",
            intents=[
                OrderIntent(symbol="2330", target_shares=2000, price="580"),
                OrderIntent(symbol="2317", target_shares=1000, price="1000"),
            ],
        )
        resolved = resolve_intents(batch, {"2330": 1000, "2317": 1000})
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].symbol, "2330")
        self.assertEqual(resolved[0].side, "buy")
        self.assertEqual(resolved[0].quantity_shares, 1000)
        self.assertEqual(resolved[0].source, "target")

    def test_target_skip_when_already_at_target(self) -> None:
        batch = OrderIntentBatch(
            schema_version=SCHEMA_VERSION,
            strategy_id="x",
            as_of="2026-06-21",
            intents=[OrderIntent(symbol="2330", target_shares=1000, price="580")],
        )
        resolved = resolve_intents(batch, {"2330": 1000})
        self.assertEqual(resolved, [])

    def test_load_intent_batch_from_file(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "strategy_id": "test",
            "as_of": "2026-06-21",
            "intents": [
                {
                    "symbol": "0050",
                    "side": "sell",
                    "quantity_shares": 2000,
                    "price": "46.5",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intents.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            batch = load_intent_batch(path)
            self.assertEqual(batch.strategy_id, "test")
            self.assertEqual(len(batch.intents), 1)
            self.assertEqual(batch.intents[0].symbol, "0050")

    def test_load_legacy_execution_intent_schema(self) -> None:
        legacy = next(iter(LEGACY_SCHEMA_VERSIONS))
        payload = {
            "schema_version": legacy,
            "strategy_id": "legacy",
            "as_of": "2026-06-21",
            "intents": [
                {
                    "symbol": "2330",
                    "side": "buy",
                    "quantity_shares": 1000,
                    "price": "580",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            batch = load_intent_batch(path)
            self.assertEqual(batch.schema_version, legacy)

    def test_rejects_both_delta_and_target(self) -> None:
        item = OrderIntent(
            symbol="2330",
            side="buy",
            quantity_shares=1000,
            target_shares=2000,
            price="580",
        )
        with self.assertRaises(ValueError):
            item.validate()


if __name__ == "__main__":
    unittest.main()
