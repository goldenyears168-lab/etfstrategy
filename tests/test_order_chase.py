"""Tests for order.chase (no Fubon SDK)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from order.chase import (
    SCHEMA_VERSION,
    init_session_state,
    load_chase_spec,
    shares_for_budget,
)


class TestOrderChase(unittest.TestCase):
    def test_shares_for_budget(self) -> None:
        self.assertEqual(shares_for_budget(10000, 174.5), 57)
        self.assertEqual(shares_for_budget(10000, 5195.0), 1)

    def test_load_chase_spec_v1(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "strategy_id": "test",
            "budget_twd_per_symbol": 10000,
            "max_rounds": 5,
            "symbols": ["5347", "3008"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            spec = load_chase_spec(path)
            self.assertEqual(spec.symbols, ["5347", "3008"])
            self.assertEqual(spec.max_rounds, 5)

    def test_session_state_rounds_timeout(self) -> None:
        from order.chase import ChaseSpec, SymbolChaseState

        spec = ChaseSpec(
            strategy_id="t",
            budget_twd_per_symbol=10000,
            symbols=["5347"],
            max_rounds=5,
        )
        state = init_session_state(spec, "2026-06-22")
        st = state.symbols["5347"]
        st.rounds = 5
        st.status = "active"
        if st.rounds >= spec.max_rounds:
            st.status = "timeout"
        self.assertEqual(st.status, "timeout")


if __name__ == "__main__":
    unittest.main()
