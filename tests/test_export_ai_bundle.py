"""export_ai_bundle：上下文與寫檔。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from export_ai_bundle import export_ai_bundle
from research_context import (
    build_ai_bundle_markdown,
    build_llm_payload,
    build_llm_prompts,
    build_research_context,
    print_llm_prompts_cli,
    write_evening_prompt_file,
)
from research_universe import DEFAULT_ETF_CODES
from stock_db import connect


class TestExportAiBundle(unittest.TestCase):
    def test_build_context_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            ctx = build_research_context(conn, DEFAULT_ETF_CODES)
            conn.close()
        for key in (
            "as_of_date",
            "tech_risk",
            "decisions",
            "pm_briefing",
            "catalyst_events",
            "cross_etf_consensus",
            "etf_signal_performance",
            "signal_layers",
            "news_verify",
            "next_day_checklist",
            "appendix",
        ):
            self.assertIn(key, ctx)
        self.assertIsInstance(ctx["decisions"], list)
        self.assertIsInstance(ctx["next_day_checklist"], list)
        self.assertIn("holdings_meta", ctx["appendix"])

    def test_export_writes_json_and_prompt_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = connect(root / "t.db")
            import export_ai_bundle as eab
            import research_context as rc

            old_rc = rc.REPORTS_DIR
            old_eab = eab.REPORTS_DIR
            reports = root / "reports"
            rc.REPORTS_DIR = reports
            eab.REPORTS_DIR = reports
            try:
                result = export_ai_bundle(
                    conn, DEFAULT_ETF_CODES, quiet=True
                )
                self.assertIsNotNone(result.json_path)
                assert result.json_path is not None
                self.assertTrue(result.json_path.exists())
                self.assertIsNotNone(result.prompt_evening_full_path)
                assert result.prompt_evening_full_path is not None
                self.assertTrue(result.prompt_evening_full_path.exists())
                prompt = result.prompt_evening_full_path.read_text(
                    encoding="utf-8"
                )
                self.assertIn("[SYSTEM]", prompt)
                self.assertIn("RESEARCH_JSON", prompt)
                data = json.loads(result.json_path.read_text(encoding="utf-8"))
                self.assertIn("data_policy", data["appendix"])
                names = {p.name for p in reports.iterdir()}
                self.assertFalse(any(n.endswith("_ai_bundle.md") for n in names))
                self.assertEqual(
                    sum(1 for n in names if n.endswith(".txt")), 1
                )
                self.assertEqual(
                    sum(1 for n in names if n.endswith(".json")), 1
                )
            finally:
                rc.REPORTS_DIR = old_rc
                eab.REPORTS_DIR = old_eab
            conn.close()

    def test_write_evening_prompt_file(self) -> None:
        prompts = build_llm_prompts({"as_of_date": "2026-06-04", "decisions": []})
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evening_prompt_file(
                prompts, reports_dir=Path(tmp) / "reports"
            )
            self.assertTrue(path.name.endswith("_prompt_evening_full.txt"))
            self.assertIn("[SYSTEM]", path.read_text(encoding="utf-8"))

    def test_bundle_markdown_helper_still_works(self) -> None:
        md = build_ai_bundle_markdown({"as_of_date": "2026-06-04", "decisions": []})
        self.assertIn("p4-v2", md)
        self.assertIn("RESEARCH_JSON", md)

    def test_llm_prompts_contain_news_verify_section(self) -> None:
        ctx = {
            "as_of_date": "2026-06-04",
            "decisions": [],
            "news_verify": [{"stock_id": "2330", "search_query": "台積電 新聞"}],
        }
        prompts = build_llm_prompts(ctx)
        self.assertIn("news_verify", prompts.user_evening)
        self.assertIn("pm_briefing", prompts.user_evening)
        self.assertIn("## 3. Why", prompts.user_evening)
        self.assertIn("## 4. Contradiction", prompts.user_evening)
        self.assertIn("聯網", prompts.system)

    def test_llm_prompts_contain_system_and_json_block(self) -> None:
        ctx = {"as_of_date": "2026-06-04", "decisions": []}
        prompts = build_llm_prompts(ctx)
        self.assertIn("台股 ETF", prompts.system)
        self.assertIn("RESEARCH_JSON", prompts.user_evening)
        self.assertIn("[SYSTEM]", prompts.evening_full)

    def test_llm_payload_slim_not_full_signal_layers(self) -> None:
        ctx = {
            "as_of_date": "2026-06-04",
            "decisions": [
                {
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "watchlist": "候選",
                    "pm_bucket": "觀察",
                    "entry_signal": "觀望",
                    "portfolio_weight_pct": 20.0,
                    "chip_tag": "法人中性",
                    "rs_20d": -5.0,
                }
            ],
            "signal_layers": {"stocks": [{"stock_id": "2330"}]},
            "pm_briefing": {"contradictions": []},
            "etf_signal_performance": [{"etf_code": "00981A", "sample_n": 0}],
        }
        payload = build_llm_payload(ctx)
        self.assertIn("pm_briefing", payload)
        self.assertIn("decision_summary", payload)
        self.assertNotIn("decisions", payload)
        self.assertNotIn("signal_layers", payload)
        self.assertNotIn("next_day_checklist", payload)
        self.assertNotIn("etf_signal_performance", payload)
        self.assertNotIn("rs_20d", payload["decision_summary"]["2330"])

    def test_print_prompts_cli_smoke(self) -> None:
        prompts = build_llm_prompts({"as_of_date": "2026-06-04", "decisions": []})
        print_llm_prompts_cli(prompts)


if __name__ == "__main__":
    unittest.main()
