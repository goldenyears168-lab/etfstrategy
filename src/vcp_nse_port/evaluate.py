"""Evaluate one symbol with nse-vcp-screener logic."""

from __future__ import annotations

import pandas as pd

from vcp_nse_port.pivot_proximity import calculate_pivot_proximity
from vcp_nse_port.relative_strength import calculate_relative_strength
from vcp_nse_port.scorer import calculate_composite_score
from stage_analysis import calculate_minervini_trend_template
from vcp_nse_port.vcp_pattern import calculate_vcp
from vcp_nse_port.volume_pattern import calculate_volume_pattern

DEFAULT_TREND_MIN_SCORE = 100.0
DEFAULT_LOOKBACK_DAYS = 120


def evaluate_vcp_nse(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
    *,
    trend_min_score: float = DEFAULT_TREND_MIN_SCORE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_contractions: int = 2,
    t1_depth_min: float = 10.0,
    t1_depth_max: float = 40.0,
    contraction_ratio: float = 0.75,
) -> dict:
    """Run full VCP pipeline on OHLCV DataFrame (ascending dates)."""
    if len(stock_df) < 200:
        return _reject("K 線不足 200 日", min_bars=len(stock_df))

    trend = calculate_minervini_trend_template(stock_df)
    if not trend["passed"]:
        return _reject(
            f"Trend Template {trend['criteria_met']}/{trend['criteria_total']} · Stage {trend.get('stage')}",
            trend=trend,
        )

    vcp = calculate_vcp(
        stock_df,
        lookback_days=lookback_days,
        min_contractions=min_contractions,
        t1_depth_min=t1_depth_min,
        t1_depth_max=t1_depth_max,
        contraction_ratio=contraction_ratio,
    )
    if not vcp["is_vcp"]:
        reason = vcp.get("details", {}).get("reason", "非 VCP 形態")
        return _reject(reason, trend=trend, vcp=vcp)

    volume = calculate_volume_pattern(stock_df)
    current_price = float(stock_df["Close"].iloc[-1])
    pivot = float(vcp["pivot"])
    pivot_prox = calculate_pivot_proximity(current_price, pivot)
    rs = calculate_relative_strength(stock_df, benchmark_df)

    composite = calculate_composite_score(
        trend_score=trend["score"],
        contraction_score=vcp["score"],
        volume_score=volume["score"],
        pivot_score=pivot_prox["score"],
        rs_score=rs["score"],
    )

    t1_depth = vcp["details"].get("t1_depth")
    final_depth = vcp["details"].get("final_depth")
    contraction_ratio_val = None
    if t1_depth and final_depth and t1_depth > 0:
        contraction_ratio_val = round(final_depth / t1_depth, 3)

    return {
        "passed": True,
        "composite_score": composite["composite_score"],
        "quality": composite["quality"],
        "trend": trend,
        "vcp": vcp,
        "volume": volume,
        "pivot_proximity": pivot_prox,
        "relative_strength": rs,
        "composite": composite,
        "current_price": current_price,
        "pivot": pivot,
        "dry_up_ratio": volume["dry_up_ratio"],
        "contraction_ratio": contraction_ratio_val,
        "reject_reason": "",
    }


def _reject(reason: str, **parts: object) -> dict:
    out: dict = {
        "passed": False,
        "composite_score": 0.0,
        "quality": "Poor",
        "reject_reason": reason,
        "trend": parts.get("trend"),
        "vcp": parts.get("vcp"),
        "volume": None,
        "pivot_proximity": None,
        "relative_strength": None,
        "composite": None,
        "current_price": None,
        "pivot": None,
        "dry_up_ratio": None,
        "contraction_ratio": None,
    }
    if "min_bars" in parts:
        out["min_bars"] = parts["min_bars"]
    return out


def evaluate_vcp_nse_diagnostic(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
    *,
    trend_min_score: float = DEFAULT_TREND_MIN_SCORE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_contractions: int = 2,
    t1_depth_min: float = 10.0,
    t1_depth_max: float = 40.0,
    contraction_ratio: float = 0.75,
) -> dict:
    """Always return trend/vcp diagnostics (for literature reverse-engineering)."""
    if len(stock_df) < 200:
        return {
            "passed": False,
            "reject_stage": "bars",
            "reject_reason": "K 線不足 200 日",
            "composite_score": 0.0,
            "trend_score": 0.0,
            "vcp_ok": False,
        }

    trend = calculate_minervini_trend_template(stock_df)
    trend_score = float(trend["score"])
    trend_ok = bool(trend.get("passed")) or trend_score >= trend_min_score

    vcp = calculate_vcp(
        stock_df,
        lookback_days=lookback_days,
        min_contractions=min_contractions,
        t1_depth_min=t1_depth_min,
        t1_depth_max=t1_depth_max,
        contraction_ratio=contraction_ratio,
    )
    vcp_ok = bool(vcp.get("is_vcp"))
    reject_stage = "none"
    reject_reason = ""
    composite_score = 0.0

    if not trend_ok:
        reject_stage = "trend"
        reject_reason = f"Trend {trend_score:.0f} < {trend_min_score:.0f}"
    elif not vcp_ok:
        reject_stage = "vcp"
        reject_reason = str(vcp.get("details", {}).get("reason", "非 VCP"))
    else:
        volume = calculate_volume_pattern(stock_df)
        current_price = float(stock_df["Close"].iloc[-1])
        pivot = float(vcp["pivot"])
        pivot_prox = calculate_pivot_proximity(current_price, pivot)
        rs = calculate_relative_strength(stock_df, benchmark_df)
        composite = calculate_composite_score(
            trend_score=trend_score,
            contraction_score=vcp["score"],
            volume_score=volume["score"],
            pivot_score=pivot_prox["score"],
            rs_score=rs["score"],
        )
        composite_score = float(composite["composite_score"])

    passed = trend_ok and vcp_ok
    return {
        "passed": passed,
        "reject_stage": reject_stage if not passed else "none",
        "reject_reason": reject_reason,
        "composite_score": composite_score,
        "trend_score": trend_score,
        "vcp_ok": vcp_ok,
        "vcp_reason": vcp.get("details", {}).get("reason"),
    }
