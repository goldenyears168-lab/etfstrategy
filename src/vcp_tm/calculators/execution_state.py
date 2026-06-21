# adapted from tradermonty/claude-trading-skills (MIT)
# https://github.com/tradermonty/claude-trading-skills/tree/main/skills/vcp-screener/scripts/calculators

"""Execution State Engine — separates pattern quality from buy-able now."""

from __future__ import annotations

from typing import Optional

STATE_ORDER = [
    "Invalid",
    "Damaged",
    "Overextended",
    "Extended",
    "Early-post-breakout",
    "Breakout",
    "Pre-breakout",
]

STATE_MAX_RATING = {
    "Invalid": "No VCP",
    "Damaged": "No VCP",
    "Overextended": "Weak VCP",
    "Extended": "Developing VCP",
    "Early-post-breakout": "Strong VCP",
    "Breakout": None,
    "Pre-breakout": None,
}

RATING_ORDER = [
    "No VCP",
    "Weak VCP",
    "Developing VCP",
    "Good VCP",
    "Strong VCP",
    "Textbook VCP",
]


def compute_execution_state(
    distance_from_pivot_pct: Optional[float],
    price: float,
    sma50: Optional[float],
    sma200: Optional[float],
    sma200_distance_pct: Optional[float],
    last_contraction_low: Optional[float],
    breakout_volume: bool,
    max_sma200_extension: float = 50.0,
) -> dict:
    reasons: list[str] = []

    if sma50 is not None and sma200 is not None:
        if price < sma50 and sma50 < sma200:
            reasons.append(
                f"Price {price:.2f} < SMA50 {sma50:.2f} < SMA200 {sma200:.2f}"
            )
            return {"state": "Invalid", "reasons": reasons}

    if last_contraction_low is not None and last_contraction_low > 0:
        if price < last_contraction_low:
            reasons.append(
                f"Price {price:.2f} below last contraction low {last_contraction_low:.2f}"
            )
            return {"state": "Damaged", "reasons": reasons}

    if sma50 is not None and price < sma50:
        reasons.append(f"Price {price:.2f} below SMA50 {sma50:.2f}")
        return {"state": "Damaged", "reasons": reasons}

    if sma200_distance_pct is not None and sma200_distance_pct > max_sma200_extension:
        reasons.append(
            f"SMA200 distance {sma200_distance_pct:.1f}% > max {max_sma200_extension:.0f}%"
        )
        return {"state": "Overextended", "reasons": reasons}

    if distance_from_pivot_pct is None:
        reasons.append("No pivot available")
        return {"state": "Pre-breakout", "reasons": reasons}

    if distance_from_pivot_pct > 10.0:
        reasons.append(f"+{distance_from_pivot_pct:.1f}% above pivot (> 10%)")
        return {"state": "Overextended", "reasons": reasons}

    if distance_from_pivot_pct > 5.0:
        reasons.append(f"+{distance_from_pivot_pct:.1f}% above pivot (5-10% zone)")
        return {"state": "Extended", "reasons": reasons}

    if distance_from_pivot_pct > 3.0:
        reasons.append(f"+{distance_from_pivot_pct:.1f}% above pivot (3-5% zone)")
        return {"state": "Early-post-breakout", "reasons": reasons}

    if distance_from_pivot_pct >= 0.0:
        if breakout_volume:
            reasons.append(
                f"+{distance_from_pivot_pct:.1f}% above pivot with volume confirmation"
            )
            return {"state": "Breakout", "reasons": reasons}
        reasons.append(f"+{distance_from_pivot_pct:.1f}% above pivot (volume unconfirmed)")
        return {"state": "Early-post-breakout", "reasons": reasons}

    reasons.append(f"{distance_from_pivot_pct:.1f}% below pivot (forming pattern)")
    return {"state": "Pre-breakout", "reasons": reasons}


def apply_state_cap(rating: str, execution_state: str) -> tuple[str, bool]:
    max_rating = STATE_MAX_RATING.get(execution_state)
    if max_rating is None:
        return rating, False

    current_idx = RATING_ORDER.index(rating) if rating in RATING_ORDER else 0
    max_idx = RATING_ORDER.index(max_rating) if max_rating in RATING_ORDER else 0

    if current_idx > max_idx:
        return max_rating, True
    return rating, False
