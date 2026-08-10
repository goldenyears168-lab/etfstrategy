"""Item #11: does the amp+volume forecast model (round 10,
tx_channel_amp_vol_combined_forecast.py) carry information about forward
amplitude that same-day VIXTWN level/delta does not already carry, or are
they redundant?

Day-level analysis (VIXTWN is a single day-constant number, so the natural
level at which to test "control for VIXTWN" is one observation per day, not
pooled minutes):
  1. Build the walk-forward amp+vol combined forecast (same as round 10) at
     horizon h=90 (mid-range of the 90-150min sweet spot flagged tonight).
  2. Day-level summary: mean of the model's forecast over that day's valid
     minutes ("model predicted amp for the day"), and mean of the realized
     forward amplitude over the day ("realized amp for the day").
  3. Same-day VIXTWN level (close) and delta (vs prior trading day) from
     causal_engine.load_vixtwn_delta (delta) and a companion level loader
     added here (level only, PIT-safe: same-day close is available by the
     time day-session forecasting happens intraday? NOT strictly PIT for the
     first bars of the day since VIXTWN close is EOD -- but this whole item
     is about *information overlap*, not about a deployable causal filter,
     so same-day close is fine per the assignment text: "same-day VIXTWN
     level and delta").
  4. corr(model_forecast, vixtwn_level), corr(model_forecast, vixtwn_delta).
  5. Partial correlation: residualize both model_forecast and realized_amp
     on [vixtwn_level, vixtwn_delta] (OLS), then correlate the residuals.
     Compare to the raw (unresidualized) correlation. n = number of days
     with both tick bars and VIXTWN available => day-clustered by
     construction (one obs/day).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers2 import vol_level30

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from tmf_channel.causal_engine import load_vixtwn_delta  # noqa: E402
import sqlite3

WARMUP_DAYS = 30
ROLL_WINDOW = 30
HORIZON = 90


def load_vixtwn_level(db_path: Path | None = None, source: str = "computed") -> dict[str, float]:
    if db_path is None:
        for cand in (Path.home() / "goldenstocks-data/data/stocks.db",):
            if cand.exists():
                db_path = cand
                break
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT date, close FROM market_vix_daily WHERE symbol='VIXTWN' AND source=? ORDER BY date",
        (source,),
    ).fetchall()
    con.close()
    return {d: float(c) for d, c in rows if c is not None}


def fit_ols(X_list, y_list):
    X = np.vstack(X_list)
    y = np.concatenate(y_list)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def residualize(x: np.ndarray, z: np.ndarray) -> np.ndarray:
    """OLS residual of x on [1, z_cols...]."""
    Z = np.column_stack([np.ones(len(x)), z])
    beta, *_ = np.linalg.lstsq(Z, x, rcond=None)
    return x - Z @ beta


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n tick-cache day sessions = {len(dates)}")

    vix_level = load_vixtwn_level()
    vix_delta = load_vixtwn_delta()
    print(f"VIXTWN level rows={len(vix_level)}, delta rows={len(vix_delta)}")

    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        vlvl = vol_level30(bars)
        series[date] = dict(bars=bars, wamp=wamp, vlvl=vlvl)

    day_data = {}
    for date in dates:
        s = series[date]
        wamp, vlvl = s["wamp"], s["vlvl"]
        famp = forward_amp(s["bars"], HORIZON)
        valid = ~(np.isnan(wamp) | np.isnan(vlvl) | np.isnan(famp))
        if valid.sum() < 20:
            continue
        day_data[date] = dict(wamp=wamp[valid], vlvl=vlvl[valid], y=famp[valid])
    avail = [d for d in dates if d in day_data]
    print(f"usable days for h={HORIZON} model = {len(avail)}")

    day_forecast_mean = {}
    day_realized_mean = {}
    for i, date in enumerate(avail):
        if i < WARMUP_DAYS:
            continue
        prior = avail[max(0, i - ROLL_WINDOW) : i]
        if len(prior) < 15:
            continue
        X_list = [
            np.column_stack([np.ones(len(day_data[p]["wamp"])), day_data[p]["wamp"], day_data[p]["vlvl"]])
            for p in prior
        ]
        y_list = [day_data[p]["y"] for p in prior]
        beta = fit_ols(X_list, y_list)

        d = day_data[date]
        a, v, act = d["wamp"], d["vlvl"], d["y"]
        f_comb = beta[0] + beta[1] * a + beta[2] * v
        day_forecast_mean[date] = float(np.mean(f_comb))
        day_realized_mean[date] = float(np.mean(act))

    model_days = sorted(day_forecast_mean)
    print(f"walk-forward model days (post warmup) = {len(model_days)}")

    common = [d for d in model_days if d in vix_level and d in vix_delta]
    print(f"days with model forecast + VIXTWN level + delta = {len(common)}")
    if len(common) < 10:
        print("INFEASIBLE: too few overlapping days (VIXTWN delta needs day t-1 too, "
              "and VIXTWN daily table stops 2026-07-31)")
        return

    f = np.array([day_forecast_mean[d] for d in common])
    y = np.array([day_realized_mean[d] for d in common])
    zl = np.array([vix_level[d] for d in common])
    zd = np.array([vix_delta[d] for d in common])

    def corr_report(name, a, b):
        r, p = sstats.pearsonr(a, b)
        print(f"  corr({name}) r={r:.3f} p={p:.4f} n={len(a)}")
        return r, p

    print("\n--- raw correlations (n=days) ---")
    corr_report("model_forecast, vixtwn_level", f, zl)
    corr_report("model_forecast, vixtwn_delta", f, zd)
    corr_report("realized_amp, vixtwn_level", y, zl)
    corr_report("realized_amp, vixtwn_delta", y, zd)
    r_fy, p_fy = corr_report("model_forecast, realized_amp (raw)", f, y)

    print("\n--- partial correlation: residualize on [vixtwn_level, vixtwn_delta] ---")
    Z = np.column_stack([zl, zd])
    f_resid = residualize(f, Z)
    y_resid = residualize(y, Z)
    r_partial, p_partial = corr_report("model_forecast_resid, realized_amp_resid (partial)", f_resid, y_resid)

    print(f"\nSUMMARY h={HORIZON}min, n_days={len(common)}")
    print(f"  raw corr(forecast, realized)     = {r_fy:.3f} (p={p_fy:.4f})")
    print(f"  partial corr after VIXTWN control = {r_partial:.3f} (p={p_partial:.4f})")
    retained_pct = (r_partial / r_fy * 100.0) if r_fy != 0 else float("nan")
    print(f"  retained fraction of raw corr      = {retained_pct:.1f}%")


if __name__ == "__main__":
    main()
