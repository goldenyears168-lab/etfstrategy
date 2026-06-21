"""Minervini Trend Template — delegates to stage_analysis (8-point SEPA gate)."""

from __future__ import annotations

from typing import Optional

from stage_analysis import calculate_minervini_trend_template_mrf


def calculate_trend_template(
    historical_prices: list[dict],
    quote_data: dict,
    rs_rank: Optional[int] = None,
    ext_threshold: float = 8.0,
    max_sma200_extension: float = 50.0,
) -> dict:
    """Evaluate stock against Minervini 8-point Trend Template (all required)."""
    return calculate_minervini_trend_template_mrf(
        historical_prices,
        quote_data,
        rs_rank=rs_rank,
        ext_threshold=ext_threshold,
        max_sma200_extension=max_sma200_extension,
    )
