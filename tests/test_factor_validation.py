"""factor_validation · Rank IC / quantile spread (Phase 2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from research.backtest.factor_validation import (
    ICDecayConfig,
    FactorDaySlice,
    FactorValidationConfig,
    TearsheetConfig,
    TrackFactorConfig,
    compute_horizon_metrics,
    compute_ic_decay,
    load_factor_validation_config,
    validate_factor,
    write_factor_validation_reports,
)
from market_labels import PM_OBSERVE
from stock_db import connect, upsert_daily_bars, upsert_pm_watchlist, upsert_stock_daily_bars


def _stock_bar(stock_id: str, d: str, close: float) -> dict:
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


def _bench_bar(d: str, close: float) -> dict:
    return {
        "code": "IX0001",
        "date": d,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 0,
        "spread": None,
        "source": "tej",
    }


def _pm_row(
    stock_id: str,
    as_of: str,
    score: float,
    *,
    bucket: str = PM_OBSERVE,
) -> dict:
    return {
        "stock_id": stock_id,
        "as_of_date": as_of,
        "score_version": "p6-tier",
        "stock_name": stock_id,
        "investment_score": score,
        "watchlist": "首要觀察",
        "entry_signal": "突破",
        "entry_tags_json": "[]",
        "chip_tag": "法人中性",
        "pm_bucket": bucket,
        "flow_score": score,
        "chip_score": score,
        "tech_score": score,
        "catalyst_score": 50.0,
        "fundamental_score": 50.0,
        "note": "",
    }


class TestFactorValidation(unittest.TestCase):
    def test_load_config(self) -> None:
        cfg = load_factor_validation_config()
        self.assertEqual(cfg.version, "factor-validation-v2")
        self.assertGreaterEqual(len(cfg.tracks), 1)
        self.assertEqual(cfg.ic_decay.train_pct, 0.7)
        ids = {t.track_id for t in cfg.tracks}
        self.assertIn("vcp-funnel", ids)

    def test_positive_ic_when_factor_ranks_winners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            dates = [f"2026-06-{d:02d}" for d in range(3, 8)]
            bench = {d: 1000.0 for d in dates}
            bench[dates[-1]] = 1010.0
            upsert_daily_bars(conn, [_bench_bar(d, c) for d, c in bench.items()])

            stocks = ["2330", "2454", "2317", "2303", "2881", "2882", "1301", "1303"]
            for di, d in enumerate(dates[:-1]):
                rows_pm = []
                bar_rows = []
                for i, sid in enumerate(stocks):
                    score = float(i + 1)
                    fwd_close = 100.0 + score * 2.0
                    rows_pm.append(_pm_row(sid, d, score))
                    bar_rows.append(_stock_bar(sid, d, 100.0))
                    bar_rows.append(_stock_bar(sid, dates[di + 1], fwd_close))
                upsert_pm_watchlist(conn, rows_pm)
                upsert_stock_daily_bars(conn, bar_rows)

            track = TrackFactorConfig(
                track_id="p6-tier-flow",
                title="test",
                source="pm_watchlist",
                factors=("investment_score",),
                score_version="p6-tier",
            )
            cfg = FactorValidationConfig(
                version="test",
                lookback_trading_days=10,
                forward_horizons_days=(1,),
                min_names_per_day=8,
                quantile_buckets=4,
                ic_decay=ICDecayConfig(train_pct=0.6, min_split_days=4),
                tearsheet=TearsheetConfig(),
                tracks=(track,),
            )
            result, _ = validate_factor(
                conn,
                track,
                "investment_score",
                cfg=cfg,
                as_of=dates[-2],
            )
            conn.close()

        self.assertEqual(result.status, "ok")
        h1 = result.horizons[0]
        self.assertIsNotNone(h1.ic_mean)
        assert h1.ic_mean is not None
        self.assertGreater(h1.ic_mean, 0.9)
        self.assertEqual(h1.monotonicity, "遞增（支持因子）")

    def test_write_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            out = Path(tmp) / "reports"
            conn = connect(db)
            t0, t1 = "2026-06-03", "2026-06-04"
            upsert_daily_bars(conn, [_bench_bar(t0, 1000.0), _bench_bar(t1, 1010.0)])
            stocks = [f"24{i:02d}" for i in range(8)]
            pm_rows = []
            bars = []
            for i, sid in enumerate(stocks):
                score = float(i + 1)
                pm_rows.append(_pm_row(sid, t0, score))
                bars.append(_stock_bar(sid, t0, 100.0))
                bars.append(_stock_bar(sid, t1, 100.0 + score))
            upsert_pm_watchlist(conn, pm_rows)
            upsert_stock_daily_bars(conn, bars)

            mini_cfg = {
                "version": "test",
                "lookback_trading_days": 5,
                "forward_horizons_days": [1],
                "min_names_per_day": 8,
                "quantile_buckets": 4,
                "tracks": {
                    "p6-tier-flow": {
                        "title": "test",
                        "source": "pm_watchlist",
                        "score_version": "p6-tier",
                        "factors": ["investment_score"],
                    }
                },
            }
            cfg_path = Path(tmp) / "fv.yaml"
            cfg_path.write_text(yaml.dump(mini_cfg), encoding="utf-8")
            cfg = load_factor_validation_config(cfg_path)
            path = write_factor_validation_reports(
                conn, cfg=cfg, as_of=t1, reports_dir=out
            )
            conn.close()

            self.assertTrue(path.is_file())
            self.assertTrue((out / "p6-tier-flow.md").is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn("p6-tier-flow", text)

    def test_quantile_spread_on_slices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            sl = FactorDaySlice(
                as_of_date="2026-06-03",
                stock_ids=tuple(str(i) for i in range(8)),
                factor_values=tuple(float(i) for i in range(8)),
                forward_returns_pct=tuple(float(i) * 2 for i in range(8)),
            )
            m = compute_horizon_metrics(
                conn,
                [sl],
                horizon_days=1,
                quantile_buckets=4,
                ic_decay_cfg=ICDecayConfig(min_split_days=1),
            )
            conn.close()
        self.assertIsNotNone(m.quantile_spread_pct)
        assert m.quantile_spread_pct is not None
        self.assertGreater(m.quantile_spread_pct, 0)

    def test_ic_decay_detects_valid_drop(self) -> None:
        slices: list[FactorDaySlice] = []
        stocks = tuple(str(i) for i in range(8))
        for day_i in range(6):
            if day_i < 4:
                factors = tuple(float(i) for i in range(8))
                rets = tuple(float(i) * 2 for i in range(8))
            else:
                factors = tuple(float(i) for i in range(8))
                rets = tuple(float(7 - i) * 2 for i in range(8))
            slices.append(
                FactorDaySlice(
                    as_of_date=f"2026-06-{day_i+3:02d}",
                    stock_ids=stocks,
                    factor_values=factors,
                    forward_returns_pct=rets,
                )
            )
        decay = compute_ic_decay(slices, cfg=ICDecayConfig(train_pct=0.67, min_split_days=6))
        self.assertEqual(decay.verdict, "severe_decay")
        assert decay.decay_delta is not None
        self.assertLess(decay.decay_delta, -0.15)

    def test_tearsheet_html_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            out = Path(tmp) / "reports"
            conn = connect(db)
            dates = [f"2026-06-{d:02d}" for d in range(3, 10)]
            upsert_daily_bars(conn, [_bench_bar(d, 1000.0) for d in dates])
            stocks = [f"24{i:02d}" for i in range(8)]
            for di, d in enumerate(dates[:-1]):
                rows_pm = []
                bar_rows = []
                for i, sid in enumerate(stocks):
                    score = float(i + 1)
                    rows_pm.append(_pm_row(sid, d, score))
                    bar_rows.append(_stock_bar(sid, d, 100.0))
                    bar_rows.append(_stock_bar(sid, dates[di + 1], 100.0 + score))
                upsert_pm_watchlist(conn, rows_pm)
                upsert_stock_daily_bars(conn, bar_rows)

            mini_cfg = {
                "version": "test",
                "lookback_trading_days": 10,
                "forward_horizons_days": [1],
                "min_names_per_day": 8,
                "quantile_buckets": 4,
                "ic_decay": {"train_pct": 0.6, "min_split_days": 4},
                "tearsheet": {"primary_horizon_days": 1, "write_html": True},
                "tracks": {
                    "p6-tier-flow": {
                        "title": "test",
                        "source": "pm_watchlist",
                        "score_version": "p6-tier",
                        "factors": ["investment_score"],
                    }
                },
            }
            cfg_path = Path(tmp) / "fv.yaml"
            cfg_path.write_text(yaml.dump(mini_cfg), encoding="utf-8")
            cfg = load_factor_validation_config(cfg_path)
            write_factor_validation_reports(
                conn, cfg=cfg, as_of=dates[-2], reports_dir=out
            )
            conn.close()
            html_path = out / "tearsheets" / "p6-tier-flow_investment_score_T1.html"
            self.assertTrue(html_path.is_file())
            self.assertIn("create_full_tear_sheet", html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
