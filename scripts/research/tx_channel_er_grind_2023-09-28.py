"""Kaufman ER as grind detector within PV8 dry/contract states -- day 2023-09-28.

1-minute causal bars (chosen because classify_pv/rvol_series constants VOL_WIN=20,
CLIMAX/EXPAND/DRY/CONTRACT are calibrated on 1-min bars in the production engine;
reusing the same bar cadence keeps classify_pv's behavior faithful to what actually
runs live, rather than re-tuning thresholds for 1s bars).
"""
import sys
import statistics as st
from pathlib import Path

sys.path.insert(0, "/Users/jackm4/goldenstocks/src")
sys.path.insert(0, "/Users/jackm4/goldenstocks/scripts/research")

from tmf_channel.causal_engine import classify_pv, rvol_series, VOL_WIN  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

DAY = "2023-09-28"
N_ER = 20
HORIZONS = [5, 10, 15]


def kaufman_er(C, t, n):
    a = t - n
    if a < 0:
        return None
    net = abs(C[t] - C[a])
    path = sum(abs(C[i] - C[i - 1]) for i in range(a + 1, t + 1))
    if path <= 0:
        return None
    return net / path, C[t] - C[a]  # ER, signed net move (direction)


def main():
    ticks = load_front_month_ticks(DAY)
    if ticks is None:
        print("NO_TICKS")
        return
    bars = resample_to_1min(ticks)
    if bars.empty:
        print("NO_BARS")
        return
    C = bars["Close"].tolist()
    O = bars["Open"].tolist()
    V = bars["Volume"].tolist()
    n_bars = len(C)

    rvol = rvol_series(V, win=VOL_WIN)

    records = []  # (t, state, er, sign)
    for t in range(n_bars):
        state, _impulse = classify_pv(C, O, rvol, t, look=5)
        if state not in ("dry", "contract"):
            continue
        er_res = kaufman_er(C, t, N_ER)
        if er_res is None:
            continue
        er, signed_move = er_res
        records.append((t, state, er, signed_move))

    n_dry_contract = len(records)
    if n_dry_contract < 6:
        print(f"TOO_FEW records={n_dry_contract}")
        return

    ers = sorted(r[2] for r in records)
    lo_cut = ers[len(ers) // 3]
    hi_cut = ers[(2 * len(ers)) // 3]

    low_group = [r for r in records if r[2] <= lo_cut]
    high_group = [r for r in records if r[2] >= hi_cut]

    def eval_group(group, horizon_min):
        hits, rets = 0, []
        for t, state, er, signed_move in group:
            fut = t + horizon_min
            if fut >= n_bars:
                continue
            direction = 1.0 if signed_move > 0 else (-1.0 if signed_move < 0 else 0.0)
            if direction == 0.0:
                continue
            fwd_ret = (C[fut] - C[t]) * direction  # positive = matches ER-predicted direction
            rets.append(fwd_ret)
            if fwd_ret > 0:
                hits += 1
        n = len(rets)
        if n == 0:
            return None, None, 0
        return hits / n, sum(rets) / n, n

    print(f"day={DAY} bars={n_bars} dry_contract_periods={n_dry_contract}")
    print(f"low_group_n={len(low_group)} high_group_n={len(high_group)} lo_cut={lo_cut:.4f} hi_cut={hi_cut:.4f}")
    for h in HORIZONS:
        lo_hit, lo_ret, lo_n = eval_group(low_group, h)
        hi_hit, hi_ret, hi_n = eval_group(high_group, h)
        print(f"H={h}min  LOW-ER  n={lo_n} hit_rate={lo_hit} avg_fwd_ret={lo_ret}")
        print(f"H={h}min  HIGH-ER n={hi_n} hit_rate={hi_hit} avg_fwd_ret={hi_ret}")


if __name__ == "__main__":
    main()
