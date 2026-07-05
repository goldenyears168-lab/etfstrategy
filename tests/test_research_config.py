"""research_config · Research 層探索主題 registry 測試。"""

from __future__ import annotations

import unittest

from research_config import load_research_config, load_research_splits


class ResearchConfigTests(unittest.TestCase):
    def test_load_research_config(self) -> None:
        cfg = load_research_config()
        self.assertEqual(cfg.layer, "research")
        self.assertEqual(cfg.version, "research-v4")
        self.assertGreater(len(cfg.principles), 0)
        self.assertIsNotNone(cfg.graduation_gates)
        self.assertEqual(cfg.splits_ref, "config/research_splits.yaml")
        ids = cfg.topic_ids()
        self.assertIn("copytrade-hypothesis-matrix", ids)
        self.assertIn("chunge-funnel-sweep", ids)

    def test_load_research_splits(self) -> None:
        splits = load_research_splits()
        self.assertEqual(splits.version, "research-splits-v1")
        spec = splits.get("intraday_is_oos_70_30")
        assert spec is not None
        self.assertEqual(spec.method, "date_ratio")
        self.assertEqual(spec.ratio, 0.7)
        self.assertEqual(spec.min_oos_n, 80)

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
        self.assertEqual(topic.split_spec, "default_is_oos")
        self.assertGreater(len(topic.hypotheses), 0)
        self.assertIsNotNone(topic.sweep)

    def test_c18acc_slot_topic_registered(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("rrg-mono-swap-accel-slots")
        assert topic is not None
        self.assertEqual(topic.phase, "is_sweep")
        self.assertEqual(topic.parent_strategy, "rrg-mono-swap-accel")
        assert topic.sweep is not None
        self.assertEqual(topic.sweep.get("dimensions", {}).get("n_slots"), [3, 5, 7, 9])

    def test_intraday_topic_artifacts_and_extra_gates(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("intraday-1m-path-predictor")
        assert topic is not None
        self.assertEqual(topic.split_spec, "intraday_is_oos_70_30")
        assert topic.artifacts is not None
        self.assertIn("rejection_registry", topic.artifacts)
        assert topic.graduation is not None
        assert topic.graduation.extra_gates is not None
        self.assertEqual(topic.graduation.extra_gates.get("calibration"), "pending")
        assert topic.graduation.gates is not None
        self.assertEqual(topic.graduation.gates.G4_rejection_registry, "passed")
        self.assertEqual(topic.graduation.gates.G3_regime_stratification, "na")

    def test_orb_topic_artifacts_moved_out_of_yaml(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("1m-orb-tw-stock-select")
        assert topic is not None
        assert topic.artifacts is not None
        self.assertIn("catalog_sweep", topic.artifacts)
        self.assertIn("rejection_registry", topic.artifacts)

    def test_cz_intraday_topic_registered(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("cz-intraday-tw-replication")
        assert topic is not None
        self.assertEqual(topic.split_spec, "intraday_is_oos_70_30")
        self.assertIn("scripts/run_cz_track2_0050_orb.py", topic.run_scripts or [])
        assert topic.hypotheses is not None
        ids = {h["id"] for h in topic.hypotheses}
        self.assertIn("H-CZ-T2-1", ids)

    def test_extension_radar_extra_gates(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("c18acc-extension-radar")
        assert topic is not None
        assert topic.graduation is not None
        assert topic.graduation.extra_gates is not None
        self.assertEqual(
            topic.graduation.extra_gates.get("spike_subset_calibration"), "pending"
        )
        assert topic.graduation.gates is not None
        self.assertEqual(topic.graduation.gates.G3_regime_stratification, "na")

    def test_retired_topics_removed(self) -> None:
        cfg = load_research_config()
        ids = cfg.topic_ids()
        for retired in (
            "factor-validation-s04",
            "external-strategy-compare",
            "tanish-momentum-breadth",
            "breadth-impulse-validation",
        ):
            self.assertNotIn(retired, ids)


if __name__ == "__main__":
    unittest.main()
