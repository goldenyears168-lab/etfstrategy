"""signal_engine / comment_engine 單元測試（無需 DB）。"""

from __future__ import annotations

import unittest

from comment_engine import compose_intent_compact, compose_intent_tags
from position_intent import apply_l2_consensus
from signal_engine import (
    ChangeLeg,
    StockSignal,
    _build_theme_flow_matrix,
    _infer_portfolio_role,
    zscore_series,
)


class TestZScore(unittest.TestCase):
    def test_zscore_centered(self) -> None:
        zs = zscore_series([1.0, 2.0, 3.0])
        self.assertAlmostEqual(sum(zs), 0.0, places=6)


class TestPortfolioRole(unittest.TestCase):
    def test_top5_is_core(self) -> None:
        role = _infer_portfolio_role(
            in_top5=True,
            in_top_decile=True,
            action_primary="加码",
            weight_delta_pp_max=0.16,
            share_growth_max=5.0,
            has_new=False,
        )
        self.assertEqual(role, "CORE")

    def test_new_small_weight_is_thematic(self) -> None:
        role = _infer_portfolio_role(
            in_top5=False,
            in_top_decile=False,
            action_primary="新进",
            weight_delta_pp_max=0.5,
            share_growth_max=None,
            has_new=True,
        )
        self.assertEqual(role, "THEMATIC")


class TestRotationMatrix(unittest.TestCase):
    def test_memory_to_ai_pair(self) -> None:
        out_sig = StockSignal(
            stock_id="3008",
            stock_name="大立光",
            theme="MOBILE_OPTICS",
            net_side="reduce",
        )
        out_sig.legs.append(
            ChangeLeg(
                stock_id="3008",
                stock_name="大立光",
                etf_code="00981A",
                action="减码",
                share_delta=-1000,
                weight_pct_prev=1.0,
                weight_pct_curr=0.5,
                weight_delta_pp=-0.5,
                share_growth_pct=-10,
                flow_ntd=-1e8,
                weight_rank=20,
                in_top5=False,
                in_top_decile=False,
                theme="MOBILE_OPTICS",
            )
        )
        in_sig = StockSignal(
            stock_id="2376",
            stock_name="技嘉",
            theme="AI_SERVER",
            net_side="add",
        )
        in_sig.legs.append(
            ChangeLeg(
                stock_id="2376",
                stock_name="技嘉",
                etf_code="00981A",
                action="加码",
                share_delta=5000,
                weight_pct_prev=0.4,
                weight_pct_curr=0.8,
                weight_delta_pp=0.4,
                share_growth_pct=100,
                flow_ntd=2e8,
                weight_rank=15,
                in_top5=False,
                in_top_decile=True,
                theme="AI_SERVER",
            )
        )
        pairs = _build_theme_flow_matrix([out_sig, in_sig])
        self.assertTrue(pairs)
        self.assertNotEqual(pairs[0][0], pairs[0][1])


class TestL2Consensus(unittest.TestCase):
    def test_strong_consensus_two_etfs(self) -> None:
        sig = StockSignal(stock_id="1303", stock_name="南亞", theme="CYCLE_CHEM", net_side="add")
        for etf, flow, wt in (("00403A", 2e8, 0.12), ("009816", 8e6, 0.01)):
            sig.legs.append(
                ChangeLeg(
                    stock_id="1303",
                    stock_name="南亞",
                    etf_code=etf,
                    action="加码",
                    share_delta=1000,
                    weight_pct_prev=1.0,
                    weight_pct_curr=2.0,
                    weight_delta_pp=wt,
                    share_growth_pct=50.0,
                    flow_ntd=flow,
                    weight_rank=10,
                    in_top5=False,
                    in_top_decile=False,
                    theme="CYCLE_CHEM",
                )
            )
        apply_l2_consensus([sig])
        self.assertIn(sig.consensus_level, ("STRONG", "WEAK", "FALSE"))


class TestComment(unittest.TestCase):
    def test_compose_intent_compact_single_line(self) -> None:
        sig = StockSignal(
            stock_id="2330",
            stock_name="台積電",
            theme="AI_SEMIS",
            portfolio_role="CORE",
            conviction_level="MEDIUM",
            net_side="add",
            weight_rank_best=1,
            weight_delta_pp_max=0.35,
            in_top5_any=True,
            position_intent="MAINTAIN_CORE",
            consensus_level="SINGLE",
        )
        text = compose_intent_compact(sig)
        self.assertNotIn("\n", text)
        self.assertIn("台積電", text)
        self.assertIn("MEDIUM", text)
        self.assertIn("CORE", text)
        self.assertIn("維持核心配置", text)
        self.assertNotIn("[CORE]", text)

    def test_compose_intent_tags_debug(self) -> None:
        sig = StockSignal(
            stock_id="2330",
            stock_name="台積電",
            theme="AI_SEMIS",
            portfolio_role="CORE",
            conviction_level="MEDIUM",
            position_intent="MAINTAIN_CORE",
            consensus_level="SINGLE",
        )
        tags = compose_intent_tags(sig)
        self.assertIn("[CORE]", tags)
        self.assertIn("[MAINTAIN_CORE]", tags)
        self.assertIn("[LONE_MANAGER]", tags)


if __name__ == "__main__":
    unittest.main()
