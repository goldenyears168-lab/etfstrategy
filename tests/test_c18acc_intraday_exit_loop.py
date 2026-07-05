"""Unit tests · C18acc intraday exit loop helpers."""

from dataclasses import replace

from research.backtest.c18acc_intraday_exit_loop import (
    exit_loop_sweep_configs,
    intraday_quad_matches,
    intraday_swap_margin_ok,
    try_intraday_swap_repick,
    _prior_trade_date,
)
from research.backtest.rrg_mono_score_swap_c import ScoreSwapCConfig
from rrg_mono_daily_brief import ScanRow


def test_intraday_quad_matches_weakening():
    assert intraday_quad_matches("weakening", "Weakening")
    assert not intraday_quad_matches("weakening", "leading")


def test_intraday_quad_matches_weakening_or_lagging():
    assert intraday_quad_matches("weakening_or_lagging", "lagging")
    assert not intraday_quad_matches("weakening_or_lagging", "improving")


def test_prior_trade_date():
    dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert _prior_trade_date(dates, "2025-01-03") == "2025-01-02"
    assert _prior_trade_date(dates, "2025-01-02") is None


def test_intraday_swap_margin_ok():
    sell = {"seg_last": 1.0}
    buy = ScanRow(
        stock_id="1234",
        stock_name="t",
        fresh=True,
        mono=True,
        seg_last=1.2,
        disp=1.0,
        segs=[],
        quadrants=[],
        rs_ratio=100.0,
        rs_momentum=100.0,
        daily_pct=None,
    )
    class Cfg:
        effective_margin = 0.1

    assert intraday_swap_margin_ok(
        sell=sell,
        buy=buy,
        sell_px=100.0,
        buy_px=100.0,
        close_s=100.0,
        close_b=100.0,
        config=Cfg(),  # type: ignore[arg-type]
    )
    assert not intraday_swap_margin_ok(
        sell=sell,
        buy=buy,
        sell_px=100.0,
        buy_px=50.0,
        close_s=100.0,
        close_b=100.0,
        config=Cfg(),  # type: ignore[arg-type]
    )


def test_exit_loop_sweep_configs_xel3():
    ids = {c.variant_id for c in exit_loop_sweep_configs()}
    assert "XEL-3" in ids
    x3 = next(c for c in exit_loop_sweep_configs() if c.variant_id == "XEL-3")
    assert x3.exit_loop_mode == "intraday_loop_phase3"
    assert x3.hard_stop_intraday is True
    assert x3.sort_key == "seg_last"


def test_try_intraday_swap_repick_requires_seg_last():
    cfg = replace(
        ScoreSwapCConfig("t", "t"),
        swap_target="worst_held",
        sort_key="avg_accel_decel",
    )
    out = try_intraday_swap_repick(
        None,  # type: ignore[arg-type]
        slots=[{"stock_id": "1", "seg_last": 1.0, "entry_date": "2025-01-02"}],
        swap_shortlist=[],
        held_ids=set(),
        close=None,  # type: ignore[arg-type]
        as_of="2025-01-03",
        config=cfg,
        kbar_cache={},
        full_dates=["2025-01-02", "2025-01-03"],
    )
    assert out == (None, None, None, None, None, None)
