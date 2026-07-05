"""minervini_sepa_daily · 月末 screen 煙霧測試。"""

from __future__ import annotations

import unittest

from minervini_sepa_daily import STRATEGY_ID, build_intent_batch, ScreenResult


class TestMinerviniSepaDaily(unittest.TestCase):
    def test_build_intent_batch_on_change(self) -> None:
        result = ScreenResult(
            session_date="2026-06-30",
            is_rebalance_day=True,
            month_end="2026-06-30",
            picks=["2330", "2317"],
            pick_names={"2330": "台積電", "2317": "鴻海"},
            prev_picks=["2330"],
            changed=True,
            dry_run=True,
        )
        batch = build_intent_batch(result, capital_ntd=100000.0)
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual(batch["strategy_id"], STRATEGY_ID)
        self.assertEqual(len(batch["intents"]), 2)
        self.assertTrue(batch["metadata"]["dry_run"])

    def test_no_intent_when_not_changed(self) -> None:
        result = ScreenResult(
            session_date="2026-06-30",
            is_rebalance_day=True,
            month_end="2026-06-30",
            picks=["2330"],
            pick_names={"2330": "台積電"},
            prev_picks=["2330"],
            changed=False,
            dry_run=True,
        )
        self.assertIsNone(build_intent_batch(result, capital_ntd=100000.0))


if __name__ == "__main__":
    unittest.main()
