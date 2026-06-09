"""briefing_builder：PM Memo 策展與矛盾預算。"""

from __future__ import annotations

import unittest

from briefing_builder import (
    build_contradictions,
    build_decision_summary,
    build_pm_briefing,
    build_top_observations,
)


def _fixture_ctx_2454() -> dict:
    """聯發科：多檔 ETF 加碼但規則回避（乖離過大）。"""
    return {
        "cross_etf_consensus": [
            {
                "stock_id": "2454",
                "stock_name": "聯發科",
                "etf_add_count": 2,
                "etf_add_list": ["00981A", "009816"],
                "flow_ntd": 765_400_000.0,
            },
            {
                "stock_id": "2368",
                "stock_name": "金像電",
                "etf_add_count": 2,
                "etf_add_list": ["00981A", "009816"],
                "flow_ntd": 244_590_000.0,
            },
        ],
        "signal_layers": {
            "stocks": [
                {
                    "stock_id": "2454",
                    "stock_name": "聯發科",
                    "net_side": "add",
                    "l2_consensus_level": "STRONG",
                    "l2_consensus_score": 3.595,
                    "l4_conviction_level": "HIGH",
                    "l4_conviction_score": 0.85,
                    "l5_position_intent": "MAINTAIN_CORE",
                    "legs": [
                        {"flow_ntd": 610_600_000.0},
                        {"flow_ntd": 154_800_000.0},
                    ],
                },
                {
                    "stock_id": "1303",
                    "stock_name": "南亞",
                    "net_side": "add",
                    "l2_consensus_level": "FALSE",
                    "l4_conviction_level": "HIGH",
                    "l4_conviction_score": 2.5,
                    "l5_position_intent": "BUILD_THEMATIC",
                    "legs": [{"flow_ntd": 115_890_500.0}],
                },
            ]
        },
        "decisions": [
            {
                "stock_id": "2454",
                "stock_name": "聯發科",
                "watchlist": "不列入",
                "pm_bucket": "回避",
                "portfolio_weight_pct": 0.0,
                "entry_signal": "乖離過大",
                "chip_tag": "法人中性",
                "total": 52.2,
                "consensus_trend_label": "擴張",
                "consensus_etf_add_latest": 2,
            },
            {
                "stock_id": "1303",
                "stock_name": "南亞",
                "watchlist": "候選",
                "pm_bucket": "觀察",
                "entry_signal": "拉回",
                "chip_tag": "外資、投信同步買超",
                "consensus_trend_label": "擴張",
                "consensus_etf_add_latest": 2,
            },
        ],
        "next_day_checklist": [
            {
                "section": "不宜追價",
                "text": "2454 聯發科 · 乖離過大 · 分 52.2 · 法人中性",
            },
            {
                "section": "人工風控",
                "text": "假共識 1303 南亞（L2=FALSE）→ 勿當聰明錢",
            },
            {
                "section": "列入觀察",
                "text": "2368 金像電 · 觀望 · 分 55.7 · 法人中性",
            },
        ],
    }


class TestBriefingBuilder(unittest.TestCase):
    def test_contradiction_2454_merged(self) -> None:
        ctx = _fixture_ctx_2454()
        rows = build_contradictions(ctx)
        mtk = next(r for r in rows if r["stock_id"] == "2454")
        self.assertIn("ETF_ADD_VS_AVOID", mtk["reason_codes"])
        self.assertIn("ETF_ADD_VS_OVEREXTENDED", mtk["reason_codes"])
        self.assertIn("乖離過大", mtk["rule_side"])
        self.assertIn("2檔加碼", mtk["etf_side"])
        self.assertEqual(len([r for r in rows if r["stock_id"] == "2454"]), 1)

    def test_false_consensus_1303(self) -> None:
        ctx = _fixture_ctx_2454()
        rows = build_contradictions(ctx)
        row = next(r for r in rows if r["stock_id"] == "1303")
        self.assertIn("ETF_ADD_VS_FALSE_CONSENSUS", row["reason_codes"])

    def test_capital_concentration_order(self) -> None:
        briefing = build_pm_briefing(_fixture_ctx_2454())
        cap = briefing["capital_concentration"]
        self.assertGreaterEqual(len(cap), 1)
        self.assertEqual(cap[0]["stock_id"], "2454")

    def test_pm_briefing_keys(self) -> None:
        briefing = build_pm_briefing(_fixture_ctx_2454())
        for key in (
            "top_observations",
            "consensus_expansion",
            "capital_concentration",
            "contradictions",
            "tomorrow_watch",
        ):
            self.assertIn(key, briefing)

    def test_top_observations_includes_high_flow(self) -> None:
        obs = build_top_observations(_fixture_ctx_2454())
        ids = [o["stock_id"] for o in obs]
        self.assertIn("2454", ids)

    def test_decision_summary_omits_features(self) -> None:
        ctx = _fixture_ctx_2454()
        decisions = [
            *ctx["decisions"],
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "watchlist": "候選",
                "pm_bucket": "觀察",
                "entry_signal": "觀望",
                "portfolio_weight_pct": 20.0,
                "suggested_ntd": 20_000.0,
                "chip_tag": "法人中性",
                "rs_20d": -5.0,
            },
        ]
        summary = build_decision_summary(decisions)
        row = summary["2454"]
        self.assertEqual(row["pm_bucket"], "回避")
        self.assertEqual(row["entry_signal"], "乖離過大")
        self.assertNotIn("rs_20d", row)
        self.assertNotIn("eps_qoq_pct", row)
        self.assertNotIn("suggested_ntd", row)
        self.assertEqual(row["reason"], "法人中性")
        self.assertEqual(summary["2330"]["portfolio_weight_pct"], 20.0)
        self.assertNotIn("suggested_ntd", summary["2330"])


if __name__ == "__main__":
    unittest.main()
