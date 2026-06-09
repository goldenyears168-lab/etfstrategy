"""flow_attribution：Coverage、固定 Seed Random、Boss Gate。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flow_attribution import _seed_for_date, run_flow_attribution
from project_config import BASELINE_RANDOM_SEED
from stock_db import connect, upsert_daily_bars, upsert_flow_events, upsert_stock_beta


def _stock_bar_row(stock_id: str, d: str, close: float) -> dict:
    return {
        "stock_id": stock_id,
        "trade_date": d,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 0,
        "source": "finmind",
    }


def _seed_prices(conn, stock_id: str, prices: dict[str, float]) -> None:
    from stock_db import upsert_stock_daily_bars

    upsert_stock_daily_bars(
        conn,
        [_stock_bar_row(stock_id, d, c) for d, c in sorted(prices.items())],
    )


def _seed_benchmark(conn, prices: dict[str, float]) -> None:
    upsert_daily_bars(
        conn,
        [
            {
                "code": "IX0001",
                "date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 0,
                "spread": None,
                "source": "tej",
            }
            for d, c in sorted(prices.items())
        ],
    )


class TestFlowAttribution(unittest.TestCase):
    def test_fixed_seed_reproducible(self) -> None:
        a = _seed_for_date("2026-06-02")
        b = _seed_for_date("2026-06-02")
        c = _seed_for_date("2026-06-03")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_coverage_and_add_alpha(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            dates = [
                "2026-06-01",
                "2026-06-02",
                "2026-06-03",
                "2026-06-04",
                "2026-06-05",
                "2026-06-06",
                "2026-06-09",
            ]
            bench = {d: 100.0 + i for i, d in enumerate(dates)}
            _seed_benchmark(conn, bench)
            _seed_prices(
                conn,
                "2330",
                {
                    "2026-06-02": 100.0,
                    "2026-06-03": 110.0,
                    "2026-06-04": 105.0,
                    "2026-06-05": 108.0,
                    "2026-06-06": 112.0,
                    "2026-06-09": 115.0,
                },
            )
            _seed_prices(
                conn,
                "2317",
                {
                    "2026-06-02": 50.0,
                    "2026-06-03": 52.0,
                    "2026-06-04": 51.0,
                    "2026-06-05": 53.0,
                    "2026-06-06": 54.0,
                    "2026-06-09": 55.0,
                },
            )
            upsert_stock_beta(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "name": "台積電",
                        "market": "TSE",
                        "beta": 1.0,
                        "beta_window": "250d",
                        "benchmark": "^TWII",
                        "source": "yahoo_computed",
                        "as_of_date": "2026-06-02",
                    }
                ],
            )
            upsert_flow_events(
                conn,
                [
                    {
                        "event_date": "2026-06-02",
                        "prev_date": "2026-06-01",
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "net_side": "add",
                        "consensus": "STRONG",
                        "intent": "BUILD_THEMATIC",
                        "conviction": 80.0,
                        "implied_flow_ntd": 1e6,
                        "etf_count": 2,
                        "source_etfs": "00929|00940",
                        "flow_version": "flow-v1",
                    }
                ],
            )
            result = run_flow_attribution(
                conn, as_of="2026-06-09", lookback=5, flow_version="flow-v1"
            )
            self.assertIsNone(result.message)
            h1 = next(c for c in result.coverage if c.horizon == 1)
            self.assertEqual(h1.expected, 1)
            self.assertEqual(h1.available, 1)
            add_h1 = next(
                g
                for g in result.groups_net_side
                if g.label == "add" and g.horizon == 1
            )
            self.assertEqual(add_h1.n, 1)
            self.assertAlmostEqual(add_h1.mean_capm or 0.0, 9.0, places=1)
            rand_h1 = next(g for g in result.random_baseline if g.horizon == 1)
            self.assertGreaterEqual(rand_h1.n, 1)
            r2 = run_flow_attribution(
                conn, as_of="2026-06-09", lookback=5, flow_version="flow-v1"
            )
            r2_rand = next(g for g in r2.random_baseline if g.horizon == 1)
            self.assertEqual(rand_h1.mean_capm, r2_rand.mean_capm)
            self.assertIn("H+3", result.boss_gate)
        finally:
            conn.close()
            tmp.cleanup()

    def test_baseline_seed_constant(self) -> None:
        self.assertEqual(BASELINE_RANDOM_SEED, 42)


if __name__ == "__main__":
    unittest.main()
