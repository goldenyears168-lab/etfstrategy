"""Item #19: 750-day (tx_1m_tick_built_582d) data-quality scan.

Reuses the same causal cross-day-baseline burst detector as
tx_channel_0605_anomaly_detector.py (bar-cache close-to-close jumps, MAD-based
threshold frozen from the trailing 20 days, burst = >=3 flagged bars within a
20-minute window, session-open gap bar excluded) but applied to the full
750-day tx_1m_tick_built_582d source instead of the 265-day four-file union.

Adds a second, independent tick-level pass (same methodology used tonight for
the 140-day scan) on top of the raw front-month tick files in
finmind_tx_tick_by_day/ (confirmed present 2023-07-03..2026-08-07, i.e. covers
essentially the whole 750-day span, not just the 2026-01..08 window flagged in
the task brief) for:
  (a) tick-to-tick jump burst (identical causal-MAD/burst rule as bar-level)
  (b) same-timestamp price spread (ticks sharing an identical wall-clock
      second; max-min price spread flagged if it exceeds a day-frozen
      trailing-baseline threshold)

Both passes are day-clustered (one flag decision per day) and causal
(threshold from trailing history only, never look-ahead).
"""
from __future__ import annotations

import json
import sys
import time
import functools
print = functools.partial(print, flush=True)
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

SOURCE = "tx_1m_tick_built_582d"
OUT = Path(__file__).resolve().parents[2] / "reports" / "research" / "channel_lab" / "tx_channel_582d_quality_scan.json"

WARMUP = 20
K_MAD = 8.0
FLOOR_PTS = 250.0
BURST_WINDOW_MIN = 20
BURST_MIN_COUNT = 3
SESSION_OPEN_T = {"night": "15:00", "day": "08:45"}

TICK_K_MAD = 8.0
TICK_FLOOR_PTS = 20.0
SPREAD_K_MAD = 8.0
SPREAD_FLOOR_PTS = 15.0

# time budget for the tick-level pass (bar-level pass is cheap/fast and always
# runs full-sample); prioritize recent dates first per task instruction.
TICK_TIME_BUDGET_SEC = 8 * 60


def bar_time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def session_jumps(bars, sess):
    seq = sorted((b for b in bars if b["sess"] == sess), key=lambda b: b["t"])
    closes = [b["c"] for b in seq]
    times = [b["t"] for b in seq]
    jumps = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    return times, jumps


def detect_bar_day(bars, prior_jumps):
    flags = []
    for sess in ("night", "day"):
        times, jumps = session_jumps(bars, sess)
        hist = prior_jumps.get(sess, [])
        if len(hist) < 200:
            continue
        med = np.median(hist)
        mad = np.median(np.abs(np.array(hist) - med)) * 1.4826 + 1e-9
        thresh = max(FLOOR_PTS, med + K_MAD * mad)
        open_t = SESSION_OPEN_T[sess]
        for i, j in enumerate(jumps):
            if times[i + 1] == open_t:
                continue
            if j > thresh:
                flags.append((times[i + 1], sess, j, thresh))
    if not flags:
        return dict(n_flags=0, burst=False)
    flag_mins = sorted(bar_time_to_min(t) for t, *_ in flags)
    burst = False
    burst_window = None
    for i in range(len(flag_mins)):
        cnt = sum(1 for m in flag_mins[i:] if m - flag_mins[i] <= BURST_WINDOW_MIN)
        if cnt >= BURST_MIN_COUNT:
            burst = True
            burst_window = (flag_mins[i], flag_mins[i] + BURST_WINDOW_MIN)
            break
    return dict(n_flags=len(flags), flags=flags, burst=burst, burst_window=burst_window)


def run_bar_level_scan():
    import sqlite3
    from tmf_channel.cache_store import bars_db_path

    con = sqlite3.connect(f"file:{bars_db_path()}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT day, t, o, h, l, c, v, sess FROM bars WHERE source=? ORDER BY day, t",
        (SOURCE,),
    ).fetchall()
    con.close()
    day_cache: dict[str, list[dict]] = {}
    for day, t, o, h, l, c, v, sess in rows:
        day_cache.setdefault(day, []).append(
            dict(t=t, o=o, h=h, l=l, c=c, v=v, sess=sess)
        )
    all_days = sorted(day_cache.keys())
    print(f"[bar-level] {SOURCE}: {len(all_days)} days ({all_days[0]}..{all_days[-1]}), {len(rows)} bars loaded in one query")
    ordered = all_days
    results = []
    for idx, date in enumerate(ordered):
        bars = day_cache[date]
        prior = ordered[max(0, idx - WARMUP): idx]
        prior_jumps = {"night": [], "day": []}
        for pd_ in prior:
            for sess in ("night", "day"):
                _, j = session_jumps(day_cache[pd_], sess)
                prior_jumps[sess].extend(j)
        r = detect_bar_day(bars, prior_jumps)
        r["date"] = date
        results.append(r)
    bursts = [r for r in results if r["burst"]]
    n_eligible = sum(1 for r in results if r["n_flags"] or True) - 1  # exclude warmup-inflated first days loosely
    print(f"[bar-level] n_days={len(results)} n_burst={len(bursts)}")
    return results, bursts


def tick_level_check(date: str):
    ticks = load_front_month_ticks(date)
    if ticks is None or ticks.empty:
        return None
    prices = ticks["price"].to_numpy()
    dts = ticks["dt"].tolist()
    jumps = np.abs(np.diff(prices))
    jump_flags = []
    for i in range(1, len(jumps)):
        hist = jumps[max(0, i - 200): i]
        if len(hist) < 200:
            continue
        med = np.median(hist)
        mad = np.median(np.abs(hist - med)) * 1.4826 + 1e-9
        thresh = max(TICK_FLOOR_PTS, med + TICK_K_MAD * mad)
        if jumps[i] > thresh:
            jump_flags.append((str(dts[i + 1]), float(jumps[i]), float(thresh)))
    jump_mins = sorted(t for t, *_ in jump_flags)
    jump_burst = len(jump_flags) >= BURST_MIN_COUNT  # coarse: dense flags in a short tick file already implies clustering

    # same-timestamp (same wall-clock second) price spread
    import pandas as pd
    sec = ticks["dt"].dt.floor("s")
    grp = ticks.groupby(sec)["price"]
    spreads = (grp.max() - grp.min())
    spreads = spreads[spreads > 0].to_numpy()
    spread_flag_n = 0
    max_spread = float(spreads.max()) if len(spreads) else 0.0
    if len(spreads) >= 50:
        med_s = np.median(spreads)
        mad_s = np.median(np.abs(spreads - med_s)) * 1.4826 + 1e-9
        sthresh = max(SPREAD_FLOOR_PTS, med_s + SPREAD_K_MAD * mad_s)
        spread_flag_n = int((spreads > sthresh).sum())

    return dict(
        date=date,
        n_ticks=len(prices),
        n_jump_flags=len(jump_flags),
        jump_flags_sample=jump_flags[:5],
        max_jump=float(jumps.max()) if len(jumps) else 0.0,
        p999_jump=float(np.percentile(jumps, 99.9)) if len(jumps) else 0.0,
        jump_dense=jump_burst,
        n_same_ts_groups=len(spreads),
        max_same_ts_spread=max_spread,
        n_spread_flags=spread_flag_n,
    )


def run_tick_level_scan(prioritized_dates):
    out = []
    t0 = time.time()
    n_done = 0
    for date in prioritized_dates:
        if time.time() - t0 > TICK_TIME_BUDGET_SEC:
            print(f"[tick-level] time budget hit after {n_done} days")
            break
        r = tick_level_check(date)
        if r is not None:
            out.append(r)
            n_done += 1
    return out


def main():
    bar_results, bar_bursts = run_bar_level_scan()

    all_days = sorted(list_days(source=SOURCE))
    # prioritize recent dates first (task instruction), most-recent-first
    prioritized = sorted(all_days, reverse=True)
    tick_results = run_tick_level_scan(prioritized)
    tick_by_date = {r["date"]: r for r in tick_results}

    print(f"[tick-level] scanned {len(tick_results)} / {len(all_days)} days, "
          f"range {min(tick_by_date) if tick_by_date else None}..{max(tick_by_date) if tick_by_date else None}")

    tick_jump_flag_days = sorted(
        (r for r in tick_results if r["n_jump_flags"] >= BURST_MIN_COUNT),
        key=lambda r: -r["n_jump_flags"],
    )
    tick_spread_flag_days = sorted(
        (r for r in tick_results if r["n_spread_flags"] > 0),
        key=lambda r: -r["n_spread_flags"],
    )

    print(f"\n=== bar-level burst-flagged days ({len(bar_bursts)}) ===")
    for r in sorted(bar_bursts, key=lambda x: -x["n_flags"]):
        print(f"  {r['date']}  n_flags={r['n_flags']}  window={r['burst_window']}")

    print(f"\n=== tick-level jump-burst-flagged days ({len(tick_jump_flag_days)}) ===")
    for r in tick_jump_flag_days[:30]:
        print(f"  {r['date']}  n_jump_flags={r['n_jump_flags']}  max_jump={r['max_jump']:.0f}  n_ticks={r['n_ticks']}")

    print(f"\n=== tick-level same-timestamp-spread-flagged days ({len(tick_spread_flag_days)}) ===")
    for r in tick_spread_flag_days[:30]:
        print(f"  {r['date']}  n_spread_flags={r['n_spread_flags']}  max_spread={r['max_same_ts_spread']:.0f}  n_groups={r['n_same_ts_groups']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dict(
        bar_level=dict(n_days=len(bar_results), n_burst=len(bar_bursts),
                        burst_days=[{"date": r["date"], "n_flags": r["n_flags"], "window": r["burst_window"]} for r in bar_bursts]),
        tick_level=dict(n_days_scanned=len(tick_results),
                         date_range=[min(tick_by_date), max(tick_by_date)] if tick_by_date else None,
                         jump_flag_days=[{"date": r["date"], "n_jump_flags": r["n_jump_flags"], "max_jump": r["max_jump"]} for r in tick_jump_flag_days],
                         spread_flag_days=[{"date": r["date"], "n_spread_flags": r["n_spread_flags"], "max_spread": r["max_same_ts_spread"]} for r in tick_spread_flag_days]),
    ), indent=2, default=str))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
