"""P1：event_ranking + research_universe 合併邏輯。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from event_ranking import CatalystEvent, load_manual_events, rank_events, score_event
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
    sig = StockSignal(
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
    return sig


class TestEventRanking(unittest.TestCase):
    def test_score_recency(self) -> None:
        today = date.today()
        ev = CatalystEvent(
            stock_id="2330",
            event_date=today,
            catalyst_type="EARNINGS",
            headline="test",
            confidence=80,
            explains_etf_add="HIGH",
        )
        old = CatalystEvent(
            stock_id="2330",
            event_date=today - timedelta(days=10),
            catalyst_type="EARNINGS",
            headline="old",
            confidence=80,
        )
        self.assertGreater(score_event(ev), score_event(old))

    def test_rank_picks_best_per_stock(self) -> None:
        today = date.today()
        events = [
            CatalystEvent("2330", today, "EARNINGS", "a", confidence=60),
            CatalystEvent("2330", today, "CAPX", "b", confidence=90, explains_etf_add="HIGH"),
            CatalystEvent("2317", today, "POLICY", "c", confidence=70),
        ]
        ranked = rank_events(events, top_n=2, pool_stock_ids={"2330", "2317"})
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].stock_id, "2330")
        self.assertEqual(ranked[0].event.headline, "b")


class TestResearchUniverse(unittest.TestCase):
    def test_smart_money_prefers_add_consensus(self) -> None:
        high = smart_money_score(_signal("2330", consensus=3.0, conviction=2.0))
        low = smart_money_score(
            _signal("2376", consensus=0.5, conviction=0.2, weight_delta=0.05, flow=1e7)
        )
        skip = smart_money_score(_signal("9999", net_side="reduce"))
        self.assertGreater(high, low)
        self.assertEqual(skip, float("-inf"))

    def test_event_channel_brings_low_flow_stock(self) -> None:
        """2330 低 MF 分仍可經 Event 進池（PRD 驗收）。"""
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "t.db"
        events_file = Path(tmp.name) / "events.json"
        events_file.write_text(
            """
            {"events": [{
              "stock_id": "2330",
              "event_date": "%s",
              "catalyst_type": "EARNINGS",
              "headline": "CoWoS 法說",
              "confidence": 95,
              "explains_etf_add": "HIGH"
            }]}
            """
            % date.today().isoformat(),
            encoding="utf-8",
        )
        conn = connect(db_path)
        snap = "2026-06-01"
        synced = "2026-06-01T00:00:00+00:00"
        for etf in ("00981A", "00982A"):
            upsert_etf_holdings_meta(
                conn,
                {
                    "etf_code": etf,
                    "snapshot_date": snap,
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
                        "snapshot_date": snap,
                        "stock_id": "2376",
                        "stock_name": "2376",
                        "shares": 1000.0,
                        "weight_pct": 10.0,
                        "amount": None,
                        "source": "t",
                        "source_edit_at": None,
                        "synced_at": synced,
                    }
                ],
            )
        prev, curr = "2026-05-28", snap
        for etf in ("00981A", "00982A"):
            upsert_etf_holdings_meta(
                conn,
                {
                    "etf_code": etf,
                    "snapshot_date": prev,
                    "nav": 100.0,
                    "holding_count": 1,
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
                    }
                ],
            )
            upsert_etf_holdings(
                conn,
                [
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
            events_path=events_file,
        )
        conn.close()
        tmp.cleanup()
        self.assertIsNotNone(result)
        assert result is not None
        ids = {e.stock_id for e in result.entries}
        self.assertIn("2330", ids)
        tsm = next(e for e in result.entries if e.stock_id == "2330")
        self.assertIn(tsm.pool_reason, ("event", "both"))


if __name__ == "__main__":
    unittest.main()
