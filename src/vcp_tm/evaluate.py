"""VCP-TM full evaluation pipeline (tradermonty lineage, TW data)."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from stage_analysis import MINERVINI_CRITERIA_TOTAL
from vcp_tm.calculators.execution_state import compute_execution_state
from vcp_tm.calculators.pattern_classifier import classify_pattern
from vcp_tm.calculators.pivot_proximity_calculator import calculate_pivot_proximity
from vcp_tm.calculators.relative_strength_calculator import calculate_relative_strength
from vcp_tm.calculators.trend_template_calculator import calculate_trend_template
from vcp_tm.calculators.vcp_pattern_calculator import calculate_vcp_pattern
from vcp_tm.calculators.volume_pattern_calculator import calculate_volume_pattern
from vcp_tm.params import VcpTmParams
from vcp_tm.price_adapter import df_to_mrf_prices, quote_from_mrf
from vcp_tm.scorer import calculate_composite_score

DEFAULT_MIN_BARS = 200
ENTRY_READY_STATES = frozenset({"Pre-breakout", "Breakout"})


def _last_contraction_low(vcp: dict) -> Optional[float]:
    contractions = vcp.get("contractions") or []
    if not contractions:
        return None
    last = contractions[-1]
    return last.get("low_price") or last.get("low")


def _final_contraction_depth(vcp: dict) -> Optional[float]:
    contractions = vcp.get("contractions") or []
    if not contractions:
        return None
    last = contractions[-1]
    return last.get("depth_pct")


def _compute_entry_ready(
    *,
    execution_state: str,
    valid_vcp: bool,
    distance_from_pivot_pct: Optional[float],
    dry_up_ratio: Optional[float],
    trade_status: Optional[str],
    risk_pct: Optional[float],
    params: VcpTmParams,
    require_valid_vcp: bool = True,
) -> bool:
    if execution_state not in ENTRY_READY_STATES:
        return False
    if require_valid_vcp and not valid_vcp:
        return False
    if distance_from_pivot_pct is None:
        return False
    if distance_from_pivot_pct < -8.0 or distance_from_pivot_pct > params.max_above_pivot:
        return False
    if dry_up_ratio is not None and dry_up_ratio > 1.0:
        return False
    if trade_status == "BELOW STOP LEVEL":
        return False
    if risk_pct is None or risk_pct <= 0 or risk_pct > params.max_risk:
        return False
    return True


def evaluate_vcp_tm(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
    *,
    params: VcpTmParams | None = None,
    rs_rank: Optional[int] = None,
    require_valid_vcp_for_entry: bool = True,
) -> dict:
    """Run full VCP-TM pipeline on ascending OHLCV DataFrame."""
    p = params or VcpTmParams()
    stock_mrf = df_to_mrf_prices(stock_df)
    bench_mrf = df_to_mrf_prices(benchmark_df) if benchmark_df is not None and not benchmark_df.empty else []

    if len(stock_mrf) < DEFAULT_MIN_BARS:
        return _reject(
            "K 線不足 200 日",
            reject_stage="bars",
            min_bars=len(stock_mrf),
        )

    quote = quote_from_mrf(stock_mrf)
    rs = (
        calculate_relative_strength(stock_mrf, bench_mrf)
        if bench_mrf
        else {
            "score": 0,
            "rs_rank_estimate": 0,
            "weighted_rs": None,
            "error": "No benchmark data",
        }
    )
    effective_rs = rs_rank if rs_rank is not None else rs.get("rs_rank_estimate")
    trend = calculate_trend_template(
        stock_mrf,
        quote,
        rs_rank=int(effective_rs) if effective_rs else None,
        ext_threshold=p.ext_threshold,
        max_sma200_extension=p.max_sma200_extension,
    )
    if not trend.get("passed") and trend.get("raw_score", trend.get("score", 0)) < p.trend_min_score:
        return _reject(
            f"Trend Template {trend.get('criteria_passed', '?')}/{MINERVINI_CRITERIA_TOTAL}",
            reject_stage="trend",
            trend=trend,
        )

    vcp = calculate_vcp_pattern(
        stock_mrf,
        lookback_days=p.lookback_days,
        atr_multiplier=p.atr_multiplier,
        min_contraction_days=p.min_contraction_days,
        min_contractions=p.min_contractions,
        t1_depth_min=p.t1_depth_min,
        contraction_ratio=p.contraction_ratio,
        wide_and_loose_threshold=p.wide_and_loose_threshold,
    )

    pivot_price = vcp.get("pivot_price")
    contractions = vcp.get("contractions") or []
    volume = calculate_volume_pattern(
        stock_mrf,
        pivot_price=pivot_price,
        contractions=contractions,
        breakout_volume_ratio=p.breakout_volume_ratio,
    )
    breakout_volume = bool(volume.get("breakout_volume"))
    current_price = float(quote["price"])

    last_low = _last_contraction_low(vcp)
    pivot_prox = calculate_pivot_proximity(
        current_price,
        pivot_price,
        last_contraction_low=last_low,
        breakout_volume=breakout_volume,
    )

    exec_state = compute_execution_state(
        distance_from_pivot_pct=pivot_prox.get("distance_from_pivot_pct"),
        price=current_price,
        sma50=trend.get("sma50"),
        sma200=trend.get("sma200"),
        sma200_distance_pct=trend.get("sma200_distance_pct"),
        last_contraction_low=last_low,
        breakout_volume=breakout_volume,
        max_sma200_extension=p.max_sma200_extension,
    )
    execution_state = exec_state["state"]

    pattern_type = classify_pattern(
        valid_vcp=bool(vcp.get("valid_vcp")),
        num_contractions=int(vcp.get("num_contractions") or 0),
        final_contraction_depth=_final_contraction_depth(vcp),
        execution_state=execution_state,
        dry_up_ratio=volume.get("dry_up_ratio"),
        wide_and_loose=bool(vcp.get("wide_and_loose")),
    )

    composite = calculate_composite_score(
        trend_score=float(trend.get("raw_score", trend.get("score", 0))),
        contraction_score=float(vcp.get("score") or 0),
        volume_score=float(volume.get("score") or 0),
        pivot_score=float(pivot_prox.get("score") or 0),
        rs_score=float(rs.get("score") or 0),
        valid_vcp=bool(vcp.get("valid_vcp")),
        execution_state=execution_state,
        pattern_type=pattern_type,
        wide_and_loose=bool(vcp.get("wide_and_loose")),
        sma200_extension_pct=trend.get("sma200_distance_pct"),
    )

    entry_ready = _compute_entry_ready(
        execution_state=execution_state,
        valid_vcp=bool(vcp.get("valid_vcp")),
        distance_from_pivot_pct=pivot_prox.get("distance_from_pivot_pct"),
        dry_up_ratio=volume.get("dry_up_ratio"),
        trade_status=pivot_prox.get("trade_status"),
        risk_pct=pivot_prox.get("risk_pct"),
        params=p,
        require_valid_vcp=require_valid_vcp_for_entry,
    )

    if not vcp.get("valid_vcp"):
        reason = (vcp.get("validation") or {}).get("reason") or vcp.get("error") or "非 VCP 形態"
        return _reject(
            str(reason),
            reject_stage="vcp",
            trend=trend,
            vcp=vcp,
            volume=volume,
            pivot_proximity=pivot_prox,
            relative_strength=rs,
            execution_state=execution_state,
            pattern_type=pattern_type,
            composite=composite,
            entry_ready=False,
        )

    return {
        "passed": True,
        "reject_reason": "",
        "reject_stage": "none",
        "composite_score": composite["composite_score"],
        "rating": composite["rating"],
        "quality_rating": composite.get("quality_rating"),
        "guidance": composite.get("guidance"),
        "valid_vcp": bool(vcp.get("valid_vcp")),
        "execution_state": execution_state,
        "execution_reasons": exec_state.get("reasons", []),
        "entry_ready": entry_ready,
        "pattern_type": pattern_type,
        "state_cap_applied": composite.get("state_cap_applied", False),
        "cap_reason": composite.get("cap_reason"),
        "pivot": pivot_price,
        "stop_loss": pivot_prox.get("stop_loss_price"),
        "risk_pct": pivot_prox.get("risk_pct"),
        "distance_from_pivot_pct": pivot_prox.get("distance_from_pivot_pct"),
        "breakout_volume": breakout_volume,
        "dry_up_ratio": volume.get("dry_up_ratio"),
        "trend": trend,
        "vcp": vcp,
        "volume": volume,
        "pivot_proximity": pivot_prox,
        "relative_strength": rs,
        "composite": composite,
        "current_price": current_price,
    }


def evaluate_vcp_tm_diagnostic(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
    *,
    params: VcpTmParams | None = None,
    rs_rank: Optional[int] = None,
) -> dict:
    """Always return trend/vcp diagnostics (literature reverse-engineering)."""
    p = params or VcpTmParams()
    stock_mrf = df_to_mrf_prices(stock_df)
    bench_mrf = df_to_mrf_prices(benchmark_df) if benchmark_df is not None and not benchmark_df.empty else []

    if len(stock_mrf) < DEFAULT_MIN_BARS:
        return {
            "passed": False,
            "reject_stage": "bars",
            "reject_reason": "K 線不足 200 日",
            "composite_score": 0.0,
            "trend_score": 0.0,
            "vcp_ok": False,
        }

    quote = quote_from_mrf(stock_mrf)
    rs = (
        calculate_relative_strength(stock_mrf, bench_mrf)
        if bench_mrf
        else {"score": 0, "rs_rank_estimate": 0}
    )
    effective_rs = rs_rank if rs_rank is not None else rs.get("rs_rank_estimate")
    trend = calculate_trend_template(
        stock_mrf,
        quote,
        rs_rank=int(effective_rs) if effective_rs else None,
    )
    trend_score = float(trend.get("raw_score", trend.get("score", 0)))
    trend_ok = bool(trend.get("passed")) or trend_score >= p.trend_min_score

    vcp = calculate_vcp_pattern(
        stock_mrf,
        lookback_days=p.lookback_days,
        atr_multiplier=p.atr_multiplier,
        min_contraction_days=p.min_contraction_days,
        min_contractions=p.min_contractions,
        t1_depth_min=p.t1_depth_min,
        contraction_ratio=p.contraction_ratio,
        wide_and_loose_threshold=p.wide_and_loose_threshold,
    )
    vcp_ok = bool(vcp.get("valid_vcp"))
    reject_stage = "none"
    reject_reason = ""
    composite_score = 0.0
    execution_state = "Pre-breakout"
    entry_ready = False

    if not trend_ok:
        reject_stage = "trend"
        reject_reason = f"Trend {trend_score:.0f} < {p.trend_min_score:.0f}"
    elif not vcp_ok:
        reject_stage = "vcp"
        reject_reason = str(
            (vcp.get("validation") or {}).get("reason") or vcp.get("error") or "非 VCP"
        )
    else:
        full = evaluate_vcp_tm(stock_df, benchmark_df, params=p, rs_rank=rs_rank)
        composite_score = float(full.get("composite_score") or 0)
        execution_state = str(full.get("execution_state") or "Pre-breakout")
        entry_ready = bool(full.get("entry_ready"))

    passed = trend_ok and vcp_ok
    return {
        "passed": passed,
        "reject_stage": reject_stage if not passed else "none",
        "reject_reason": reject_reason,
        "composite_score": composite_score,
        "trend_score": trend_score,
        "vcp_ok": vcp_ok,
        "vcp_reason": (vcp.get("validation") or {}).get("reason") or vcp.get("error"),
        "execution_state": execution_state,
        "entry_ready": entry_ready,
    }


def _reject(reason: str, *, reject_stage: str, **parts: object) -> dict:
    composite = parts.get("composite") or {}
    return {
        "passed": False,
        "reject_reason": reason,
        "reject_stage": reject_stage,
        "composite_score": float(composite.get("composite_score", 0) if composite else 0),
        "rating": composite.get("rating", "No VCP") if composite else "No VCP",
        "quality_rating": composite.get("quality_rating") if composite else None,
        "guidance": composite.get("guidance") if composite else None,
        "valid_vcp": False,
        "execution_state": parts.get("execution_state", "Invalid"),
        "execution_reasons": [],
        "entry_ready": parts.get("entry_ready", False),
        "pattern_type": parts.get("pattern_type", "Damaged"),
        "state_cap_applied": False,
        "cap_reason": None,
        "pivot": (parts.get("vcp") or {}).get("pivot_price") if parts.get("vcp") else None,
        "stop_loss": None,
        "risk_pct": None,
        "distance_from_pivot_pct": (parts.get("pivot_proximity") or {}).get(
            "distance_from_pivot_pct"
        ),
        "breakout_volume": False,
        "dry_up_ratio": (parts.get("volume") or {}).get("dry_up_ratio"),
        "trend": parts.get("trend"),
        "vcp": parts.get("vcp"),
        "volume": parts.get("volume"),
        "pivot_proximity": parts.get("pivot_proximity"),
        "relative_strength": parts.get("relative_strength"),
        "composite": composite if composite else None,
        "current_price": None,
        **{k: v for k, v in parts.items() if k == "min_bars"},
    }
