#!/usr/bin/env python3
"""2026-08-13: BVC/VPIN-style toxicity vs MAE magnitude, single-day probe
(assigned day 2024-10-09, part of tonight's "detective" research-gap scan).

Different angle from the already-dead tick-rule OFI test: that one
classified each TICK's direction (corrupted by bid-ask bounce) and
targeted return DIRECTION. This one buckets EQUAL VOLUME into bins,
classifies each BIN's buy/sell split via the standardized within-bin price
change (BVC, Easley/Lopez de Prado/O'Hara VPIN paper) -- approximated here
with a normal CDF (NOT a fitted t-distribution; noted per task ask) -- and
targets MAE MAGNITUDE (how far adverse a trade goes), not direction.

Read-only research script. No live state touched.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Must be set before order.tmf_channel_config is first imported anywhere in
# this process, since PAPER_RECIPE (and therefore session_pv_book) is built
# once at module-import time from specialized_cell_book().
os.environ.setdefault("ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE", "1")

DAY = "2024-10-09"
N_BUCKETS_TARGET = 50
TRAILING_BUCKETS = 30

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def load_ticks(day: str) -> list[dict]:
    path = TICK_DIR / f"{day}.json"
    rows = json.loads(path.read_text())
    rows = [r for r in rows if "/" not in str(r.get("contract_date", ""))]
    if not rows:
        return []
    from collections import Counter

    counts = Counter(r["contract_date"] for r in rows)
    front = counts.most_common(1)[0][0]
    rows = [r for r in rows if r["contract_date"] == front]
    rows.sort(key=lambda r: r["date"])
    return rows


def build_equal_volume_buckets(ticks: list[dict], n_buckets_target: int) -> list[dict]:
    """Simple equal-volume bucketing: accumulate raw tick volume into a
    running bucket; once accumulated volume reaches bucket_size, close the
    bucket at the CURRENT tick's price/time. Approximation noted: a tick
    that would overflow a bucket is NOT split across the boundary -- its
    full volume goes to the bucket being closed. bucket_size is fixed for
    the whole day from total day volume / n_buckets_target (not reset
    causally per se, but the *bucketing* itself only ever looks backward
    tick-by-tick as it accumulates -- no future ticks are used to decide
    where a bucket closes)."""
    total_volume = sum(float(r["volume"]) for r in ticks)
    bucket_size = total_volume / n_buckets_target
    buckets: list[dict] = []
    acc_vol = 0.0
    last_price = None
    bucket_start_price = None
    bucket_end_time = None
    for r in ticks:
        price = float(r["price"])
        vol = float(r["volume"])
        if bucket_start_price is None:
            bucket_start_price = price
        acc_vol += vol
        last_price = price
        bucket_end_time = r["date"]
        if acc_vol >= bucket_size:
            buckets.append(dict(close=last_price, end_time=bucket_end_time, volume=acc_vol))
            acc_vol = 0.0
            bucket_start_price = None
    # trailing partial bucket dropped (incomplete, matches causal-only-on-
    # completed-buckets stance used for the toxicity series below).
    return buckets


def compute_bvc_toxicity_series(buckets: list[dict], trailing: int) -> list[dict]:
    """Causal: bucket i's toxicity only uses buckets <= i. sigma_i estimated
    from the trailing (up to `trailing`) prior bucket-to-bucket price
    changes ending at bucket i-1 (i.e. does NOT include bucket i's own
    delta in its own sigma denominator, and never looks at bucket i+1+)."""
    closes = [b["close"] for b in buckets]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]  # delta for bucket i (1-indexed into buckets)
    out = []
    imbalances: list[float] = []
    for i in range(1, len(buckets)):
        delta = deltas[i - 1]
        hist = deltas[max(0, i - 1 - trailing):(i - 1)]  # prior deltas, excluding this one
        if len(hist) >= 2:
            mean_h = sum(hist) / len(hist)
            var_h = sum((x - mean_h) ** 2 for x in hist) / (len(hist) - 1)
            sigma = math.sqrt(var_h)
        else:
            sigma = 0.0
        z = (delta / sigma) if sigma > 1e-9 else 0.0
        imbalance = abs(2.0 * _norm_cdf(z) - 1.0)
        imbalances.append(imbalance)
        vpin_window = imbalances[-trailing:]
        vpin = sum(vpin_window) / len(vpin_window)
        n_in_window = len(vpin_window)
        out.append(dict(end_time=buckets[i]["end_time"], vpin=vpin, n_buckets_in_window=n_in_window))
    return out


def toxicity_at(series: list[dict], et_dt: datetime, min_buckets: int) -> float | None:
    """Last toxicity value whose bucket closed at or before et (causal
    lookup) -- and only if the trailing window has at least min_buckets
    real buckets behind it (else the score is a warm-up artifact)."""
    best = None
    for row in series:
        row_dt = datetime.fromisoformat(row["end_time"])
        if row_dt <= et_dt:
            if row["n_buckets_in_window"] >= min_buckets:
                best = row["vpin"]
        else:
            break
    return best


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None

    def rank(vals: list[float]) -> list[float]:
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[idx[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    mean_rx, mean_ry = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    var_x = sum((v - mean_rx) ** 2 for v in rx)
    var_y = sum((v - mean_ry) ** 2 for v in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def main() -> None:
    from tmf_walkforward_harness import run_batch

    ticks = load_ticks(DAY)
    print(f"n_ticks={len(ticks)}", file=sys.stderr)
    buckets = build_equal_volume_buckets(ticks, N_BUCKETS_TARGET)
    print(f"n_buckets={len(buckets)} target={N_BUCKETS_TARGET}", file=sys.stderr)
    tox_series = compute_bvc_toxicity_series(buckets, TRAILING_BUCKETS)

    result = run_batch([DAY], label="bvc-mae-probe")
    trades = [t for t in result["trades"] if t.get("day") == DAY]
    print(f"n_trades={len(trades)}", file=sys.stderr)

    pairs = []
    for t in trades:
        et_raw = t.get("et")
        if not et_raw:
            continue
        et_dt = datetime.fromisoformat(et_raw)
        et_naive = et_dt.replace(tzinfo=None)
        tox = toxicity_at(tox_series, et_naive, min_buckets=TRAILING_BUCKETS)
        pairs.append(dict(et=et_raw, s=t.get("s"), toxicity=tox, mae=t.get("mae"), mfe=t.get("mfe"), pnl=t.get("pnl")))

    valid_pairs = [p for p in pairs if p["toxicity"] is not None]
    xs = [p["toxicity"] for p in valid_pairs]
    ys = [p["mae"] for p in valid_pairs]
    rho = spearman(xs, ys)

    out = dict(
        day=DAY,
        n_trades=len(trades),
        n_trades_with_toxicity=len(valid_pairs),
        spearman_toxicity_vs_mae=rho,
        pairs=pairs,
    )
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
