"""Breadth impulse · Zweig thrust + Deemer BAM (LuxAlgo Market Breadth Toolkit).

Complements **Breadth zone** (MA participation level) with event-style thrust signals
(adv/decl ratio EMA spike). Used by Regime market breadth（市場廣度）sub-block and validation backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from market_breadth_ma import classify_breadth_zone
from research.backtest.finpilot_local_backtest import load_price_panels


@dataclass(frozen=True)
class BreadthImpulseParams:
    zweig_low: float = 0.40
    zweig_high: float = 0.615
    zweig_ema_span: int = 10
    deemer_10d_ratio: float = 1.97
    thrust_hold_days: int = 63
    exposure_mid: float = 0.75
    exposure_low: float = 0.50

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BreadthImpulseParams:
        if not raw:
            return cls()
        return cls(
            zweig_low=float(raw.get("zweig_low", 0.40)),
            zweig_high=float(raw.get("zweig_high", 0.615)),
            zweig_ema_span=int(raw.get("zweig_ema_span", 10)),
            deemer_10d_ratio=float(raw.get("deemer_10d_ratio", 1.97)),
            thrust_hold_days=int(raw.get("thrust_hold_days", 63)),
            exposure_mid=float(raw.get("exposure_mid", 0.75)),
            exposure_low=float(raw.get("exposure_low", 0.50)),
        )


ZweigEmaTier = Literal["off", "low", "mid", "high"]

ZWEIG_EMA_TIER_DISPLAY: dict[ZweigEmaTier, str] = {
    "off": "Off · 關閉 (<45%)",
    "low": "Low · 偏低 (45–50%)",
    "mid": "Mid · 中等 (50–58%)",
    "high": "High · 偏強 (≥58%)",
}


def classify_zweig_ema_tier(
    zweig_ema: float,
    *,
    off_max: float = 0.45,
    low_max: float = 0.50,
    mid_max: float = 0.58,
) -> ZweigEmaTier:
    if zweig_ema < off_max:
        return "off"
    if zweig_ema < low_max:
        return "low"
    if zweig_ema < mid_max:
        return "mid"
    return "high"


def daily_adv_decl(close: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    rets = close.pct_change(fill_method=None)
    adv = (rets > 0).sum(axis=1).astype(float)
    decl = (rets < 0).sum(axis=1).astype(float)
    return adv, decl


def zweig_thrust_flags(
    zweig_ema: pd.Series,
    *,
    zweig_low: float,
    zweig_high: float,
) -> pd.Series:
    """True on days where 10D EMA breadth ratio crosses from <low to >high."""
    out = pd.Series(False, index=zweig_ema.index)
    vals = zweig_ema.to_numpy(dtype=float)
    for i in range(9, len(vals)):
        window = vals[i - 9 : i + 1]
        if np.nanmin(window) < zweig_low and vals[i] > zweig_high:
            out.iloc[i] = True
    return out


def compute_impulse_panel(
    adv: pd.Series,
    decl: pd.Series,
    index: pd.Index,
    params: BreadthImpulseParams,
) -> pd.DataFrame:
    """Daily Zweig / Deemer impulse flags and thrust window state."""
    ratio = adv / (adv + decl).replace(0, np.nan)
    zweig_ema = ratio.ewm(span=params.zweig_ema_span, adjust=False).mean().reindex(index).ffill()
    deemer = (adv.rolling(10).sum() / decl.rolling(10).sum().replace(0, np.nan)).reindex(index)
    thrust_today = zweig_thrust_flags(
        zweig_ema,
        zweig_low=params.zweig_low,
        zweig_high=params.zweig_high,
    )
    deemer_today = deemer >= params.deemer_10d_ratio

    thrust_active: list[bool] = []
    days_remaining: list[int] = []
    thrust_until = -1
    for i, d in enumerate(index):
        if bool(thrust_today.get(d, False)) or bool(deemer_today.get(d, False)):
            thrust_until = i + params.thrust_hold_days
        active = i <= thrust_until
        thrust_active.append(active)
        days_remaining.append(max(thrust_until - i, 0) if active else 0)

    return pd.DataFrame(
        {
            "trade_date": index.astype(str),
            "zweig_ema": zweig_ema.values,
            "deemer_ratio": deemer.values,
            "zweig_thrust_today": thrust_today.reindex(index).fillna(False).values,
            "deemer_bam_today": deemer_today.reindex(index).fillna(False).values,
            "thrust_active": thrust_active,
            "thrust_days_remaining": days_remaining,
        },
        index=index,
    )


def ma_zone_exposure(pct_above_200: float | pd.Series) -> float | pd.Series:
    """Exposure tiers from **Breadth zone** (200MA % only · state, no events)."""

    def _one(pct: float) -> float:
        zone = classify_breadth_zone(pct)
        return {
            "oversold": 0.0,
            "weak": 0.25,
            "neutral": 0.50,
            "strong": 0.75,
            "overbought": 1.0,
        }[zone]

    if isinstance(pct_above_200, pd.Series):
        return pct_above_200.map(lambda v: _one(float(v)) if pd.notna(v) else 0.0)
    return _one(float(pct_above_200))


def zweig_state_exposure(
    impulse: pd.DataFrame,
    params: BreadthImpulseParams,
) -> pd.Series:
    """Zweig EMA tier exposure · **no** thrust / Deemer hold window."""
    exp = pd.Series(0.0, index=impulse.index)
    for d in impulse.index:
        z = float(impulse.at[d, "zweig_ema"])
        if np.isnan(z):
            exp.loc[d] = 0.0
        elif z >= params.zweig_high:
            exp.loc[d] = 1.0
        elif z >= 0.50:
            exp.loc[d] = params.exposure_mid
        elif z >= 0.45:
            exp.loc[d] = params.exposure_low
        else:
            exp.loc[d] = 0.0
    return exp


def luxalgo_exposure(
    impulse: pd.DataFrame,
    params: BreadthImpulseParams,
) -> pd.Series:
    """Full LuxAlgo-style exposure: thrust window overrides + Zweig EMA tiers."""
    exp = pd.Series(0.0, index=impulse.index)
    for d in impulse.index:
        if bool(impulse.at[d, "thrust_active"]):
            exp.loc[d] = 1.0
            continue
        z = float(impulse.at[d, "zweig_ema"])
        if np.isnan(z):
            exp.loc[d] = 0.0
        elif z >= params.zweig_high:
            exp.loc[d] = 1.0
        elif z >= 0.50:
            exp.loc[d] = params.exposure_mid
        elif z >= 0.45:
            exp.loc[d] = params.exposure_low
        else:
            exp.loc[d] = 0.0
    return exp


def _impulse_row_at(impulse: pd.DataFrame, as_of: str) -> tuple[pd.Series, str] | tuple[None, None]:
    if impulse.empty:
        return None, None
    sub = impulse[impulse.index <= as_of]
    if sub.empty:
        return None, None
    return sub.iloc[-1], str(sub.index[-1])


def rhythm_snapshot_at(
    impulse: pd.DataFrame,
    as_of: str,
    *,
    tier_cfg: dict[str, float] | None = None,
) -> dict[str, Any]:
    """PIT snapshot · Zweig EMA rhythm tier (diagnostic · no exposure %)."""
    cfg = tier_cfg or {"off_max": 0.45, "low_max": 0.50, "mid_max": 0.58}
    row, trade_date = _impulse_row_at(impulse, as_of)
    if row is None or trade_date is None:
        return {"available": False, "error": "no rhythm on as_of"}
    z = float(row["zweig_ema"]) if pd.notna(row["zweig_ema"]) else None
    if z is None:
        return {"available": False, "error": "zweig_ema missing"}
    tier = classify_zweig_ema_tier(z, **cfg)
    sub = impulse[impulse.index <= as_of]
    delta_5d: float | None = None
    if len(sub) >= 6:
        z_now = float(sub.iloc[-1]["zweig_ema"])
        z_prev = float(sub.iloc[-6]["zweig_ema"])
        if pd.notna(z_now) and pd.notna(z_prev):
            delta_5d = round((z_now - z_prev) * 100.0, 1)
    return {
        "available": True,
        "trade_date": trade_date,
        "zweig_ema_pct": round(z * 100.0, 1),
        "zweig_ema_tier": tier,
        "display": ZWEIG_EMA_TIER_DISPLAY[tier],
        "zweig_ema_delta_5d": delta_5d,
        "tier_off_max_pct": round(float(cfg["off_max"]) * 100.0, 1),
        "tier_low_max_pct": round(float(cfg["low_max"]) * 100.0, 1),
        "tier_mid_max_pct": round(float(cfg["mid_max"]) * 100.0, 1),
    }


def impulse_event_snapshot_at(
    impulse: pd.DataFrame,
    as_of: str,
    *,
    params: BreadthImpulseParams | None = None,
) -> dict[str, Any]:
    """PIT snapshot · Zweig thrust + Deemer BAM event layer."""
    p = params or BreadthImpulseParams()
    row, trade_date = _impulse_row_at(impulse, as_of)
    if row is None or trade_date is None:
        return {"available": False, "error": "no impulse on as_of"}
    deemer = float(row["deemer_ratio"]) if pd.notna(row["deemer_ratio"]) else None
    return {
        "available": True,
        "trade_date": trade_date,
        "deemer_ratio": round(deemer, 2) if deemer is not None else None,
        "zweig_thrust_today": bool(row["zweig_thrust_today"]),
        "deemer_bam_today": bool(row["deemer_bam_today"]),
        "deemer_flag": bool(row["deemer_bam_today"]) or bool(row["thrust_active"]),
        "thrust_active": bool(row["thrust_active"]),
        "thrust_days_remaining": int(row["thrust_days_remaining"]),
        "thrust_hold_days": p.thrust_hold_days,
        "zweig_low_pct": round(p.zweig_low * 100.0, 1),
        "zweig_high_pct": round(p.zweig_high * 100.0, 1),
        "deemer_threshold": p.deemer_10d_ratio,
    }


def impulse_snapshot_at(
    impulse: pd.DataFrame,
    as_of: str,
    *,
    params: BreadthImpulseParams | None = None,
    tier_cfg: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Legacy merged snapshot (rhythm + impulse fields)."""
    rhythm = rhythm_snapshot_at(impulse, as_of, tier_cfg=tier_cfg)
    event = impulse_event_snapshot_at(impulse, as_of, params=params)
    if not rhythm.get("available") and not event.get("available"):
        return {"available": False, "error": event.get("error") or rhythm.get("error")}
    out: dict[str, Any] = {"available": True}
    if rhythm.get("available"):
        out.update({k: v for k, v in rhythm.items() if k != "available"})
    if event.get("available"):
        out.update({k: v for k, v in event.items() if k not in out or k == "trade_date"})
    return out


def build_impulse_panel_from_close(
    close: pd.DataFrame,
    *,
    params: BreadthImpulseParams | None = None,
) -> pd.DataFrame:
    p = params or BreadthImpulseParams()
    adv, decl = daily_adv_decl(close)
    return compute_impulse_panel(adv, decl, close.index, p)


def load_impulse_panel(
    conn,
    *,
    params: BreadthImpulseParams | None = None,
) -> pd.DataFrame:
    close, _, _ = load_price_panels(conn)
    return build_impulse_panel_from_close(close, params=params)
