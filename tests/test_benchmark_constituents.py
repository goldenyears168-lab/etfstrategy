"""sync_benchmark_constituents parser tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sync_benchmark_constituents import fetch_yuanta_benchmark_snapshot


class TestBenchmarkConstituents(unittest.TestCase):
    def test_fetch_yuanta_filters_listed_cjk_names(self) -> None:
        html = (
            '"2330","台積電","Taiwan Semiconductor Manufacturing Co. Ltd.",'
            '"2395","研華","Advantech Co. Ltd.",'
            '"5000","歷史淨值","Historical NAV",'
            '"2000","0.1","Foo Bar"'
        )
        with patch("sync_benchmark_constituents.requests.Session.get") as get:
            get.return_value.text = html
            get.return_value.raise_for_status = lambda: None
            snap = fetch_yuanta_benchmark_snapshot(
                "0050",
                listed_ids={"2330", "2395", "2000"},
                min_holdings=2,
            )
        self.assertEqual(snap.benchmark_code, "0050")
        ids = [row["stock_id"] for row in snap.holdings]
        self.assertEqual(ids, ["2330", "2395"])


if __name__ == "__main__":
    unittest.main()
