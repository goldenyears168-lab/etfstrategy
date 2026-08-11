#!/usr/bin/env python3
"""2026-08-11 v2: corrected timestamp construction using
tmf_channel.cache_store.bar_timestamps() (source-aware, uses each bar's own
`cal` field for post-midnight bars) instead of the naive
T=f"{day}T{t}..." used in tmf_tw_us_ma5h_deviation_spread_ic.py -- that
naive construction is 24h stale for the 00:00-04:59 tail on
"session"-convention sources (tx_1m_fullnight_cache_full.json, which is
what IS/OOS actually use), per the 2026-08-11 audit
(scripts/research/audit_tx_1m_fullnight_cache_quality.py) and the new
cache_store.assert_real_timestamps guard.

Parameterized by window_hours so both 3h and 5h can be tested with the
identical, now-correct methodology. Re-run of the IS+OOS IC check.
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
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
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


def us_ma_dev(bundle, dt_et, min_age, window_hours):
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    points = []
    for k in range(window_hours + 1):
        px = price_at_or_before(nq_1h, dt_et - timedelta(hours=k), min_age=min_age)
        if px is not None:
            points.append(px)
        if len(points) >= window_hours + 1:
            break
    if len(points) < window_hours + 1:
        return None
    now_px = points[0]
    ma = sum(points[1:window_hours + 1]) / window_hours
    if ma <= 0:
        return None
    return (now_px - ma) / ma * 100.0


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    rx = {v: i for i, v in enumerate(sorted(range(n), key=lambda i: xs[i]))}
    ry = {v: i for i, v in enumerate(sorted(range(n), key=lambda i: ys[i]))}
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n ** 2 - 1))


def run_days(label, days, source_map, bundle, min_age, window_hours):
    tw_window_min = window_hours * 60
    per_day_ic = {h: [] for h in HORIZONS_MIN}
    us_cache: dict[str, float | None] = {}
    total_bars = 0

    for d in days:
        source = source_map.get(d, "tx_1m_fullnight_cache_full.json") if isinstance(source_map, dict) else source_map
        rows = load_day(d, source=source)
        if not rows:
            continue
        C = [float(r["c"]) for r in rows]
        T = bar_timestamps(d, rows, source=source)
        n = len(C)

        spreads = [None] * n
        roll_sum = sum(C[:tw_window_min]) if n >= tw_window_min else 0.0
        for i in range(tw_window_min, n):
            if i > tw_window_min:
                roll_sum += C[i - 1] - C[i - 1 - tw_window_min]
            tw_ma = roll_sum / tw_window_min
            if tw_ma <= 0:
                continue
            tw_dev = (C[i] - tw_ma) / tw_ma * 100.0
            dt_et = datetime.fromisoformat(T[i]).astimezone(_TZ).astimezone(nq_signal.TZ_ET)
            cache_key = dt_et.strftime("%Y-%m-%d %H")
            if cache_key not in us_cache:
                us_cache[cache_key] = us_ma_dev(bundle, dt_et, min_age, window_hours)
            us_dev = us_cache[cache_key]
            if us_dev is None:
                continue
            spreads[i] = tw_dev - us_dev

        for horizon in HORIZONS_MIN:
            xs, ys = [], []
            for i in range(tw_window_min, n - horizon):
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

    print(f"=== {label}, window={window_hours}h, total bars={total_bars} ===")
    for horizon in HORIZONS_MIN:
        ics = per_day_ic[horizon]
        if len(ics) < 2:
            print(f"horizon={horizon}min: insufficient (n={len(ics)})")
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


def main():
    patch_nq_gate_for_backfill(lookback_days=500)
    min_age = nq_signal.NQ_ES_1H_MIN_AGE
    bundle = nq_gate_mod.get_cached("nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle)

    oos_days = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

    for window_hours in (3, 5):
        run_days("IS_22d", IS_DAYS, SOURCE_FOR_DAY, bundle, min_age, window_hours)
        run_days("OOS_66d", oos_days, "tx_1m_fullnight_cache_full.json", bundle, min_age, window_hours)
        print()


if __name__ == "__main__":
    main()
