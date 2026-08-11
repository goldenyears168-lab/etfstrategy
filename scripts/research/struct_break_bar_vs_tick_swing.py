#!/usr/bin/env python3
"""Item #5: struct_break swing bar-extreme vs tick-extreme.

The struct_break mechanism (causal_engine.py ~line 1642-1656) computes the
swing level from the BAR low/high array (`window = L[a0:t]`), even though the
exit trigger itself is checked tick-by-tick (`px = tk_px[k]`). This script
asks: how different is that bar-derived swing level from the TRUE tick-level
extreme over the same bar window (a0..t-1), for every struct_break trade in
the current PAPER_RECIPE (v1.4.0) w83 backtest? Does any discrepancy explain
part of the ~19pt overshoot found earlier tonight between
-swing_distance_at_entry and actual struct_break pnl?
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np

from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import RECIPE_VERSION
from datetime import date, timedelta

from tmf_channel.cache_store import bar_timestamps, list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate
from tx_channel_tick_validation import load_front_month_ticks

CACHE = "tx_1m_fullnight_cache_full.json"


def _ticks_spanning_session(day: str):
    """Front-month ticks covering BOTH calendar dates a session touches.

    A session key spans `day` 08:45 -> `day`+1 04:59 for a session-convention
    cache, so a single calendar-day tick frame cannot cover the post-midnight
    tail. Concatenate both days and let the dt window select.
    """
    import pandas as pd

    frames = []
    for cal in (day, (date.fromisoformat(day) + timedelta(days=1)).isoformat()):
        f = load_front_month_ticks(cal)
        if f is not None and not f.empty:
            frames.append(f)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True).sort_values("dt").reset_index(drop=True)


def arrays_from_rows(rows, day):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=CACHE)
    return O, H, L, C, V, T


def main():
    days = list_days(CACHE)
    print(f"w83 days: {len(days)}  recipe_version={RECIPE_VERSION}")
    look = int(PAPER_RECIPE.get("struct_exit_look") or 12)
    print(f"struct_exit_look={look}")

    vix = load_vixtwn_delta() or {}

    all_diag = []
    ticks_cache = {}
    n_no_tick_data = 0

    for day in days:
        rows = load_day(day, source=CACHE)
        if not rows:
            continue
        O, H, L, C, V, T = arrays_from_rows(rows, day)
        trades, events, ws, wl, rvol, regime, open_pos = simulate(
            O, H, L, C, V, T, PAPER_RECIPE, vix_delta=vix
        )
        sb_trades = [tr for tr in trades if str(tr.get("why", "")).startswith("struct_break")]
        if not sb_trades:
            continue

        if day not in ticks_cache:
            ticks_cache[day] = _ticks_spanning_session(day)
        tdf = ticks_cache[day]
        if tdf is None or tdf.empty:
            n_no_tick_data += len(sb_trades)
            continue
        tdf = tdf.sort_values("dt")
        tdt = tdf["dt"].values
        tprice = tdf["price"].to_numpy()

        for tr in sb_trades:
            xb = int(tr["xb"])
            side = tr["s"]
            a0 = max(0, xb - look)
            if xb - a0 < 3:
                continue
            bar_swing = float(tr["why"].split("|")[1])

            # bar window a0..xb-1 (the same slicing as window = L[a0:t])
            import pandas as pd

            def to_ts(iso: str) -> "pd.Timestamp":
                # T now carries each bar's REAL calendar date (cache_store
                # .bar_timestamps): for tx_1m_fullnight_cache* the 00:00-04:59
                # tail is day+1. Previously this reconstructed the date as the
                # session key, which indexed 24h into the wrong tick file for
                # every post-midnight bar (fixed 2026-08-11).
                return pd.Timestamp(f"{iso[:10]} {iso[11:16]}:00")

            ts_start = to_ts(T[a0])
            # window end = bar close of bar xb-1, i.e. up to (but not
            # including) the open of bar xb; approximate window end as
            # start-of-bar xb (next bar boundary) so it exactly spans the
            # same wall-clock range the bar-low array covers.
            ts_end = to_ts(T[xb])

            mask = (tdt >= np.datetime64(ts_start)) & (tdt < np.datetime64(ts_end))
            win_ticks = tprice[mask]
            if win_ticks.size == 0:
                n_no_tick_data += 1
                continue

            if side == "L":
                tick_swing = float(win_ticks.min())
                # bar_swing should equal true tick min if bars built
                # correctly; positive diff means bar array UNDERSTATED how
                # low price actually went (true low lower than bar-derived
                # swing) -> struct_break should have fired earlier/looser.
                diff = bar_swing - tick_swing
            else:
                tick_swing = float(win_ticks.max())
                diff = tick_swing - bar_swing

            swing_distance = float(tr["ep"]) - bar_swing if side == "L" else bar_swing - float(tr["ep"])
            swing_distance = abs(swing_distance)
            pnl = float(tr["pnl"])
            overshoot = -pnl - swing_distance  # positive = lost more than swing_distance implies

            all_diag.append(
                dict(
                    day=day,
                    side=side,
                    xb=xb,
                    bar_swing=bar_swing,
                    tick_swing=tick_swing,
                    diff=diff,
                    swing_distance=swing_distance,
                    pnl=pnl,
                    overshoot=overshoot,
                )
            )

    print(f"\nstruct_break trades diagnosed: {len(all_diag)}  (skipped no-tick-data: {n_no_tick_data})")
    if not all_diag:
        return

    diffs = np.array([d["diff"] for d in all_diag])
    print("\n--- bar_swing vs tick_swing diff (positive = bar array UNDERSTATED true extreme) ---")
    print(f"mean={diffs.mean():.3f}  median={np.median(diffs):.3f}  std={diffs.std():.3f}")
    print(f"min={diffs.min():.1f}  max={diffs.max():.1f}")
    nz = diffs[diffs != 0]
    print(f"n exact match (diff==0): {(diffs == 0).sum()} / {len(diffs)} ({100*(diffs==0).sum()/len(diffs):.1f}%)")
    print(f"n |diff|>=1pt: {(np.abs(diffs) >= 1).sum()}")
    print(f"n |diff|>=5pt: {(np.abs(diffs) >= 5).sum()}")
    if nz.size:
        print(f"among nonzero: mean={nz.mean():.2f} median={np.median(nz):.2f} n={nz.size}")

    # correlate diff with overshoot
    overshoots = np.array([d["overshoot"] for d in all_diag])
    if diffs.std() > 0 and overshoots.std() > 0:
        ic = np.corrcoef(diffs, overshoots)[0, 1]
        print(f"\nIC(diff, overshoot) = {ic:.3f}  n={len(diffs)}")
    else:
        print("\nIC undefined (zero variance)")

    # day-clustered check on diff (is nonzero diff a few days or spread out)
    import collections

    per_day = collections.defaultdict(list)
    for d in all_diag:
        per_day[d["day"]].append(d["diff"])
    nz_days = [dd for dd, vals in per_day.items() if any(v != 0 for v in vals)]
    print(f"\nn distinct days with any nonzero diff: {len(nz_days)} / {len(per_day)}")

    # show worst offenders
    worst = sorted(all_diag, key=lambda d: -abs(d["diff"]))[:15]
    print("\nworst |diff| trades:")
    for d in worst:
        print(
            f"  {d['day']} xb={d['xb']:4d} {d['side']} bar_swing={d['bar_swing']:.0f} "
            f"tick_swing={d['tick_swing']:.0f} diff={d['diff']:+.1f} "
            f"swing_dist={d['swing_distance']:.1f} pnl={d['pnl']:.1f} overshoot={d['overshoot']:.1f}"
        )


if __name__ == "__main__":
    main()
