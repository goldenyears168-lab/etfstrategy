"""etf_flow_hypothesis：分 ETF 策略假設檢定。"""

from __future__ import annotations

import unittest

from research.archive.etf_flow_hypothesis import (
    HYPOTHESES,
    _macro_dates,
    test_hypothesis,
)
from research.archive.etf_flow_factor_screen import FeatureRow


class EtfFlowHypothesisTests(unittest.TestCase):
    def test_macro_dates_tx_gap_neg(self) -> None:
        tech = {
            "2026-06-10": {"tx_gap_pct": -1.5},
            "2026-06-11": {"tx_gap_pct": 2.0},
        }
        dates = _macro_dates(tech, "tx_gap_neg")
        self.assertEqual(dates, {"2026-06-10"})

    def test_hypothesis_supported_higher(self) -> None:
        spec = next(h for h in HYPOTHESES if h.id == "H1_rs14")
        ev = [FeatureRow("2026-06-10", f"s{i}", "add", {"rs_univ14": 8.0 + i * 0.1}) for i in range(12)]
        ctrl = [FeatureRow("2026-06-10", f"c{i}", "control", {"rs_univ14": -1.0 + i * 0.05}) for i in range(25)]
        r = test_hypothesis(
            etf_code="00981A",
            spec=spec,
            event_rows=ev,
            ctrl_rows=ctrl,
            tech={},
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.verdict, "SUPPORTED")
        self.assertIsNotNone(r.p_value)
        assert r.p_value is not None
        self.assertLess(r.p_value, 0.05)

    def test_hypothesis_insufficient_sample(self) -> None:
        spec = next(h for h in HYPOTHESES if h.id == "H1_rs14")
        ev = [FeatureRow("2026-06-10", "2330", "add", {"rs_univ14": 10.0})]
        ctrl = [FeatureRow("2026-06-10", "2317", "control", {"rs_univ14": 1.0})] * 5
        r = test_hypothesis(
            etf_code="00981A",
            spec=spec,
            event_rows=ev,
            ctrl_rows=ctrl,
            tech={},
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.verdict, "INSUFFICIENT")


if __name__ == "__main__":
    unittest.main()
