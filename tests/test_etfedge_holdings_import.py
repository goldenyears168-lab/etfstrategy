import unittest

from etfedge_holdings_import import build_snapshots_from_histories


class BuildSnapshotsTests(unittest.TestCase):
    def test_groups_by_date_and_computes_weight(self) -> None:
        histories = {
            "2330": [
                {"trade_date": "2026-06-10", "share_count": 1000, "close": 100.0},
                {"trade_date": "2026-06-11", "share_count": 1000, "close": 100.0},
            ],
            "2454": [
                {"trade_date": "2026-06-10", "share_count": 500, "close": 200.0},
                {"trade_date": "2026-06-11", "share_count": 0, "close": 200.0},
            ],
        }
        names = {"2330": "台積電", "2454": "聯發科"}
        snapshots, skipped = build_snapshots_from_histories(
            "00981A",
            histories,
            names,
            min_holdings=2,
        )
        self.assertEqual(skipped, 1)
        self.assertEqual(sorted(snapshots), ["2026-06-10"])
        rows = {r["stock_id"]: r for r in snapshots["2026-06-10"]}
        self.assertAlmostEqual(rows["2330"]["weight_pct"], 50.0)
        self.assertAlmostEqual(rows["2454"]["weight_pct"], 50.0)
        self.assertEqual(rows["2330"]["source"], "etfedge")

    def test_skips_sparse_dates(self) -> None:
        histories = {
            "2330": [{"trade_date": "2026-06-10", "share_count": 100, "close": 10.0}],
        }
        snapshots, skipped = build_snapshots_from_histories(
            "00981A",
            histories,
            {"2330": "台積電"},
            min_holdings=5,
        )
        self.assertEqual(snapshots, {})
        self.assertEqual(skipped, 1)

    def test_fills_weight_from_shares_when_close_missing(self) -> None:
        histories = {
            "2330": [{"trade_date": "2026-06-10", "share_count": 3000, "close": None}],
            "2454": [{"trade_date": "2026-06-10", "share_count": 1000, "close": None}],
        }
        snapshots, skipped = build_snapshots_from_histories(
            "00981A",
            histories,
            {"2330": "台積電", "2454": "聯發科"},
            min_holdings=2,
        )
        self.assertEqual(skipped, 0)
        rows = {r["stock_id"]: r for r in snapshots["2026-06-10"]}
        self.assertAlmostEqual(rows["2330"]["weight_pct"], 75.0)
        self.assertAlmostEqual(rows["2454"]["weight_pct"], 25.0)


if __name__ == "__main__":
    unittest.main()
