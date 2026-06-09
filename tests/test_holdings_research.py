"""holdings_research：持股 diff、grow%、flow、對齊 cohort（fixture DB）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from holdings_research import (
    build_etf_holdings_changes_block,
    holding_growth_pct,
    implied_close_from_holdings,
    implied_flow_ntd,
    resolve_aligned_cohort,
    resolve_change_dates,
)
from stock_db import (
    compute_etf_holdings_changes,
    connect,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
)

SYNCED = "2026-06-01T00:00:00+00:00"


def _seed_two_day_holdings(conn, etf_code: str, prev: str, curr: str) -> None:
    for snap, count, rows in (
        (
            prev,
            1,
            [
                {
                    "etf_code": etf_code,
                    "snapshot_date": prev,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "shares": 1000.0,
                    "weight_pct": 5.0,
                    "amount": 500_000.0,
                    "source": "t",
                    "source_edit_at": None,
                    "synced_at": SYNCED,
                }
            ],
        ),
        (
            curr,
            2,
            [
                {
                    "etf_code": etf_code,
                    "snapshot_date": curr,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "shares": 1100.0,
                    "weight_pct": 5.5,
                    "amount": 605_000.0,
                    "source": "t",
                    "source_edit_at": None,
                    "synced_at": SYNCED,
                },
                {
                    "etf_code": etf_code,
                    "snapshot_date": curr,
                    "stock_id": "2454",
                    "stock_name": "聯發科",
                    "shares": 200.0,
                    "weight_pct": 1.0,
                    "amount": None,
                    "source": "t",
                    "source_edit_at": None,
                    "synced_at": SYNCED,
                },
            ],
        ),
    ):
        upsert_etf_holdings_meta(
            conn,
            {
                "etf_code": etf_code,
                "snapshot_date": snap,
                "nav": 100.0,
                "holding_count": count,
                "source": "t",
                "source_edit_at": None,
            },
        )
        upsert_etf_holdings(conn, rows)


class TestHoldingGrowthPct(unittest.TestCase):
    def test_new_position_returns_none(self) -> None:
        self.assertIsNone(holding_growth_pct(0, 100, "新进"))

    def test_zero_prev_returns_none(self) -> None:
        self.assertIsNone(holding_growth_pct(0, 100, "加码"))

    def test_normal_growth(self) -> None:
        self.assertAlmostEqual(holding_growth_pct(100, 150, "加码"), 50.0)


class TestImpliedFlow(unittest.TestCase):
    def test_no_close(self) -> None:
        self.assertIsNone(implied_flow_ntd(1000, None))
        self.assertIsNone(implied_flow_ntd(1000, 0))

    def test_with_close(self) -> None:
        self.assertEqual(implied_flow_ntd(1000, 500.0), 500_000.0)


class TestHoldingsDbFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_compute_changes_new_and_add(self) -> None:
        _seed_two_day_holdings(self.conn, "00981A", "2026-05-28", "2026-06-01")
        rows = {r["stock_id"]: r for r in compute_etf_holdings_changes(self.conn, "00981A")}
        self.assertEqual(rows["2330"]["action"], "加码")
        self.assertAlmostEqual(float(rows["2330"]["share_delta"]), 100.0)
        self.assertAlmostEqual(float(rows["2330"]["weight_delta"]), 0.5)
        self.assertEqual(rows["2454"]["action"], "新进")
        self.assertAlmostEqual(float(rows["2454"]["share_delta"]), 200.0)

    def test_implied_close_from_amount_shares(self) -> None:
        _seed_two_day_holdings(self.conn, "00981A", "2026-05-28", "2026-06-01")
        close = implied_close_from_holdings(self.conn, "2330", "2026-06-01")
        self.assertAlmostEqual(close or 0, 550.0)

    def test_resolve_aligned_cohort_two_etfs(self) -> None:
        for etf in ("00981A", "00982A"):
            _seed_two_day_holdings(self.conn, etf, "2026-05-28", "2026-06-01")
        cohort = resolve_aligned_cohort(self.conn, ("00981A", "00982A"))
        self.assertIsNotNone(cohort)
        assert cohort is not None
        self.assertEqual(cohort.prev_date, "2026-05-28")
        self.assertEqual(cohort.curr_date, "2026-06-01")
        self.assertEqual(set(cohort.etf_codes), {"00981A", "00982A"})

    def test_resolve_change_dates_default_pair(self) -> None:
        _seed_two_day_holdings(self.conn, "00981A", "2026-05-28", "2026-06-01")
        pair = resolve_change_dates(self.conn, "00981A")
        self.assertEqual(pair, ("2026-06-01", "2026-05-28"))

    def test_build_etf_holdings_changes_block(self) -> None:
        _seed_two_day_holdings(self.conn, "00981A", "2026-05-28", "2026-06-01")
        blocks = build_etf_holdings_changes_block(self.conn, ("00981A",))
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block["etf_code"], "00981A")
        self.assertEqual(block["prev_date"], "2026-05-28")
        self.assertEqual(block["curr_date"], "2026-06-01")
        by_id = {c["stock_id"]: c for c in block["changes"]}
        self.assertEqual(by_id["2330"]["action"], "加码")
        row_2330 = by_id["2330"]
        self.assertAlmostEqual(float(row_2330["share_delta"]), 100.0)
        self.assertAlmostEqual(row_2330["flow_ntd"], 50_000.0)
        self.assertNotIn("shares_prev", row_2330)
        self.assertNotIn("growth_pct", row_2330)
        self.assertNotIn("beta", row_2330)
        self.assertEqual(by_id["2454"]["action"], "新进")


if __name__ == "__main__":
    unittest.main()
