"""report_summary：早盤/收盤/週報摘要（空 DB 不崩）。"""

from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
from pathlib import Path

from report_summary import (
    print_evening_data_health,
    print_morning_report,
    print_weekly_report,
)
from stock_db import connect


class TestReportSummary(unittest.TestCase):
    def test_morning_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_morning_report(conn)
            out = buf.getvalue()
            self.assertIn("pre_open", out)
            self.assertIn("執行時間軸", out)
            self.assertIn("建議掛單價", out)
            self.assertIn("研究參考", out)
            conn.close()

    def test_evening_health_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_evening_data_health(conn)
            out = buf.getvalue()
            self.assertIn("資料健康", out)
            conn.close()

    def test_weekly_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_weekly_report(conn)
            out = buf.getvalue()
            self.assertIn("週日深度補庫", out)
            conn.close()


if __name__ == "__main__":
    unittest.main()
