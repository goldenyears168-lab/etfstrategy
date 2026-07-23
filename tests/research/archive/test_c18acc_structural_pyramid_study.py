"""Tests for C18acc structural pyramid add study (RP-2 port)."""

from __future__ import annotations

import pandas as pd

from research.backtest.abc_v3_f1_structural_pyramid_study import (
    PyramidCondition,
    find_add_idx,
    simulate_add,
)
from research.backtest.archive.c18acc_structural_pyramid_study import (
    legs_from_cache,
    run_c18acc_structural_pyramid_study,
)


def _snap(**kw) -> dict:
    base = {
        "ret_from_entry_pct": 0.0,
        "trade_date": "2025-01-02",
        "minute": "10:00:00",
        "px": 100.0,
        "w5_quad": "leading",
        "dmv_w5": -0.1,
        "gap_mv_w20_w5": 5.0,
        "w3_rv": 100.0,
    }
    base.update(kw)
    return base


def test_underwater_rebound_fires_on_rv3_trough_rebound():
    t0 = _snap(ret_from_entry_pct=0.0, w3_rv=102.0, gap_mv_w20_w5=5.0)
    t1 = _snap(ret_from_entry_pct=-2.0, w3_rv=100.5)
    t2 = _snap(ret_from_entry_pct=-1.0, w3_rv=101.0)
    t3 = _snap(ret_from_entry_pct=4.0, w3_rv=101.5)
    leg = {
        "return_pct": 4.0,
        "hold_days": 5,
        "entry_t0": t0,
        "timeline": [t0, t1, t2, t3],
    }
    cond = PyramidCondition(
        id="underwater_rebound",
        label="test",
        kind="underwater_rebound",
        rebound_min=0.3,
    )
    idx = find_add_idx(leg, cond)
    assert idx == 2


def test_simulate_add_blended_sync_math():
    t0 = _snap(ret_from_entry_pct=0.0)
    t1 = _snap(ret_from_entry_pct=-3.0, px=97.0, w3_rv=101.0)
    t2 = _snap(ret_from_entry_pct=6.0, px=106.0)
    leg = {
        "return_pct": 6.0,
        "stock_id": "2330",
        "entry_date": "2025-01-02",
        "entry_t0": t0,
        "timeline": [t0, t1, t2],
    }
    row = simulate_add(leg, 1)
    assert row["leg1_ret_pct"] == 6.0
    assert row["add_ret_from_entry_pct"] == -3.0
    # leg2_sync = (1.06/0.97 - 1)*100 ≈ 9.278%
    assert abs(row["leg2_sync_ret_pct"] - 9.278) < 0.05
    assert abs(row["blended_sync_ret_pct"] - (0.5 * 6.0 + 0.5 * row["leg2_sync_ret_pct"])) < 0.01


def test_run_study_smoke(monkeypatch):
    cache = {
        "schema": "c18acc_kinematic_timeline-v1",
        "date_start": "2025-01-02",
        "date_end": "2025-06-01",
        "n_slots": 99,
        "n_legs": 2,
        "legs": [
            {
                "entry_date": "2025-01-02",
                "return_pct": 5.0,
                "hold_days": 5,
                "entry_t0": _snap(ret_from_entry_pct=0, gap_mv_w20_w5=5.0, w3_rv=102.0),
                "timeline": [
                    _snap(ret_from_entry_pct=0, w3_rv=102.0),
                    _snap(ret_from_entry_pct=-2.0, w3_rv=100.5),
                    _snap(ret_from_entry_pct=5.0, w3_rv=101.0),
                ],
            },
            {
                "entry_date": "2025-02-03",
                "return_pct": 3.0,
                "hold_days": 5,
                "entry_t0": _snap(ret_from_entry_pct=0, gap_mv_w20_w5=4.0, w3_rv=101.0),
                "timeline": [
                    _snap(ret_from_entry_pct=0, w3_rv=101.0),
                    _snap(ret_from_entry_pct=-1.0, w3_rv=100.0),
                    _snap(ret_from_entry_pct=3.0, w3_rv=100.8),
                ],
            },
        ],
    }
    assert len(legs_from_cache(cache)) == 2

    class _FakeConn:
        pass

    monkeypatch.setattr(
        "research.backtest.archive.c18acc_structural_pyramid_study.load_price_panels",
        lambda _conn: (_FakeClose(), None, None),
    )
    monkeypatch.setattr(
        "research.backtest.archive.c18acc_structural_pyramid_study.IndependentExitCtx",
        lambda **kw: None,
    )

    class _FakeClose:
        index = pd.Index(["2025-01-02", "2025-02-03", "2025-06-01"])

    payload = run_c18acc_structural_pyramid_study(
        _FakeConn(),
        cache,
        min_trigger_n=1,
        min_oos_trigger_n=1,
    )
    assert payload["schema"] == "c18acc_structural_pyramid-v1"
    assert payload["hypothesis"] == "H-C18-PYRAMID-1"
    assert "underwater_rebound" in payload["full_results"]
