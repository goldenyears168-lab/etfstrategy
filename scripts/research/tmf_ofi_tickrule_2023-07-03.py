"""
OFI (tick-rule signed-volume) IC probe for TX/TMF front-month tick data.
Assigned day: 2023-07-03.

Method:
- Load raw ticks via tx_channel_tick_validation.load_front_month_ticks(day)
- Classify each tick with the tick rule: +1 if price > prev price, -1 if price < prev price,
  carry-forward previous sign if price unchanged (zero-tick)
- signed_volume_i = sign_i * volume_i
- OFI(t) = rolling sum of signed_volume over ticks with time in (t - 60s, t], strictly causal
  (uses real elapsed seconds via the tick's own timestamp, not bar/tick count)
- Forward return at horizon h: price at the next available tick with time >= t + h, minus price at t
- IC = correlation(OFI(t), fwd_return(t, h)) for h in {1min, 3min, 5min}, both Pearson and Spearman reported
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from scipy import stats

from tx_channel_tick_validation import load_front_month_ticks, DAY_SESSION_START, DAY_SESSION_END

DAY = "2023-07-03"


def main():
    df = load_front_month_ticks(DAY)
    if df is None or df.empty:
        print(f"LOAD_FAILED: no ticks for {DAY}")
        return

    # restrict to day session to match the code's convention (avoid mixing session boundaries)
    df = df.set_index("dt")
    df = df.between_time(DAY_SESSION_START, DAY_SESSION_END)
    df = df.reset_index()
    n_raw = len(df)
    if n_raw < 100:
        print(f"TOO_SPARSE: only {n_raw} ticks in day session for {DAY}")
        return

    df = df.sort_values("dt").reset_index(drop=True)
    price = df["price"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    ts = df["dt"].to_numpy()  # datetime64[ns]
    t_sec = (df["dt"].astype("int64") / 1e9).to_numpy()  # seconds since epoch, float

    # tick rule classification with carry-forward for zero-ticks
    sign = np.zeros(n_raw, dtype=float)
    sign[0] = 1.0  # arbitrary init for first tick (no prior info)
    for i in range(1, n_raw):
        if price[i] > price[i - 1]:
            sign[i] = 1.0
        elif price[i] < price[i - 1]:
            sign[i] = -1.0
        else:
            sign[i] = sign[i - 1]

    signed_vol = sign * vol

    # rolling 60s signed-volume sum, strictly causal: window = (t-60, t]
    csum = np.concatenate([[0.0], np.cumsum(signed_vol)])  # csum[i] = sum of signed_vol[0:i]
    left_idx = np.searchsorted(t_sec, t_sec - 60.0, side="right")  # first index with t > t-60
    ofi = csum[np.arange(n_raw) + 1] - csum[left_idx]

    horizons_sec = {"1min": 60.0, "3min": 180.0, "5min": 300.0}
    results = {}
    for label, hsec in horizons_sec.items():
        target_t = t_sec + hsec
        # next available tick at or after t+h
        fwd_idx = np.searchsorted(t_sec, target_t, side="left")
        valid = fwd_idx < n_raw
        idx_valid = np.where(valid)[0]
        fwd_price = price[fwd_idx[idx_valid]]
        base_price = price[idx_valid]
        fwd_ret = fwd_price - base_price
        ofi_valid = ofi[idx_valid]

        n = len(ofi_valid)
        if n < 30:
            results[label] = dict(n=n, pearson=None, spearman=None)
            continue
        pear = stats.pearsonr(ofi_valid, fwd_ret)
        spear = stats.spearmanr(ofi_valid, fwd_ret)
        results[label] = dict(n=n, pearson=float(pear.statistic), pearson_p=float(pear.pvalue),
                               spearman=float(spear.statistic), spearman_p=float(spear.pvalue))

    print(f"day={DAY}")
    print(f"n_ticks_day_session={n_raw}")
    print(f"session_first={df['dt'].iloc[0]}  session_last={df['dt'].iloc[-1]}")
    for label, r in results.items():
        print(f"--- horizon={label} ---")
        print(r)


if __name__ == "__main__":
    main()
