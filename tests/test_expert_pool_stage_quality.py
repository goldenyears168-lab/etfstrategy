"""Unit tests for expert_pool_stage_quality annotation."""

from __future__ import annotations

import unittest

from research.expert_pool_stage_quality import (
    detect_stage_quality,
    format_stage_quality_lines,
)


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    """Minimal conn.execute(...).fetchall() stub over stock_daily_bars rows."""

    def __init__(self, bars: list[tuple]) -> None:
        self._bars = bars

    def execute(self, _sql: str, _params: tuple) -> _FakeCursor:
        return _FakeCursor(self._bars)


def _bars(n: int, *, start: float, step: float) -> list[tuple]:
    """(trade_date, open, high, low, close) newest-first (matches DESC query)."""
    import pandas as pd

    dates = pd.bdate_range("2022-01-03", periods=n)
    rows = []
    for i, d in enumerate(dates):
        close = start + i * step
        rows.append((d.date().isoformat(), close, close + 1.0, close - 0.8, close))
    return list(reversed(rows))


class TestDetectStageQuality(unittest.TestCase):
    def test_stage2_confirm_on_uptrend(self) -> None:
        conn = _FakeConn(_bars(280, start=100.0, step=0.4))
        sq = detect_stage_quality(conn, stock_id="9999", asof="2022-12-31")
        self.assertIsNotNone(sq)
        self.assertEqual(sq["weinstein_stage"], 2)
        self.assertEqual(sq["ta_quality"], "S2_confirm")
        self.assertEqual(sq["follow_weight"], "full")

    def test_stage4_deweight_on_downtrend(self) -> None:
        conn = _FakeConn(_bars(280, start=200.0, step=-0.5))
        sq = detect_stage_quality(conn, stock_id="9999", asof="2022-12-31")
        self.assertIsNotNone(sq)
        self.assertIn(sq["weinstein_stage"], (3, 4))
        self.assertEqual(sq["ta_quality"], "non_S2_deweight")
        self.assertEqual(sq["follow_weight"], "deweight")

    def test_none_when_no_bars(self) -> None:
        conn = _FakeConn([])
        sq = detect_stage_quality(conn, stock_id="9999", asof="2022-12-31")
        self.assertIsNone(sq)


class TestFormatStageQualityLines(unittest.TestCase):
    def test_empty_on_none(self) -> None:
        self.assertEqual(format_stage_quality_lines(None), [])

    def test_block_on_s2_confirm(self) -> None:
        lines = format_stage_quality_lines(
            {
                "weinstein_stage": 2,
                "weinstein_stage_name": "advancing",
                "extension_pct": 5.0,
                "ta_quality": "S2_confirm",
                "follow_weight": "full",
                "asof": "2026-07-20",
                "note_zh": "技術品質（Weinstein）Stage 2（advancing）· S2_confirm（週線第2階／確認）",
                "ix_weinstein_stage": 2,
                "dual_s2_confirm": True,
            }
        )
        blob = "\n".join(lines)
        self.assertIn("觀察註解", blob)
        self.assertIn("非下單訊號", blob)
        self.assertIn("dual_S2", blob)
        self.assertIn("S2_confirm", blob)
        self.assertNotIn("畢業", blob)


if __name__ == "__main__":
    unittest.main()
