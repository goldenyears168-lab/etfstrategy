"""Market breadth: % of universe above 50/200-day MA (TradingView S5TH/S5FI style).

Terminology: classify readings into **Breadth zones** (oversold … overbought).
Distinct from **Trend posture** (`trend_posture` on IX0001).
Copytrade **exposure_decision** is an ex-post stratification label only, not a live gate.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

import pandas as pd

from research.backtest.finpilot_local_backtest import load_price_panels
from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

MA_SHORT = 50
MA_LONG = 200
MIN_VALID_STOCKS = 40

# TradingView / StockCharts reference levels (% stocks above MA).
REF_LEVELS_50 = (20.0, 50.0, 80.0)
REF_LEVELS_200 = (20.0, 50.0, 80.0)

BreadthZone = Literal["oversold", "weak", "neutral", "strong", "overbought"]

BREADTH_ZONE_DISPLAY: dict[BreadthZone, str] = {
    "oversold": "Oversold · 超賣 (<20%)",
    "weak": "Weak · 偏弱 (20–40%)",
    "neutral": "Neutral · 中性 (40–60%)",
    "strong": "Strong · 強勢 (60–80%)",
    "overbought": "Overbought · 過熱 (>80%)",
}

BREADTH_ZONE_ZH: dict[BreadthZone, str] = {
    "oversold": "超賣",
    "weak": "偏弱",
    "neutral": "中性",
    "strong": "強勢",
    "overbought": "過熱",
}

BREADTH_ZONE_COLOR: dict[BreadthZone, str] = {
    "oversold": "#7B64B8",
    "weak": "#F0A040",
    "neutral": "#70B0D8",
    "strong": "#1F8A65",
    "overbought": "#C04848",
}

BREADTH_ZONES_ORDER: tuple[BreadthZone, ...] = (
    "oversold",
    "weak",
    "neutral",
    "strong",
    "overbought",
)


def classify_breadth_zone(pct: float) -> BreadthZone:
    """Classify a single %-above-MA reading (TradingView / StockCharts convention)."""
    if pct < 20.0:
        return "oversold"
    if pct < 40.0:
        return "weak"
    if pct < 60.0:
        return "neutral"
    if pct < 80.0:
        return "strong"
    return "overbought"


def compute_ma_breadth_frame(close: pd.DataFrame) -> pd.DataFrame:
    ma50 = close.rolling(MA_SHORT, min_periods=MA_SHORT).mean()
    ma200 = close.rolling(MA_LONG, min_periods=MA_LONG).mean()
    valid50 = close.notna() & ma50.notna()
    valid200 = close.notna() & ma200.notna()
    above50 = (close > ma50) & valid50
    above200 = (close > ma200) & valid200
    n50 = valid50.sum(axis=1)
    n200 = valid200.sum(axis=1)
    pct50 = above50.sum(axis=1) / n50.replace(0, pd.NA) * 100.0
    pct200 = above200.sum(axis=1) / n200.replace(0, pd.NA) * 100.0
    out = pd.DataFrame(
        {
            "trade_date": close.index,
            "pct_above_50": pct50.values,
            "pct_above_200": pct200.values,
            "n_valid_50": n50.values.astype(int),
            "n_valid_200": n200.values.astype(int),
        }
    )
    mask = (out["n_valid_50"] >= MIN_VALID_STOCKS) & (out["n_valid_200"] >= MIN_VALID_STOCKS)
    out.loc[~mask, ["pct_above_50", "pct_above_200"]] = pd.NA
    return out


def _pct_change_over(series: pd.Series, idx: int, lookback: int) -> float | None:
    if idx < lookback:
        return None
    cur = series.iloc[idx]
    prev = series.iloc[idx - lookback]
    if pd.isna(cur) or pd.isna(prev):
        return None
    return float(cur - prev)


def _bench_ret_pct(bench: pd.Series, dates: list[str], as_of: str, lookback: int = 20) -> float | None:
    if as_of not in dates:
        return None
    i = dates.index(as_of)
    if i < lookback:
        return None
    start = dates[i - lookback]
    if start not in bench.index or as_of not in bench.index:
        return None
    b0 = float(bench.loc[start])
    b1 = float(bench.loc[as_of])
    if b0 <= 0:
        return None
    return (b1 / b0 - 1.0) * 100.0


def enrich_breadth_panel(
    frame: pd.DataFrame,
    bench: pd.Series,
    *,
    date_start: str | None = None,
    date_end: str | None = None,
) -> pd.DataFrame:
    panel = frame.dropna(subset=["pct_above_50", "pct_above_200"]).copy()
    if date_start:
        panel = panel[panel["trade_date"] >= date_start]
    if date_end:
        panel = panel[panel["trade_date"] <= date_end]
    if panel.empty:
        return panel

    all_dates = list(frame["trade_date"])
    pct50_series = panel["pct_above_50"].reset_index(drop=True)
    zones_50: list[str] = []
    zones_200: list[str] = []
    bench_closes: list[float | None] = []
    bench_rets: list[float | None] = []
    pct50_chgs: list[float | None] = []
    divergences: list[bool] = []

    for j, (_, row) in enumerate(panel.iterrows()):
        d = str(row["trade_date"])
        p50 = float(row["pct_above_50"])
        p200 = float(row["pct_above_200"])
        zones_50.append(classify_breadth_zone(p50))
        zones_200.append(classify_breadth_zone(p200))
        ret20 = _bench_ret_pct(bench, all_dates, d) if d in all_dates else None
        chg20 = _pct_change_over(pct50_series, j, 20)
        div = (
            ret20 is not None
            and ret20 > 2.0
            and chg20 is not None
            and chg20 < -8.0
            and p50 < 45.0
        )
        bench_closes.append(float(bench.loc[d]) if d in bench.index else None)
        bench_rets.append(ret20)
        pct50_chgs.append(chg20)
        divergences.append(div)

    panel = panel.reset_index(drop=True)
    panel["zone_50"] = zones_50
    panel["zone_200"] = zones_200
    panel["zone_50_display"] = panel["zone_50"].map(BREADTH_ZONE_DISPLAY)
    panel["zone_200_display"] = panel["zone_200"].map(BREADTH_ZONE_DISPLAY)
    panel["participation_gap"] = panel["pct_above_50"] - panel["pct_above_200"]
    panel["bench_close"] = bench_closes
    panel["bench_ret_20d_pct"] = bench_rets
    panel["pct50_chg_20d"] = pct50_chgs
    panel["divergence_flag"] = divergences
    return panel


def build_breadth_panel(
    conn: sqlite3.Connection | None = None,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-12-31",
) -> pd.DataFrame:
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn)
    if own:
        conn.close()
    frame = compute_ma_breadth_frame(close)
    return enrich_breadth_panel(frame, bench, date_start=date_start, date_end=date_end)


def breadth_map_by_date(panel: pd.DataFrame, *, use: Literal["50", "200"] = "200") -> dict[str, str]:
    col = "zone_50" if use == "50" else "zone_200"
    if panel.empty or col not in panel.columns:
        return {}
    return {str(r.trade_date): str(getattr(r, col)) for r in panel.itertuples()}


def monthly_breadth_summary(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    p = panel.copy()
    p["month"] = p["trade_date"].str[:7]
    rows: list[dict] = []
    for ym, g in p.groupby("month", sort=True):
        z200 = g["zone_200"].value_counts()
        dominant = str(z200.idxmax()) if not z200.empty else "neutral"
        rows.append(
            {
                "month": ym,
                "days": len(g),
                "pct50_mean": round(float(g["pct_above_50"].mean()), 1),
                "pct50_min": round(float(g["pct_above_50"].min()), 1),
                "pct50_max": round(float(g["pct_above_50"].max()), 1),
                "pct200_mean": round(float(g["pct_above_200"].mean()), 1),
                "pct200_min": round(float(g["pct_above_200"].min()), 1),
                "pct200_max": round(float(g["pct_above_200"].max()), 1),
                "dominant_zone_200": dominant,
                "dominant_zone_200_zh": BREADTH_ZONE_ZH.get(dominant, dominant),  # type: ignore[arg-type]
                "divergence_days": int(g["divergence_flag"].sum()),
            }
        )
    return pd.DataFrame(rows)


def divergence_events(panel: pd.DataFrame, *, min_gap_days: int = 5) -> list[dict]:
    if panel.empty:
        return []
    flagged = panel[panel["divergence_flag"]]
    if flagged.empty:
        return []
    events: list[dict] = []
    prev: str | None = None
    all_dates = list(panel["trade_date"])
    for row in flagged.itertuples():
        d = str(row.trade_date)
        if prev is not None and d in all_dates and prev in all_dates:
            if all_dates.index(d) - all_dates.index(prev) < min_gap_days:
                prev = d
                continue
        events.append(
            {
                "trade_date": d,
                "pct_above_50": round(float(row.pct_above_50), 1),
                "pct_above_200": round(float(row.pct_above_200), 1),
                "bench_ret_20d_pct": round(float(row.bench_ret_20d_pct), 2)
                if row.bench_ret_20d_pct is not None
                else None,
                "pct50_chg_20d": round(float(row.pct50_chg_20d), 1)
                if row.pct50_chg_20d is not None
                else None,
            }
        )
        prev = d
    return events
