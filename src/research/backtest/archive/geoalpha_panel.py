"""GeoAlpha dual-track · geometry layer φ + structural / alpha / joint scores.

Phase 0: intraday + daily-close geometry on dual-WMA spread.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from project_config import DEFAULT_ETF_CODES
from research.backtest.archive.rrg_drs5d_score_backtest import build_drs5d_panel
from research.backtest.archive.rrg_hybrid_drs5d_ret5d_backtest import (
    attach_daily_dual_wma_spread,
    attach_pit_fundamentals,
    compute_hybrid_structural_score,
)
from rrg_universe_intraday_panel import (
    DEFAULT_HOOK_POOL_RETRACE_EPS,
    build_dual_wma_intraday_trajectories,
)

PHI_LABELS: tuple[str, ...] = (
    "EXT_OPEN",
    "HOOK_POOL",
    "HOOK_IMP",
    "HOOK_STRICT",
    "PULLBACK",
    "OTHER",
)

PHI_GEO_GATE: dict[str, float] = {
    "EXT_OPEN": 1.0,
    "HOOK_POOL": 0.55,
    "HOOK_IMP": 0.85,
    "HOOK_STRICT": 1.0,
    "PULLBACK": 0.45,
    "OTHER": 0.25,
}

_STRUCT_SCALE = 2.5


def classify_geo_phase(point: dict[str, Any]) -> str:
    """Arc phase φ on intraday dual-WMA spread trajectory point."""
    if point.get("hook_complete"):
        return "HOOK_STRICT"

    w20q = str((point.get("w20") or {}).get("quadrant") or "")
    w5q = str((point.get("w5") or {}).get("quadrant") or "")
    sc = str(point.get("spread_class") or "")
    srs = float(point.get("spread_rs") or 0)
    sms = float(point.get("spread_mom") or 0)
    retrace = float(point.get("hook_retrace_pct") or 0)

    if point.get("hook_candidate"):
        if (
            w20q == "improving"
            and w5q == "improving"
            and srs >= 0
            and retrace >= DEFAULT_HOOK_POOL_RETRACE_EPS
        ):
            return "HOOK_IMP"
        return "HOOK_POOL"

    dist_rising = sms > 0 or bool(point.get("dist_rising"))
    if sc == "extension" and srs > 0 and dist_rising:
        return "EXT_OPEN"

    if sc == "pullback":
        return "PULLBACK"

    return "OTHER"


def extract_geo_features(point: dict[str, Any]) -> dict[str, float]:
    """Bounded geometry features g_* from spread / hook fields."""
    dist = float(point.get("spread_dist") or 0)
    rs = float(point.get("spread_rs") or 0)
    mom = float(point.get("spread_mom") or 0)
    w20 = point.get("w20") or {}
    w20_rs = float(w20.get("rs_ratio") or 100.0)
    retrace = float(point.get("hook_retrace_pct") or 0.0)
    peak = float(point.get("hook_peak_dist") or point.get("hook_pool_peak_dist") or 0.0)
    curv = float(point.get("hook_curvature") or 0.0)

    return {
        "g_dist": round(min(dist / 5.0, 1.0), 4),
        "g_retrace": round(min(max(retrace, 0.0), 1.0), 4),
        "g_peak": round(min(peak / 5.0, 1.0), 4),
        "g_curv": round(min(max(-curv, 0.0) / 2.0, 1.0), 4),
        "g_rs": round(min(max(rs, 0.0) / 3.0, 1.0), 4),
        "g_mom": round(min(max(mom, 0.0) / 3.0, 1.0), 4),
        "g_w20_rs": round(min(max(w20_rs - 98.0, 0.0) / 5.0, 1.0), 4),
    }


def compute_p_geo(phi: str) -> float:
    return PHI_GEO_GATE.get(phi, 0.25)


def compute_p_struct(s_struct: float, *, max_scale: float = _STRUCT_SCALE) -> float:
    if not np.isfinite(s_struct):
        return 0.0
    return round(min(max(float(s_struct), 0.0) / max_scale, 1.0), 6)


def compute_s_alpha(
    point: dict[str, Any],
    row_daily: dict[str, Any] | None = None,
) -> float:
    """Geometry-conditioned alpha score · bounded 0..1."""
    row_daily = row_daily or {}
    geo = extract_geo_features(point)
    phi = classify_geo_phase(point)

    w5_v = float(row_daily.get("w5_drs_rsr_1d", float("nan")))
    if not np.isfinite(w5_v):
        mom = float(point.get("spread_mom") or 0)
        w5_v = mom / 5.0 if mom else 0.0

    raw = 0.0
    if np.isfinite(w5_v):
        raw += min(max(w5_v, 0.0) / 1.5, 1.0) * 0.35
    raw += geo["g_rs"] * 0.20
    raw += geo["g_mom"] * 0.15

    if phi in ("HOOK_IMP", "HOOK_STRICT"):
        raw += geo["g_retrace"] * 0.15 + geo["g_curv"] * 0.10
    elif phi == "EXT_OPEN":
        raw += geo["g_dist"] * 0.15

    bench = float(row_daily.get("bench_today_ret", 0.0))
    if bench >= 0:
        raw += 0.10 + min(bench / 2.0, 1.0) * 0.10

    roe = float(row_daily.get("roe_latest_q", float("nan")))
    if np.isfinite(roe) and roe > 0:
        raw += min(roe / 20.0, 1.0) * 0.08

    rev_yoy = float(row_daily.get("revenue_yoy_pct", float("nan")))
    if np.isfinite(rev_yoy) and rev_yoy > 0:
        raw += min(rev_yoy / 30.0, 1.0) * 0.05

    sig_ret = float(row_daily.get("sig_ret", 0.0))
    if sig_ret >= 3.0:
        raw -= 0.15

    return round(min(max(raw, 0.0), 1.0), 6)


def compute_s_joint(s_struct: float, phi: str, s_alpha: float) -> float:
    """Gated joint score · P_struct × P_geo × S_alpha."""
    p_struct = compute_p_struct(s_struct)
    p_geo = compute_p_geo(phi)
    s_a = min(max(float(s_alpha), 0.0), 1.0) if np.isfinite(s_alpha) else 0.0
    return round(p_struct * p_geo * s_a, 6)


def daily_row_to_point(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    """Daily-close spread row → geometry point dict."""
    if isinstance(row, pd.Series):
        row = row.to_dict()
    w5_dr1 = float(row.get("w5_drs_rsr_1d", float("nan")))
    return {
        "date": str(row.get("date") or ""),
        "spread_class": str(row.get("spread_class") or ""),
        "spread_dist": float(row.get("spread_dist") or 0),
        "spread_rs": float(row.get("spread_rs") or 0),
        "spread_mom": float(row.get("spread_mom") or 0),
        "hook_candidate": bool(row.get("hook_candidate")),
        "hook_complete": bool(row.get("hook_complete")),
        "hook_retrace_pct": row.get("hook_retrace_pct"),
        "hook_peak_dist": row.get("hook_peak_dist"),
        "hook_pool_peak_dist": row.get("hook_pool_peak_dist"),
        "hook_curvature": row.get("hook_curvature"),
        "dist_rising": np.isfinite(w5_dr1) and w5_dr1 > 0,
        "w20": {"quadrant": str(row.get("w20_quadrant") or row.get("quadrant") or "")},
        "w5": {"quadrant": str(row.get("w5_quadrant") or "")},
        "fresh": True,
    }


def _panel_row_from_point(
    *,
    stock_id: str,
    point: dict[str, Any],
    row_daily: dict[str, Any],
    s_struct: float,
) -> dict[str, Any]:
    phi = classify_geo_phase(point)
    geo = extract_geo_features(point)
    s_alpha = compute_s_alpha(point, row_daily)
    s_joint = compute_s_joint(s_struct, phi, s_alpha) if np.isfinite(s_struct) else float("nan")
    return {
        "stock_id": stock_id,
        "date": str(point["date"]),
        "minute": str(point.get("minute") or "close"),
        "frame_id": str(point.get("frame_id") or f"{point['date']}Tclose"),
        "phi": phi,
        **geo,
        "s_struct": s_struct,
        "s_alpha": s_alpha,
        "s_joint": s_joint,
    }


def build_geoalpha_daily_panel(
    conn: Any | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    pool_only: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Daily-close geometry · long-history IC (no 1m K required)."""
    conn = conn or __import__("stock_db", fromlist=["connect"]).connect()
    panel, meta = build_drs5d_panel(conn)
    panel = attach_daily_dual_wma_spread(panel, conn)
    panel = attach_pit_fundamentals(panel, conn)

    if date_from:
        panel = panel[panel["date"].astype(str) >= date_from]
    if date_to:
        panel = panel[panel["date"].astype(str) <= date_to]

    sub = panel[panel["in_pool_omega_kinematic"]].copy() if pool_only else panel.copy()
    rows: list[dict[str, Any]] = []
    for _, row in sub.iterrows():
        pt = daily_row_to_point(row)
        row_d = row.to_dict()
        s_struct = compute_hybrid_structural_score(row)
        out = _panel_row_from_point(
            stock_id=str(row["sid"]),
            point=pt,
            row_daily=row_d,
            s_struct=s_struct,
        )
        out["sid"] = str(row["sid"])
        out["drs_5d"] = row.get("drs_5d", float("nan"))
        out["ret_5d"] = row.get("ret_5d", float("nan"))
        out["excess"] = row.get("excess", float("nan"))
        rows.append(out)

    df = pd.DataFrame(rows)
    return df, {
        **meta,
        "mode": "daily",
        "n_rows": len(df),
        "date_from": date_from,
        "date_to": date_to,
        "pool_only": pool_only,
    }


def build_geoalpha_intraday_panel(
    conn: Any,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Intraday dual-WMA trajectories · hook annotation · daily S_struct merge."""
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn, dates=dates, etf_codes=etf_codes
    )

    panel, _panel_meta = build_drs5d_panel(conn)
    panel = attach_daily_dual_wma_spread(panel, conn)
    panel = attach_pit_fundamentals(panel, conn)
    date_set = set(dates)
    panel_sub = panel[panel["date"].astype(str).isin(date_set)]

    daily_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in panel_sub.iterrows():
        key = (str(row["sid"]), str(row["date"]))
        daily_lookup[key] = {
            "row": row.to_dict(),
            "s_struct": compute_hybrid_structural_score(row),
            "drs_5d": row.get("drs_5d", float("nan")),
            "ret_5d": row.get("ret_5d", float("nan")),
            "excess": row.get("excess", float("nan")),
        }

    rows: list[dict[str, Any]] = []
    for traj in trajectories:
        sid = str(traj["stock_id"])
        for p in traj["points"]:
            if not p.get("fresh", True):
                continue
            key = (sid, str(p["date"]))
            daily = daily_lookup.get(key, {})
            row_daily = daily.get("row", {})
            s_struct = daily.get("s_struct", float("nan"))
            out = _panel_row_from_point(
                stock_id=sid,
                point=p,
                row_daily=row_daily,
                s_struct=s_struct,
            )
            out["sid"] = sid
            out["drs_5d"] = daily.get("drs_5d", float("nan"))
            out["ret_5d"] = daily.get("ret_5d", float("nan"))
            out["excess"] = daily.get("excess", float("nan"))
            rows.append(out)

    df = pd.DataFrame(rows)
    return df, {
        **meta,
        "mode": "intraday",
        "n_rows": len(df),
        "n_frames": len(frames),
        "dates": dates,
    }
