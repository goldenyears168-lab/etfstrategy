#!/usr/bin/env python3
"""Single-day test: does Kaufman Efficiency Ratio separate genuine chop from
'grinding' drift within PV8 dry/contract states? Day = 2023-07-03.

Uses 1-minute causal bars (day session only), reuses classify_pv/rvol_series
from src/tmf_channel/causal_engine.py verbatim.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmf_channel.causal_engine import classify_pv, rvol_series  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

DAY = "2023-07-03"
N_ER = 20
HORIZONS_MIN = (5, 10, 15)


def kaufman_er(C: list[float], t: int, n: int) -> float | None:
    if t < n:
        return None
    a = t - n
    net = abs(C[t] - C[a])
    path = sum(abs(C[i] - C[i - 1]) for i in range(a + 1, t + 1))
    if path <= 0:
        return None
    return net / path


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"NO TICK DATA for {DAY}")
        return
    bars = resample_to_1min(ticks)
    if bars.empty:
        print(f"NO 1-MIN BARS for {DAY}")
        return

    C = bars["Close"].tolist()
    O = bars["Open"].tolist()
    V = bars["Volume"].tolist()
    n_bars = len(C)
    print(f"day={DAY} n_1min_bars={n_bars}")

    rvol = rvol_series(V)

    states = []
    for t in range(n_bars):
        state, impulse = classify_pv(C, O, rvol, t, look=5)
        states.append(state)

    dry_contract_idx = [t for t in range(n_bars) if states[t] in ("dry", "contract")]
    print(f"n_dry_contract_periods={len(dry_contract_idx)} (dry={sum(1 for t in dry_contract_idx if states[t]=='dry')}, contract={sum(1 for t in dry_contract_idx if states[t]=='contract')})")

    er_points = []  # (t, er, sign)
    for t in dry_contract_idx:
        er = kaufman_er(C, t, N_ER)
        if er is None:
            continue
        net = C[t] - C[t - N_ER]
        sign = 1 if net > 0 else (-1 if net < 0 else 0)
        if sign == 0:
            continue
        er_points.append((t, er, sign))

    print(f"n_er_points(valid, sign!=0)={len(er_points)}")
    if len(er_points) < 6:
        print("TOO FEW POINTS for tercile split -- aborting")
        return

    er_vals = sorted(p[1] for p in er_points)
    n = len(er_vals)
    lo_cut = er_vals[n // 3 - 1] if n // 3 >= 1 else er_vals[0]
    hi_cut = er_vals[-(n // 3)] if n // 3 >= 1 else er_vals[-1]

    low_group = [p for p in er_points if p[1] <= lo_cut]
    high_group = [p for p in er_points if p[1] >= hi_cut]
    print(f"tercile cuts: lo<={lo_cut:.3f} hi>={hi_cut:.3f}")
    print(f"n_low_er={len(low_group)} n_high_er={len(high_group)}")

    def eval_group(group, horizon_min):
        rets = []
        for t, er, sign in group:
            fut = t + horizon_min
            if fut >= n_bars:
                continue
            fwd_ret = sign * (C[fut] - C[t])
            rets.append(fwd_ret)
        if not rets:
            return None, None, 0
        hit = sum(1 for r in rets if r > 0) / len(rets)
        avg = sum(rets) / len(rets)
        return hit, avg, len(rets)

    print()
    print(f"{'horizon(min)':>12} | {'LOW-ER hit':>10} {'LOW-ER avg':>10} {'n':>4} | {'HIGH-ER hit':>11} {'HIGH-ER avg':>11} {'n':>4}")
    results = {}
    for h in HORIZONS_MIN:
        lo_hit, lo_avg, lo_n = eval_group(low_group, h)
        hi_hit, hi_avg, hi_n = eval_group(high_group, h)
        results[h] = dict(lo_hit=lo_hit, lo_avg=lo_avg, lo_n=lo_n, hi_hit=hi_hit, hi_avg=hi_avg, hi_n=hi_n)
        print(f"{h:>12} | {lo_hit!s:>10} {lo_avg!s:>10} {lo_n:>4} | {hi_hit!s:>11} {hi_avg!s:>11} {hi_n:>4}")

    print()
    print("RESULTS_JSON", results)


if __name__ == "__main__":
    main()
