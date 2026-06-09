"""evening_digest：空 DB 不崩、輸出結構。"""

from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
from pathlib import Path

from evening_digest import (
    print_evening_human_digest,
    write_evening_brief_file,
)
from research_universe import DEFAULT_ETF_CODES
from stock_db import connect


class TestEveningDigest(unittest.TestCase):
    def test_empty_db_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_evening_human_digest(
                    conn, DEFAULT_ETF_CODES, reports_dir=Path(tmp) / "reports"
                )
            out = buf.getvalue()
            self.assertIn("收盤雷達", out)
            self.assertIn("① 今日結論", out)
            self.assertIn("evening_brief.md", out)
            self.assertNotIn("evening_digest.md", out)
            reports = list((Path(tmp) / "reports").glob("*.md"))
            self.assertEqual(len(reports), 1)
            self.assertTrue(reports[0].name.endswith("_evening_brief.md"))
            conn.close()

    def test_write_brief_only_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            path = write_evening_brief_file(
                conn, DEFAULT_ETF_CODES, reports_dir=Path(tmp) / "reports"
            )
            self.assertTrue(path.name.endswith("_evening_brief.md"))
            text = path.read_text(encoding="utf-8")
            self.assertIn("# 收盤研究 brief", text)
            self.assertIn("## 待查新聞", text)
            self.assertIn("## 隔日 Checklist", text)
            md_files = list((Path(tmp) / "reports").glob("*.md"))
            self.assertEqual(len(md_files), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
