"""research_config · Research 層探索主題 registry 測試。"""

from __future__ import annotations

import unittest

from research_config import load_research_config, load_research_splits

# Phase A 收斂後仍 active 的主線（2026-07-09）
PHASE_A_ACTIVE_TOPICS = frozenset(
    {
        "c18acc-abc-dual-sleeve",
        "c18acc-extension-radar",
        "c18acc-snapshot-1300",
        "copytrade-rrg-audit",
        "finmind-low-ps-growth",
        "finmind-low-rebound-foreign-it-sync",
    }
)

PHASE_A_RETIRED_TOPIC_IDS = frozenset(
    {
        "breadth-impulse-validation",
        "external-strategy-compare",
        "factor-validation-s04",
        "tanish-momentum-breadth",
    }
)


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
        self.assertIn(
            "vcp-pivot-gate",
            vcp.graduated_strategies or vcp.graduation.strategy_ids if vcp.graduation else (),
        )

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

    def test_phase_a_active_topics(self) -> None:
        cfg = load_research_config()
        active = {t.topic_id for t in cfg.topics if t.status == "active"}
        self.assertEqual(active, PHASE_A_ACTIVE_TOPICS)

    def test_abc_graduated_from_triple_wma_topic(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("triple-wma-pullback")
        assert topic is not None
        self.assertEqual(topic.status, "graduated")
        self.assertEqual(topic.phase, "graduated")
        assert topic.graduation is not None
        self.assertEqual(topic.graduation.strategy_id, "abc-v3-f1-pullback")
        self.assertEqual(topic.graduation.gates.G6_adoption_report, "passed")

    def test_archived_lens_score_swap(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("rrg-lens-score-swap")
        assert topic is not None
        self.assertEqual(topic.status, "archived")
        self.assertEqual(topic.phase, "rejected")

    def test_c18acc_snapshot_1300_topic_registered(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("c18acc-snapshot-1300")
        assert topic is not None
        self.assertEqual(topic.status, "active")
        self.assertEqual(topic.phase, "hypothesis")
        self.assertEqual(topic.parent_strategy, "rrg-mono-swap-accel")
        self.assertEqual(topic.split_spec, "c18acc_snapshot_1300_2021")
        assert topic.sweep is not None
        self.assertEqual(topic.sweep.get("sample_start"), "2021-01-01")
        self.assertEqual(topic.sweep.get("snapshot_minute"), "13:00:00")
        variant_ids = {v["id"] for v in topic.sweep.get("variants") or []}
        self.assertIn("S0", variant_ids)
        self.assertIn("S2", variant_ids)
        splits = load_research_splits()
        split = splits.get("c18acc_snapshot_1300_2021")
        assert split is not None
        self.assertEqual(split.holdout_start, "2026-01-01")
        self.assertEqual(split.cost_model_rt_pct, 0.585)

    def test_extension_radar_graduation(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("c18acc-extension-radar")
        assert topic is not None
        self.assertEqual(topic.status, "active")
        assert topic.graduation is not None
        assert topic.graduation.gates is not None
        self.assertEqual(topic.graduation.gates.G3_regime_stratification, "partial")
        self.assertEqual(topic.graduation.gates.G5_frozen_spec, "passed")
        self.assertEqual(
            topic.graduation.champion_artifact,
            "reports/research/rrg/20260627_c18acc_phase3b_report.md",
        )

    def test_retired_topics_removed(self) -> None:
        cfg = load_research_config()
        ids = set(cfg.topic_ids())
        for retired in PHASE_A_RETIRED_TOPIC_IDS:
            self.assertNotIn(retired, ids)

    def test_lane_discovery_archived_not_deleted(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("rrg-lane-discovery-loop")
        assert topic is not None
        self.assertEqual(topic.status, "archived")

    def test_improving_lifecycle_archived_phase_c(self) -> None:
        cfg = load_research_config()
        topic = cfg.get("rrg-improving-lifecycle")
        assert topic is not None
        self.assertEqual(topic.status, "archived")
        self.assertEqual(topic.phase, "rejected")


if __name__ == "__main__":
    unittest.main()
