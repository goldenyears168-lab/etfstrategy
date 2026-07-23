"""Tests for C18acc entry win/loss overlay OOS holdout."""

from research.backtest.archive.c18acc_entry_winloss_oos_overlay import (
    _adoption_verdict,
    _overlay_results,
    _summarize_legs,
)


def _leg(entry: str, ret: float, *, spread: str = "aligned", w5: str = "leading") -> dict:
    return {
        "entry_date": entry,
        "return_pct": ret,
        "excess_pct": ret - 1.0,
        "spread_class": spread,
        "w5_quadrant": w5,
    }


def test_summarize_legs_empty():
    assert _summarize_legs([])["n"] == 0


def test_overlay_avoid_mixed_splits():
    legs = [
        _leg("2025-06-01", 10.0, spread="mixed"),
        _leg("2025-06-02", 5.0, spread="aligned"),
        _leg("2026-01-05", 8.0, spread="mixed"),
        _leg("2026-02-01", 12.0, spread="pullback"),
    ]
    rows = _overlay_results(
        legs,
        date_start="2025-01-01",
        date_end="2026-12-31",
        is_end="2025-12-31",
        oos_start="2026-01-01",
    )
    baseline = next(r for r in rows if r["variant_id"] == "baseline")
    avoid = next(r for r in rows if r["variant_id"] == "avoid_mixed")
    assert baseline["slices"]["full"]["n"] == 4
    assert avoid["slices"]["full"]["n"] == 2
    assert avoid["slices"]["oos"]["n"] == 1


def test_adoption_verdict_rejects_small_oos_n():
    variants = [
        {
            "variant_id": "baseline",
            "slices": {"is": {"mean_excess_pct": 5.0}, "oos": {"n": 10, "mean_excess_pct": 6.0}},
        },
        {
            "variant_id": "prefer_w5_weakening",
            "label": "prefer w5_weakening",
            "delta_oos_excess_vs_baseline_pp": 2.0,
            "slices": {"is": {"mean_excess_pct": 5.5}, "oos": {"n": 3, "mean_excess_pct": 8.0}},
        },
    ]
    v = _adoption_verdict(variants, min_oos_n=8)
    assert v["recommend_adopt_any"] is False
