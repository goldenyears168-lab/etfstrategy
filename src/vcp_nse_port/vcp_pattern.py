"""VCP contraction detection (ported from nse-vcp-screener)."""

from __future__ import annotations

import pandas as pd


def find_contractions(
    df: pd.DataFrame,
    lookback_days: int = 120,
    min_contraction_days: int = 5,
) -> list[dict]:
    del min_contraction_days  # kept for API parity with upstream
    if len(df) < lookback_days:
        lookback_days = len(df)

    recent = df.tail(lookback_days).copy()
    contractions: list[dict] = []
    window_sizes = _get_adaptive_windows(lookback_days)

    for window in window_sizes:
        if len(recent) < window:
            continue

        segment = recent.tail(window)
        seg_high = float(segment["High"].max())
        seg_low = float(segment["Low"].min())

        if seg_low <= 0:
            continue

        depth_pct = (seg_high - seg_low) / seg_high * 100
        contractions.append(
            {
                "high": round(seg_high, 2),
                "low": round(seg_low, 2),
                "depth_pct": round(depth_pct, 2),
                "duration_days": window,
            }
        )

    contractions.sort(key=lambda x: x["duration_days"], reverse=True)
    return contractions


def _get_adaptive_windows(lookback: int) -> list[int]:
    if lookback >= 100:
        return [lookback, lookback // 2, lookback // 4, lookback // 8]
    if lookback >= 60:
        return [lookback, lookback // 2, lookback // 4]
    return [lookback, lookback // 2]


def calculate_vcp(
    df: pd.DataFrame,
    lookback_days: int = 120,
    min_contractions: int = 2,
    t1_depth_min: float = 10.0,
    t1_depth_max: float = 40.0,
    contraction_ratio: float = 0.75,
    min_contraction_days: int = 5,
) -> dict:
    contractions = find_contractions(df, lookback_days, min_contraction_days)

    if len(contractions) < min_contractions:
        return {
            "is_vcp": False,
            "score": 0.0,
            "contractions": contractions,
            "pivot": None,
            "details": {
                "reason": f"Only {len(contractions)} contractions found (need {min_contractions})",
            },
        }

    t1 = contractions[0]
    if t1["depth_pct"] < t1_depth_min:
        return {
            "is_vcp": False,
            "score": 0.0,
            "contractions": contractions,
            "pivot": None,
            "details": {"reason": f"T1 depth {t1['depth_pct']}% < minimum {t1_depth_min}%"},
        }

    if t1["depth_pct"] > t1_depth_max:
        return {
            "is_vcp": False,
            "score": 0.0,
            "contractions": contractions,
            "pivot": None,
            "details": {"reason": f"T1 depth {t1['depth_pct']}% > maximum {t1_depth_max}%"},
        }

    is_tightening = True
    for i in range(1, len(contractions)):
        ratio = contractions[i]["depth_pct"] / contractions[i - 1]["depth_pct"]
        if ratio > contraction_ratio:
            is_tightening = False
            break

    if not is_tightening:
        return {
            "is_vcp": False,
            "score": 0.0,
            "contractions": contractions,
            "pivot": None,
            "details": {"reason": "Contractions not consistently tightening"},
        }

    pivot = contractions[-1]["high"]
    score = _score_contraction_quality(contractions, t1_depth_min, t1_depth_max)

    return {
        "is_vcp": True,
        "score": round(score, 1),
        "contractions": contractions,
        "pivot": pivot,
        "details": {
            "num_contractions": len(contractions),
            "t1_depth": t1["depth_pct"],
            "final_depth": contractions[-1]["depth_pct"],
        },
    }


def _score_contraction_quality(
    contractions: list[dict],
    t1_depth_min: float,
    t1_depth_max: float,
) -> float:
    n = len(contractions)
    if n >= 4:
        base = 85
    elif n >= 3:
        base = 72
    else:
        base = 57

    score = float(base)
    ratios = [
        contractions[i]["depth_pct"] / contractions[i - 1]["depth_pct"]
        for i in range(1, n)
    ]
    avg_ratio = sum(ratios) / len(ratios) if ratios else 1.0

    if avg_ratio < 0.60:
        score += 10
    elif avg_ratio < 0.70:
        score += 5

    if contractions[-1]["depth_pct"] < 5.0:
        score += 10
    elif contractions[-1]["depth_pct"] < 8.0:
        score += 5

    t1_depth = contractions[0]["depth_pct"]
    if 15.0 <= t1_depth <= 30.0:
        score += 5

    if t1_depth < t1_depth_min:
        score -= 10
    if t1_depth > t1_depth_max:
        score -= 15

    return max(0.0, min(100.0, score))
