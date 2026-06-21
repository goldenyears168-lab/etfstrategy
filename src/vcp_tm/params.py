"""VCP-TM parameter dataclass (tradermonty CLI defaults + TW calibration overrides)."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class VcpTmParams:
    trend_min_score: float = 100.0
    lookback_days: int = 120
    min_contractions: int = 2
    t1_depth_min: float = 8.0
    t1_depth_max: float = 40.0
    contraction_ratio: float = 0.75
    atr_multiplier: float = 1.5
    min_contraction_days: int = 5
    wide_and_loose_threshold: float = 15.0
    breakout_volume_ratio: float = 1.5
    max_above_pivot: float = 3.0
    max_risk: float = 15.0
    max_sma200_extension: float = 50.0
    ext_threshold: float = 8.0

    def as_kwargs(self) -> dict:
        return asdict(self)
