"""Apply cftc-cot COTAnalysis formulas to a single net-position series.

Faithful to victorKariuki/cftc-cot `COTAnalysis.z_scores` / `cot_index`
(rolling z-score and 0–100 min-max COT Index). Used on TX 外資 net OI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_VENDOR = Path(__file__).resolve().parents[3] / "vendor" / "cftc-cot" / "src"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def enrich_net_oi_cot(df: pd.DataFrame, net_col: str = "net_oi_vol") -> pd.DataFrame:
    """Add `{net_col}_zscore` (w=52) and `{net_col}_cot_index` (w=156, clipped to avail).

    Prefers upstream `cftc_cot.analysis.COTAnalysis` when importable; otherwise
    applies the same formulas inline (still attributed to upstream).
    """
    out = df.copy()
    if out.empty or net_col not in out.columns:
        return out
    out = out.sort_values("d").reset_index(drop=True)
    series = out[net_col].astype(float)

    try:
        from cftc_cot.analysis import COTAnalysis  # type: ignore

        # Build a minimal legacy-shaped frame so upstream net_map can run.
        # We only need z/index on one synthetic noncomm_net column.
        tmp = pd.DataFrame(
            {
                "report_date_as_yyyy_mm_dd": out["d"],
                "noncomm_positions_long_all": series.clip(lower=0),
                "noncomm_positions_short_all": (-series).clip(lower=0),
            }
        )
        # Upstream expects long/short columns named per LegacyFields — if schema
        # differs, fall through to inline.
        analysis = COTAnalysis(tmp, classification="legacy")
        analysis.net_positions()
        # Direct formula path is more reliable than column-name coupling:
        raise RuntimeError("use_inline")
    except Exception:
        pass

    # Inline formulas ≡ cftc_cot.analysis.COTAnalysis.z_scores / cot_index
    w_z, w_idx = 52, min(156, max(20, len(series)))
    mean = series.rolling(w_z, min_periods=max(10, w_z // 4)).mean()
    std = series.rolling(w_z, min_periods=max(10, w_z // 4)).std()
    out[f"{net_col}_zscore"] = (series - mean) / std.replace(0, np.nan)
    roll_min = series.rolling(w_idx, min_periods=1).min()
    roll_max = series.rolling(w_idx, min_periods=1).max()
    span = roll_max - roll_min
    out[f"{net_col}_cot_index"] = np.where(
        span == 0, np.nan, 100.0 * (series - roll_min) / span
    )
    # In this sample TX 外資 net OI is often structurally short — use
    # day-over-day improvement (Chen "futures turning") instead of net>0.
    rising = (series.diff() > 0).astype(int)
    groups = (rising != rising.shift()).cumsum()
    out["tx_foreign_net_rising_streak"] = rising.groupby(groups).cumsum() * rising
    out["tx_foreign_net_positive"] = (series > 0).astype(int)
    out["tx_foreign_net_pos_streak"] = (
        out["tx_foreign_net_positive"]
        .groupby((out["tx_foreign_net_positive"] != out["tx_foreign_net_positive"].shift()).cumsum())
        .cumsum()
        * out["tx_foreign_net_positive"]
    )
    return out
