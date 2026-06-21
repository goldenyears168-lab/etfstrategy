"""sync_flow_event_legs + flow_returns enrichment。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from signal_engine import ChangeLeg
from stock_db import connect, load_flow_event_legs, upsert_etf_holdings, upsert_etf_holdings_meta
from sync_flow_event_legs import enrich_flow_event_legs, flow_leg_rows_from_signals


def _bar(stock_id: str, d: str, close: float) -> dict:
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


class TestFlowEventLegs(unittest.TestCase):
    def test_enrich_returns(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            from stock_db import upsert_daily_bars, upsert_stock_daily_bars

            dates = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-06"]
            upsert_daily_bars(
                conn,
                [
                    {
                        "code": "IX0001",
                        "date": d,
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0 + i,
                        "volume": 0,
                        "spread": None,
                        "source": "tej",
                    }
                    for i, d in enumerate(dates)
                ],
            )
            upsert_stock_daily_bars(
                conn,
                [_bar("2330", d, 100.0 + i * 2) for i, d in enumerate(dates)],
            )
            rows = flow_leg_rows_from_signals(
                prev_date="2026-06-01",
                event_date="2026-06-02",
                legs=[
                    ChangeLeg(
                        stock_id="2330",
                        stock_name="台積電",
                        etf_code="00981A",
                        action="加码",
                        share_delta=100.0,
                        weight_pct_prev=1.0,
                        weight_pct_curr=2.0,
                        weight_delta_pp=1.0,
                        share_growth_pct=10.0,
                        flow_ntd=100_000.0,
                        weight_rank=1,
                        in_top5=True,
                        in_top_decile=True,
                        theme="AI_SEMIS",
                    )
                ],
            )
            from stock_db import upsert_flow_event_legs

            upsert_flow_event_legs(conn, rows)
            n = enrich_flow_event_legs(conn)
            self.assertEqual(n, 1)
            loaded = load_flow_event_legs(
                conn, flow_version="flow-v1", event_dates=["2026-06-02"]
            )
            self.assertEqual(len(loaded), 1)
            self.assertIsNotNone(loaded[0]["return_after_1d"])
            self.assertIsNotNone(loaded[0]["alpha_after_1d"])
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
