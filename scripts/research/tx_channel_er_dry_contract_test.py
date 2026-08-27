#!/usr/bin/env python3
"""ER (Kaufman Efficiency Ratio) as grind detector within PV8 dry/contract states.

Assigned day: 2024-07-08. 1-minute causal bars (day session only), reusing
classify_pv/rvol_series from src/tmf_channel/causal_engine.py verbatim.

ER[t] = |C[t] - C[t-N]| / sum(|C[i]-C[i-1]| for i in t-N+1..t), N=20 bars.
At every bar where classify_pv() returns "dry" or "contract", record ER and
the sign of (C[t]-C[t-N]) as ER's predicted direction. Split into HIGH-ER
(top tercile) vs LOW-ER (bottom tercile) among those flagged bars, and check
forward return in ER's predicted direction at 5/10/15 min horizons.
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402
from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402

DAY = "2024-07-08"
N_ER = 20
HORIZONS = [5, 10, 15]


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None:
        print(f"no tick data for {DAY}")
        return
    bars = resample_to_1min(ticks)
    if bars.empty:
        print(f"no day-session bars for {DAY}")
        return
    C = bars["Close"].tolist()
    O = bars["Open"].tolist()
    V = bars["Volume"].tolist()
    n = len(C)
    print(f"day={DAY} n_1m_bars={n} (day session 08:45-13:45)")

    rvol = rvol_series(V)

    # collect flagged bars with causal ER
    records = []  # (t, state, er, pred_dir)
    for t in range(n):
        state, _impulse = classify_pv(C, O, rvol, t, look=5)
        if state not in ("dry", "contract"):
            continue
        if t < N_ER:
            continue
        net = C[t] - C[t - N_ER]
        path_sum = sum(abs(C[i] - C[i - 1]) for i in range(t - N_ER + 1, t + 1))
        if path_sum <= 0:
            continue
        er = abs(net) / path_sum
        pred_dir = 1 if net > 0 else (-1 if net < 0 else 0)
        if pred_dir == 0:
            continue
        records.append((t, state, er, pred_dir))

    n_dry_contract = len(records)
    print(f"n_dry_contract_flagged_bars_with_valid_ER={n_dry_contract}")
    if n_dry_contract < 6:
        print("too few flagged bars for tercile split")
        return

    ers = sorted(r[2] for r in records)
    lo_cut = ers[len(ers) // 3]
    hi_cut = ers[(2 * len(ers)) // 3]
    low_group = [r for r in records if r[2] <= lo_cut]
    high_group = [r for r in records if r[2] >= hi_cut]
    print(f"ER tercile cuts: lo<= {lo_cut:.4f}  hi>= {hi_cut:.4f}")
    print(f"n_low_er={len(low_group)}  n_high_er={len(high_group)}")

    def eval_group(group, label):
        print(f"\n-- {label} (n={len(group)}) --")
        results = {}
        for hz in HORIZONS:
            fwd_rets = []
            hits = 0
            used = 0
            for t, state, er, pred_dir in group:
                if t + hz >= n:
                    continue
                fwd = C[t + hz] - C[t]
                signed_fwd = fwd * pred_dir  # positive = drift continued in ER's predicted direction
                fwd_rets.append(signed_fwd)
                if signed_fwd > 0:
                    hits += 1
                used += 1
            if used == 0:
                print(f"  {hz}min: no valid samples")
                results[hz] = (None, None, 0)
                continue
            avg = st.mean(fwd_rets)
            hit_rate = hits / used
            print(f"  {hz}min: n={used} hit_rate={hit_rate:.3f} avg_signed_fwd_ret={avg:+.2f} pt")
            results[hz] = (avg, hit_rate, used)
        return results

    low_res = eval_group(low_group, "LOW-ER (bottom tercile)")
    high_res = eval_group(high_group, "HIGH-ER (top tercile)")

    print("\nSummary (HIGH-ER minus LOW-ER, per horizon):")
    for hz in HORIZONS:
        la, lh, ln = low_res[hz]
        ha, hh, hn = high_res[hz]
        if la is None or ha is None:
            print(f"  {hz}min: insufficient data")
            continue
        print(f"  {hz}min: hit_rate diff={hh-lh:+.3f}  avg_fwd_ret diff={ha-la:+.2f} pt")


if __name__ == "__main__":
    main()
