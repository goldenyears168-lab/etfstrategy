"""春哥漏斗 VCP 研究 · 單元測試。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from vcp_funnel_screen import (
    MODEL_ID,
    load_vcp_funnel_params,
    run_vcp_funnel_screen,
)
from stock_db import (
    connect,
    upsert_daily_bars,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
    upsert_stock_daily_bars,
    upsert_stock_fundamental,
)
from stage_analysis import calculate_simple_trend


def _uptrend_df(n: int = 260) -> pd.DataFrame:
    start = date(2024, 1, 2)
    rows: list[dict] = []
    price = 80.0
    for i in range(n):
        d = start + timedelta(days=i)
        if i >= n - 120:
            seg = (i - (n - 120)) // 30
            width = max(1.0, 8.0 - seg * 2.5)
        else:
            width = 6.0
        vol = 2_000_000 if i < n - 20 else 800_000
        price += 0.3
        rows.append(
            {
                "date": d.isoformat(),
                "Open": price,
                "High": price + width,
                "Low": price - width * 0.35,
                "Close": price,
                "Volume": vol,
            }
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


class SimpleTrendTests(unittest.TestCase):
    def test_simple_trend_passes_uptrend(self) -> None:
        df = _uptrend_df(260)
        out = calculate_simple_trend(df)
        self.assertTrue(out["passed"])
        self.assertEqual(out["score"], 100.0)

    def test_simple_trend_rejects_short_history(self) -> None:
        df = _uptrend_df(100)
        out = calculate_simple_trend(df)
        self.assertFalse(out["passed"])


class ChungeFunnelScreenTests(unittest.TestCase):
    def _seed_bars(self, stock_id: str, n: int, *, price_start: float = 100.0) -> list[dict]:
        start = date(2024, 1, 2)
        rows: list[dict] = []
        price = price_start
        for i in range(n):
            d = start + timedelta(days=i)
            price += 0.2
            rows.append(
                {
                    "stock_id": stock_id,
                    "trade_date": d.isoformat(),
                    "open": price,
                    "high": price + 3,
                    "low": price - 1,
                    "close": price,
                    "volume": 1_500_000,
                    "source": "finmind",
                }
            )
        return rows

    def _seed_bars_through(
        self, stock_id: str, n: int, through: date, *, price_start: float = 100.0
    ) -> list[dict]:
        rows: list[dict] = []
        price = price_start
        for i in range(n):
            d = through - timedelta(days=n - 1 - i)
            price += 0.2
            rows.append(
                {
                    "stock_id": stock_id,
                    "trade_date": d.isoformat(),
                    "open": price,
                    "high": price + 3,
                    "low": price - 1,
                    "close": price,
                    "volume": 1_500_000,
                    "source": "finmind",
                }
            )
        return rows

    def _uptrend_bars_through(self, stock_id: str, n: int, through: date) -> list[dict]:
        df = _uptrend_df(n)
        rows: list[dict] = []
        for i in range(n):
            d = through - timedelta(days=n - 1 - i)
            row = df.iloc[i]
            rows.append(
                {
                    "stock_id": stock_id,
                    "trade_date": d.isoformat(),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                    "source": "finmind",
                }
            )
        return rows

    def test_run_screen_produces_funnel_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = connect(db)
            try:
                synced = "2026-06-15T00:00:00+00:00"
                upsert_etf_holdings_meta(
                    conn,
                    {
                        "etf_code": "00981A",
                        "snapshot_date": "2026-06-15",
                        "nav": 100.0,
                        "holding_count": 2,
                        "source": "test",
                        "source_edit_at": None,
                        "synced_at": synced,
                    },
                )
                upsert_etf_holdings(
                    conn,
                    [
                        {
                            "etf_code": "00981A",
                            "snapshot_date": "2026-06-15",
                            "stock_id": "2330",
                            "stock_name": "台積電",
                            "shares": 1000.0,
                            "weight_pct": 5.0,
                            "amount": None,
                            "source": "test",
                            "source_edit_at": None,
                            "synced_at": synced,
                        },
                        {
                            "etf_code": "00981A",
                            "snapshot_date": "2026-06-15",
                            "stock_id": "2317",
                            "stock_name": "鴻海",
                            "shares": 1000.0,
                            "weight_pct": 3.0,
                            "amount": None,
                            "source": "test",
                            "source_edit_at": None,
                            "synced_at": synced,
                        },
                    ],
                )
                upsert_stock_daily_bars(
                    conn, self._seed_bars_through("2330", 260, date(2026, 6, 15))
                )
                upsert_stock_daily_bars(
                    conn,
                    self._seed_bars_through("2317", 260, date(2026, 6, 15), price_start=50.0),
                )
                bench_end = date(2026, 6, 15)
                bench_rows = [
                    {
                        "code": "IX0001",
                        "date": (bench_end - timedelta(days=259 - i)).isoformat(),
                        "open": 18000 + i * 5,
                        "high": 18100 + i * 5,
                        "low": 17900 + i * 5,
                        "close": 18000 + i * 5,
                        "volume": 0,
                        "spread": 0.0,
                        "source": "tej",
                    }
                    for i in range(260)
                ]
                upsert_daily_bars(conn, bench_rows)

                as_of, results, layer_counts, _cfg = run_vcp_funnel_screen(
                    conn,
                    etf_codes=("00981A",),
                )
                self.assertTrue(as_of)
                self.assertEqual(as_of, "2026-06-15")
                self.assertEqual(layer_counts["L1"], 2)
                self.assertGreaterEqual(layer_counts["L2"], 1)
                self.assertTrue(any(r.stock_id == "2330" for r in results))
            finally:
                conn.close()

    def test_run_screen_as_of_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = connect(db)
            try:
                synced = "2026-06-15T00:00:00+00:00"
                upsert_etf_holdings_meta(
                    conn,
                    {
                        "etf_code": "00981A",
                        "snapshot_date": "2026-06-15",
                        "nav": 100.0,
                        "holding_count": 1,
                        "source": "test",
                        "source_edit_at": None,
                        "synced_at": synced,
                    },
                )
                upsert_etf_holdings(
                    conn,
                    [
                        {
                            "etf_code": "00981A",
                            "snapshot_date": "2026-06-15",
                            "stock_id": "2330",
                            "stock_name": "台積電",
                            "shares": 1000.0,
                            "weight_pct": 5.0,
                            "amount": None,
                            "source": "test",
                            "source_edit_at": None,
                            "synced_at": synced,
                        },
                    ],
                )
                upsert_stock_daily_bars(
                    conn,
                    self._uptrend_bars_through("2330", 260, date(2026, 6, 10)),
                )
                target = "2026-06-10"
                upsert_stock_fundamental(
                    conn,
                    [
                        {
                            "stock_id": "2330",
                            "as_of_date": target,
                            "pe": 20.0,
                            "pb": 5.0,
                            "roe_ttm": 20.0,
                            "eps_ttm": 40.0,
                            "eps_latest_q": 10.0,
                            "roe_latest_q": 20.0,
                            "dividend_yield": 2.0,
                            "revenue_yoy_pct": 10.0,
                            "revenue_mom_accel_pp": 1.0,
                            "source": "test",
                        }
                    ],
                )
                bench_end = date(2026, 6, 10)
                bench_rows = [
                    {
                        "code": "IX0001",
                        "date": (bench_end - timedelta(days=259 - i)).isoformat(),
                        "open": 18000 + i * 5,
                        "high": 18100 + i * 5,
                        "low": 17900 + i * 5,
                        "close": 18000 + i * 5,
                        "volume": 0,
                        "spread": 0.0,
                        "source": "tej",
                    }
                    for i in range(260)
                ]
                upsert_daily_bars(conn, bench_rows)

                as_of, results, layer_counts, _cfg = run_vcp_funnel_screen(
                    conn,
                    etf_codes=("00981A",),
                    as_of_date=target,
                    persist=True,
                )
                self.assertEqual(as_of, target)
                self.assertEqual(layer_counts["L1"], 1)
                if layer_counts.get("L7", 0) > 0:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM vcp_screen_scores_v2
                        WHERE model_id = ? AND as_of_date = ?
                        """,
                        (MODEL_ID, target),
                    ).fetchone()
                    self.assertGreater(int(row["n"] or 0), 0)
                if layer_counts.get("L7", 0) > 0:
                    stop_row = conn.execute(
                        """
                        SELECT stop_loss, risk_pct, entry_ready
                        FROM vcp_screen_scores_v2
                        WHERE model_id = ? AND as_of_date = ?
                          AND stop_loss IS NOT NULL AND stop_loss > 0
                        LIMIT 1
                        """,
                        (MODEL_ID, target),
                    ).fetchone()
                    self.assertIsNotNone(stop_row, "L7 寫入應含 vcp-tm stop_loss")
                self.assertTrue(any(r.stock_id == "2330" for r in results))
            finally:
                conn.close()

    def test_load_default_calibration(self) -> None:
        params = load_vcp_funnel_params()
        self.assertGreaterEqual(params.t1_depth_max, 60.0)
        self.assertGreaterEqual(params.contraction_ratio, 0.85)

    def test_real_db_l7_if_available(self) -> None:
        from stock_db import DEFAULT_DB_PATH

        if not DEFAULT_DB_PATH.is_file():
            self.skipTest("no stocks.db")
        conn = connect(DEFAULT_DB_PATH)
        try:
            as_of, _results, layer_counts, _cfg = run_vcp_funnel_screen(conn)
            if not as_of:
                self.skipTest("insufficient bars")
            self.assertGreater(
                layer_counts.get("L7", 0),
                0,
                "calibrated funnel should pass at least one stock to L7",
            )
        finally:
            conn.close()

    def test_fundamental_layer_with_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = connect(db)
            try:
                upsert_stock_fundamental(
                    conn,
                    [
                        {
                            "stock_id": "2330",
                            "as_of_date": "2026-06-10",
                            "pe": 20.0,
                            "pb": 5.0,
                            "roe_ttm": 15.0,
                            "eps_ttm": 40.0,
                            "eps_latest_q": 10.0,
                            "roe_latest_q": 15.0,
                            "dividend_yield": 1.5,
                            "revenue_yoy_pct": 20.0,
                            "revenue_mom_accel_pp": 1.0,
                            "source": "finmind",
                        }
                    ],
                )
                row = conn.execute(
                    "SELECT roe_ttm FROM stock_fundamental WHERE stock_id='2330'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(float(row["roe_ttm"]), 15.0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
