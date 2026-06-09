"""portfolio_book：YAML 匯入與持倉表。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from portfolio_book import infer_asset_type, load_books_yaml, normalize_symbol, sync_books_from_yaml
from stock_db import connect, load_portfolio_positions


class TestPortfolioBook(unittest.TestCase):
    def test_normalize_etf_codes(self) -> None:
        self.assertEqual(normalize_symbol("00981a"), "00981A")
        self.assertEqual(normalize_symbol("00878"), "00878")

    def test_infer_asset_type(self) -> None:
        self.assertEqual(infer_asset_type("2330"), "stock")
        self.assertEqual(infer_asset_type("00981A"), "etf")
        self.assertEqual(infer_asset_type("1409", "stock"), "stock")

    def test_sync_lily_jack_annie(self) -> None:
        example = (
            Path(__file__).resolve().parent.parent / "config" / "portfolio_books.example.yaml"
        )
        books = load_books_yaml(example)
        ids = {b["book_id"] for b in books}
        self.assertEqual(ids, {"lily", "jack", "annie"})

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            counts = sync_books_from_yaml(conn, example)
            self.assertEqual(counts["lily"], 7)
            self.assertEqual(counts["jack"], 3)
            self.assertEqual(counts["annie"], 2)

            jack = load_portfolio_positions(conn, "jack")
            by_sym = {r["symbol"]: r["asset_type"] for r in jack}
            self.assertEqual(by_sym["2330"], "stock")
            self.assertEqual(by_sym["00981A"], "etf")
            self.assertEqual(by_sym["00878"], "etf")
            conn.close()


if __name__ == "__main__":
    unittest.main()
