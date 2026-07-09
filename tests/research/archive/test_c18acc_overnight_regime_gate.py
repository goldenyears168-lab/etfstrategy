"""Tests for c18acc overnight regime panel + annot helpers."""

from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from research.backtest.archive.c18acc_overnight_regime_annot import _risk_off_compare
from research.backtest.archive.c18acc_overnight_regime_panel import (
    load_overnight_regime_panel,
    mark_open_trap_days,
)


class TestOvernightRegimePanel(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE tech_risk_daily_snapshot (
                session_date TEXT PRIMARY KEY,
                us_trade_date TEXT,
                tsm_daily_return_pct REAL,
                sox_daily_return_pct REAL,
                smh_daily_return_pct REAL,
                tx_gap_pct REAL,
                te_overnight_pct REAL,
                semi_benchmark TEXT DEFAULT 'SOX',
                source_us TEXT DEFAULT 'yahoo',
                source_tw TEXT DEFAULT 'finmind',
                synced_at TEXT DEFAULT 'x'
            );
            CREATE TABLE us_daily_bars (
                ticker TEXT, trade_date TEXT, close REAL,
                PRIMARY KEY (ticker, trade_date)
            );
            CREATE TABLE daily_bars (
                code TEXT, date TEXT, close REAL, source TEXT DEFAULT 'tej',
                PRIMARY KEY (code, date, source)
            );
            CREATE TABLE futures_institutional_daily (
                futures_id TEXT, trade_date TEXT, inst_name TEXT,
                net_oi_vol REAL, source TEXT DEFAULT 'finmind',
                PRIMARY KEY (futures_id, trade_date, inst_name, source)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO tech_risk_daily_snapshot
            (session_date, us_trade_date, sox_daily_return_pct, smh_daily_return_pct)
            VALUES ('2026-07-07', '2026-07-03', 2.5, 2.4)
            """
        )
        conn.execute(
            "INSERT INTO us_daily_bars VALUES ('SPY', '2026-07-03', 100.0)"
        )
        conn.execute(
            "INSERT INTO us_daily_bars VALUES ('SPY', '2026-07-02', 98.0)"
        )
        conn.execute(
            "INSERT INTO daily_bars VALUES ('IX0001', '2026-07-06', 46000, 'tej')"
        )
        conn.execute(
            "INSERT INTO daily_bars VALUES ('IX0001', '2026-07-07', 45479, 'tej')"
        )
        for i, v in enumerate(range(10, 70)):
            conn.execute(
                "INSERT INTO futures_institutional_daily VALUES ('TX', ?, '外資', ?, 'finmind')",
                (f"2026-06-{10+i:02d}", float(v)),
            )
        conn.commit()
        return conn

    def test_panel_flags_open_trap_proxy(self) -> None:
        conn = self._conn()
        panel = load_overnight_regime_panel(conn, ["2026-07-07"])
        row = panel.iloc[0]
        self.assertAlmostEqual(float(row["sox_t1_ret_pct"]), 2.5)
        self.assertTrue(bool(row["open_trap_proxy_sox_up"]))
        self.assertFalse(bool(row["combo_risk_off"]))

    def test_mark_open_trap_days(self) -> None:
        panel = pd.DataFrame(
            [{"session_date": "2026-07-07", "sox_t1_ret_pct": 2.5}]
        )
        ix = pd.Series({"2026-07-07": -2.31})
        traps = mark_open_trap_days(panel, ix)
        self.assertTrue(bool(traps["2026-07-07"]))


class TestOvernightRegimeAnnot(unittest.TestCase):
    def test_risk_off_compare_pass(self) -> None:
        legs = [
            {"combo_risk_off": True, "return_pct": -2.0, "excess_pct": -1.0},
            {"combo_risk_off": True, "return_pct": -1.5, "excess_pct": -0.5},
        ] + [
            {"combo_risk_off": True, "return_pct": -1.0, "excess_pct": -0.3}
            for _ in range(18)
        ] + [
            {"combo_risk_off": False, "return_pct": 1.0, "excess_pct": 0.5},
        ] + [
            {"combo_risk_off": False, "return_pct": 0.5, "excess_pct": 0.2}
            for _ in range(20)
        ]
        out = _risk_off_compare(legs, flag_field="combo_risk_off")
        self.assertEqual(out["flagged_n"], 20)
        self.assertLess(out["delta_return_pp"], -0.2)
        self.assertTrue(out["h_or_0_pass"])


if __name__ == "__main__":
    unittest.main()
