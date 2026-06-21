from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.backtest.mutual_fund_copytrade import (
    ACTION_FILTER_INITIATION,
    ACTION_FILTER_TOP3_INITIATION,
    compute_mutual_fund_holdings_changes,
    estimate_disclosure_date,
    iter_mutual_fund_copytrade_signals,
    _passes_action_filter,
    MutualFundChangeRow,
)
from stock_db import (
    connect,
    upsert_mutual_fund_holdings,
    upsert_mutual_fund_holdings_meta,
    upsert_stock_daily_bars,
)
from sync_mutual_fund_holdings import ALLIANZ_TW_TECH, DISCLOSURE_MONTHLY


def _seed_snap(
    conn: sqlite3.Connection,
    snap: str,
    holdings: list[tuple[str, int, float, float]],
) -> None:
    synced = "2026-06-01T00:00:00+00:00"
    upsert_mutual_fund_holdings_meta(
        conn,
        {
            "fund_code": ALLIANZ_TW_TECH.fund_code,
            "snapshot_date": snap,
            "fund_name": ALLIANZ_TW_TECH.fund_name,
            "disclosure_type": DISCLOSURE_MONTHLY,
            "fund_size_billion": 50.0,
            "holding_count": len(holdings),
            "source": "test",
            "source_edit_at": snap,
        },
    )
    upsert_mutual_fund_holdings(
        conn,
        [
            {
                "fund_code": ALLIANZ_TW_TECH.fund_code,
                "snapshot_date": snap,
                "disclosure_type": DISCLOSURE_MONTHLY,
                "stock_id": sid,
                "stock_name": sid,
                "rank_no": rank,
                "shares": None,
                "weight_pct": weight,
                "amount": amount,
                "asset_type": "國內上市",
                "source": "test",
                "source_edit_at": snap,
            }
            for sid, rank, weight, amount in holdings
        ],
    )


def _seed_trade_dates(conn: sqlite3.Connection, dates: list[str]) -> None:
    rows = []
    for i, d in enumerate(dates):
        px = 100.0 + i
        rows.append(
            {
                "stock_id": "2330",
                "trade_date": d,
                "open": px,
                "high": px + 1,
                "low": px - 1,
                "close": px + 0.5,
                "volume": 1000,
                "source": "finmind",
            }
        )
    upsert_stock_daily_bars(conn, rows)


class MutualFundCopytradeTests(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return connect(Path(tmp.name))

    def test_compute_mutual_fund_holdings_changes(self) -> None:
        conn = self._conn()
        _seed_snap(
            conn,
            "2025-05-31",
            [("2330", 1, 8.0, 1_000), ("2345", 2, 7.0, 900)],
        )
        _seed_snap(
            conn,
            "2025-06-30",
            [("2330", 1, 8.5, 1_200), ("6223", 3, 6.0, 800)],
        )
        rows = compute_mutual_fund_holdings_changes(
            conn, ALLIANZ_TW_TECH.fund_code, "2025-06-30", "2025-05-31"
        )
        by_id = {r.stock_id: r for r in rows}
        self.assertEqual(by_id["2330"].action, "加码")
        self.assertEqual(by_id["6223"].action, "新进")
        self.assertEqual(by_id["6223"].rank_no_curr, 3)
        self.assertEqual(by_id["2345"].action, "出清")

    def test_top3_initiation_filter(self) -> None:
        row_top3 = MutualFundChangeRow(
            stock_id="6223",
            stock_name="旺矽",
            action="新进",
            amount_prev=0,
            amount_curr=800,
            amount_delta=800,
            weight_pct_prev=None,
            weight_pct_curr=6.0,
            weight_delta=6.0,
            rank_no_curr=3,
            snapshot_curr="2025-06-30",
            snapshot_prev="2025-05-31",
        )
        row_top5 = MutualFundChangeRow(
            stock_id="2345",
            stock_name="智邦",
            action="新进",
            amount_prev=0,
            amount_curr=500,
            amount_delta=500,
            weight_pct_prev=None,
            weight_pct_curr=5.0,
            weight_delta=5.0,
            rank_no_curr=5,
            snapshot_curr="2025-06-30",
            snapshot_prev="2025-05-31",
        )
        self.assertTrue(_passes_action_filter(row_top3, ACTION_FILTER_TOP3_INITIATION))
        self.assertFalse(_passes_action_filter(row_top5, ACTION_FILTER_TOP3_INITIATION))
        self.assertTrue(_passes_action_filter(row_top5, ACTION_FILTER_INITIATION))

    def test_estimate_disclosure_date_lag28(self) -> None:
        conn = self._conn()
        _seed_trade_dates(
            conn,
            ["2025-07-28", "2025-07-29", "2025-07-30"],
        )
        got = estimate_disclosure_date(conn, "2025-06-30", "lag28")
        self.assertEqual(got, "2025-07-28")

    def test_iter_signals_top3_initiation(self) -> None:
        conn = self._conn()
        _seed_snap(
            conn,
            "2025-05-31",
            [("2330", 1, 8.0, 1_000), ("2345", 2, 7.0, 900)],
        )
        _seed_snap(
            conn,
            "2025-06-30",
            [
                ("2330", 1, 8.5, 1_200),
                ("6223", 3, 6.0, 800),
                ("2454", 4, 5.5, 700),
            ],
        )
        _seed_trade_dates(conn, ["2025-07-28", "2025-07-29"])
        sigs = iter_mutual_fund_copytrade_signals(
            conn,
            ALLIANZ_TW_TECH,
            disclosure_method="lag28",
            action_filter=ACTION_FILTER_TOP3_INITIATION,
        )
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].stock_id, "6223")
        self.assertEqual(sigs[0].action, "新进")


if __name__ == "__main__":
    unittest.main()
