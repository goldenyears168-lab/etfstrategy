"""Item #10: do the current PV8 regime boundaries (classify_pv in causal_engine.py,
thresholds CLIMAX=2.50 EXPAND=1.50 CONTRACT=0.70 DRY=0.45 on rvol=V[t]/median(V,win=20))
line up with tonight's amp+volume combined forecast model's own natural continuous
scale?

Uses w83 (tx_1m_fullnight_cache_full.json, 83 days, day+night, 2026-04-01..07-31) via
tmf_channel.cache_store.load_day. For each bar: compute rvol_series + classify_pv exactly
as causal_engine does (same functions, imported directly -- not reimplemented), and
compute weighted_amp30 (30-bar recency-weighted range) + vol_level30 (30-bar mean
volume) exactly as round-10 defines them, then fit ONE global in-sample OLS
predicted_amp = b0 + b1*wamp30 + b2*vlvl30 at horizon h=12 (near-term, matches
struct_exit_look=12 and is a reasonable "PIT decision horizon"). This is a diagnostic
overlay (continuous score vs discrete regime bucket), not an out-of-sample forecast
claim -- in-sample global fit is fine for asking "does the discrete bucketing look
well-calibrated against the continuous score".

Not wired into any pipeline; scratch research script.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import pandas as pd
from scipy import stats as sstats

from tmf_channel.cache_store import list_days, load_day
from tmf_channel.causal_engine import rvol_series, classify_pv, VOL_WIN, CLIMAX, EXPAND, CONTRACT, DRY

WINDOW = 30
H = 12  # forward horizon (bars) for the predicted-amplitude target


def weighted_amp30(ranges: np.ndarray) -> np.ndarray:
    n = len(ranges)
    w = np.arange(1, WINDOW + 1)
    wsum = w.sum()
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        out[i] = (ranges[i - WINDOW + 1 : i + 1] * w).sum() / wsum
    return out


def vol_level30(vol: np.ndarray) -> np.ndarray:
    n = len(vol)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        out[i] = vol[i - WINDOW + 1 : i + 1].mean()
    return out


def forward_amp(ranges: np.ndarray, h: int) -> np.ndarray:
    n = len(ranges)
    out = np.full(n, np.nan)
    for i in range(n - h):
        out[i] = ranges[i + 1 : i + 1 + h].mean()
    return out


def main():
    days = list_days(source="tx_1m_fullnight_cache_full.json")
    print(f"w83 days available: {len(days)}")

    all_rows = []
    for day in days:
        bars = load_day(day, source="tx_1m_fullnight_cache_full.json")
        if len(bars) < WINDOW + H + VOL_WIN + 5:
            continue
        O = [b["o"] for b in bars]
        H_ = [b["h"] for b in bars]
        L = [b["l"] for b in bars]
        C = [b["c"] for b in bars]
        V = [b["v"] or 0 for b in bars]
        n = len(bars)

        rvol = rvol_series(V, win=VOL_WIN)
        ranges = np.array([H_[i] - L[i] for i in range(n)], dtype=float)
        vol_arr = np.array(V, dtype=float)

        wamp = weighted_amp30(ranges)
        vlvl = vol_level30(vol_arr)
        famp = forward_amp(ranges, H)

        for t in range(n):
            if rvol[t] is None:
                continue
            reg, _imp = classify_pv(C, O, rvol, t)
            if reg == "na":
                continue
            if np.isnan(wamp[t]) or np.isnan(vlvl[t]) or np.isnan(famp[t]):
                continue
            all_rows.append(dict(day=day, t=t, sess=bars[t].get("sess"), rv=rvol[t], regime=reg,
                                  wamp=wamp[t], vlvl=vlvl[t], famp=famp[t]))

    df = pd.DataFrame(all_rows)
    print(f"usable bars (rvol+wamp+vlvl+famp all valid, day/night both incl.): {len(df)}")
    print(f"unique days contributing: {df['day'].nunique()}")

    # global in-sample OLS: predicted_amp = b0 + b1*wamp + b2*vlvl, target = forward_amp(h=12)
    X = np.column_stack([np.ones(len(df)), df["wamp"].to_numpy(), df["vlvl"].to_numpy()])
    y = df["famp"].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    df["pred_amp"] = X @ beta
    resid = y - df["pred_amp"].to_numpy()
    r2 = 1 - resid.var() / y.var()
    print(f"\nglobal in-sample fit: b0={beta[0]:.2f} b_wamp={beta[1]:.3f} b_vlvl={beta[2]:.5f}  R^2={r2:.3f}")

    # rvol <-> pred_amp correlation (day-clustered spearman)
    ics = []
    for day, g in df.groupby("day"):
        if len(g) < 20:
            continue
        ic, _ = sstats.spearmanr(g["rv"], g["pred_amp"])
        ics.append(ic)
    ics = np.array(ics)
    t, p = sstats.ttest_1samp(ics, 0.0)
    print(f"day-clustered IC(rvol, pred_amp): mean={ics.mean():.3f} t={t:.2f} p={p:.4f} n_days={len(ics)}")

    order = ["dry", "contract", "normal", "div_hh_weak_vol", "expand_up", "expand_dn", "climax_up", "climax_dn"]
    print(f"\n{'regime':<18}{'n':>8}{'pct':>7}{'rv_med':>9}{'pred_amp_mean':>15}{'pred_amp_med':>14}{'pred_amp_p25':>14}{'pred_amp_p75':>14}")
    summary = {}
    for reg in order:
        g = df[df["regime"] == reg]
        if len(g) == 0:
            print(f"{reg:<18}{0:>8}")
            continue
        pct = 100 * len(g) / len(df)
        row = dict(n=len(g), pct=pct, rv_med=g["rv"].median(),
                   pa_mean=g["pred_amp"].mean(), pa_med=g["pred_amp"].median(),
                   pa_p25=g["pred_amp"].quantile(0.25), pa_p75=g["pred_amp"].quantile(0.75))
        summary[reg] = row
        print(f"{reg:<18}{row['n']:>8}{pct:>6.1f}%{row['rv_med']:>9.2f}{row['pa_mean']:>15.2f}{row['pa_med']:>14.2f}{row['pa_p25']:>14.2f}{row['pa_p75']:>14.2f}")

    print(f"\nthresholds: DRY<={DRY}  CONTRACT<={CONTRACT}  EXPAND>={EXPAND}  CLIMAX>={CLIMAX}  (rvol=V/median20)")

    # Mann-Whitney between adjacent volume-magnitude tiers (direction-collapsed):
    # dry vs contract vs normal(+div) vs expand(up+dn) vs climax(up+dn)
    tiers = {
        "dry": df[df.regime == "dry"],
        "contract": df[df.regime == "contract"],
        "normal_or_div": df[df.regime.isin(["normal", "div_hh_weak_vol"])],
        "expand": df[df.regime.isin(["expand_up", "expand_dn"])],
        "climax": df[df.regime.isin(["climax_up", "climax_dn"])],
    }
    tier_order = ["dry", "contract", "normal_or_div", "expand", "climax"]
    print("\nadjacent-tier separation (Mann-Whitney U on pred_amp, direction-collapsed):")
    for a, b in zip(tier_order, tier_order[1:]):
        ga, gb = tiers[a]["pred_amp"], tiers[b]["pred_amp"]
        if len(ga) < 10 or len(gb) < 10:
            print(f"  {a:<15} vs {b:<15}: insufficient n")
            continue
        u, pu = sstats.mannwhitneyu(ga, gb, alternative="two-sided")
        # rank-biserial effect size / overlap coefficient proxy via medians+IQR overlap
        med_a, med_b = ga.median(), gb.median()
        print(f"  {a:<15}(n={len(ga)}, med={med_a:.1f}) vs {b:<15}(n={len(gb)}, med={med_b:.1f}): "
              f"p={pu:.2e}  median_gap={med_b-med_a:.1f}")

    # direction pairs: does amp model see climax_up vs climax_dn (or expand_up/dn) as different?
    print("\ndirection-pair check (amp model has no directional signal -- expect near-identical):")
    for base in ("expand", "climax"):
        gu = df[df.regime == f"{base}_up"]["pred_amp"]
        gd = df[df.regime == f"{base}_dn"]["pred_amp"]
        if len(gu) < 10 or len(gd) < 10:
            continue
        u, pu = sstats.mannwhitneyu(gu, gd, alternative="two-sided")
        print(f"  {base}_up (n={len(gu)}, med={gu.median():.1f}) vs {base}_dn (n={len(gd)}, med={gd.median():.1f}): p={pu:.3f}")

    df.to_csv("/tmp/pv8_amp_calibration_bars.csv", index=False)
    print("\nwrote /tmp/pv8_amp_calibration_bars.csv")


if __name__ == "__main__":
    main()
