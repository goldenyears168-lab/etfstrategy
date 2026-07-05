"""Batch technical indicators from daily OHLCV (KD, MA, volume ratios)."""
from __future__ import annotations

from typing import Sequence

KD_PERIOD = 9
KD_SMOOTH = 3
HIGH_LOOKBACK = 120


def _sma(values: Sequence[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def compute_kd_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    period: int = KD_PERIOD,
    smooth: int = KD_SMOOTH,
) -> tuple[list[float | None], list[float | None], list[int]]:
    """Taiwan-style stochastic KD (9,3,3) · returns K, D, cross_above_20 flags."""
    n = len(closes)
    k_vals: list[float | None] = [None] * n
    d_vals: list[float | None] = [None] * n
    cross: list[int] = [0] * n
    k_prev = 50.0
    d_prev = 50.0

    for i in range(n):
        if i + 1 < period:
            continue
        window_high = max(highs[i + 1 - period : i + 1])
        window_low = min(lows[i + 1 - period : i + 1])
        if window_high <= window_low:
            rsv = 50.0
        else:
            rsv = (closes[i] - window_low) / (window_high - window_low) * 100.0
        k_prev = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * rsv
        d_prev = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_prev
        k_vals[i] = round(k_prev, 4)
        d_vals[i] = round(d_prev, 4)
        prev_k = k_vals[i - 1] if i > 0 else None
        if prev_k is not None and prev_k < 20.0 and k_prev >= 20.0:
            cross[i] = 1

    return k_vals, d_vals, cross


def compute_technical_rows(
    stock_id: str,
    bars: list[dict],
    *,
    source: str = "computed",
) -> list[dict]:
    """bars: ascending trade_date · keys open/high/low/close/volume."""
    if not bars:
        return []

    closes = [float(b["close"]) for b in bars]
    highs = [float(b.get("high") or b["close"]) for b in bars]
    lows = [float(b.get("low") or b["close"]) for b in bars]
    volumes = [float(b["volume"]) if b.get("volume") is not None else 0.0 for b in bars]

    k_series, d_series, cross_flags = compute_kd_series(highs, lows, closes)

    rows: list[dict] = []
    for i, bar in enumerate(bars):
        close = closes[i]
        ma5 = _sma(closes[: i + 1], 5)
        ma10 = _sma(closes[: i + 1], 10)
        ma20 = _sma(closes[: i + 1], 20)
        ma60 = _sma(closes[: i + 1], 60)

        ret_1d = None
        if i >= 1 and closes[i - 1] > 0:
            ret_1d = round((close / closes[i - 1] - 1.0) * 100.0, 4)
        ret_5d = None
        if i >= 5 and closes[i - 5] > 0:
            ret_5d = round((close / closes[i - 5] - 1.0) * 100.0, 4)

        vol_slice = volumes[max(0, i - 5) : i]
        vol_avg = sum(vol_slice) / len(vol_slice) if vol_slice else None
        vol_ratio = None
        if vol_avg and vol_avg > 0 and volumes[i] > 0:
            vol_ratio = round(volumes[i] / vol_avg, 4)

        start = max(0, i + 1 - HIGH_LOOKBACK)
        high_120 = max(highs[start : i + 1]) if i >= 0 else None
        is_high = 1 if high_120 is not None and close >= high_120 else 0

        rows.append(
            {
                "stock_id": stock_id,
                "trade_date": str(bar["trade_date"])[:10],
                "ma5": round(ma5, 4) if ma5 is not None else None,
                "ma10": round(ma10, 4) if ma10 is not None else None,
                "ma20": round(ma20, 4) if ma20 is not None else None,
                "ma60": round(ma60, 4) if ma60 is not None else None,
                "return_1d_pct": ret_1d,
                "return_5d_pct": ret_5d,
                "vol_avg_5d": round(vol_avg, 2) if vol_avg is not None else None,
                "vol_ratio_5d": vol_ratio,
                "kd_k": k_series[i],
                "kd_d": d_series[i],
                "kd_cross_above_20": cross_flags[i],
                "high_120d": round(high_120, 4) if high_120 is not None else None,
                "is_120d_high": is_high,
                "source": source,
            }
        )
    return rows
