#!/usr/bin/env python3
"""One-off: BVC/VPIN-style toxicity vs MAE magnitude, single day 2024-07-08.

Detective-scan follow-up (see docstring convo) -- targets MAE MAGNITUDE, not
tick-direction (that line is dead per tx_channel_tick_validation findings).

Method:
  1. Load front-month ticks via tx_channel_tick_validation.load_front_month_
     ticks(day) (price+volume+timestamp only, no bid/ask).
  2. Bucket ticks into EQUAL-VOLUME bins, bucket_size = total_day_volume/50
     (whole-tick accumulation, no intra-tick volume splitting).
  3. Per bucket: delta_p = close_i - close_{i-1}; sigma estimated causally
     from the std of delta_p over the trailing 30 completed buckets (NOT
     including the current one). buy_frac = NORMAL CDF(delta_p / sigma) --
     normal approx used instead of Easley/Lopez de Prado/O'Hara's
     Student-t, per task instructions (note: this is the approximation,
     not the t-distribution version).
  4. order_imbalance_i = |2*buy_frac - 1| (in [0,1]).
  5. VPIN/toxicity at each bucket close = mean(order_imbalance) over the
     trailing 30 completed buckets (causal, ending at that bucket).
  6. For each trade, toxicity at entry = VPIN as of the last bucket that
     CLOSED AT OR BEFORE the trade's entry timestamp (et) -- no lookahead
     into the bucket still forming at entry time.
  7. Spearman corr(toxicity_at_entry, mae) across the day's trades.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

DAY = "2024-07-08"
N_BUCKET_TARGET = 50
TRAILING_BUCKETS = 30


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def build_buckets(day: str):
    from tx_channel_tick_validation import load_front_month_ticks

    df = load_front_month_ticks(day)
    if df is None or df.empty:
        return []
    df = df.sort_values("dt").reset_index(drop=True)
    total_vol = float(df["volume"].sum())
    bucket_size = max(1.0, total_vol / N_BUCKET_TARGET)

    buckets = []
    cur_vol = 0.0
    cur_last_price = None
    cur_start = None
    for _, row in df.iterrows():
        if cur_start is None:
            cur_start = row["dt"]
        cur_vol += float(row["volume"])
        cur_last_price = float(row["price"])
        if cur_vol >= bucket_size:
            buckets.append({"end_t": row["dt"], "close": cur_last_price, "vol": cur_vol})
            cur_vol = 0.0
            cur_start = None
    # trailing partial bucket at day end intentionally dropped (incomplete,
    # would need lookahead to know its final volume/close).
    return buckets, bucket_size, total_vol


def compute_toxicity_series(buckets: list[dict]) -> list[dict]:
    """Adds delta_p, sigma (trailing), order_imbalance, vpin (trailing mean)
    to each bucket dict, all causal (only using buckets strictly before /
    at-or-before the current one, per description above)."""
    deltas: list[float] = []
    imbalances: list[float] = []
    out = []
    prev_close = None
    for b in buckets:
        delta_p = None if prev_close is None else (b["close"] - prev_close)
        prev_close = b["close"]
        sigma = None
        oi = None
        if delta_p is not None:
            trailing_deltas = deltas[-TRAILING_BUCKETS:]
            if len(trailing_deltas) >= 5:
                mean_d = sum(trailing_deltas) / len(trailing_deltas)
                var = sum((d - mean_d) ** 2 for d in trailing_deltas) / (len(trailing_deltas) - 1)
                sigma = math.sqrt(var) if var > 0 else None
            if sigma:
                buy_frac = norm_cdf(delta_p / sigma)
                oi = abs(2 * buy_frac - 1)
            deltas.append(delta_p)
        vpin = None
        trailing_oi = [x for x in imbalances[-TRAILING_BUCKETS:]]
        if len(trailing_oi) >= 5:
            vpin = sum(trailing_oi) / len(trailing_oi)
        if oi is not None:
            imbalances.append(oi)
        out.append({**b, "delta_p": delta_p, "sigma": sigma, "order_imbalance": oi, "vpin": vpin})
    return out


def toxicity_at(buckets_tox: list[dict], et_iso: str) -> float | None:
    et = datetime.fromisoformat(et_iso.replace("+08:00", ""))
    last_vpin = None
    for b in buckets_tox:
        end_t = b["end_t"]
        end_t_naive = end_t.to_pydatetime().replace(tzinfo=None) if hasattr(end_t, "to_pydatetime") else end_t
        if end_t_naive <= et:
            if b["vpin"] is not None:
                last_vpin = b["vpin"]
        else:
            break
    return last_vpin


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

    result = run_batch([DAY], label="bvc_mae")
    trades = result["trades"]

    buckets, bucket_size, total_vol = build_buckets(DAY)
    buckets_tox = compute_toxicity_series(buckets)

    pairs = []
    for t in trades:
        tox = toxicity_at(buckets_tox, t["et"])
        pairs.append({"et": t["et"], "side": t["s"], "toxicity": tox, "mae": t["mae"], "pnl": t["pnl"]})

    valid = [p for p in pairs if p["toxicity"] is not None]
    xs = [p["toxicity"] for p in valid]
    ys = [p["mae"] for p in valid]
    rho = spearman(xs, ys)

    print(json.dumps({
        "day": DAY,
        "n_trades": len(trades),
        "n_trades_with_toxicity": len(valid),
        "n_buckets": len(buckets),
        "bucket_size_contracts": round(bucket_size, 1),
        "total_day_volume": total_vol,
        "spearman_toxicity_vs_mae": rho,
        "pairs": pairs,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
