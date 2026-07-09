"""Tests for ABC v3 trade quality profiling."""

from __future__ import annotations

import unittest

from research.backtest.archive.abc_v3_trade_quality import (
    _finmind_factor_specs,
    backtest_filter_combo,
    classify_trade_tiers,
    flatten_abc_v3_candidates,
    pit_as_of_date,
    search_half_filters,
    validate_filter_oos,
)


def _fake_trade(net: float, **extra) -> dict:
    return {
        "stock_id": extra.get("stock_id", "2330"),
        "entry_date": "2026-06-01",
        "net_pct": net,
        "outcome": "win" if net > 0 else "loss",
        **extra,
    }


class TestAbcV3TradeQuality(unittest.TestCase):
    def test_flatten_candidates(self) -> None:
        rows = flatten_abc_v3_candidates(
            [{"stock_id": "2330", "net_pct": 3.0, "features": {"w20_rv": 102.0}}]
        )
        self.assertEqual(rows[0]["outcome"], "win")
        self.assertEqual(rows[0]["w20_rv"], 102.0)
        self.assertEqual(rows[0]["daily_ret_net_pct"], 1.0)

    def test_classify_tiers(self) -> None:
        trades = [_fake_trade(float(i - 5)) for i in range(10)]
        tiers = classify_trade_tiers(trades)
        self.assertEqual(tiers["n"], 10)
        self.assertEqual(tiers["good_n"], 3)
        self.assertEqual(tiers["bad_n"], 4)
        self.assertGreater(float(tiers["good_mean_ret"] or 0), float(tiers["bad_mean_ret"] or 0))

    def test_half_filter_search(self) -> None:
        trades = []
        for i in range(100):
            flag = i % 2 == 0
            net = 3.0 if flag else -1.0
            trades.append(_fake_trade(net, even=flag))
        specs = [("even_flag", "even", lambda r: bool(r.get("even")))]
        found = search_half_filters(trades, specs, keep_lo=0.45, keep_hi=0.55, min_kept=40)
        self.assertTrue(found)
        self.assertAlmostEqual(found[0]["keep_pct"], 50.0)
        self.assertGreater(found[0]["mean_ret_kept"], found[0]["mean_ret_drop"])

    def test_filter_combo(self) -> None:
        trades = [_fake_trade(2.0, a=True), _fake_trade(-2.0, a=False)]
        specs = [("a_true", "A", lambda r: bool(r.get("a")))]
        combo = backtest_filter_combo(trades, specs, ["a_true"])
        self.assertEqual(combo["n_kept"], 1)
        self.assertEqual(combo["kept"]["n"], 1)

    def test_finmind_specs_non_empty(self) -> None:
        self.assertGreater(len(_finmind_factor_specs()), 10)

    def test_pit_as_of_date_lags_one_day(self) -> None:
        cal = ["2026-06-01", "2026-06-02", "2026-06-03"]
        self.assertEqual(pit_as_of_date("2026-06-03", cal), "2026-06-02")
        self.assertIsNone(pit_as_of_date("2026-06-01", cal))

    def test_validate_filter_oos_focus(self) -> None:
        # train days keep the "good" flag helpful; val days flip it (unstable).
        trades = []
        for d in range(60):  # train window
            date = f"2026-03-{(d % 28) + 1:02d}"
            trades.append(_fake_trade(3.0 if d % 2 == 0 else -1.0, entry_date=date, g=(d % 2 == 0)))
        for d in range(30):  # val window (later dates), relationship reversed
            date = f"2026-06-{(d % 28) + 1:02d}"
            trades.append(_fake_trade(-1.0 if d % 2 == 0 else 3.0, entry_date=date, g=(d % 2 == 0)))
        specs = [("g_true", "G", lambda r: bool(r.get("g")))]
        res = validate_filter_oos(
            trades, specs, train_days=28, val_days=28, min_n=5,
            focus_filters=[("g_true", "keep_pos")],
        )
        self.assertEqual(len(res["focus_eval"]), 1)
        fe = res["focus_eval"][0]
        self.assertIn("g_true", fe["filter_ids"])
        # keep_pos on G is good in train but reversed in val → should fail OOS.
        self.assertFalse(fe["val_passed"])


if __name__ == "__main__":
    unittest.main()
