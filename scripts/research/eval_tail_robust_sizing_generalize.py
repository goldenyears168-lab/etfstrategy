"""Item V (wave 5): does the tail-robust vol-target sizing overlay generalize?

`scripts/research/chip_macro/eval_tail_robust_sizing.py` found that a
trailing-realized-vol-target sizing overlay (and a vol-of-vol shock brake) on
top of chip-macro's champion signal made OOS Deflated Sharpe Ratio WORSE, not
better -- despite the champion's known kurtosis problem. This script asks:
is that a property specific to that one signal, or does it hold for other
already-live/adopted strategies too?

Applies the SAME overlay idea (trailing realized vol used to scale exposure,
lag-1 causal, cap at 1.0x -- i.e. can only de-risk, never lever up) to two
different strategies, at the trade level (both are event-driven, multi-name,
irregular-cadence strategies -- unlike chip-macro's single continuous index
series -- so "trailing realized vol" here is computed on each strategy's own
trade-return stream, not on one underlying instrument's price series; see
docstring note per strategy below for why).

  1. leading-dip: reports/research/rrg/20260715_leading_dip_events.csv,
     T2==True quality track (n=67, matches the frozen/adopted sleeve spec's
     "quality" filter used by run_leading_dip_sleeve_validate.py), trade
     return = ex3 (%) -- verified against the spec's own self-validator
     summarize_trades() which also keys off ex3 (median/OOS-median match the
     numbers in reports/research/rrg/20260715_leading_dip_validate.json).
  2. dayflip-futures-short: reports/research/dayflip_revenue_momentum_filter/
     trades_with_revyoy.csv (n=190), trade return = pnl_pct (%).

Sizing choice per strategy (explicit, per task instructions):
  - Both use the strategy's OWN TRADE P&L STREAM for trailing vol, not a
    single underlying instrument's price series. Reason: unlike chip-macro
    (one index, one continuous r_cc series), both leading-dip and dayflip
    open positions in a rotating basket of different single-stock tickers
    (sid / stock column differs trade to trade) with irregular, event-driven
    entry timing -- there is no single continuous underlying price series to
    compute "instrument volatility" on that would apply uniformly across
    trades. The trade-P&L-stream is the natural, available analogue of
    r_cc's role in the original script.

Overlay construction (causal, lag-1, no hindsight):
  - expanding_vol(t)  = std of ALL trade returns strictly BEFORE trade t
                         (min_periods=8; long-run reference level, grows with
                         history -- analogous role to a fixed "target vol").
  - trailing_vol(t)   = std of the last W=10 trade returns strictly BEFORE
                         trade t (short-run realized vol, same role as
                         eval_tail_robust_sizing.py's 20d rolling std).
  - scalar(t)         = clip(expanding_vol(t) / trailing_vol(t), 0, 1.0)
                         -- de-risk only when RECENT vol is running hot
                         relative to the strategy's own long-run vol; never
                         lever above the unscaled baseline (cap 1.0x, same
                         convention as V1 in the chip-macro script).
  - scaled_return(t)  = scalar(t) * raw_trade_return(t)
  - baseline           = raw_trade_return(t) (flat/unscaled, i.e. the
                         as-adopted sizing both strategies actually run).
  - Only known-at-decision-time information (t-1 and earlier trades) is used
    for scalar(t) -- matches the lag-1 discipline in the source script.

Note on statistics: unlike chip-macro (~6y daily index panel), these are
short trade-count series (n=67 / n=190) with no natural "N competing trials"
universe to deflate against, so a full multi-testing Deflated Sharpe Ratio
is not meaningful here. Instead this script reports: per-trade Sharpe
(mean/std of the trade-return stream, NOT annualized -- these are event
returns, not daily bars), skew, excess kurtosis, and the Probabilistic
Sharpe Ratio (PSR, Bailey & Lopez de Prado) against a benchmark SR*=0 using
each series' own n and higher moments -- i.e. the same skew/kurtosis-aware
machinery as chip-macro's dsr() but WITHOUT the trial-count deflation term
(explicitly labeled "PSR-style, undeflated" in the output, not "DSR").

Run: PYTHONPATH=src .venv/bin/python scripts/research/eval_tail_robust_sizing_generalize.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports" / "research" / "tail_robust_sizing_generalize"
OUT.mkdir(parents=True, exist_ok=True)

LEADING_DIP_CSV = ROOT / "reports" / "research" / "rrg" / "20260715_leading_dip_events.csv"
DAYFLIP_CSV = ROOT / "reports" / "research" / "dayflip_revenue_momentum_filter" / "trades_with_revyoy.csv"

W_TRAIL = 10
MIN_EXPAND = 8


def moments(x: pd.Series) -> dict:
    x = pd.Series(x).dropna()
    n = len(x)
    if n < 5 or x.std() == 0:
        return dict(n=n, mean=np.nan, std=np.nan, shp=np.nan, skew=np.nan, kurt=np.nan, psr=np.nan)
    sr = x.mean() / x.std()
    g3 = x.skew()
    g4 = x.kurtosis() + 3.0
    # PSR vs SR*=0, no trial deflation (Bailey & Lopez de Prado 2012, eq. without var_trials term)
    psr = norm.cdf(sr * np.sqrt(n - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2))
    return dict(n=n, mean=x.mean(), std=x.std(), shp=sr, skew=g3, kurt=g4, psr=psr)


def build_overlay(returns: pd.Series) -> pd.DataFrame:
    """returns: chronologically sorted raw trade returns (%). Returns df with scalar/scaled cols."""
    r = returns.reset_index(drop=True)
    expanding_vol = r.expanding(min_periods=MIN_EXPAND).std().shift(1)
    trailing_vol = r.rolling(W_TRAIL, min_periods=W_TRAIL).std().shift(1)
    scalar = (expanding_vol / trailing_vol).clip(0.0, 1.0)
    scalar = scalar.fillna(1.0)  # not enough history yet -> unscaled (flat) baseline
    scaled = scalar * r
    return pd.DataFrame({"raw": r, "scalar": scalar, "scaled": scaled})


def report_strategy(name: str, dates: pd.Series, returns: pd.Series, half: pd.Series | None = None):
    print(f"\n{'='*70}\n{name}  (n={len(returns)})\n{'='*70}")
    ov = build_overlay(returns)
    if half is not None:
        half = half.reset_index(drop=True)
        oos_mask = half == "OOS"
    else:
        n = len(ov)
        oos_mask = pd.Series(np.arange(n) >= int(n * 0.7))

    rows = []
    for label, mask in [("full", pd.Series(True, index=ov.index)), ("OOS", oos_mask)]:
        base = moments(ov.loc[mask, "raw"])
        scal = moments(ov.loc[mask, "scaled"])
        avg_scalar = ov.loc[mask, "scalar"].mean()
        print(
            f"  [{label:4s}] baseline (flat)  n={base['n']:3d} mean={base['mean']:+6.2f}% std={base['std']:5.2f} "
            f"Shp={base['shp']:+.3f} skew={base['skew']:+.2f} kurt={base['kurt']:5.2f} PSR={base['psr']:.3f}"
        )
        print(
            f"  [{label:4s}] vol-target ovl   n={scal['n']:3d} mean={scal['mean']:+6.2f}% std={scal['std']:5.2f} "
            f"Shp={scal['shp']:+.3f} skew={scal['skew']:+.2f} kurt={scal['kurt']:5.2f} PSR={scal['psr']:.3f} "
            f"| avg scalar={avg_scalar:.3f}"
        )
        rows.append(dict(strategy=name, half=label, variant="baseline_flat", **base))
        rows.append(dict(strategy=name, half=label, variant="vol_target_overlay", avg_scalar=avg_scalar, **scal))
    return pd.DataFrame(rows), ov


# ---- 1. leading-dip ----
ld = pd.read_csv(LEADING_DIP_CSV).sort_values(["date", "minute"]).reset_index(drop=True)
ld_t2 = ld[ld["T2"].astype(bool)].copy().sort_values("date").reset_index(drop=True)
assert len(ld_t2) == 67, f"expected 67 T2 trades, got {len(ld_t2)} -- upstream csv changed?"
ld_rows, ld_ov = report_strategy("leading-dip (T2 quality track, ex3 % return)", ld_t2["date"], ld_t2["ex3"], ld_t2["half"])

# ---- 2. dayflip-futures-short ----
df = pd.read_csv(DAYFLIP_CSV).sort_values(["trade_date", "stock"]).reset_index(drop=True)
df_rows, df_ov = report_strategy("dayflip-futures-short (pnl_pct % return)", df["trade_date"], df["pnl_pct"], None)

# ---- combined output ----
all_rows = pd.concat([ld_rows, df_rows], ignore_index=True)
all_rows.to_csv(OUT / "tail_robust_sizing_generalize_summary.csv", index=False)
print(f"\n-> {OUT/'tail_robust_sizing_generalize_summary.csv'}")

ld_ov.assign(date=ld_t2["date"].values, sid=ld_t2["sid"].values, half=ld_t2["half"].values).to_csv(
    OUT / "leading_dip_overlay_trades.csv", index=False
)
df_ov.assign(trade_date=df["trade_date"].values, stock=df["stock"].values).to_csv(
    OUT / "dayflip_overlay_trades.csv", index=False
)
print(f"-> {OUT/'leading_dip_overlay_trades.csv'}")
print(f"-> {OUT/'dayflip_overlay_trades.csv'}")
