"""Tests for supabase_research_sync backfill date discovery."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from supabase_research_sync import (
    backfill_dates,
    discover_report_dates_between,
)


class TestDiscoverReportDatesBetween(unittest.TestCase):
    def test_filters_to_report_files_not_all_calendar_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "reports/daily"
            daily.mkdir(parents=True)
            (daily / "20260618_etf_daily.md").write_text("# brief", encoding="utf-8")
            (daily / "20260620_regime_daily.md").write_text("# brief", encoding="utf-8")

            with patch("supabase_research_sync.PROJECT_ROOT", root):
                found = discover_report_dates_between(date(2026, 6, 18), date(2026, 6, 20))

            self.assertEqual(found, [date(2026, 6, 18), date(2026, 6, 20)])

    def test_empty_when_start_after_end(self) -> None:
        self.assertEqual(
            discover_report_dates_between(date(2026, 6, 20), date(2026, 6, 18)),
            [],
        )


class TestBackfillDatesSignalHits(unittest.TestCase):
    @patch("supabase_research_sync.upsert_brief")
    @patch("supabase_research_sync.load_brief", return_value=None)
    @patch("supabase_research_sync.supabase_configured", return_value=True)
    def test_skips_signal_hits_by_default(
        self,
        _configured: object,
        _load: object,
        _upsert: object,
    ) -> None:
        with patch.dict("sys.modules", {"supabase_signal_sync": MagicMock()}):
            import sys

            signal_mod = sys.modules["supabase_signal_sync"]
            result = backfill_dates([date(2026, 6, 18)])
            signal_mod.sync_signal_hits_for_date.assert_not_called()
            self.assertEqual(result.errors, [])

    @patch("supabase_research_sync.upsert_brief")
    @patch("supabase_research_sync.load_brief", return_value=None)
    @patch("supabase_research_sync.supabase_configured", return_value=True)
    def test_syncs_signal_hits_when_opted_in(
        self,
        _configured: object,
        _load: object,
        _upsert: object,
    ) -> None:
        mock_signal = MagicMock()
        with patch.dict("sys.modules", {"supabase_signal_sync": mock_signal}):
            backfill_dates([date(2026, 6, 18)], sync_signal_hits=True)
            mock_signal.sync_signal_hits_for_date.assert_called_once_with(date(2026, 6, 18))


if __name__ == "__main__":
    unittest.main()
