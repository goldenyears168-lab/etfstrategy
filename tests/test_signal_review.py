"""signal_review：T+1 alpha、IC、分桶、Paper P&L。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market_labels import PM_AVOID, PM_BREAKOUT, PM_OBSERVE
from project_config import DEFAULT_FLOW_EVENT_LOOKBACK
from signal_review import (
    OutcomeRow,
    aggregate_bucket_stats,
    build_report_text,
    capm_alpha_pct,
    compute_horizon_cell,
    compute_paper_day,
    compute_paper_horizon_row,
    load_review_result,
    persist_review_result,
    render_report_for_run,
    return_pct,
    run_review,
    spearman_correlation,
)
from stock_db import (
    connect,
    upsert_daily_bars,
    upsert_pm_watchlist,
    upsert_portfolio_weights,
    upsert_stock_beta,
    upsert_stock_daily_bars,
)


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


def _seed_prices(
    conn,
    stock_id: str,
    prices: dict[str, float],
) -> None:
    upsert_stock_daily_bars(
        conn,
        [_stock_bar_row(stock_id, d, c) for d, c in sorted(prices.items())],
    )


def _bar_row(code: str, d: str, close: float) -> dict:
    return {
        "code": code,
        "date": d,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 0,
        "spread": None,
        "source": "tej",
    }


def _seed_benchmark(conn, prices: dict[str, float]) -> None:
    upsert_daily_bars(
        conn,
        [_bar_row("IX0001", d, c) for d, c in sorted(prices.items())],
    )


def _seed_beta(conn, stock_id: str, beta: float, as_of: str = "2026-06-01") -> None:
    upsert_stock_beta(
        conn,
        [
            {
                "stock_id": stock_id,
                "name": stock_id,
                "market": "TSE",
                "beta": beta,
                "beta_window": "250d",
                "benchmark": "^TWII",
                "source": "yahoo_computed",
                "as_of_date": as_of,
            }
        ],
    )


class TestSignalReviewMath(unittest.TestCase):
    def test_return_pct(self) -> None:
        self.assertAlmostEqual(return_pct(100.0, 110.0), 10.0)

    def test_capm_alpha_pct(self) -> None:
        # R=15%, Rm=10%, β=1.5 → α=0%
        self.assertAlmostEqual(capm_alpha_pct(15.0, 10.0, 1.5), 0.0)
        # R=-5%, Rm=-10%, β=1 → α=+5%
        self.assertAlmostEqual(capm_alpha_pct(-5.0, -10.0, 1.0), 5.0)

    def test_spearman_positive(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        ic = spearman_correlation(xs, ys)
        self.assertIsNotNone(ic)
        assert ic is not None
        self.assertAlmostEqual(ic, 1.0, places=5)

    def test_aggregate_bucket_stats(self) -> None:
        outcomes = [
            OutcomeRow(
                "2330", "台積電", "2026-06-03", "2026-06-04",
                PM_BREAKOUT, "突破", "法人中性", 80.0,
                2.0, 0.5, 1.5, 1.5, 1.0,
            ),
            OutcomeRow(
                "2454", "聯發科", "2026-06-03", "2026-06-04",
                PM_AVOID, "觀望", "法人中性", 50.0,
                -1.0, 0.5, -1.5, -1.5, 1.0,
            ),
        ]
        stats = aggregate_bucket_stats(outcomes)
        by_bucket = {s.bucket: s for s in stats}
        self.assertEqual(by_bucket[PM_BREAKOUT].n, 1)
        self.assertAlmostEqual(by_bucket[PM_BREAKOUT].mean_alpha or 0, 1.5)
        self.assertEqual(by_bucket[PM_AVOID].n, 1)


class TestSignalReviewIntegration(unittest.TestCase):
    def test_run_review_paper_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            t, t1 = "2026-06-03", "2026-06-04"
            _seed_benchmark(conn, {t: 1000.0, t1: 1010.0})
            _seed_prices(conn, "2330", {t: 100.0, t1: 105.0})
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "investment_score": 75.0,
                        "watchlist": "首要觀察",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "chip_tag": "法人中性",
                        "pm_bucket": PM_BREAKOUT,
                        "flow_score": 70.0,
                        "chip_score": 70.0,
                        "tech_score": 70.0,
                        "catalyst_score": 50.0,
                        "fundamental_score": 50.0,
                        "note": "",
                    }
                ],
            )
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            result = run_review(
                conn,
                as_of=t1,
                lookback=7,
                score_version="p4-v2",
                capital_ntd=100_000.0,
            )
            conn.close()

        self.assertEqual(len(result.signal_dates), 1)
        self.assertEqual(len(result.outcomes), 1)
        self.assertAlmostEqual(result.outcomes[0].alpha_pct, 4.0)
        self.assertEqual(len(result.paper_days), 1)
        paper = result.paper_days[0]
        self.assertEqual(paper.status, "complete")
        self.assertAlmostEqual(paper.pnl_ntd, 2000.0)
        self.assertAlmostEqual(paper.bench_return_pct, 1.0)
        self.assertAlmostEqual(paper.capm_alpha_ntd, paper.alpha_ntd)

    def test_paper_capm_alpha_with_high_beta(self) -> None:
        """β=1.5、個股+5%、大盤+1% → raw excess 有值、CAPM α 較小。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t, t1 = "2026-06-03", "2026-06-04"
            _seed_benchmark(conn, {t: 1000.0, t1: 1010.0})
            _seed_prices(conn, "2330", {t: 100.0, t1: 105.0})
            _seed_beta(conn, "2330", 1.5)
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 100.0,
                        "suggested_ntd": 100_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            paper = compute_paper_day(
                conn, t, t1, 1.0, score_version="p4-v2", capital_ntd=100_000.0
            )
            conn.close()

        self.assertEqual(paper.status, "complete")
        self.assertAlmostEqual(paper.portfolio_beta, 1.5)
        self.assertAlmostEqual(paper.alpha_ntd, 4000.0)  # 5k pnl - 1k bench@β=1
        self.assertAlmostEqual(paper.capm_alpha_ntd, 3500.0)  # 5k - 1.5k bench@β=1.5

    def test_compute_paper_day_skip_without_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            row = compute_paper_day(
                conn, "2026-06-03", "2026-06-04", 1.0, score_version="p4-v2", capital_ntd=100_000.0
            )
            conn.close()
        self.assertEqual(row.status, "skip_no_weights")

    def test_horizon_curve_h1_h2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t0, t1, t2 = "2026-06-03", "2026-06-04", "2026-06-05"
            _seed_benchmark(conn, {t0: 1000.0, t1: 1010.0, t2: 1020.0})
            _seed_prices(conn, "2330", {t0: 100.0, t1: 105.0, t2: 110.0})
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            row = compute_paper_horizon_row(
                conn, t0, score_version="p4-v2", capital_ntd=100_000.0, horizons=(1, 2)
            )
            conn.close()

        self.assertEqual(row.deployed_ntd, 40_000.0)
        by_h = {c.horizon: c for c in row.cells}
        self.assertEqual(by_h[1].status, "complete")
        self.assertAlmostEqual(by_h[1].pnl_ntd or 0, 2000.0)
        self.assertAlmostEqual(by_h[1].return_pct or 0, 5.0)
        self.assertEqual(by_h[2].status, "complete")
        self.assertAlmostEqual(by_h[2].pnl_ntd or 0, 4000.0)
        self.assertAlmostEqual(by_h[2].return_pct or 0, 10.0)

    def test_horizon_h1_matches_paper_day(self) -> None:
        """§2c H+1 應與 §2b 同日 compute_paper_day 一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t0, t1 = "2026-06-03", "2026-06-04"
            _seed_benchmark(conn, {t0: 1000.0, t1: 1010.0})
            _seed_prices(conn, "2330", {t0: 100.0, t1: 105.0})
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            paper = compute_paper_day(
                conn, t0, t1, 1.0, score_version="p4-v2", capital_ntd=100_000.0
            )
            h1 = compute_horizon_cell(
                conn, t0, 1, score_version="p4-v2", capital_ntd=100_000.0
            )
            conn.close()

        self.assertEqual(paper.status, "complete")
        self.assertEqual(h1.status, "complete")
        self.assertAlmostEqual(paper.pnl_ntd or 0, h1.pnl_ntd or 0)
        self.assertAlmostEqual(paper.day_return_pct or 0, h1.return_pct or 0)

    def test_horizon_h5_missing_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t0, t1 = "2026-06-03", "2026-06-04"
            _seed_benchmark(conn, {t0: 1000.0, t1: 1010.0})
            _seed_prices(conn, "2330", {t0: 100.0, t1: 105.0})
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            cell = compute_horizon_cell(
                conn, t0, 5, score_version="p4-v2", capital_ntd=100_000.0
            )
            conn.close()
        self.assertEqual(cell.status, "skip_no_date")


class TestFormatReport(unittest.TestCase):
    def test_section6_placeholder_when_no_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t0 = "2026-06-04"
            _seed_benchmark(conn, {t0: 1000.0})
            _seed_prices(conn, "2330", {t0: 100.0})
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "investment_score": 75.0,
                        "watchlist": "首要觀察",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "chip_tag": "法人中性",
                        "pm_bucket": PM_BREAKOUT,
                        "flow_score": 70.0,
                        "chip_score": 70.0,
                        "tech_score": 70.0,
                        "catalyst_score": 50.0,
                        "fundamental_score": 50.0,
                        "note": "",
                    }
                ],
            )
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            result = run_review(conn, as_of=t0, lookback=7, score_version="p4-v2")
            text = build_report_text(conn=conn, result=result, score_version="p4-v2")
            conn.close()

        self.assertIn("## §6 異常個案（Top ±CAPM α）", text)
        self.assertIn("（無 complete outcome）", text)

    def test_section2b_lists_signal_day_as_skip_without_t1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t0 = "2026-06-04"
            _seed_benchmark(conn, {t0: 1000.0})
            _seed_prices(conn, "2330", {t0: 100.0})
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "investment_score": 75.0,
                        "watchlist": "首要觀察",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "chip_tag": "法人中性",
                        "pm_bucket": PM_BREAKOUT,
                        "flow_score": 70.0,
                        "chip_score": 70.0,
                        "tech_score": 70.0,
                        "catalyst_score": 50.0,
                        "fundamental_score": 50.0,
                        "note": "",
                    }
                ],
            )
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t0,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            result = run_review(conn, as_of=t0, lookback=7, score_version="p4-v2")
            text = build_report_text(conn=conn, result=result, score_version="p4-v2")
            conn.close()

        self.assertIn("## §2b Paper Portfolio", text)
        self.assertIn(f"| {t0} | 40,000 | — | — | — | — | — | skip |", text)
        self.assertIn("Mean CAPM α", text)
        self.assertIn("CAPM α", text)

    def test_persist_and_render_from_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            t, t1 = "2026-06-03", "2026-06-04"
            _seed_benchmark(conn, {t: 1000.0, t1: 1010.0})
            _seed_prices(conn, "2330", {t: 100.0, t1: 105.0})
            upsert_pm_watchlist(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "investment_score": 75.0,
                        "watchlist": "首要觀察",
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "chip_tag": "法人中性",
                        "pm_bucket": PM_BREAKOUT,
                        "flow_score": 70.0,
                        "chip_score": 70.0,
                        "tech_score": 70.0,
                        "catalyst_score": 50.0,
                        "fundamental_score": 50.0,
                        "note": "",
                    }
                ],
            )
            upsert_portfolio_weights(
                conn,
                [
                    {
                        "stock_id": "2330",
                        "as_of_date": t,
                        "score_version": "p4-v2",
                        "stock_name": "台積電",
                        "watchlist": "首要觀察",
                        "position_score": 75.0,
                        "risk_score": 30.0,
                        "portfolio_weight_pct": 40.0,
                        "suggested_ntd": 40_000.0,
                        "capital_ntd": 100_000.0,
                        "entry_signal": "突破",
                        "entry_tags_json": "[]",
                        "pm_bucket": PM_OBSERVE,
                        "note": "",
                    }
                ],
            )
            result = run_review(
                conn,
                as_of=t1,
                lookback=7,
                score_version="p4-v2",
                capital_ntd=100_000.0,
            )
            direct_text = build_report_text(
                result,
                conn,
                score_version="p4-v2",
                capital_ntd=100_000.0,
                lookback_event_days=DEFAULT_FLOW_EVENT_LOOKBACK,
            )
            run_id = persist_review_result(
                conn,
                result,
                review_date="2026-06-05",
                score_version="p4-v2",
                capital_ntd=100_000.0,
                lookback_trading_days=7,
                lookback_event_days=DEFAULT_FLOW_EVENT_LOOKBACK,
            )
            loaded = load_review_result(conn, run_id)
            db_text = render_report_for_run(conn, run_id)
            conn.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(len(loaded.outcomes), len(result.outcomes))
        self.assertAlmostEqual(loaded.outcomes[0].capm_alpha_pct, result.outcomes[0].capm_alpha_pct)
        self.assertEqual(direct_text, db_text)


if __name__ == "__main__":
    unittest.main()
