"""etf_daily_report：空 DB 與報告輸出。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from etf_daily_report import build_etf_daily_markdown, write_etf_daily_reports
from project_config import ETF_CODES_HOLDINGS, ETF_CODES_LISTED
from stock_db import connect


class TestEtfDailyReport(unittest.TestCase):
    def test_empty_db_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            text = build_etf_daily_markdown(conn, ETF_CODES_HOLDINGS, as_of="2026-06-20")
            self.assertIn("# ETF 日報", text)
            self.assertIn("## 各 ETF 持股變化", text)
            self.assertIn("持股同步（已掛牌）", text)
            self.assertIn(f"/{len(ETF_CODES_LISTED)}", text)
            conn.close()

    def test_write_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            conn = connect(tmp_root / "t.db")
            track_dir = tmp_root / "daily" / "etf-daily"
            reports_dir = tmp_root / "daily"
            paths = write_etf_daily_reports(
                conn,
                ETF_CODES_HOLDINGS,
                reports_dir=reports_dir,
                track_dir=track_dir,
                as_of="2026-06-20",
            )
            self.assertEqual(len(paths), 2)
            for p in paths:
                self.assertTrue(p.is_file())
                self.assertIn(str(tmp_root), str(p.resolve()))
                self.assertIn("ETF 日報", p.read_text(encoding="utf-8"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
