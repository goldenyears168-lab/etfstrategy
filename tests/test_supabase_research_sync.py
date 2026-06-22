"""Supabase research sync · brief catalog paths."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from supabase_research_sync import (
    BRIEF_CATALOG,
    INTRADAY_WATCH_META,
    STRATEGY_SCREEN_META,
    SLOT_BRIEF_TYPES,
    _find_brief_file,
    _intraday_data_baseline,
    load_brief,
    sync_slot,
)


class TestBriefCatalog(unittest.TestCase):
    def test_strategy_briefs_in_1630_slot(self) -> None:
        slot_types = SLOT_BRIEF_TYPES["1630"]
        self.assertIn("rrg_mono_daily", slot_types)
        self.assertIn("copytrade_l1h9", slot_types)

    def test_strategy_briefs_in_1300_slot(self) -> None:
        slot_types = SLOT_BRIEF_TYPES["1300"]
        self.assertIn("vcp_pivot_gate", slot_types)
        self.assertIn("vcp_coil_close", slot_types)

    def test_strategy_screen_meta_covers_daily_screens(self) -> None:
        for brief_type in (
            "copytrade_l1h9",
            "rrg_mono_daily",
            "vcp_pivot_gate",
            "vcp_coil_close",
        ):
            meta = STRATEGY_SCREEN_META[brief_type]
            self.assertIn("strategy_id", meta)
            self.assertEqual(meta["layer"], "strategy")

    def test_find_rrg_mono_daily_dated_file(self) -> None:
        root = Path(__file__).resolve().parent.parent
        day = date(2026, 6, 20)
        rel = "reports/daily/20260620_rrg_mono_daily.md"
        path = root / rel
        if not path.is_file():
            self.skipTest(f"missing fixture {rel}")
        found = _find_brief_file("rrg_mono_daily", day)
        self.assertIsNotNone(found)
        self.assertTrue(found.name.endswith("_rrg_mono_daily.md"))

    @patch("supabase_research_sync.build_backtest_reference")
    @patch("supabase_research_sync._find_brief_file")
    def test_load_brief_attaches_strategy_snapshot_json(
        self, mock_find: object, _mock_ref: object
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        sample = root / "reports/daily/20260620_copytrade_l1h9_daily.md"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(
            "# ETF00981A 跟單策略 · 2026-06-20\n\n## 摘要\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        mock_find.return_value = sample

        record = load_brief("copytrade_l1h9", date(2026, 6, 20))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.brief_type, "copytrade_l1h9")
        self.assertIsNone(record.content_html)
        self.assertIsNotNone(record.snapshot_json)
        assert record.snapshot_json is not None
        self.assertEqual(record.snapshot_json.get("strategy_id"), "00981a-l1h9")
        self.assertEqual(record.snapshot_json.get("contract"), "copytrade-daily-v1")

    def test_canonical_brief_catalog_single_path(self) -> None:
        self.assertEqual(len(BRIEF_CATALOG["etf_daily"][1]), 1)
        self.assertEqual(len(BRIEF_CATALOG["regime_daily"][1]), 1)

    @patch("supabase_research_sync._find_brief_file")
    def test_load_etf_daily_attaches_etf_snapshot_json(self, mock_find: object) -> None:
        root = Path(__file__).resolve().parent.parent
        sample = root / "reports/daily/20260620_etf_daily.md"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(
            "# ETF 日報 · 2026-06-20\n\n## 摘要\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        mock_find.return_value = sample

        record = load_brief("etf_daily", date(2026, 6, 20))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.content_html)
        self.assertIsNotNone(record.snapshot_json)
        assert record.snapshot_json is not None
        self.assertEqual(record.snapshot_json.get("contract"), "etf-daily-v1")
        self.assertEqual(record.snapshot_json.get("layer"), "facts")

    @patch("supabase_research_sync._find_brief_file")
    def test_load_vcp_funnel_attaches_vcp_snapshot_json(self, mock_find: object) -> None:
        root = Path(__file__).resolve().parent.parent
        sample = root / "reports/daily/20260620_vcp_funnel_specs_daily_brief.md"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(
            "# VCP Pivot Gate · daily brief · 2026-06-20\n\n## 候選\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        mock_find.return_value = sample

        record = load_brief("vcp_funnel_specs", date(2026, 6, 20))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.content_html)
        self.assertIsNotNone(record.snapshot_json)
        assert record.snapshot_json is not None
        self.assertEqual(record.snapshot_json.get("contract"), "vcp-daily-v1")
        self.assertEqual(record.snapshot_json.get("layer"), "research")

    def test_intraday_watch_meta_not_strategy_screen(self) -> None:
        meta = INTRADAY_WATCH_META["rrg_mono_intraday"]
        self.assertEqual(meta["strategy_id"], "rrg-mono-hold7")
        self.assertNotIn("rrg_mono_intraday", STRATEGY_SCREEN_META)

    @patch("supabase_research_sync._intraday_data_baseline")
    @patch("supabase_research_sync._find_brief_file")
    def test_load_intraday_attaches_intraday_snapshot_json(
        self, mock_find: object, mock_baseline: object
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        sample = root / "reports/daily/20260622_rrg_mono_intraday_watch.md"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(
            "# RRG mono 收盤前預警 · 2026-06-22\n\n"
            "- tick 覆蓋：**0 / 100** 檔 · 大盤 tick：沿用昨收\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: sample.unlink(missing_ok=True))

        mock_find.return_value = sample
        mock_baseline.return_value = date(2026, 6, 18)

        record = load_brief("rrg_mono_intraday")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.trade_date, date(2026, 6, 18))
        self.assertIsNotNone(record.snapshot_json)
        assert record.snapshot_json is not None
        self.assertEqual(record.snapshot_json.get("contract"), "rrg-mono-daily-v1")
        self.assertEqual(record.snapshot_json.get("session_date"), "2026-06-22")
        self.assertEqual(record.snapshot_json.get("data_baseline_date"), "2026-06-18")

    @patch("supabase_research_sync.build_backtest_reference")
    @patch("supabase_research_sync._find_brief_file")
    def test_load_strategy_screen_includes_backtest_reference(
        self, mock_find: object, mock_ref: object
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        sample = root / "reports/daily/20260620_copytrade_l1h9_daily.md"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(
            "# ETF00981A 跟單策略 · 2026-06-20\n\n## 摘要\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        mock_find.return_value = sample
        mock_ref.return_value = {
            "n_periods": 12,
            "win_rate_vs_bench_pct": 55.0,
            "expected_excess_pct": 3.2,
            "disclaimer_zh": "test",
        }

        record = load_brief("copytrade_l1h9", date(2026, 6, 20))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.content_html)
        ref = (record.snapshot_json or {}).get("backtest_reference")
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref["win_rate_vs_bench_pct"], 55.0)

    @patch("supabase_research_sync.supabase_configured", return_value=True)
    @patch("supabase_research_sync.allow_scheduled_supabase_push", return_value=False)
    def test_sync_slot_skips_non_trading_day(
        self, _mock_allow: object, _mock_cfg: object
    ) -> None:
        result = sync_slot("1630")
        self.assertEqual(result.uploaded, [])
        self.assertTrue(any("non-trading-day" in s for s in result.skipped))
        self.assertEqual(result.errors, [])

    @patch("supabase_research_sync.supabase_configured", return_value=True)
    @patch("supabase_research_sync.allow_scheduled_supabase_push", return_value=False)
    @patch("supabase_research_sync.load_brief")
    def test_sync_slot_with_explicit_date_bypasses_trading_day_gate(
        self, mock_load: object, _mock_allow: object, _mock_cfg: object
    ) -> None:
        from supabase_research_sync import BriefRecord

        mock_load.return_value = BriefRecord(
            trade_date=date(2026, 6, 19),
            schedule_slot="1630",
            brief_type="etf_daily",
            title="t",
            content_md="md",
            source_path="reports/daily/x.md",
        )
        with patch("supabase_research_sync.upsert_brief"):
            result = sync_slot("1630", date(2026, 6, 19))
        self.assertIn("etf_daily", result.uploaded)
        mock_load.assert_called()


if __name__ == "__main__":
    unittest.main()
