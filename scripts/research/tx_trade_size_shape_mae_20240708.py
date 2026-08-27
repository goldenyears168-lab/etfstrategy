#!/usr/bin/env python3
"""One-off: trade-SIZE-DISTRIBUTION SHAPE (not direction, not net imbalance)
vs MAE magnitude, single day 2024-07-08, hang_anchor="O" main entries.

New angle vs prior-tested lines (Wyckoff effort/result divergence: killed,
day/night sign flip, struct_break exits only; large-trade-net-volume-ratio:
tested directionally, narrow 08:45-09:00 window only). This tests whether
the SHAPE of the trade-size distribution in the 5 minutes before entry
(concentration / tail-heaviness of trade sizes, ignoring buy/sell sign and
net imbalance) predicts MAE magnitude on the MAIN hang_anchor entry.

Method:
  1. Load front-month ticks via tx_channel_tick_validation.load_front_month_
     ticks(day) (price+volume+timestamp, no bid/ask, no buy/sell sign).
  2. Run tmf_walkforward_harness.run_batch([day]) with the live baseline
     recipe (order.tmf_channel_pv16_book.specialized_cell_book() is what
     build_recipe() -> PAPER_RECIPE already encodes; night uses day recipe
     via ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE=1) to get real trades incl.
     mae/mfe.
  3. For each trade's entry time (et), take the trailing 5-minute window of
     ticks [et-5min, et) and compute, from the raw per-tick `volume` field
     (trade size):
       - Hill tail-index estimate on the upper tail of trade sizes. k =
         max(5, round(0.05 * n_window)). gamma_hill = (1/k) * sum_{i=1}^{k}
         [ln(X_(n-i+1)) - ln(X_(n-k))] over sizes sorted ascending; alpha =
         1/gamma_hill (higher alpha = thinner tail / less extreme-size
         concentration). CAVEAT: sizes are highly discrete (min=2, mode=2,
         values step by 2 -- see EDA below) so there are heavy ties at and
         near the k-th order statistic, which biases gamma_hill toward 0
         (many zero log-differences) and inflates alpha. Reported as-is,
         flagged, not silently swapped for a proxy -- Hill is NOT
         impractical at these window sizes (n_window typically 300-2000+
         ticks in 5 min), the ties are a distributional-shape fact, not a
         sample-size problem.
       - top1pct_share: sum(size) of the top ceil(1%*n_window) trades by
         size, divided by sum(all sizes) in the window.
       - herfindahl: sum((size_i / sum(size))^2) over all ticks in the
         window (share-of-total-volume HHI; higher = more concentrated in
         a few large prints).
  4. Spearman correlation of each feature vs mae across the day's trades
     (only if n_trades >= 4 per task instructions; else raw pairs only).

Read-only against ${GOLDENSTOCKS_DATA_DIR} tick cache; no live state touched.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

DAY = "2024-07-08"
TRAIL_MINUTES = 5


def hill_tail_index(sizes: list[float]) -> tuple[float | None, int]:
    """Hill estimator on the upper tail. Returns (alpha, k) or (None, k) if
    degenerate (k too small or X_(n-k) == 0)."""
    n = len(sizes)
    if n < 20:
        return None, 0
    xs = sorted(sizes)
    k = max(5, round(0.05 * n))
    k = min(k, n - 1)
    threshold = xs[n - k - 1]
    if threshold <= 0:
        return None, k
    top_k = xs[n - k:]
    gamma = sum(math.log(x) - math.log(threshold) for x in top_k) / k
    if gamma <= 0:
        return None, k
    return 1.0 / gamma, k


def top1pct_share(sizes: list[float]) -> float | None:
    n = len(sizes)
    if n < 5:
        return None
    xs = sorted(sizes, reverse=True)
    n_top = max(1, math.ceil(0.01 * n))
    total = sum(xs)
    if total <= 0:
        return None
    return sum(xs[:n_top]) / total


def herfindahl(sizes: list[float]) -> float | None:
    total = sum(sizes)
    if total <= 0:
        return None
    return sum((s / total) ** 2 for s in sizes)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None

    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    mean_rx, mean_ry = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    var_x = sum((v - mean_rx) ** 2 for v in rx)
    var_y = sum((v - mean_ry) ** 2 for v in ry)
    if var_x == 0 or var_y == 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def main():
    import os

    os.environ["ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE"] = "1"
    from tmf_walkforward_harness import run_batch
    from tx_channel_tick_validation import load_front_month_ticks

    result = run_batch([DAY], label="size_shape")
    trades = result["trades"]

    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(json.dumps({"day": DAY, "error": "no tick data"}))
        return
    ticks = ticks.sort_values("dt").reset_index(drop=True)
    ticks_dt = ticks["dt"].tolist()
    ticks_vol = ticks["volume"].astype(float).tolist()

    import bisect

    pairs = []
    for t in trades:
        et = datetime.fromisoformat(t["et"].replace("+08:00", ""))
        lo = et - timedelta(minutes=TRAIL_MINUTES)
        lo_idx = bisect.bisect_left(ticks_dt, lo)
        hi_idx = bisect.bisect_left(ticks_dt, et)
        window_sizes = ticks_vol[lo_idx:hi_idx]
        n_window = len(window_sizes)
        alpha, k_used = hill_tail_index(window_sizes)
        t1 = top1pct_share(window_sizes)
        hhi = herfindahl(window_sizes)
        pairs.append({
            "et": t["et"], "side": t["s"], "mae": t["mae"], "pnl": t["pnl"],
            "n_window_ticks": n_window, "hill_k": k_used,
            "hill_alpha": round(alpha, 3) if alpha is not None else None,
            "top1pct_share": round(t1, 4) if t1 is not None else None,
            "herfindahl": round(hhi, 5) if hhi is not None else None,
        })

    def corr_if_enough(key):
        valid = [p for p in pairs if p[key] is not None]
        if len(valid) < 4:
            return None, len(valid)
        xs = [p[key] for p in valid]
        ys = [p["mae"] for p in valid]
        return spearman(xs, ys), len(valid)

    rho_hill, n_hill = corr_if_enough("hill_alpha")
    rho_t1, n_t1 = corr_if_enough("top1pct_share")
    rho_hhi, n_hhi = corr_if_enough("herfindahl")

    out = {
        "day": DAY,
        "n_trades": len(trades),
        "trailing_window_minutes": TRAIL_MINUTES,
        "eda_note": "tick volume field: min=2, mode=2 (82299/101466 ticks), "
                     "values step by 2 -- heavy discreteness/ties, flagged "
                     "as Hill-estimator caveat above, not treated as an "
                     "impractical-n case.",
        "pairs": pairs,
        "spearman": {
            "hill_alpha_vs_mae": rho_hill, "n_hill": n_hill,
            "top1pct_share_vs_mae": rho_t1, "n_top1pct": n_t1,
            "herfindahl_vs_mae": rho_hhi, "n_herfindahl": n_hhi,
        },
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
