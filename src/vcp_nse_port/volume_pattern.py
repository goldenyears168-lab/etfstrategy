"""Volume dry-up scoring (ported from nse-vcp-screener)."""

from __future__ import annotations

import pandas as pd


def calculate_volume_pattern(df: pd.DataFrame) -> dict:
    if len(df) < 50 or "Volume" not in df.columns:
        return {
            "dry_up_ratio": 1.0,
            "score": 0.0,
            "details": {"error": "Insufficient data or no volume column"},
        }

    vol = df["Volume"]
    avg_50 = float(vol.tail(50).mean())
    avg_10 = float(vol.tail(10).mean())

    if avg_50 <= 0:
        return {
            "dry_up_ratio": 1.0,
            "score": 0.0,
            "details": {"error": "Zero or negative 50-day average volume"},
        }

    dry_up_ratio = avg_10 / avg_50
    return {
        "dry_up_ratio": round(dry_up_ratio, 3),
        "score": round(_score_dry_up(dry_up_ratio), 1),
        "details": {
            "avg_volume_50d": round(avg_50, 0),
            "avg_volume_10d": round(avg_10, 0),
        },
    }


def _score_dry_up(ratio: float) -> float:
    if ratio < 0.40:
        return 90.0
    if ratio < 0.50:
        return 80.0
    if ratio < 0.60:
        return 70.0
    if ratio < 0.70:
        return 60.0
    if ratio < 0.80:
        return 45.0
    if ratio < 0.90:
        return 30.0
    return 15.0
