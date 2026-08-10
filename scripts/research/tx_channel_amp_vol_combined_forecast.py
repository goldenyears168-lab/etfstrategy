"""Ad-hoc round 10: does combining current amplitude + current volume level
into one forecast beat amplitude alone, out of sample?

Motivation: round 9 (clean data) showed volume_level's independent
contribution (after removing what's shared with amp) grows with horizon —
small at 12min, comparable-to-bigger than amp's own residual by 150min. This
builds the natural next model and validates it walk-forward (same discipline
as round 5: causal, rolling re-fit, day-clustered paired comparison against
naive), not just in-sample R^2.

Uses the corrected ticks_to_1m_bars. Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers2 import vol_level30
from tx_channel_amp_corrected_forecast import paired_day_test

WARMUP_DAYS = 30
ROLL_WINDOW = 30  # trading days of trailing history for the walk-forward fit


def fit_ols(X_list, y_list):
    X = np.vstack(X_list)
    y = np.concatenate(y_list)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}")

    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        vlvl = vol_level30(bars)
        series[date] = dict(bars=bars, wamp=wamp, vlvl=vlvl)

    for h in (12, 35, 90, 150):
        print(f"\n=== h={h} ===")
        day_data = {}
        for date in dates:
            s = series[date]
            wamp, vlvl = s["wamp"], s["vlvl"]
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(vlvl) | np.isnan(famp))
            if valid.sum() < 20:
                continue
            day_data[date] = dict(wamp=wamp[valid], vlvl=vlvl[valid], y=famp[valid])
        avail = [d for d in dates if d in day_data]

        naive_mae, comb_mae = [], []
        naive_std, comb_std = [], []
        betas_hist = []
        for i, date in enumerate(avail):
            if i < WARMUP_DAYS:
                continue
            prior = avail[max(0, i - ROLL_WINDOW) : i]
            if len(prior) < 15:
                continue
            X_list = [np.column_stack([np.ones(len(day_data[p]["wamp"])), day_data[p]["wamp"], day_data[p]["vlvl"]]) for p in prior]
            y_list = [day_data[p]["y"] for p in prior]
            beta = fit_ols(X_list, y_list)
            betas_hist.append(beta)

            d = day_data[date]
            a, v, act = d["wamp"], d["vlvl"], d["y"]
            f_naive = a
            f_comb = beta[0] + beta[1] * a + beta[2] * v

            err_n = act - f_naive
            err_c = act - f_comb
            naive_mae.append(np.abs(err_n).mean())
            comb_mae.append(np.abs(err_c).mean())
            naive_std.append(err_n.std())
            comb_std.append(err_c.std())

        betas_hist = np.array(betas_hist)
        print(f"  scored days = {len(naive_mae)}")
        print(f"  fitted beta (mean over walk-forward refits): intercept={betas_hist[:,0].mean():.1f} b_wamp={betas_hist[:,1].mean():.3f} b_vlvl={betas_hist[:,2].mean():.4f}")
        print(f"  {'':<10} {'MAE':>10} {'std':>10}")
        print(f"  {'naive(amp)':<10} {np.mean(naive_mae):>10.2f} {np.mean(naive_std):>10.2f}")
        print(f"  {'combined':<10} {np.mean(comb_mae):>10.2f} {np.mean(comb_std):>10.2f}")

        dmae, tmae, pmae = paired_day_test(naive_mae, comb_mae)
        dstd, tstd, pstd = paired_day_test(naive_std, comb_std)
        print(f"  paired (day-clustered) naive-combined MAE: Δ={dmae:.2f} t={tmae:.2f} p={pmae:.4f}")
        print(f"  paired (day-clustered) naive-combined std: Δ={dstd:.2f} t={tstd:.2f} p={pstd:.4f}")


if __name__ == "__main__":
    main()
