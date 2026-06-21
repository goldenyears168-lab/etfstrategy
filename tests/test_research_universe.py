"""research_universe：Money Flow Top N。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_universe import build_research_universe, smart_money_score
from signal_engine import ChangeLeg, StockSignal
from stock_db import connect, upsert_etf_holdings, upsert_etf_holdings_meta


def _signal(
    stock_id: str,
    *,
    net_side: str = "add",
    consensus: float = 2.0,
    conviction: float = 1.5,
    weight_delta: float = 0.5,
    flow: float | None = 1e9,
) -> StockSignal:
    leg = ChangeLeg(
        stock_id=stock_id,
        stock_name=stock_id,
        etf_code="00981A",
        action="加码",
        share_delta=1000.0,
        weight_pct_prev=5.0,
        weight_pct_curr=5.5,
        weight_delta_pp=weight_delta,
        share_growth_pct=10.0,
        flow_ntd=flow,
        weight_rank=1,
        in_top5=True,
        in_top_decile=True,
        theme="semiconductor",
    )
    return StockSignal(
        stock_id=stock_id,
        stock_name=stock_id,
        theme="semiconductor",
        legs=[leg],
        net_side=net_side,
        weight_delta_pp_max=weight_delta,
        flow_ntd_total=flow,
        consensus_score=consensus,
        conviction_score=conviction,
    )


class TestResearchUniverse(unittest.TestCase):
    def test_smart_money_prefers_add_consensus(self) -> None:
        high = smart_money_score(_signal("2330", consensus=3.0, conviction=2.0))
        low = smart_money_score(
            _signal("2376", consensus=0.5, conviction=0.2, weight_delta=0.05, flow=1e7)
        )
        skip = smart_money_score(_signal("9999", net_side="reduce"))
        self.assertGreater(high, low)
        self.assertEqual(skip, float("-inf"))

    def test_money_flow_universe(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "t.db"
        conn = connect(db_path)
        snap = "2026-06-01"
        synced = "2026-06-01T00:00:00+00:00"
        prev, curr = "2026-05-28", snap
        for etf in ("00981A", "00982A"):
            for d in (prev, curr):
                upsert_etf_holdings_meta(
                    conn,
                    {
                        "etf_code": etf,
                        "snapshot_date": d,
                        "nav": 100.0,
                        "holding_count": 2,
                        "source": "t",
                        "source_edit_at": None,
                    },
                )
            upsert_etf_holdings(
                conn,
                [
                    {
                        "etf_code": etf,
                        "snapshot_date": prev,
                        "stock_id": "2376",
                        "stock_name": "2376",
                        "shares": 500.0,
                        "weight_pct": 8.0,
                        "amount": None,
                        "source": "t",
                        "source_edit_at": None,
                        "synced_at": synced,
                    },
                    {
                        "etf_code": etf,
                        "snapshot_date": curr,
                        "stock_id": "2376",
                        "stock_name": "2376",
                        "shares": 2000.0,
                        "weight_pct": 12.0,
                        "amount": None,
                        "source": "t",
                        "source_edit_at": None,
                        "synced_at": synced,
                    },
                    {
                        "etf_code": etf,
                        "snapshot_date": curr,
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "shares": 100.0,
                        "weight_pct": 0.2,
                        "amount": None,
                        "source": "t",
                        "source_edit_at": None,
                        "synced_at": synced,
                    },
                ],
            )
        result = build_research_universe(
            conn,
            ("00981A", "00982A"),
            top_n=5,
            max_pool=20,
        )
        conn.close()
        tmp.cleanup()
        self.assertIsNotNone(result)
        assert result is not None
        ids = {e.stock_id for e in result.entries}
        self.assertIn("2376", ids)
        self.assertTrue(all(e.pool_reason == "money" for e in result.entries))


if __name__ == "__main__":
    unittest.main()
