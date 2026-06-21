"""Stage Analysis — Weinstein (1988) weekly stages + Minervini Trend Template (2013).

Weinstein: weekly chart + 30-week SMA lifecycle (Stages 1–4).
Minervini: daily 8-point Trend Template confirming Stage 2 uptrends.
"""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd

Stage = Literal[0, 1, 2, 3, 4]
RegimeName = Literal["broadening", "concentration", "transitional", "contraction"]

WEEKLY_MA_PERIOD = 30
WEEKLY_SLOPE_LOOKBACK = 4
SMA200_TREND_DAYS = 22
MINERVINI_CRITERIA_TOTAL = 8
MINERVINI_RS_MIN = 70
MINERVINI_LOW_PCT = 30.0
MINERVINI_HIGH_PCT = 25.0

STAGE_NAMES: dict[int, str] = {
    0: "unknown",
    1: "basing",
    2: "advancing",
    3: "topping",
    4: "declining",
}

REGIME_SCORES: dict[str, int] = {
    "broadening": 80,
    "concentration": 60,
    "transitional": 50,
    "inflationary": 40,
    "contraction": 20,
}

TREND_POSTURE_SCORES = REGIME_SCORES


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    for src, dst in rename.items():
        if src in out.columns and dst not in out.columns:
            out = out.rename(columns={src: dst})
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
        out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        out = out.set_index("date")
    elif not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
        out = out.sort_index()
    return out


def daily_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to weekly bars (Fri anchor)."""
    daily = _normalize_ohlcv(df)
    if daily.empty:
        return daily
    agg: dict[str, str] = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
    }
    if "Volume" in daily.columns:
        agg["Volume"] = "sum"
    weekly = daily.resample("W-FRI").agg(agg).dropna(subset=["Close"])
    return weekly


def classify_weinstein_stage(df: pd.DataFrame) -> dict[str, Any]:
    """Classify Weinstein stage (1–4) from daily OHLCV via weekly 30-week SMA."""
    weekly = daily_to_weekly(df)
    min_bars = WEEKLY_MA_PERIOD + WEEKLY_SLOPE_LOOKBACK + 8
    if len(weekly) < min_bars:
        return {
            "stage": 0,
            "stage_name": STAGE_NAMES[0],
            "error": f"Insufficient weekly data (need {min_bars}+ bars)",
        }

    close = weekly["Close"]
    low = weekly["Low"]
    ma = close.rolling(WEEKLY_MA_PERIOD, min_periods=WEEKLY_MA_PERIOD).mean()

    price = float(close.iloc[-1])
    ma_now = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-1 - WEEKLY_SLOPE_LOOKBACK])
    ma_slope_pct = (ma_now - ma_prev) / ma_prev * 100.0 if ma_prev else 0.0
    extension_pct = (price - ma_now) / ma_now * 100.0 if ma_now else 0.0

    if len(low) >= 12:
        recent_low = float(low.iloc[-4:].min())
        prior_low = float(low.iloc[-8:-4].min())
        higher_lows = recent_low > prior_low
    else:
        higher_lows = False

    ma_rising = ma_slope_pct > 0.25
    ma_falling = ma_slope_pct < -0.25
    ma_flat = not ma_rising and not ma_falling
    price_above = price > ma_now

    # Weinstein Ch.2 rules — weekly 30W MA + lifecycle structure
    if not price_above and ma_falling:
        stage: Stage = 4
    elif not price_above and extension_pct < -3.0:
        stage = 4
    elif price_above and ma_rising and higher_lows:
        if extension_pct > 12.0 and ma_slope_pct < 0.75:
            stage = 3
        else:
            stage = 2
    elif price_above and ma_flat:
        stage = 3
    elif not price_above and (ma_flat or ma_rising):
        stage = 3
    elif ma_flat or (not price_above and not ma_falling):
        stage = 1
    else:
        stage = 1

    return {
        "stage": stage,
        "stage_name": STAGE_NAMES[stage],
        "price": round(price, 4),
        "ma30": round(ma_now, 4),
        "ma_slope_pct": round(ma_slope_pct, 2),
        "extension_pct": round(extension_pct, 2),
        "higher_lows": higher_lows,
        "price_above_ma30": price_above,
        "ma_rising": ma_rising,
        "ma_falling": ma_falling,
        "weekly_bars": len(weekly),
    }


def ix_stage_to_trend_posture(
    stage: int,
    *,
    trend_score: float = 0.0,
    extension_pct: float | None = None,
) -> RegimeName | str:
    """Map Weinstein stage (+ Minervini score) to trend_posture."""
    if stage == 2:
        if extension_pct is not None and extension_pct > 10.0:
            return "concentration"
        if trend_score >= 100.0:
            return "broadening"
        if trend_score >= 87.5:
            return "broadening"
        return "concentration"
    if stage in (1, 3):
        return "transitional"
    if stage == 4:
        return "contraction"
    return "transitional"


def ix_stage_to_regime_name(stage: int, *, trend_score: float = 0.0, extension_pct: float | None = None) -> str:
    """Deprecated alias → trend_posture."""
    return ix_stage_to_trend_posture(stage, trend_score=trend_score, extension_pct=extension_pct)


def score_trend_posture_from_benchmark(
    *,
    stage: int,
    trend_score: float,
    extension_pct: float | None = None,
) -> int:
    name = ix_stage_to_trend_posture(
        stage, trend_score=trend_score, extension_pct=extension_pct
    )
    return TREND_POSTURE_SCORES.get(name, 50)


def score_regime_from_benchmark(
    *,
    stage: int,
    trend_score: float,
    extension_pct: float | None = None,
) -> int:
    """Deprecated alias."""
    return score_trend_posture_from_benchmark(
        stage=stage, trend_score=trend_score, extension_pct=extension_pct
    )


def _year_extremes(close: pd.Series) -> tuple[float | None, float | None]:
    if len(close) < 20:
        return None, None
    window = close.iloc[-252:] if len(close) >= 252 else close
    return float(window.max()), float(window.min())


def _minervini_criteria_from_closes(
    closes: pd.Series,
    *,
    price: float,
    year_high: float | None,
    year_low: float | None,
    rs_rank: int | None,
) -> tuple[list[bool], dict[str, dict[str, Any]]]:
    if len(closes) < 200:
        return [False] * MINERVINI_CRITERIA_TOTAL, {}

    sma50 = float(closes.iloc[-50:].mean())
    sma150 = float(closes.iloc[-150:].mean())
    sma200 = float(closes.iloc[-200:].mean())
    sma200_prev = float(closes.iloc[-222:-22].mean()) if len(closes) >= 222 else sma200

    c1 = price > sma150 and price > sma200
    c2 = sma150 > sma200
    c3 = sma200 > sma200_prev
    c4 = sma50 > sma150 and sma50 > sma200
    c5 = price > sma50
    c6 = False
    if year_low and year_low > 0:
        c6 = (price - year_low) / year_low * 100.0 >= MINERVINI_LOW_PCT
    c7 = False
    if year_high and year_high > 0:
        c7 = (year_high - price) / year_high * 100.0 <= MINERVINI_HIGH_PCT
    c8 = rs_rank is not None and rs_rank > MINERVINI_RS_MIN

    flags = [c1, c2, c3, c4, c5, c6, c7, c8]
    criteria = {
        "c1_price_above_sma150_200": {
            "passed": c1,
            "detail": f"Price {price:.2f} vs SMA150 {sma150:.2f} / SMA200 {sma200:.2f}",
        },
        "c2_sma150_above_sma200": {
            "passed": c2,
            "detail": f"SMA150 {sma150:.2f} vs SMA200 {sma200:.2f}",
        },
        "c3_sma200_trending_up": {
            "passed": c3,
            "detail": f"SMA200 {sma200:.2f} vs 22d ago {sma200_prev:.2f}",
        },
        "c4_sma50_above_sma150_200": {
            "passed": c4,
            "detail": f"SMA50 {sma50:.2f} above SMA150/200",
        },
        "c5_price_above_sma50": {
            "passed": c5,
            "detail": f"Price {price:.2f} vs SMA50 {sma50:.2f}",
        },
        "c6_30pct_above_52w_low": {
            "passed": c6,
            "detail": f"52w low {year_low} (need +{MINERVINI_LOW_PCT:.0f}%)",
        },
        "c7_within_25pct_52w_high": {
            "passed": c7,
            "detail": f"52w high {year_high} (need within {MINERVINI_HIGH_PCT:.0f}%)",
        },
        "c8_rs_rank_above_70": {
            "passed": c8,
            "detail": f"RS rank {rs_rank} (need > {MINERVINI_RS_MIN})",
        },
    }
    return flags, criteria


def calculate_minervini_trend_template(
    df: pd.DataFrame,
    *,
    rs_rank: int | None = None,
    year_high: float | None = None,
    year_low: float | None = None,
) -> dict[str, Any]:
    """Minervini 8-point Trend Template · daily SMA · all-or-nothing pass."""
    norm = _normalize_ohlcv(df)
    if len(norm) < 200:
        return {
            "criteria": [],
            "criteria_met": 0,
            "criteria_total": MINERVINI_CRITERIA_TOTAL,
            "score": 0.0,
            "raw_score": 0.0,
            "passed": False,
            "stage": 0,
            "details": {"error": "Insufficient data (need 200+ days)"},
        }

    close = norm["Close"]
    price = float(close.iloc[-1])
    yh, yl = _year_extremes(close)
    yh = year_high if year_high is not None else yh
    yl = year_low if year_low is not None else yl

    flags, criteria = _minervini_criteria_from_closes(
        close, price=price, year_high=yh, year_low=yl, rs_rank=rs_rank
    )
    met = sum(flags)
    score = round(met * (100.0 / MINERVINI_CRITERIA_TOTAL), 1)
    passed = met == MINERVINI_CRITERIA_TOTAL

    weinstein = classify_weinstein_stage(norm)
    if passed:
        stage: Stage = 2
    else:
        stage = weinstein["stage"] if weinstein["stage"] else 0

    sma50 = float(close.iloc[-50:].mean())
    sma150 = float(close.iloc[-150:].mean())
    sma200 = float(close.iloc[-200:].mean())

    return {
        "criteria": flags,
        "criteria_met": met,
        "criteria_total": MINERVINI_CRITERIA_TOTAL,
        "score": score,
        "raw_score": score,
        "passed": passed,
        "stage": stage,
        "stage_name": STAGE_NAMES.get(stage, STAGE_NAMES[0]),
        "details": {
            "price": price,
            "ma_50": round(sma50, 2),
            "ma_150": round(sma150, 2),
            "ma_200": round(sma200, 2),
            "criteria_met": met,
            "weinstein": weinstein,
        },
        "criteria_detail": criteria,
    }


def calculate_minervini_trend_template_mrf(
    historical_prices: list[dict],
    quote_data: dict,
    *,
    rs_rank: int | None = None,
    ext_threshold: float = 8.0,
    max_sma200_extension: float = 50.0,
) -> dict[str, Any]:
    """Minervini template for vcp_tm MRF price rows (most-recent-first)."""
    if not historical_prices or len(historical_prices) < 50:
        return {
            "score": 0,
            "raw_score": 0,
            "passed": False,
            "criteria": {},
            "error": "Insufficient historical data (need 50+ days)",
        }

    closes = [float(d.get("close", d.get("adjClose", 0))) for d in historical_prices]
    price = float(quote_data.get("price", closes[0] if closes else 0))
    year_high = float(quote_data.get("yearHigh") or 0) or None
    year_low = float(quote_data.get("yearLow") or 0) or None

    series = pd.Series(list(reversed(closes)))
    flags, criteria = _minervini_criteria_from_closes(
        series,
        price=price,
        year_high=year_high,
        year_low=year_low,
        rs_rank=rs_rank,
    )
    met = sum(flags)
    raw_score = round(met * (100.0 / MINERVINI_CRITERIA_TOTAL), 1)
    passed = met == MINERVINI_CRITERIA_TOTAL

    sma50 = float(series.iloc[-50:].mean()) if len(series) >= 50 else None
    sma150 = float(series.iloc[-150:].mean()) if len(series) >= 150 else None
    sma200 = float(series.iloc[-200:].mean()) if len(series) >= 200 else None

    extended_penalty, sma50_distance_pct = _extended_penalty(
        price, sma50, base_threshold=ext_threshold
    )
    sma200_penalty, sma200_distance_pct = _sma200_penalty(
        price, sma200, max_extension=max_sma200_extension
    )
    score = max(0.0, raw_score + extended_penalty)

    return {
        "score": score,
        "raw_score": raw_score,
        "passed": passed,
        "extended_penalty": extended_penalty,
        "sma200_penalty": sma200_penalty,
        "sma50_distance_pct": round(sma50_distance_pct, 2)
        if sma50_distance_pct is not None
        else None,
        "sma200_distance_pct": round(sma200_distance_pct, 2)
        if sma200_distance_pct is not None
        else None,
        "criteria_passed": met,
        "criteria_total": MINERVINI_CRITERIA_TOTAL,
        "criteria": criteria,
        "stage": 2 if passed else 0,
        "sma50": round(sma50, 2) if sma50 else None,
        "sma150": round(sma150, 2) if sma150 else None,
        "sma200": round(sma200, 2) if sma200 else None,
        "error": None,
    }


def calculate_simple_trend(df: pd.DataFrame) -> dict[str, Any]:
    """Chunge simplified gate: price above MA50 and MA200."""
    full = calculate_minervini_trend_template(df)
    if full.get("details", {}).get("error"):
        return {"passed": False, "score": 0.0, "details": full["details"]}
    close = _normalize_ohlcv(df)["Close"]
    price = float(close.iloc[-1])
    ma50 = float(close.iloc[-50:].mean())
    ma200 = float(close.iloc[-200:].mean())
    passed = price > ma50 and price > ma200
    return {
        "passed": passed,
        "score": 100.0 if passed else 0.0,
        "details": {
            **full["details"],
            "above_ma50": price > ma50,
            "above_ma200": price > ma200,
        },
    }


def vectorized_minervini_criteria_count(close: pd.DataFrame) -> pd.DataFrame:
    """Per-date per-symbol Minervini criteria met (0–7; RS omitted in universe scans)."""
    ma50 = close.rolling(50, min_periods=50).mean()
    ma150 = close.rolling(150, min_periods=150).mean()
    ma200 = close.rolling(200, min_periods=200).mean()
    ma200_up = ma200 > ma200.shift(SMA200_TREND_DAYS)
    roll_high = close.rolling(252, min_periods=50).max()
    roll_low = close.rolling(252, min_periods=50).min()
    pct_above_low = (close - roll_low) / roll_low.replace(0, pd.NA) * 100.0
    pct_below_high = (roll_high - close) / roll_high.replace(0, pd.NA) * 100.0

    crit = (
        ((close > ma150) & (close > ma200)).astype(int)
        + (ma150 > ma200).astype(int)
        + ma200_up.astype(int)
        + ((ma50 > ma150) & (ma50 > ma200)).astype(int)
        + (close > ma50).astype(int)
        + (pct_above_low >= MINERVINI_LOW_PCT).astype(int)
        + (pct_below_high <= MINERVINI_HIGH_PCT).astype(int)
    )
    return crit


def vectorized_minervini_pass_pct(
    close: pd.DataFrame,
    *,
    min_pass: int = MINERVINI_CRITERIA_TOTAL,
) -> pd.Series:
    """Daily fraction of universe passing ≥ min_pass criteria (RS excluded → max 7)."""
    crit = vectorized_minervini_criteria_count(close)
    effective_min = min(min_pass, 7)
    passed = crit >= effective_min
    valid = crit.notna() & (crit >= 0)
    return (passed.sum(axis=1) / valid.sum(axis=1).replace(0, pd.NA)).fillna(0.0)


def minervini_pass_at_date(
    close: pd.DataFrame,
    signal_date: str,
    stock_ids: pd.Index,
    *,
    min_pass: int = 7,
) -> pd.Index:
    """Symbols passing Minervini template at signal_date (vectorized, RS omitted)."""
    if signal_date not in close.index:
        return pd.Index([])
    idx = close.index.get_loc(signal_date)
    if idx < 200:
        return pd.Index([])
    sub = close.iloc[: idx + 1]
    px = sub.iloc[-1]
    sma50 = sub.rolling(50).mean().iloc[-1]
    sma150 = sub.rolling(150).mean().iloc[-1]
    sma200 = sub.rolling(200).mean().iloc[-1]
    sma200_prev = sub.rolling(200).mean().iloc[-22]
    roll_high = sub.rolling(252, min_periods=50).max().iloc[-1]
    roll_low = sub.rolling(252, min_periods=50).min().iloc[-1]

    met = (
        ((px > sma150) & (px > sma200)).astype(int)
        + (sma150 > sma200).astype(int)
        + (sma200 > sma200_prev).astype(int)
        + ((sma50 > sma150) & (sma50 > sma200)).astype(int)
        + (px > sma50).astype(int)
    )
    if roll_low is not None and roll_low.notna().any():
        met = met + (
            (px - roll_low) / roll_low.replace(0, pd.NA) * 100 >= MINERVINI_LOW_PCT
        ).astype(int)
    if roll_high is not None and roll_high.notna().any():
        met = met + (
            (roll_high - px) / roll_high.replace(0, pd.NA) * 100 <= MINERVINI_HIGH_PCT
        ).astype(int)

    ok = met >= min(min_pass, 7)
    ok = ok[ok.index.isin(stock_ids)]
    return ok[ok].index


def classify_ix_trend_posture(df: pd.DataFrame) -> dict[str, Any]:
    """Weinstein weekly stage + Minervini daily score → trend_posture axis."""
    weinstein = classify_weinstein_stage(df)
    minervini = calculate_minervini_trend_template(df)
    stage = int(weinstein.get("stage") or 0)
    trend_score = float(minervini.get("score") or 0.0)
    extension_pct = weinstein.get("extension_pct")
    trend_posture = ix_stage_to_trend_posture(
        stage,
        trend_score=trend_score,
        extension_pct=extension_pct if isinstance(extension_pct, (int, float)) else None,
    )
    return {
        "stage": stage,
        "stage_name": weinstein.get("stage_name"),
        "trend_score": trend_score,
        "trend_posture": trend_posture,
        "trend_posture_score": score_trend_posture_from_benchmark(
            stage=stage,
            trend_score=trend_score,
            extension_pct=extension_pct if isinstance(extension_pct, (int, float)) else None,
        ),
        "weinstein": weinstein,
        "minervini": minervini,
        "extension_pct": extension_pct,
    }


def classify_benchmark_regime(df: pd.DataFrame) -> dict[str, Any]:
    """Deprecated alias — returns trend_posture (+ legacy regime_name key)."""
    out = classify_ix_trend_posture(df)
    out["regime_name"] = out["trend_posture"]
    out["regime_score"] = out["trend_posture_score"]
    return out


def _extended_penalty(
    price: float, sma50: float | None, *, base_threshold: float
) -> tuple[int, float | None]:
    if sma50 is None or sma50 <= 0:
        return 0, None
    distance_pct = (price - sma50) / sma50 * 100.0
    if distance_pct < base_threshold:
        return 0, distance_pct
    excess = distance_pct - base_threshold
    if excess >= 17:
        return -20, distance_pct
    if excess >= 10:
        return -15, distance_pct
    if excess >= 4:
        return -10, distance_pct
    return -5, distance_pct


def _sma200_penalty(
    price: float, sma200: float | None, *, max_extension: float
) -> tuple[int, float | None]:
    if sma200 is None or sma200 <= 0:
        return 0, None
    distance_pct = (price - sma200) / sma200 * 100.0
    if distance_pct <= max_extension:
        return 0, distance_pct
    excess = distance_pct - max_extension
    if excess >= 20:
        return -20, distance_pct
    if excess >= 10:
        return -15, distance_pct
    return -10, distance_pct
