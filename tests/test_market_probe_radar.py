"""Unit tests for market probe radar helpers."""

from research.backtest.market_probe_radar import abs_return_decile, apply_meta_router, AxisScore
from research.backtest.probe_weather_detectors import (
    detect_orb_probe,
    detect_session_momentum_probe,
)
from stock_db.kbar import KbarBar


def _bar(minute: str, o: float, h: float, l: float, c: float, vol: int = 1000) -> KbarBar:
    return KbarBar(minute=minute, open=o, high=h, low=l, close=c, volume=vol)


def test_abs_return_decile_bounds():
    hist = [0.1 * i for i in range(1, 101)]
    assert abs_return_decile(hist, 0.05, n_deciles=10) == 1
    assert abs_return_decile(hist, 10.0, n_deciles=10) == 10


def test_orb_probe_fires_without_volume_filter():
    bars = (
        _bar("09:05:00", 100, 101, 99, 100),
        _bar("09:10:00", 100, 102, 99, 101),
        _bar("09:20:00", 101, 103, 100, 102.5),
    )
    trig = detect_orb_probe(bars)
    assert trig is not None
    assert trig.entry_px == 102.5


def test_session_momentum_probe():
    bars = (
        _bar("09:01:00", 100, 101, 99, 100),
        _bar("09:35:00", 100, 103, 99, 102),
        _bar("13:30:00", 102, 104, 101, 103),
    )
    trig = detect_session_momentum_probe(bars)
    assert trig is not None
    assert trig.entry_minute == "09:35:00"


def test_meta_router_no_edge_day():
    axes = [
        AxisScore("momentum", "動能", 1.0, 80, 0.6, -2.0, ("orb_probe",), True),
        AxisScore("mean_reversion", "均值", -0.5, 90, 0.4, -1.0, ("vwap_reclaim_probe",), True),
        AxisScore("trend_pullback", "回踩", -0.3, 85, 0.3, -0.8, ("bone_zone_probe",), True),
        AxisScore("structure", "結構", -1.0, 120, 0.0, -0.5, ("session_momentum_probe",), True),
    ]
    meta = {
        "default_team_id": "team_native_super",
        "market_types": [
            {
                "type_id": "no_edge_day",
                "label": "無優勢日",
                "rules": {"all_axes_z_max": -0.3},
                "priority_boost": {"rrg-mono-swap-accel": 0},
                "slot_cap_scale": 0.6,
            },
        ],
    }
    route = apply_meta_router(meta_router=meta, axis_scores=axes, vol_decile=5)
    assert route["market_type_id"] == "no_edge_day"
    assert route["slot_cap_scale"] == 0.6


def test_meta_router_skips_insufficient_axes():
    axes = [
        AxisScore("momentum", "動能", 1.0, 10, 0.6, None, ("orb_probe",), False),
        AxisScore("mean_reversion", "均值", -0.5, 10, 0.4, -1.0, ("vwap_reclaim_probe",), True),
    ]
    meta = {
        "default_team_id": "team_native_super",
        "market_types": [
            {
                "type_id": "no_edge_day",
                "label": "無優勢日",
                "rules": {"all_axes_z_max": -0.3},
            },
        ],
    }
    route = apply_meta_router(meta_router=meta, axis_scores=axes, vol_decile=5)
    assert route["market_type_id"] == "no_edge_day"


def test_meta_router_momentum_day():
    axes = [
        AxisScore("momentum", "動能", 1.0, 80, 0.6, 0.8, ("orb_probe",), True),
        AxisScore("mean_reversion", "均值", -0.5, 90, 0.4, -0.5, ("vwap_reclaim_probe",), True),
        AxisScore("volatility", "波動", 8.0, 0, None, None, (), True, vol_decile=8),
    ]
    meta = {
        "default_team_id": "team_native_super",
        "market_types": [
            {
                "type_id": "momentum_day",
                "label": "動能日",
                "rules": {"momentum_z_min": 0.5, "vol_decile_min": 6},
                "priority_boost": {"rrg-mono-swap-accel": 0},
            },
            {
                "type_id": "neutral",
                "label": "中性",
                "rules": {},
            },
        ],
    }
    route = apply_meta_router(meta_router=meta, axis_scores=axes, vol_decile=8)
    assert route["market_type_id"] == "momentum_day"
