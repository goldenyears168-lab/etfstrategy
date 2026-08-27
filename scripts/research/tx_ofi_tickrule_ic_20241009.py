"""
OFI (tick-rule signed volume) IC probe for TX front-month, single day 2024-10-09.

Tick rule proxy: classify each tick as buyer-initiated (+vol) if price > prev price,
seller-initiated (-vol) if price < prev price, carry-forward classification if unchanged.
Rolling 60-real-second signed volume sum, strictly causal (ticks at or before t).
Forward return = price at next tick >= t+horizon, minus price at t.
IC = Spearman corr(OFI_t, fwd_ret_t) at horizons 1min/3min/5min.
"""
import sys
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, "/Users/jackm4/goldenstocks/scripts/research")
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

DAY = "2024-10-09"


def main():
    df = load_front_month_ticks(DAY)
    if df is None or df.empty:
        print("LOAD_FAILED_OR_EMPTY")
        return

    df = df.reset_index(drop=True)
    n_raw = len(df)
    price = df["price"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    ts = df["dt"].to_numpy()  # datetime64[ns]
    ts_sec = df["dt"].astype("int64").to_numpy() / 1e9  # seconds epoch

    # tick rule classification
    sign = np.zeros(n_raw, dtype=float)
    sign[0] = 1.0  # arbitrary init for first tick
    for i in range(1, n_raw):
        if price[i] > price[i - 1]:
            sign[i] = 1.0
        elif price[i] < price[i - 1]:
            sign[i] = -1.0
        else:
            sign[i] = sign[i - 1]

    signed_vol = sign * vol

    # rolling 60s causal sum of signed_vol via searchsorted on ts_sec
    window = 60.0
    lo_idx = np.searchsorted(ts_sec, ts_sec - window, side="left")
    cumsum = np.concatenate([[0.0], np.cumsum(signed_vol)])
    ofi = cumsum[np.arange(n_raw) + 1] - cumsum[lo_idx]

    results = {}
    for horizon_min, label in [(1, "1min"), (3, "3min"), (5, "5min")]:
        target_sec = ts_sec + horizon_min * 60.0
        fwd_idx = np.searchsorted(ts_sec, target_sec, side="left")
        valid = fwd_idx < n_raw
        idx_valid = np.where(valid)[0]
        fwd_price = price[fwd_idx[idx_valid]]
        fwd_ret = fwd_price - price[idx_valid]
        ofi_valid = ofi[idx_valid]

        # drop any nan/inf
        mask = np.isfinite(ofi_valid) & np.isfinite(fwd_ret)
        ofi_f = ofi_valid[mask]
        ret_f = fwd_ret[mask]
        n = len(ofi_f)

        if n < 30:
            results[label] = (np.nan, np.nan, n)
            continue

        rho, p_s = stats.spearmanr(ofi_f, ret_f)
        r, p_p = stats.pearsonr(ofi_f, ret_f)
        results[label] = (rho, r, n, p_s, p_p)

    print(f"day={DAY} n_ticks_raw={n_raw}")
    for label, vals in results.items():
        if len(vals) == 3:
            print(f"{label}: insufficient n={vals[2]}")
        else:
            rho, r, n, p_s, p_p = vals
            print(f"{label}: spearman_ic={rho:.4f} (p={p_s:.4g})  pearson_ic={r:.4f} (p={p_p:.4g})  n={n}")

    # sanity stats
    print(f"ofi stats: mean={np.nanmean(ofi):.2f} std={np.nanstd(ofi):.2f} "
          f"min={np.nanmin(ofi):.2f} max={np.nanmax(ofi):.2f}")
    print(f"price range: {price.min():.1f}..{price.max():.1f}  "
          f"first_ts={df['dt'].iloc[0]}  last_ts={df['dt'].iloc[-1]}")


if __name__ == "__main__":
    main()
