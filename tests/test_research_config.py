"""research_config · Research 層探索主題 registry 測試。"""

from __future__ import annotations

import unittest

from research_config import load_research_config


class ResearchConfigTests(unittest.TestCase):
    def test_load_research_config(self) -> None:
        cfg = load_research_config()
        self.assertEqual(cfg.layer, "research")
        self.assertGreater(len(cfg.principles), 0)
        ids = cfg.topic_ids()
        self.assertIn("copytrade-hypothesis-matrix", ids)
        self.assertIn("chunge-funnel-sweep", ids)

    def test_graduation_links(self) -> None:
        cfg = load_research_config()
        copytrade = cfg.get("copytrade-hypothesis-matrix")
        assert copytrade is not None
        self.assertEqual(copytrade.graduated_strategy, "00981a-l1h9")
        vcp = cfg.get("chunge-funnel-sweep")
        assert vcp is not None
        self.assertIn("vcp-pivot-gate", vcp.graduated_strategies)


if __name__ == "__main__":
    unittest.main()
