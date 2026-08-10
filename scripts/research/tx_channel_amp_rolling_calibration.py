"""Ad-hoc round 5: fix round 4's residual OOS bias by making the z_extreme
relative-shrinkage correction (forecast = dayMean_so_far + k*(wamp-dayMean))
walk-forward instead of a single static IS/OOS fit.

Round 4 diagnosis: a single k fit on Jan-Jun and tested on Jun-Aug still had
180-1065pt bias because TX's own volatility level shifted ~60% between the
two periods. A rolling/expanding re-estimate of k should track that regime
drift instead of freezing it.

Three calibration schemes compared against naive (forecast=wamp), walked
forward day-by-day starting once enough trailing history exists (first 30
days used purely as warm-up, never scored):
  static      k fit once on the first 70% of days (round 4's approach, for reference)
  rolling20   k re-fit every day using only the trailing 20 trading days
  rolling40   k re-fit every day using only the trailing 40 trading days
  expanding   k re-fit every day using ALL prior days (no forgetting)

All causal — day i's forecast only ever uses days < i. Day-clustered paired
comparison against naive is the headline test.

Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_corrected_forecast import day_mean_so_far, paired_day_test

WARMUP_DAYS = 30


def fit_k(dev_list, tgt_dev_list):
    dev = np.concatenate(dev_list)
    tgt = np.concatenate(tgt_dev_list)
    denom = dev @ dev
    if denom <= 0:
        return np.nan
    return float((dev @ tgt) / denom)


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    print(f"n days = {len(dates)}  ({dates[0]}..{dates[-1]})")

    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        series[date] = dict(bars=bars, wamp=wamp, dmean=day_mean_so_far(wamp))

    n_is_static = int(round(len(dates) * 0.70))
    static_is_dates = dates[:n_is_static]

    for h in (12, 35, 90):
        print(f"\n=== h={h} ===")
        # precompute per-day (dev, tgt_dev, actual, wamp, dmean) valid arrays, once
        day_data = {}
        for date in dates:
            s = series[date]
            wamp, dmean = s["wamp"], s["dmean"]
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(dmean) | np.isnan(famp))
            if valid.sum() < 20:
                continue
            day_data[date] = dict(
                dev=wamp[valid] - dmean[valid],
                tgt_dev=famp[valid] - dmean[valid],
                actual=famp[valid],
                wamp=wamp[valid],
                dmean=dmean[valid],
            )
        avail_dates = [d for d in dates if d in day_data]

        # static k (round 4's reference, fit once on the fixed 70% IS split)
        static_dev = [day_data[d]["dev"] for d in static_is_dates if d in day_data]
        static_tgt = [day_data[d]["tgt_dev"] for d in static_is_dates if d in day_data]
        k_static = fit_k(static_dev, static_tgt)

        schemes = ["naive", "static", "rolling20", "rolling40", "expanding"]
        mae_by_scheme = {s: [] for s in schemes}
        std_by_scheme = {s: [] for s in schemes}
        bias_by_scheme = {s: [] for s in schemes}
        k_rolling20_hist, k_rolling40_hist, k_expanding_hist = [], [], []

        for i, date in enumerate(avail_dates):
            if i < WARMUP_DAYS:
                continue
            d = day_data[date]
            act, wamp_v, dmean_v = d["actual"], d["wamp"], d["dmean"]

            prior = avail_dates[:i]
            k_roll20 = fit_k([day_data[p]["dev"] for p in prior[-20:]], [day_data[p]["tgt_dev"] for p in prior[-20:]])
            k_roll40 = fit_k([day_data[p]["dev"] for p in prior[-40:]], [day_data[p]["tgt_dev"] for p in prior[-40:]])
            k_exp = fit_k([day_data[p]["dev"] for p in prior], [day_data[p]["tgt_dev"] for p in prior])
            k_rolling20_hist.append(k_roll20)
            k_rolling40_hist.append(k_roll40)
            k_expanding_hist.append(k_exp)

            forecasts = {
                "naive": wamp_v,
                "static": dmean_v + k_static * (wamp_v - dmean_v),
                "rolling20": dmean_v + k_roll20 * (wamp_v - dmean_v),
                "rolling40": dmean_v + k_roll40 * (wamp_v - dmean_v),
                "expanding": dmean_v + k_exp * (wamp_v - dmean_v),
            }
            for name, fc in forecasts.items():
                err = act - fc
                mae_by_scheme[name].append(np.abs(err).mean())
                std_by_scheme[name].append(err.std())
                bias_by_scheme[name].append(err.mean())

        n_scored = len(mae_by_scheme["naive"])
        print(f"  scored days = {n_scored} (after {WARMUP_DAYS}-day warmup)")
        print(f"  k history: rolling20 mean={np.nanmean(k_rolling20_hist):.3f} std={np.nanstd(k_rolling20_hist):.3f} | "
              f"rolling40 mean={np.nanmean(k_rolling40_hist):.3f} std={np.nanstd(k_rolling40_hist):.3f} | "
              f"expanding last={k_expanding_hist[-1]:.3f} | static(fixed)={k_static:.3f}")

        print(f"\n  {'scheme':<12} {'MAE':>10} {'std':>10} {'bias':>10}")
        for name in schemes:
            print(f"  {name:<12} {np.mean(mae_by_scheme[name]):>10.1f} {np.mean(std_by_scheme[name]):>10.1f} {np.mean(bias_by_scheme[name]):>10.1f}")

        print()
        for name in ("static", "rolling20", "rolling40", "expanding"):
            dmae, tmae, pmae = paired_day_test(mae_by_scheme["naive"], mae_by_scheme[name])
            print(f"  paired MAE naive-vs-{name:<10}: Δ={dmae:>8.1f}  t={tmae:>6.2f}  p={pmae:.4f}")


if __name__ == "__main__":
    main()
