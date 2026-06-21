"""VCP v1 簡化規則（daily brief vcp-v1 對應）。"""

from __future__ import annotations

import pandas as pd

MIN_BARS = 60
PIVOT_LOOKBACK = 10
CONTRACTION_DAYS = 30
SEGMENTS = 3


def _ma(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    val = float(series.iloc[-window:].mean())
    return val if val > 0 else None


def _segment_range_pct(highs: pd.Series, lows: pd.Series) -> float:
    hi = float(highs.max())
    lo = float(lows.min())
    if hi <= 0:
        return 0.0
    return (hi - lo) / hi * 100.0


def _position_52w(closes: pd.Series) -> tuple[float | None, float | None]:
    if len(closes) < 126:
        return None, None
    window = closes.iloc[-252:] if len(closes) >= 252 else closes
    hi = float(window.max())
    lo = float(window.min())
    close = float(closes.iloc[-1])
    if hi <= lo:
        return None, None
    pos = (close - lo) / (hi - lo) * 100.0
    dist_high = (close - hi) / hi * 100.0
    return round(pos, 2), round(dist_high, 2)


def evaluate_vcp_v1(stock_df: pd.DataFrame) -> dict:
    """
    v1 規則（對應 reports vcp-v1 brief）：
    1. close > MA50 > MA150（不足 150 日則 MA60）
    2. 52w 位 ≥ 65%、距 52w 高 > -12%
    3. 近 30 日分 3 段，末段 range% ≤ 首段 × 0.78
    4. 近 5 日均量 ≤ 中段均量 × 0.85
    5. pivot = 近 10 日最高
    """
    if len(stock_df) < MIN_BARS:
        return _reject("K 線不足", min_bars=len(stock_df))

    closes = stock_df["Close"]
    highs = stock_df["High"]
    lows = stock_df["Low"]
    volumes = stock_df["Volume"]
    close = float(closes.iloc[-1])

    ma50 = _ma(closes, 50)
    ma_long = _ma(closes, 150) if len(closes) >= 150 else _ma(closes, 60)
    pos_52w, dist_high = _position_52w(closes)

    trend_ok = (
        ma50 is not None
        and ma_long is not None
        and close > ma50 > ma_long
        and pos_52w is not None
        and pos_52w >= 65.0
        and dist_high is not None
        and dist_high > -12.0
    )

    tail = stock_df.tail(CONTRACTION_DAYS)
    seg_len = CONTRACTION_DAYS // SEGMENTS
    seg_ranges: list[float] = []
    for i in range(SEGMENTS):
        chunk = tail.iloc[i * seg_len : (i + 1) * seg_len if i < SEGMENTS - 1 else CONTRACTION_DAYS]
        if chunk.empty:
            seg_ranges.append(999.0)
        else:
            seg_ranges.append(_segment_range_pct(chunk["High"], chunk["Low"]))
    contraction_ratio = seg_ranges[-1] / seg_ranges[0] if seg_ranges[0] > 0 else 999.0
    contraction_ok = seg_ranges[-1] <= seg_ranges[0] * 0.78

    vol_tail = volumes.tail(CONTRACTION_DAYS)
    v_seg = CONTRACTION_DAYS // SEGMENTS
    vol_first = float(vol_tail.iloc[:v_seg].mean()) if v_seg else 0.0
    vol_mid = float(vol_tail.iloc[v_seg : 2 * v_seg].mean()) if v_seg else vol_first
    vol_last5 = float(volumes.tail(5).mean())
    vol_dry_ratio = vol_last5 / vol_mid if vol_mid > 0 else 999.0
    vol_ok = vol_mid > 0 and vol_last5 <= vol_mid * 0.85

    pivot = float(highs.tail(PIVOT_LOOKBACK).max())
    dist_pivot = (pivot - close) / pivot * 100.0 if pivot > 0 else 0.0

    if dist_pivot < 0:
        stage = "pivot_break"
    elif dist_pivot <= 3.0:
        stage = "pivot_near"
    else:
        stage = "setup"

    parts = [trend_ok, contraction_ok, vol_ok]
    passed = all(parts)
    score = sum(25 if p else 0 for p in parts) + (25 if stage in ("pivot_near", "pivot_break") else 10)

    if not passed:
        reasons = []
        if not trend_ok:
            reasons.append("趨勢未達標")
        if not contraction_ok:
            reasons.append(f"收斂比 {contraction_ratio:.2f}")
        if not vol_ok:
            reasons.append(f"量縮比 {vol_dry_ratio:.2f}")
        return _reject(" · ".join(reasons) or "未達標", score=score, stage=stage, pivot=pivot)

    return {
        "passed": True,
        "composite_score": float(score),
        "stage": stage,
        "pivot": round(pivot, 2),
        "dist_pivot_pct": round(abs(dist_pivot), 2),
        "contraction_ratio": round(contraction_ratio, 3),
        "vol_dry_ratio": round(vol_dry_ratio, 3),
        "position_52w_pct": pos_52w,
        "quality": "Good" if score >= 75 else "Fair",
    }


def _reject(reason: str, **extra) -> dict:
    return {
        "passed": False,
        "composite_score": float(extra.get("score") or 0.0),
        "reject_reason": reason,
        "stage": extra.get("stage", "reject"),
        "pivot": extra.get("pivot"),
        "quality": "Poor",
    }
