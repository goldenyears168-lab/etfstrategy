#!/usr/bin/env python3
"""2026-08-11: user's new idea -- instead of NQ vs its own prior close,
compute BOTH TW (TX) and US (NQ) deviation from their own 5-period moving
average, then take the spread (tw_dev - us_dev). If one side is anomalously
stretched relative to its own recent trend more than the other, that gap
might be informative (catch-up/convergence or confirmation signal).

Exploratory step first (this script): does this spread actually predict
FORWARD TX returns at all, in a PIT-safe causal sense, before designing a
full gate mechanism and backtest around it? Computes day-clustered IC
(Spearman) between spread(t) and forward TX return over several horizons.

tw_dev(t)  = (C_tx[t] - MA5(C_tx)[t]) / MA5(C_tx)[t] * 100   -- 1m bars, PIT
us_dev(t)  = (nq_now - MA5(nq_1h, last 5 SETTLED bars up to t)) / MA5 * 100
             -- reuses tonight's forming-bar-safe price_at_or_before(min_age=1h)
spread(t)  = tw_dev(t) - us_dev(t)

Does NOT touch causal_engine.py or any live file. Diagnostic only.
"""
from __future__ import annotations

import statistics as st
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from tmf_channel import nq_gate as nq_gate_mod  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from tmf_channel.cache_store import load_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from us_futures_overnight import price_at_or_before  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})
IS_DAYS = JULY_DAYS + AUG_DAYS

HORIZONS_MIN = [15, 30, 60]


def us_ma5_dev(bundle, dt_et, min_age):
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    points = []
    for k in range(6):  # now + 5 prior settled bars
        px = price_at_or_before(nq_1h, dt_et - timedelta(hours=k), min_age=min_age)
        if px is not None:
            points.append(px)
        if len(points) >= 6:
            break
    if len(points) < 6:
        return None
    now_px = points[0]
    ma5 = sum(points[1:6]) / 5.0
    if ma5 <= 0:
        return None
    return (now_px - ma5) / ma5 * 100.0


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    rx = {v: i for i, v in enumerate(sorted(range(n), key=lambda i: xs[i]))}
    ry = {v: i for i, v in enumerate(sorted(range(n), key=lambda i: ys[i]))}
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n ** 2 - 1))


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    min_age = nq_signal.NQ_ES_1H_MIN_AGE
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)

    per_day_ic = {h: [] for h in HORIZONS_MIN}
    total_bars = 0

    for d in IS_DAYS:
        source = SOURCE_FOR_DAY.get(d, "tx_1m_fullnight_cache_full.json")
        rows = load_day(d, source=source)
        if not rows:
            continue
        C = [float(r["c"]) for r in rows]
        T = [f"{d}T{r.get('t')}:00.000+08:00" for r in rows]
        n = len(C)

        spreads = [None] * n
        for i in range(5, n):
            tw_ma5 = sum(C[i - 5:i]) / 5.0
            if tw_ma5 <= 0:
                continue
            tw_dev = (C[i] - tw_ma5) / tw_ma5 * 100.0
            dt_et = datetime.fromisoformat(T[i]).astimezone(_TZ).astimezone(nq_signal.TZ_ET)
            us_dev = us_ma5_dev(bundle, dt_et, min_age)
            if us_dev is None:
                continue
            spreads[i] = tw_dev - us_dev

        for horizon in HORIZONS_MIN:
            xs, ys = [], []
            for i in range(5, n - horizon):
                if spreads[i] is None:
                    continue
                fwd_ret = (C[i + horizon] - C[i]) / C[i] * 100.0
                xs.append(spreads[i])
                ys.append(fwd_ret)
            if len(xs) >= 10:
                ic = spearman(xs, ys)
                if ic is not None:
                    per_day_ic[horizon].append(ic)
        total_bars += n

    print(f"IS_22d, total bars scanned: {total_bars}")
    for horizon in HORIZONS_MIN:
        ics = per_day_ic[horizon]
        if len(ics) < 2:
            print(f"horizon={horizon}min: insufficient days with data (n={len(ics)})")
            continue
        mean_ic = st.mean(ics)
        sd_ic = st.stdev(ics)
        t = mean_ic / (sd_ic / (len(ics) ** 0.5)) if sd_ic > 0 else 0.0
        try:
            from scipy import stats as sp
            p = float(2 * (1 - sp.t.cdf(abs(t), df=len(ics) - 1)))
        except Exception:
            p = None
        print(f"horizon={horizon}min: n_days={len(ics)} mean_daily_IC={mean_ic:.4f} "
              f"std={sd_ic:.4f} t={t:.3f} p={p}")


if __name__ == "__main__":
    main()
