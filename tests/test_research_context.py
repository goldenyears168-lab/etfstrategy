"""research_context：精簡 JSON schema。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_context import build_research_context
from research_universe import DEFAULT_ETF_CODES
from stock_db import connect


class TestResearchContextSchema(unittest.TestCase):
    def test_slim_context_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            ctx = build_research_context(conn, DEFAULT_ETF_CODES)
            conn.close()

        for key in (
            "as_of_date",
            "tech_risk",
            "signal_layers",
            "cross_etf_consensus",
            "etf_signal_performance",
            "decisions",
            "pm_briefing",
            "position_exit_summary",
            "catalyst_events",
            "news_verify",
            "next_day_checklist",
            "appendix",
        ):
            self.assertIn(key, ctx)

        briefing = ctx["pm_briefing"]
        for bkey in (
            "top_observations",
            "consensus_expansion",
            "capital_concentration",
            "contradictions",
            "tomorrow_watch",
        ):
            self.assertIn(bkey, briefing)

        for removed in (
            "etf_holdings_changes",
            "holdings_meta",
            "scores",
            "pm_watchlist",
            "portfolio_weights",
            "universe",
            "data_policy",
        ):
            self.assertNotIn(removed, ctx)

        appendix = ctx["appendix"]
        self.assertIn("holdings_meta", appendix)
        self.assertIn("score_version", appendix)
        self.assertIn("data_policy", appendix)

    def test_signal_layers_leg_fields_slim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            ctx = build_research_context(conn, DEFAULT_ETF_CODES)
            conn.close()

        layers = ctx.get("signal_layers")
        if layers is None or not layers.get("stocks"):
            return
        leg = layers["stocks"][0]["legs"][0]
        for field in ("share_delta", "weight_delta_pp", "flow_ntd"):
            self.assertIn(field, leg)
        for removed in (
            "weight_pct_prev",
            "weight_pct_curr",
            "share_growth_pct",
            "weight_rank",
            "in_top5",
        ):
            self.assertNotIn(removed, leg)
        stock = layers["stocks"][0]
        self.assertNotIn("flow_ntd_total", stock)
        self.assertNotIn("share_growth_pct_max", stock)


if __name__ == "__main__":
    unittest.main()
