"""position_review：持倉 × 研究池訊號。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market_labels import ENTRY_OVEREXTENDED, ENTRY_SKIP, PM_AVOID
from position_review import (
    ACTION_CONTRADICTION,
    ACTION_EXIT,
    ACTION_OUT_OF_POOL,
    ACTION_TRIM,
    build_position_exit_summary,
    build_position_review,
    pending_exit_lines,
    review_stock_position,
)
from portfolio_book import sync_books_from_yaml
from stock_db import connect


class TestPositionReview(unittest.TestCase):
    def test_review_overextended_holding(self) -> None:
        class FakePm:
            def __getitem__(self, k):
                return {
                    "pm_bucket": PM_AVOID,
                    "entry_signal": ENTRY_OVEREXTENDED,
                    "stock_name": "聯發科",
                    "chip_tag": "法人中性",
                }[k]

            def keys(self):
                return ("pm_bucket", "entry_signal", "stock_name", "chip_tag")

        row = review_stock_position(
            book_id="lily",
            symbol="2454",
            stock_name=None,
            in_pool=True,
            pm=FakePm(),
            sig={"net_side": "add", "l2_consensus_level": "STRONG"},
            cons=None,
        )
        self.assertEqual(row.action, ACTION_CONTRADICTION)
        self.assertIn("ETF_RULE_CONTRADICTION", row.reason_codes)

    def test_review_out_of_pool(self) -> None:
        row = review_stock_position(
            book_id="lily",
            symbol="1409",
            stock_name="新纖",
            in_pool=False,
            pm=None,
            sig=None,
            cons=None,
        )
        self.assertEqual(row.action, ACTION_OUT_OF_POOL)
        self.assertFalse(row.in_research_pool)

    def test_pending_exit_lines_only_exit_action(self) -> None:
        exit_row = review_stock_position(
            book_id="annie",
            symbol="2317",
            stock_name="鴻海",
            in_pool=True,
            pm=_fake_pm(PM_AVOID, ENTRY_SKIP),
            sig=None,
            cons=None,
        )
        trim_row = review_stock_position(
            book_id="annie",
            symbol="2330",
            stock_name="台積電",
            in_pool=True,
            pm=_fake_pm(PM_AVOID, ENTRY_OVEREXTENDED),
            sig={"net_side": "reduce", "l2_consensus_level": "WEAK"},
            cons=None,
        )
        reviews = {"annie": [exit_row, trim_row]}
        lines = pending_exit_lines(reviews)
        self.assertEqual(len(lines), 1)
        self.assertIn("2317", lines[0])
        self.assertIn("出清觀察", lines[0])

    def test_exit_summary_excludes_out_of_pool_and_contradiction(self) -> None:
        out_row = review_stock_position(
            book_id="lily",
            symbol="1409",
            stock_name="新纖",
            in_pool=False,
            pm=None,
            sig=None,
            cons=None,
        )
        trim_row = review_stock_position(
            book_id="lily",
            symbol="2303",
            stock_name="聯電",
            in_pool=True,
            pm=_fake_pm("觀察", ENTRY_OVEREXTENDED),
            sig={"net_side": "reduce", "l2_consensus_level": "WEAK"},
            cons=None,
        )
        summary = build_position_exit_summary({"lily": [out_row, trim_row]})
        self.assertEqual(len(summary["lily"]), 1)
        self.assertEqual(summary["lily"][0]["symbol"], "2303")
        self.assertEqual(summary["lily"][0]["action"], ACTION_TRIM)

    def test_build_review_empty_book_when_no_pool_stocks(self) -> None:
        example = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "portfolio_books.example.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            sync_books_from_yaml(conn, example)
            rows = build_position_review(conn, "annie", pool=set())
            self.assertEqual(rows, [])
            conn.close()

    def test_build_review_with_positions(self) -> None:
        example = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "portfolio_books.example.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            sync_books_from_yaml(conn, example)
            pool = {"2317", "2330"}
            rows = build_position_review(conn, "annie", pool=pool)
            self.assertEqual(len(rows), 2)
            symbols = {r.symbol for r in rows}
            self.assertEqual(symbols, {"2317", "2330"})
            conn.close()


def _fake_pm(bucket: str, entry: str):
    class FakePm:
        def __getitem__(self, k):
            return {
                "pm_bucket": bucket,
                "entry_signal": entry,
                "stock_name": "測試",
                "chip_tag": "",
            }[k]

        def keys(self):
            return ("pm_bucket", "entry_signal", "stock_name", "chip_tag")

    return FakePm()


if __name__ == "__main__":
    unittest.main()
