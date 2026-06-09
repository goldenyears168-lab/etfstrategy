"""tech_risk 依交易日對齊（開盤前須讀當日 session，非僅最新 TEJ 日）。"""

from __future__ import annotations

import sqlite3
import unittest

from pre_trade_check import load_tsm_adr_pct
from stock_db import load_latest_tech_risk, upsert_tech_risk_daily_snapshots


def _seed_snapshots(conn: sqlite3.Connection) -> None:
    upsert_tech_risk_daily_snapshots(
        conn,
        [
            {
                "session_date": "2026-06-04",
                "us_trade_date": "2026-06-03",
                "tsm_close": 436.69,
                "tsm_daily_return_pct": -2.2387,
                "tsm_ma5": None,
                "tsm_ma10": None,
                "tsm_vs_ma5_pct": None,
                "tsm_vs_ma10_pct": None,
                "tsm_above_ma5": None,
                "tsm_above_ma10": None,
                "sox_close": None,
                "sox_daily_return_pct": -2.15,
                "sox_ma5": None,
                "sox_above_ma5": None,
                "smh_close": None,
                "smh_daily_return_pct": None,
                "semi_benchmark": "SOX",
                "tw_spot_date": "2026-06-03",
                "tw_spot_code": "IX0001",
                "tw_spot_prev_close": 22000.0,
                "tx_futures_id": "TX",
                "tx_contract_date": None,
                "tx_futures_price": None,
                "tx_futures_session": None,
                "tx_gap_pct": 0.17,
                "te_futures_id": "TE",
                "te_contract_date": None,
                "te_futures_price": None,
                "te_futures_session": None,
                "te_overnight_pct": -0.6,
                "notes": None,
                "source_us": "yahoo",
                "source_tw": "finmind",
            },
            {
                "session_date": "2026-06-05",
                "us_trade_date": "2026-06-04",
                "tsm_close": 444.92,
                "tsm_daily_return_pct": 1.8846,
                "tsm_ma5": None,
                "tsm_ma10": None,
                "tsm_vs_ma5_pct": None,
                "tsm_vs_ma10_pct": None,
                "tsm_above_ma5": None,
                "tsm_above_ma10": None,
                "sox_close": None,
                "sox_daily_return_pct": 0.5,
                "sox_ma5": None,
                "sox_above_ma5": None,
                "smh_close": None,
                "smh_daily_return_pct": None,
                "semi_benchmark": "SOX",
                "tw_spot_date": "2026-06-04",
                "tw_spot_code": "IX0001",
                "tw_spot_prev_close": 22100.0,
                "tx_futures_id": "TX",
                "tx_contract_date": None,
                "tx_futures_price": None,
                "tx_futures_session": None,
                "tx_gap_pct": 0.1,
                "te_futures_id": "TE",
                "te_contract_date": None,
                "te_futures_price": None,
                "te_futures_session": None,
                "te_overnight_pct": -0.2,
                "notes": None,
                "source_us": "yahoo",
                "source_tw": "finmind",
            },
        ],
    )


class TestTechRiskTradeDate(unittest.TestCase):
    def test_load_prefers_trade_date_over_latest_te_j_day(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE tech_risk_daily_snapshot (
                session_date TEXT PRIMARY KEY,
                us_trade_date TEXT,
                tsm_close REAL,
                tsm_daily_return_pct REAL,
                tsm_ma5 REAL, tsm_ma10 REAL,
                tsm_vs_ma5_pct REAL, tsm_vs_ma10_pct REAL,
                tsm_above_ma5 INTEGER, tsm_above_ma10 INTEGER,
                sox_close REAL, sox_daily_return_pct REAL,
                sox_ma5 REAL, sox_above_ma5 INTEGER,
                smh_close REAL, smh_daily_return_pct REAL,
                semi_benchmark TEXT,
                tw_spot_date TEXT, tw_spot_code TEXT, tw_spot_prev_close REAL,
                tx_futures_id TEXT, tx_contract_date TEXT,
                tx_futures_price REAL, tx_futures_session TEXT, tx_gap_pct REAL,
                te_futures_id TEXT, te_contract_date TEXT,
                te_futures_price REAL, te_futures_session TEXT, te_overnight_pct REAL,
                notes TEXT, source_us TEXT, source_tw TEXT, synced_at TEXT
            );
            """
        )
        _seed_snapshots(conn)

        latest = load_latest_tech_risk(conn)
        self.assertEqual(latest["session_date"], "2026-06-05")

        for_trade = load_latest_tech_risk(conn, trade_date="2026-06-05")
        self.assertIsNotNone(for_trade)
        assert for_trade is not None
        self.assertEqual(for_trade["session_date"], "2026-06-05")
        self.assertAlmostEqual(float(for_trade["tsm_daily_return_pct"]), 1.8846, places=3)

        stale_if_wrong = load_latest_tech_risk(conn, trade_date="2026-06-05")
        self.assertNotAlmostEqual(
            float(stale_if_wrong["tsm_daily_return_pct"]), -2.2387, places=2
        )

        conn.close()

    def test_load_tsm_prefers_fresher_daily_bars_over_stale_snapshot(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE daily_bars (
                code TEXT, date TEXT, open REAL, high REAL, low REAL,
                close REAL, volume REAL, spread REAL, source TEXT,
                synced_at TEXT,
                PRIMARY KEY (code, date, source)
            );
            CREATE TABLE tech_risk_daily_snapshot (
                session_date TEXT PRIMARY KEY,
                us_trade_date TEXT,
                tsm_close REAL,
                tsm_daily_return_pct REAL,
                tsm_ma5 REAL, tsm_ma10 REAL,
                tsm_vs_ma5_pct REAL, tsm_vs_ma10_pct REAL,
                tsm_above_ma5 INTEGER, tsm_above_ma10 INTEGER,
                sox_close REAL, sox_daily_return_pct REAL,
                sox_ma5 REAL, sox_above_ma5 INTEGER,
                smh_close REAL, smh_daily_return_pct REAL,
                semi_benchmark TEXT,
                tw_spot_date TEXT, tw_spot_code TEXT, tw_spot_prev_close REAL,
                tx_futures_id TEXT, tx_contract_date TEXT,
                tx_futures_price REAL, tx_futures_session TEXT, tx_gap_pct REAL,
                te_futures_id TEXT, te_contract_date TEXT,
                te_futures_price REAL, te_futures_session TEXT, te_overnight_pct REAL,
                notes TEXT, source_us TEXT, source_tw TEXT, synced_at TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO daily_bars
                (code, date, open, high, low, close, volume, spread, source, synced_at)
            VALUES (?, ?, NULL, NULL, NULL, ?, NULL, ?, 'yahoo', 'test')
            """,
            [
                ("TSM_ADR", "2026-06-03", 436.69, -2.2387),
                ("TSM_ADR", "2026-06-04", 444.92, 1.8846),
            ],
        )
        upsert_tech_risk_daily_snapshots(
            conn,
            [
                {
                    "session_date": "2026-06-05",
                    "us_trade_date": "2026-06-03",
                    "tsm_close": 436.69,
                    "tsm_daily_return_pct": -2.2387,
                    "tsm_ma5": None,
                    "tsm_ma10": None,
                    "tsm_vs_ma5_pct": None,
                    "tsm_vs_ma10_pct": None,
                    "tsm_above_ma5": None,
                    "tsm_above_ma10": None,
                    "sox_close": None,
                    "sox_daily_return_pct": None,
                    "sox_ma5": None,
                    "sox_above_ma5": None,
                    "smh_close": None,
                    "smh_daily_return_pct": None,
                    "semi_benchmark": "SOX",
                    "tw_spot_date": "2026-06-04",
                    "tw_spot_code": "IX0001",
                    "tw_spot_prev_close": 22100.0,
                    "tx_futures_id": "TX",
                    "tx_contract_date": None,
                    "tx_futures_price": None,
                    "tx_futures_session": None,
                    "tx_gap_pct": None,
                    "te_futures_id": "TE",
                    "te_contract_date": None,
                    "te_futures_price": None,
                    "te_futures_session": None,
                    "te_overnight_pct": None,
                    "notes": None,
                    "source_us": "yahoo",
                    "source_tw": "finmind",
                }
            ],
        )
        self.assertAlmostEqual(load_tsm_adr_pct(conn, "2026-06-05"), 1.8846, places=3)
        conn.close()


if __name__ == "__main__":
    unittest.main()
