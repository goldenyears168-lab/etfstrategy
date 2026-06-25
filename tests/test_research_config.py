"""research_config · Research 層探索主題 registry 測試。"""

from __future__ import annotations

import unittest

from research_config import GraduationGates, load_research_config


class ResearchConfigTests(unittest.TestCase):
    def test_load_research_config(self) -> None:
        cfg = load_research_config()
        self.assertEqual(cfg.layer, "research")
        self.assertEqual(cfg.version, "research-v3")
        self.assertGreater(len(cfg.principles), 0)
        self.assertIsNotNone(cfg.graduation_gates)
        ids = cfg.topic_ids()
        self.assertIn("copytrade-hypothesis-matrix", ids)
        self.assertIn("chunge-funnel-sweep", ids)

    def test_graduation_links(self) -> None:
        cfg = load_research_config()
        copytrade = cfg.get("copytrade-hypothesis-matrix")
        assert copytrade is not None
        self.assertEqual(copytrade.status, "graduated")
        self.assertEqual(copytrade.graduated_strategy, "00981a-l1h9")
        assert copytrade.graduation is not None
        self.assertEqual(copytrade.graduation.strategy_id, "00981a-l1h9")
        assert copytrade.graduation.gates is not None
        self.assertEqual(copytrade.graduation.gates.G5_frozen_spec, "passed")

        vcp = cfg.get("chunge-funnel-sweep")
        assert vcp is not None
        self.assertIn("vcp-pivot-gate", vcp.graduated_strategies or vcp.graduation.strategy_ids if vcp.graduation else ())

    def test_graduated_c18acc_gates(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("rrg-mono-score-swap-c")
        assert topic is not None
        self.assertEqual(topic.status, "graduated")
        self.assertEqual(topic.phase, "graduated")
        assert topic.graduation is not None
        gates = topic.graduation.gates
        assert gates is not None
        self.assertEqual(gates.G2_oos_holdout, "passed")
        self.assertEqual(gates.G3_regime_stratification, "passed")

    def test_active_topic_has_phase(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("rrg-lens-score-swap")
        assert topic is not None
        self.assertEqual(topic.phase, "is_sweep")
        self.assertGreater(len(topic.hypotheses), 0)
        self.assertIsNotNone(topic.sweep)

    def test_factor_validation_includes_s04_compare_scripts(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("factor-validation-s04")
        assert topic is not None
        scripts = topic.run_scripts
        self.assertIn("scripts/run_s04_freq_compare.py", scripts)
        self.assertIn("scripts/run_s04_monthly_tri_compare.py", scripts)


if __name__ == "__main__":
    unittest.main()
