"""Regime layer · JSON snapshot for Supabase / React / mobile (regime-snapshot-v1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from market_breadth_impulse import build_impulse_panel_from_close
from market_breadth_ma import build_breadth_panel
from regime_charts import (
    BREADTH_CHART_DAYS,
    BAR_LOOKBACK,
    _breadth_records,
    _load_ix_df,
    _weekly_bar_stage,
    enrich_rrg_rotation_rankings,
    load_rrg_scatter_points,
)
from regime_config import load_regime_config
from regime_interpret import (
    interpret_breadth_composite,
    interpret_breadth_impulse,
    interpret_breadth_level,
    interpret_breadth_rhythm,
    interpret_market_structure,
    interpret_rrg,
    interpret_stage2,
    interpret_trend,
)
from regime_snapshot import build_regime_snapshot
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_rotation import DEFAULT_LENGTH
from stage_analysis import WEEKLY_MA_PERIOD, daily_to_weekly, vectorized_minervini_pass_pct

_TPE = ZoneInfo("Asia/Taipei")
SCHEMA_VERSION = "regime-snapshot-v1"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, pd.Series):
        return _json_safe(value.to_dict())
    if isinstance(value, pd.DataFrame):
        return _json_safe(value.to_dict(orient="records"))
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return str(value)


def _breadth_series(conn: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    panel = build_breadth_panel(conn, date_end=as_of)
    if panel.empty:
        return []
    sub = panel[panel["trade_date"] <= as_of].tail(BREADTH_CHART_DAYS)
    if sub.empty:
        return []
    rows = _breadth_records(sub)
    return [
        {
            "trade_date": r["d"],
            "pct_above_50": r["p50"],
            "pct_above_200": r["p200"],
            "zone_200_zh": r["z200zh"],
            "zone_200_color": r["c"],
            "bench_close": r["bench"],
            "divergence_flag": r["div"],
        }
        for r in rows
    ]


def _zweig_ema_series(conn: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    try:
        close, _, _ = load_price_panels(conn)
        panel = build_impulse_panel_from_close(close)
        panel.index = panel.index.astype(str)
        sub = panel[panel.index <= as_of].tail(BREADTH_CHART_DAYS)
        if sub.empty or "zweig_ema" not in sub.columns:
            return []
        out: list[dict[str, Any]] = []
        for idx, val in sub["zweig_ema"].items():
            if pd.isna(val):
                continue
            out.append(
                {
                    "trade_date": str(idx),
                    "zweig_ema_pct": round(float(val) * 100.0, 2),
                }
            )
        return out
    except RuntimeError:
        return []


def _weinstein_weekly_series(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    bench_code: str,
    tail_weeks: int = 52,
) -> list[dict[str, Any]]:
    df = _load_ix_df(conn, as_of, code=bench_code)
    if df.empty:
        return []
    weekly = daily_to_weekly(df)
    if len(weekly) < WEEKLY_MA_PERIOD + 8:
        return []
    close = weekly["Close"]
    ma = close.rolling(WEEKLY_MA_PERIOD, min_periods=WEEKLY_MA_PERIOD).mean()
    sub = pd.DataFrame({"close": close, "ma": ma}).dropna().tail(tail_weeks)
    if len(sub) < 2:
        return []
    out: list[dict[str, Any]] = []
    cvals = [float(v) for v in sub["close"]]
    mvals = [float(v) for v in sub["ma"]]
    for i, (idx, row) in enumerate(sub.iterrows()):
        prev_ma = mvals[i - 1] if i > 0 else None
        stage = _weekly_bar_stage(cvals[i], mvals[i], prev_ma)
        out.append(
            {
                "week_end": str(idx.date()) if hasattr(idx, "date") else str(idx),
                "close": round(float(row["close"]), 2),
                "ma30w": round(float(row["ma"]), 2),
                "stage": stage,
            }
        )
    return out


def _rrg_chart_series(
    conn: sqlite3.Connection,
    as_of: str,
) -> dict[str, Any] | None:
    try:
        rrg_date, points = load_rrg_scatter_points(conn, as_of)
    except (ValueError, RuntimeError):
        return None
    serialized: list[dict[str, Any]] = []
    for p in points:
        trail = p.get("trail") or []
        serialized.append(
            {
                "stock_id": p["stock_id"],
                "rs_ratio": p["rs_ratio"],
                "rs_momentum": p["rs_momentum"],
                "quadrant": p["quadrant"],
                "trail": [
                    {"rs_ratio": round(float(a), 2), "rs_momentum": round(float(b), 2)}
                    for a, b in trail
                ],
            }
        )
    return {
        "as_of": rrg_date,
        "length": DEFAULT_LENGTH,
        "points": serialized,
    }


def _stage2_series(conn: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    try:
        close, _, _ = load_price_panels(conn)
        close.index = close.index.astype(str)
        if as_of not in close.index:
            valid = close.index[close.index <= as_of]
            as_of_px = str(valid[-1]) if len(valid) else as_of
        else:
            as_of_px = as_of
        pct_series = vectorized_minervini_pass_pct(close.loc[:as_of_px], min_pass=7)
        tail = (pct_series.dropna().tail(BREADTH_CHART_DAYS) * 100.0).round(1)
        return [
            {"trade_date": str(idx), "pass_pct": float(val)}
            for idx, val in tail.items()
        ]
    except RuntimeError:
        return []


def _interpretations(snap: dict[str, Any], *, bench: str) -> dict[str, str]:
    b = snap.get("breadth_zone_200") or {}
    t = snap.get("trend_posture") or {}
    r = snap.get("rrg_rotation") or {}
    s = snap.get("stage2_participation") or {}
    rhythm = b.get("rhythm") or {}
    impulse = b.get("impulse") or {}
    out: dict[str, str] = {
        "synopsis": interpret_market_structure(b, t, r, s, bench=bench),
    }
    if b.get("available"):
        out["breadth_level"] = interpret_breadth_level(b)
        composite = interpret_breadth_composite(b)
        if composite:
            out["breadth_composite"] = composite
    if rhythm.get("available"):
        out["breadth_rhythm"] = interpret_breadth_rhythm(rhythm)
    if impulse.get("available"):
        out["breadth_impulse"] = interpret_breadth_impulse(impulse)
    if t.get("available"):
        out["trend"] = interpret_trend(t, bench=bench)
    if r.get("available"):
        out["rrg"] = interpret_rrg(r)
    if s.get("available"):
        out["stage2"] = interpret_stage2(s, b)
    return out


def build_regime_snapshot_json(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    benchmark_code: str | None = None,
) -> dict[str, Any]:
    """PIT-safe regime payload for frontend rendering (no HTML/CSS)."""
    cfg = load_regime_config()
    bench = benchmark_code or str(cfg.get("benchmark_code") or "IX0001")
    snap = build_regime_snapshot(conn, as_of, benchmark_code=bench)
    enrich_rrg_rotation_rankings(snap, conn, as_of)

    b = snap.get("breadth_zone_200") or {}
    t = snap.get("trend_posture") or {}
    r = snap.get("rrg_rotation") or {}
    s = snap.get("stage2_participation") or {}

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": snap.get("as_of") or as_of,
        "benchmark_code": bench,
        "axis_order": snap.get("axis_order") or [],
        "axes": {
            "breadth_zone_200": _json_safe(b),
            "trend_posture": _json_safe(t),
            "rrg_rotation": _json_safe(r),
            "stage2_participation": _json_safe(s),
        },
        "interpretations": _interpretations(snap, bench=bench),
        "chart_series": {
            "breadth": _breadth_series(conn, as_of),
            "zweig_ema": _zweig_ema_series(conn, as_of),
            "weinstein_weekly": _weinstein_weekly_series(conn, as_of, bench_code=bench),
            "stage2_participation": _stage2_series(conn, as_of),
        },
        "meta": {
            "generated_at": datetime.now(_TPE).isoformat(),
            "breadth_chart_days": BREADTH_CHART_DAYS,
            "bar_lookback": BAR_LOOKBACK,
            "regime_config_version": cfg.get("version"),
        },
    }
    rrg_series = _rrg_chart_series(conn, as_of)
    if rrg_series is not None:
        payload["chart_series"]["rrg_scatter"] = rrg_series

    return _json_safe(payload)


def regime_snapshot_json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
