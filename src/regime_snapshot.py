"""Regime layer · multi-axis diagnostic snapshot (PIT-safe · config-driven axes)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pandas as pd

from market_breadth_impulse import (
    BreadthImpulseParams,
    build_impulse_panel_from_close,
    impulse_event_snapshot_at,
    rhythm_snapshot_at,
)
from market_breadth_ma import (
    BREADTH_ZONE_DISPLAY,
    BREADTH_ZONE_ZH,
    build_breadth_panel,
    classify_breadth_zone,
)
from regime_config import impulse_params_from_regime, load_regime_config, rhythm_tiers_from_regime
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_rotation import QUADRANT_LABEL, compute_rrg_panel
from stage_analysis import (
    MINERVINI_CRITERIA_TOTAL,
    classify_ix_trend_posture,
    vectorized_minervini_pass_pct,
)
from vcp_nse_port.bars import rows_to_ohlcv_df

BAR_LOOKBACK = 280

DEFAULT_AXIS_ORDER: tuple[str, ...] = (
    "breadth_zone_200",
    "trend_posture",
    "rrg_rotation",
    "stage2_participation",
)


def _load_ix_bars(conn: sqlite3.Connection, as_of: str, *, code: str = "IX0001") -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE code = ? AND source = 'tej' AND date <= ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, as_of, BAR_LOOKBACK),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    return rows_to_ohlcv_df(list(reversed(rows)))


def _panel_row_delta(
    panel: pd.DataFrame,
    *,
    as_of: str,
    col: str,
    lookback: int = 5,
) -> float | None:
    sub = panel[panel["trade_date"] <= as_of]
    if len(sub) < lookback + 1:
        return None
    cur = float(sub.iloc[-1][col])
    prev = float(sub.iloc[-1 - lookback][col])
    return round(cur - prev, 1)


def _load_impulse_panel(conn: sqlite3.Connection, *, params: BreadthImpulseParams) -> pd.DataFrame | None:
    try:
        close, _, _ = load_price_panels(conn)
    except RuntimeError:
        return None
    return build_impulse_panel_from_close(close, params=params)


def _breadth_sub_blocks(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    sub_blocks: list[str],
    impulse_params: BreadthImpulseParams,
    tier_cfg: dict[str, float],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    need_panel = bool({"rhythm", "impulse"} & set(sub_blocks))
    if not need_panel:
        return out
    panel = _load_impulse_panel(conn, params=impulse_params)
    if panel is None or panel.empty:
        err = {"available": False, "error": "no impulse panel"}
        for key in sub_blocks:
            if key in ("rhythm", "impulse"):
                out[key] = dict(err)
        return out
    if "rhythm" in sub_blocks:
        out["rhythm"] = rhythm_snapshot_at(panel, as_of, tier_cfg=tier_cfg)
    if "impulse" in sub_blocks:
        out["impulse"] = impulse_event_snapshot_at(panel, as_of, params=impulse_params)
    return out


def _market_breadth(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    impulse_params: BreadthImpulseParams,
    tier_cfg: dict[str, float],
    sub_blocks: list[str] | None = None,
) -> dict[str, Any]:
    panel = build_breadth_panel(conn, date_end=as_of)
    if panel.empty:
        return {"available": False, "error": "no breadth panel"}
    sub = panel[panel["trade_date"] <= as_of]
    if sub.empty:
        return {"available": False, "error": "no breadth on as_of"}
    row = sub.iloc[-1]
    trade_date = str(row["trade_date"])
    p50 = float(row["pct_above_50"])
    p200 = float(row["pct_above_200"])
    zone200 = classify_breadth_zone(p200)
    out: dict[str, Any] = {
        "available": True,
        "trade_date": trade_date,
        "pct_above_50": round(p50, 1),
        "pct_above_200": round(p200, 1),
        "breadth_zone_200": zone200,
        "display": BREADTH_ZONE_DISPLAY.get(zone200, zone200),
        "display_zh": BREADTH_ZONE_ZH.get(zone200, zone200),
        "participation_gap": round(p50 - p200, 1),
        "n_valid": int(row.get("n_valid_200") or row.get("n_valid_50") or 0),
        "divergence_flag": bool(row.get("divergence_flag", False)),
        "pct50_delta_5d": _panel_row_delta(sub, as_of=trade_date, col="pct_above_50"),
        "pct200_delta_5d": _panel_row_delta(sub, as_of=trade_date, col="pct_above_200"),
    }
    blocks = sub_blocks if sub_blocks is not None else ["rhythm", "impulse"]
    out.update(
        _breadth_sub_blocks(
            conn,
            trade_date,
            sub_blocks=blocks,
            impulse_params=impulse_params,
            tier_cfg=tier_cfg,
        )
    )
    return out


def _trend_posture_axis(conn: sqlite3.Connection, as_of: str, *, bench_code: str = "IX0001") -> dict[str, Any]:
    df = _load_ix_bars(conn, as_of, code=bench_code)
    if len(df) < 200:
        return {"available": False, "error": "insufficient IX bars"}
    out = classify_ix_trend_posture(df)
    out["available"] = True
    return out


def _rrg_rotation_axis(conn: sqlite3.Connection, as_of: str) -> dict[str, Any]:
    try:
        close, _, _vol = load_price_panels(conn)
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)}
    close.index = close.index.astype(str)
    if as_of not in close.index:
        valid = close.index[close.index <= as_of]
        if len(valid) == 0:
            return {"available": False, "error": "no stock panel on as_of"}
        as_of = str(valid[-1])

    bench_rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code = 'IX0001' AND source = 'tej' AND date <= ?
        ORDER BY date
        """,
        (as_of,),
    ).fetchall()
    if not bench_rows:
        return {"available": False, "error": "no benchmark"}
    bench = pd.Series(
        {str(r["date"]): float(r["close"]) for r in bench_rows},
        dtype=float,
    )
    sub_close = close.loc[:as_of].tail(BAR_LOOKBACK)
    sub_bench = bench.reindex(sub_close.index).ffill()
    _, _, quad = compute_rrg_panel(sub_close, sub_bench)
    if as_of not in quad.index:
        return {"available": False, "error": "RRG not ready"}

    dates = list(quad.index)
    prev_date: str | None = None
    if as_of in dates:
        idx = dates.index(as_of)
        if idx > 0:
            prev_date = str(dates[idx - 1])

    migrations = {
        "improving_to_leading": 0,
        "leading_to_weakening": 0,
        "lagging_to_improving": 0,
        "weakening_to_lagging": 0,
    }
    if prev_date:
        for sid in quad.columns:
            q0 = quad.at[prev_date, sid]
            q1 = quad.at[as_of, sid]
            if q0 == "improving" and q1 == "leading":
                migrations["improving_to_leading"] += 1
            elif q0 == "leading" and q1 == "weakening":
                migrations["leading_to_weakening"] += 1
            elif q0 == "lagging" and q1 == "improving":
                migrations["lagging_to_improving"] += 1
            elif q0 == "weakening" and q1 == "lagging":
                migrations["weakening_to_lagging"] += 1

    counts: dict[str, int] = {"leading": 0, "weakening": 0, "lagging": 0, "improving": 0}
    for sid in quad.columns:
        q = quad.at[as_of, sid]
        if q in counts:
            counts[str(q)] += 1
    total = sum(counts.values())
    if total <= 0:
        return {"available": False, "error": "no quadrant data"}

    pcts = {k: round(v / total * 100.0, 1) for k, v in counts.items()}
    dominant = max(counts, key=counts.get)
    rotation_health_pct = round(pcts["leading"] + pcts["improving"], 1)
    return {
        "available": True,
        "trade_date": as_of,
        "universe_n": total,
        "dominant_quadrant": dominant,
        "dominant_label": QUADRANT_LABEL.get(dominant, dominant),
        "counts": counts,
        "pct": pcts,
        "leading_pct": pcts["leading"],
        "improving_pct": pcts["improving"],
        "weakening_pct": pcts["weakening"],
        "lagging_pct": pcts["lagging"],
        "rotation_health_pct": rotation_health_pct,
        "migrations": migrations,
        "prev_trade_date": prev_date,
    }


def _stage2_participation_axis(conn: sqlite3.Connection, as_of: str, *, min_pass: int = 7) -> dict[str, Any]:
    try:
        close, _, _ = load_price_panels(conn)
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)}
    close.index = close.index.astype(str)
    if as_of not in close.index:
        valid = close.index[close.index <= as_of]
        if len(valid) == 0:
            return {"available": False, "error": "no stock panel"}
        as_of = str(valid[-1])

    pct_series = vectorized_minervini_pass_pct(close.loc[:as_of], min_pass=min_pass)
    if as_of not in pct_series.index:
        return {"available": False, "error": "participation not ready"}
    pct = float(pct_series.loc[as_of]) * 100.0
    valid_dates = list(pct_series.index[pct_series.index <= as_of])
    pass_delta_5d: float | None = None
    if len(valid_dates) >= 6:
        prev = valid_dates[-6]
        pass_delta_5d = round(pct - float(pct_series.loc[prev]) * 100.0, 1)
    n_universe = int(close.loc[:as_of].iloc[-1].notna().sum())
    return {
        "available": True,
        "trade_date": as_of,
        "pass_pct": round(pct, 1),
        "pass_delta_5d": pass_delta_5d,
        "universe_n": n_universe,
        "min_criteria": min_pass,
        "criteria_total": MINERVINI_CRITERIA_TOTAL,
        "note": "bulk scan ≥7/8 (RS omitted)",
    }


AxisBuilder = Callable[..., dict[str, Any]]


def _axis_builders(
    *,
    benchmark_code: str,
    impulse_params: BreadthImpulseParams,
    tier_cfg: dict[str, float],
) -> dict[str, AxisBuilder]:
    return {
        "breadth_zone_200": lambda conn, as_of, axis_cfg: _market_breadth(
            conn,
            as_of,
            impulse_params=impulse_params,
            tier_cfg=tier_cfg,
            sub_blocks=list(axis_cfg.get("sub_blocks") or ["rhythm", "impulse"]),
        ),
        "trend_posture": lambda conn, as_of, _axis_cfg: _trend_posture_axis(
            conn, as_of, bench_code=benchmark_code
        ),
        "rrg_rotation": lambda conn, as_of, _axis_cfg: _rrg_rotation_axis(conn, as_of),
        "stage2_participation": lambda conn, as_of, _axis_cfg: _stage2_participation_axis(conn, as_of),
    }


def configured_axis_ids(cfg: dict[str, Any]) -> list[str]:
    axes = cfg.get("axes")
    if isinstance(axes, dict) and axes:
        return list(axes.keys())
    return list(DEFAULT_AXIS_ORDER)


def build_regime_snapshot(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    benchmark_code: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Regime layer PIT snapshot @ as_of · axes driven by config/regime.yaml."""
    cfg = config if config is not None else load_regime_config()
    bench = benchmark_code or str(cfg.get("benchmark_code") or "IX0001")
    impulse_params = impulse_params_from_regime(cfg)
    tier_cfg = rhythm_tiers_from_regime(cfg)
    builders = _axis_builders(
        benchmark_code=bench, impulse_params=impulse_params, tier_cfg=tier_cfg
    )
    axes_cfg = cfg.get("axes") if isinstance(cfg.get("axes"), dict) else {}

    snap: dict[str, Any] = {
        "as_of": as_of,
        "benchmark_code": bench,
        "axis_order": configured_axis_ids(cfg),
    }
    for axis_id in snap["axis_order"]:
        builder = builders.get(axis_id)
        if builder is None:
            snap[axis_id] = {"available": False, "error": f"unknown axis: {axis_id}"}
            continue
        axis_def = axes_cfg.get(axis_id) if isinstance(axes_cfg.get(axis_id), dict) else {}
        snap[axis_id] = builder(conn, as_of, axis_def)
    return snap
