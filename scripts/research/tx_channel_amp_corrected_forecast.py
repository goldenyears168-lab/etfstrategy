"""Ad-hoc round 4: build the corrected amplitude forecast implied by round 2's
finding (z_extreme / mean-reversion to today's own running baseline dominates)
and test out-of-sample whether it actually beats the naive "current amp
persists" forecast — not just in-sample IC, but real held-out forecast error.

Chronological 70/30 IS/OOS split (matches this repo's existing convention,
see audit_orderflow_hac_recheck.py) — fit correction coefficients on the
first 70% of trading days, evaluate on the last 30% never touched during
fitting. Day-clustered paired comparison (naive vs corrected error, same OOS
days) is the headline test, not a pooled-minute one.

Models compared, all causal (same-day-so-far info only):
  naive       forecast = wamp[t]                          (current amp persists)
  corrected   forecast = OLS(wamp, dayMean_so_far)         (round 2's shrinkage-to-baseline)
  corrected+  forecast = OLS(wamp, dayMean_so_far, ER)     (+ round 2's smaller path-cleanliness factor)

Not wired into any pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
from scipy import stats as sstats

from tx_channel_amp_volume_interaction import forward_amp, load_all_day_bars, weighted_amp30
from tx_channel_amp_persistence_drivers2 import efficiency_ratio30


def day_mean_so_far(wamp: np.ndarray, min_hist: int = 10) -> np.ndarray:
    n = len(wamp)
    out = np.full(n, np.nan)
    hist = []
    for i in range(n):
        if not np.isnan(wamp[i]):
            if len(hist) >= min_hist:
                out[i] = np.mean(hist)
            hist.append(wamp[i])
    return out


def paired_day_test(a: list[float], b: list[float]) -> tuple[float, float, float]:
    a, b = np.array(a), np.array(b)
    d = a - b
    t, p = sstats.ttest_1samp(d, 0.0)
    return float(d.mean()), float(t), float(p)


def main():
    bars_by_day = load_all_day_bars()
    dates = sorted(bars_by_day)
    n_is = int(round(len(dates) * 0.70))
    is_dates, oos_dates = dates[:n_is], dates[n_is:]
    print(f"n days={len(dates)}  IS={len(is_dates)} ({is_dates[0]}..{is_dates[-1]})  OOS={len(oos_dates)} ({oos_dates[0]}..{oos_dates[-1]})")

    series = {}
    for date, bars in bars_by_day.items():
        wamp = weighted_amp30(bars)
        series[date] = dict(bars=bars, wamp=wamp, dmean=day_mean_so_far(wamp), er=efficiency_ratio30(bars))

    for h in (12, 35, 90):
        print(f"\n=== h={h} ===")
        # --- build IS pooled training set ---
        X_b, X_c, Y = [], [], []
        dev_is, tgt_dev_is = [], []  # relative-form (dimensionless shrinkage) training data
        for date in is_dates:
            s = series[date]
            wamp, dmean, er = s["wamp"], s["dmean"], s["er"]
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(dmean) | np.isnan(er) | np.isnan(famp))
            X_b.append(np.column_stack([np.ones(valid.sum()), wamp[valid], dmean[valid]]))
            X_c.append(np.column_stack([np.ones(valid.sum()), wamp[valid], dmean[valid], er[valid]]))
            Y.append(famp[valid])
            dev_is.append(wamp[valid] - dmean[valid])
            tgt_dev_is.append(famp[valid] - dmean[valid])
        Xb, Xc, y = np.vstack(X_b), np.vstack(X_c), np.concatenate(Y)
        beta_b, *_ = np.linalg.lstsq(Xb, y, rcond=None)
        beta_c, *_ = np.linalg.lstsq(Xc, y, rcond=None)
        print(f"  fitted (IS, n={len(y)}): corrected  intercept={beta_b[0]:.1f} b_wamp={beta_b[1]:.3f} b_dayMean={beta_b[2]:.3f}")
        print(f"  fitted (IS, n={len(y)}): corrected+ intercept={beta_c[0]:.1f} b_wamp={beta_c[1]:.3f} b_dayMean={beta_c[2]:.3f} b_ER={beta_c[3]:.1f}")

        # relative-form model D: forecast = dmean + k*(wamp-dmean), NO fixed intercept/level
        # term -> self-adjusts to whatever vol regime "today" is in, same as naive, but still
        # applies the (regime-invariant, dimensionless) reversion-shrinkage learned on IS.
        dev_is = np.concatenate(dev_is)
        tgt_dev_is = np.concatenate(tgt_dev_is)
        k_rel = float((dev_is @ tgt_dev_is) / (dev_is @ dev_is))
        print(f"  fitted (IS, n={len(dev_is)}): relative-shrinkage k={k_rel:.3f}  (forecast = dayMean + k*(wamp-dayMean))")

        # --- evaluate on OOS, per-day, paired ---
        naive_mae_day, corr_mae_day, corrp_mae_day, rel_mae_day = [], [], [], []
        naive_std_day, corr_std_day, corrp_std_day, rel_std_day = [], [], [], []
        naive_bias_day, corr_bias_day, rel_bias_day = [], [], []
        all_naive_err, all_corr_err, all_corrp_err, all_rel_err, all_actual = [], [], [], [], []
        for date in oos_dates:
            s = series[date]
            wamp, dmean, er = s["wamp"], s["dmean"], s["er"]
            famp = forward_amp(s["bars"], h)
            valid = ~(np.isnan(wamp) | np.isnan(dmean) | np.isnan(er) | np.isnan(famp))
            if valid.sum() < 20:
                continue
            a, dm, ee, act = wamp[valid], dmean[valid], er[valid], famp[valid]
            f_naive = a
            f_corr = beta_b[0] + beta_b[1] * a + beta_b[2] * dm
            f_corrp = beta_c[0] + beta_c[1] * a + beta_c[2] * dm + beta_c[3] * ee
            f_rel = dm + k_rel * (a - dm)

            err_naive = act - f_naive
            err_corr = act - f_corr
            err_corrp = act - f_corrp
            err_rel = act - f_rel

            naive_mae_day.append(np.abs(err_naive).mean())
            corr_mae_day.append(np.abs(err_corr).mean())
            corrp_mae_day.append(np.abs(err_corrp).mean())
            rel_mae_day.append(np.abs(err_rel).mean())
            naive_std_day.append(err_naive.std())
            corr_std_day.append(err_corr.std())
            corrp_std_day.append(err_corrp.std())
            rel_std_day.append(err_rel.std())
            naive_bias_day.append(err_naive.mean())
            corr_bias_day.append(err_corr.mean())
            rel_bias_day.append(err_rel.mean())

            all_naive_err.append(err_naive)
            all_corr_err.append(err_corr)
            all_corrp_err.append(err_corrp)
            all_rel_err.append(err_rel)
            all_actual.append(act)

        all_naive_err = np.concatenate(all_naive_err)
        all_corr_err = np.concatenate(all_corr_err)
        all_corrp_err = np.concatenate(all_corrp_err)
        all_rel_err = np.concatenate(all_rel_err)
        all_actual = np.concatenate(all_actual)
        var_actual = all_actual.var()
        r2_naive = 1 - (all_naive_err**2).mean() / var_actual
        r2_corr = 1 - (all_corr_err**2).mean() / var_actual
        r2_corrp = 1 - (all_corrp_err**2).mean() / var_actual
        r2_rel = 1 - (all_rel_err**2).mean() / var_actual

        print(f"\n  OOS (n_days={len(naive_mae_day)}, n_obs={len(all_naive_err)}):")
        print(f"  {'model':<14} {'MAE(day-avg)':>13} {'std(day-avg)':>13} {'bias(day-avg)':>14} {'pooled_OOS_R2':>14}")
        print(f"  {'naive':<14} {np.mean(naive_mae_day):>13.1f} {np.mean(naive_std_day):>13.1f} {np.mean(naive_bias_day):>14.1f} {r2_naive:>14.4f}")
        print(f"  {'corrected':<14} {np.mean(corr_mae_day):>13.1f} {np.mean(corr_std_day):>13.1f} {np.mean(corr_bias_day):>14.1f} {r2_corr:>14.4f}")
        print(f"  {'corrected+':<14} {np.mean(corrp_mae_day):>13.1f} {np.mean(corrp_std_day):>13.1f} {'':>14} {r2_corrp:>14.4f}")
        print(f"  {'relative-k':<14} {np.mean(rel_mae_day):>13.1f} {np.mean(rel_std_day):>13.1f} {np.mean(rel_bias_day):>14.1f} {r2_rel:>14.4f}")

        dmae, tmae, pmae = paired_day_test(naive_mae_day, corr_mae_day)
        dmae3, tmae3, pmae3 = paired_day_test(naive_mae_day, rel_mae_day)
        print(f"\n  paired (day-clustered) MAE reduction, naive - corrected(fixed-level):  Δ={dmae:.1f}  t={tmae:.2f} p={pmae:.4f}")
        print(f"  paired (day-clustered) MAE reduction, naive - relative-k (fixed):       Δ={dmae3:.1f}  t={tmae3:.2f} p={pmae3:.4f}")


if __name__ == "__main__":
    main()
