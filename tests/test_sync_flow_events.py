"""sync_flow_events：source_etfs 與 flow_events 落地。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from signal_engine import ChangeLeg, StockSignal
from stock_db import connect, load_flow_events, upsert_flow_events
from sync_flow_events import flow_rows_from_signals


def _leg(etf_code: str, action: str) -> ChangeLeg:
    return ChangeLeg(
        stock_id="2330",
        stock_name="台積電",
        etf_code=etf_code,
        action=action,
        share_delta=1000.0,
        weight_pct_prev=1.0,
        weight_pct_curr=2.0,
        weight_delta_pp=1.0,
        share_growth_pct=10.0,
        flow_ntd=1_000_000.0,
        weight_rank=1,
        in_top5=True,
        in_top_decile=True,
        theme="SEMICON",
    )


class TestFlowRowsFromSignals(unittest.TestCase):
    def test_source_etfs_add_side(self) -> None:
        sig = StockSignal(
            stock_id="2330",
            stock_name="台積電",
            theme="SEMICON",
            legs=[
                _leg("00929", "加码"),
                _leg("00940", "新进"),
                _leg("00940", "减码"),
            ],
            net_side="add",
            flow_ntd_total=2_000_000.0,
            conviction_score=72.0,
            consensus_level="STRONG",
            position_intent="BUILD_THEMATIC",
        )
        rows = flow_rows_from_signals(
            prev_date="2026-06-01",
            event_date="2026-06-02",
            signals=[sig],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_etfs"], "00929|00940")
        self.assertEqual(rows[0]["etf_count"], 2)
        self.assertEqual(rows[0]["intent"], "BUILD_THEMATIC")

    def test_upsert_and_load_roundtrip(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "t.db")
        try:
            row = {
                "event_date": "2026-06-02",
                "prev_date": "2026-06-01",
                "stock_id": "2330",
                "stock_name": "台積電",
                "net_side": "add",
                "consensus": "STRONG",
                "intent": "BUILD_THEMATIC",
                "conviction": 72.0,
                "implied_flow_ntd": 1_000_000.0,
                "etf_count": 2,
                "source_etfs": "00929|00940",
                "flow_version": "flow-v1",
            }
            n = upsert_flow_events(conn, [row])
            self.assertEqual(n, 1)
            loaded = load_flow_events(
                conn, flow_version="flow-v1", event_dates=["2026-06-02"]
            )
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["source_etfs"], "00929|00940")
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
