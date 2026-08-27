#!/usr/bin/env python3
"""Tick-level rebuild + re-verify of CELL_TUNE_V3_PATCHES (day|normal +
day|div_hh_weak_vol full block) for the 3 holdout windows (julsep25, octdec25,
janmar26), closing the second validation gap noted in
src/order/tmf_channel_pv16_book.py's v1.4.0 docstring.

Pattern (same as the already-done w83 tick rebuild): for each day, keep the
night-session rows straight from the engine's own 1m holdout cache
(tmf_channel.cache_store.load_day), but REPLACE the day-session rows
(sess=='day', 08:45-13:44) with 1-min OHLCV bars resampled directly from the
front-month-filtered tick stream (tx_channel_tick_validation.load_front_month_ticks
+ resample_to_1min — this repo's already-fixed contract-mixing/spread-quote
filter). Minutes with no matching tick-derived bar fall back to the original
cache bar (reported per day). Feed the hybrid row list into
tmf_channel.engine.simulate() directly (bypassing tmf_channel.harness.run_day's
cache_name loader, since we need the hybrid rows, not a named cache).

Reports before(V2-only)/after(V2+V3) delta on both the bar-level cache and the
tick-rebuilt hybrid rows, plus day-clustered paired t-test on the delta.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmf_channel.cache_store import load_day, list_days  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate, summarize  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
)
from tx_channel_tick_validation import load_front_month_ticks, resample_to_1min  # noqa: E402

WINDOWS = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}


def v2_only_book():
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def build_hybrid_rows(day: str, cache_rows: list[dict]) -> tuple[list[dict], dict]:
    """Replace sess=='day' rows with tick-resampled bars where available."""
    ticks = load_front_month_ticks(day)
    stats = dict(day_rows=0, replaced=0, fallback=0, had_ticks=ticks is not None)
    if ticks is None or ticks.empty:
        return cache_rows, stats
    tb = resample_to_1min(ticks)
    if tb.empty:
        return cache_rows, stats
    tb_by_hhmm = {
        row["Datetime"].strftime("%H:%M"): row
        for _, row in tb.iterrows()
    }
    out = []
    for r in cache_rows:
        if r["sess"] != "day":
            out.append(r)
            continue
        stats["day_rows"] += 1
        hhmm = r["t"]
        tr = tb_by_hhmm.get(hhmm)
        if tr is None:
            stats["fallback"] += 1
            out.append(r)
            continue
        stats["replaced"] += 1
        out.append(dict(
            t=hhmm, o=float(tr["Open"]), h=float(tr["High"]),
            l=float(tr["Low"]), c=float(tr["Close"]), v=float(tr["Volume"]),
            sess="day",
        ))
    return out, stats


def sim_net(day: str, rows: list[dict], recipe: dict, vix: dict) -> tuple[float, int]:
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    trades, *_ = simulate(O, H, L, C, V, T, deepcopy(recipe), vix_delta=vix)
    s = summarize(trades) if trades else {"n": 0, "net": 0.0}
    return float(s.get("net") or 0.0), int(s.get("n") or 0)


def day_clustered_t(deltas: list[float]) -> tuple[float, float]:
    n = len(deltas)
    if n < 2:
        return float("nan"), float("nan")
    dmean = mean(deltas)
    dsd = stdev(deltas)
    se = dsd / (n ** 0.5)
    t = dmean / se if se > 0 else float("inf")
    try:
        from scipy import stats
        p = 2 * (1 - stats.t.cdf(abs(t), df=n - 1))
    except ImportError:
        p = float("nan")
    return t, p


def main():
    before = deepcopy(PAPER_RECIPE)
    before["session_pv_book"] = v2_only_book()
    after = PAPER_RECIPE
    before.setdefault("hang_anchor", "O")
    after.setdefault("hang_anchor", "O")
    vix = load_vixtwn_delta() or {}

    for wname, cache_src in WINDOWS.items():
        days = list_days(source=cache_src)
        print(f"\n{'='*60}\n{wname}: {days[0]}..{days[-1]} n={len(days)} (cache={cache_src})")

        bar_deltas, tick_deltas = [], []
        n_tick_days = 0
        total_day_rows = total_replaced = total_fallback = 0
        for d in days:
            rows = load_day(d, source=cache_src)
            if not rows:
                continue
            nb_before, _ = sim_net(d, rows, before, vix)
            nb_after, _ = sim_net(d, rows, after, vix)
            bar_deltas.append(nb_after - nb_before)

            hybrid, stats = build_hybrid_rows(d, rows)
            if stats["had_ticks"] and stats["replaced"] > 0:
                n_tick_days += 1
                total_day_rows += stats["day_rows"]
                total_replaced += stats["replaced"]
                total_fallback += stats["fallback"]
                nt_before, _ = sim_net(d, hybrid, before, vix)
                nt_after, _ = sim_net(d, hybrid, after, vix)
                tick_deltas.append(nt_after - nt_before)

        bt, bp = day_clustered_t(bar_deltas)
        print(f"  BAR-LEVEL:  n_days={len(bar_deltas)} sum(delta)={sum(bar_deltas):.1f} "
              f"mean={mean(bar_deltas):.2f} t={bt:.3f} p={bp:.4g}")

        if tick_deltas:
            tt, tp = day_clustered_t(tick_deltas)
            cov = 100.0 * total_replaced / total_day_rows if total_day_rows else 0.0
            print(f"  TICK-REBUILT: n_days={n_tick_days} sum(delta)={sum(tick_deltas):.1f} "
                  f"mean={mean(tick_deltas):.2f} t={tt:.3f} p={tp:.4g} "
                  f"(day-bar tick-coverage={cov:.1f}%, fallback={total_fallback}/{total_day_rows})")
        else:
            print("  TICK-REBUILT: no tick data available for this window")


if __name__ == "__main__":
    main()
