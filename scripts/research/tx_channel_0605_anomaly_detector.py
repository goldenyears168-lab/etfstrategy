"""Item #6: 2026-06-05-style anomaly real-time detector.

設計一個causal、real-time可算的「bar/tick跳動異常」偵測器，能在事件當下（不用未來資料）
標記出像2026-06-05 04:29-04:42那種「短窗內多次300-1300pt跳動」的可疑資料，並在其餘
w83+janmar+julsep+octdec（共265天）樣本上測false positive rate。

方法：
1. 先確認異常本體在哪一層——raw front-month tick stream本身 vs 引擎bar cache。
2. 偵測器：對bar cache的close-to-close跳動，用session內trailing causal rolling MAD
   （warmup 20根bar，threshold = max(floor_pts, median + k*MAD)）逐bar判斷是否為
   outlier tick；再用「20分鐘窗內≥3根outlier bar」的burst準則才判定該天為可疑
   （避免單一真實大波動誤殺，鎖定描述中的『短窗內反覆』特徵）。
3. Day-clustered報告：對每天記錄是否觸發burst、觸發時段、觸發bar數。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

SOURCES = [
    "tx_1m_fullnight_cache_full.json",
    "tx_1m_janmar_holdout_cache.json",
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
]

WARMUP = 20
K_MAD = 8.0
FLOOR_PTS = 250.0  # tuned: pooled night-session p99.9=147pt; session-open gap jumps
                    # (15:00 night / 08:45 day, once-per-day, legitimate) are common up
                    # to ~1000pt and must be excluded separately, not raised via floor.
BURST_WINDOW_MIN = 20
BURST_MIN_COUNT = 3
SESSION_OPEN_T = {"night": "15:00", "day": "08:45"}


def bar_time_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def session_jumps(bars: list[dict], sess: str) -> tuple[list[str], list[float]]:
    seq = sorted((b for b in bars if b["sess"] == sess), key=lambda b: b["t"])
    closes = [b["c"] for b in seq]
    times = [b["t"] for b in seq]
    jumps = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    return times, jumps


def detect_day(bars: list[dict], prior_jumps: dict[str, list[float]]) -> dict:
    """Cross-day frozen baseline detector: threshold per session is built ONLY from the
    trailing N prior trading days' close-to-close jump pool (frozen for the whole
    session), NOT from the current day's own trailing bars — avoids the self-masking
    failure mode where an ongoing ramp/burst inflates its own adaptive threshold.
    The single once-per-day session-open bar (15:00 night / 08:45 day) is excluded from
    flagging: it is a legitimate recurring gap (day-close -> night-open, or
    night-close -> day-open), not a data-quality anomaly, and pools up to ~1000pt on
    ordinary days -- including it in the outlier statistic would swamp the true signal
    (multi-jump clustering away from session open)."""
    flags = []  # (t_min, sess, jump, thresh)
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


def tick_level_check(date: str) -> dict | None:
    """Raw front-month tick-to-tick jump check for the same causal-MAD/burst logic."""
    ticks = load_front_month_ticks(date)
    if ticks is None or ticks.empty:
        return None
    prices = ticks["price"].to_numpy()
    dts = ticks["dt"].tolist()
    jumps = np.abs(np.diff(prices))
    flags = []
    for i in range(1, len(jumps)):
        hist = jumps[max(0, i - 200):i]
        if len(hist) < 200:
            continue
        med = np.median(hist)
        mad = np.median(np.abs(hist - med)) * 1.4826 + 1e-9
        thresh = max(20.0, med + K_MAD * mad)
        if jumps[i] > thresh:
            flags.append((dts[i + 1], float(jumps[i]), float(thresh)))
    return dict(n_flags=len(flags), max_jump=float(jumps.max()) if len(jumps) else 0.0,
                p999_jump=float(np.percentile(jumps, 99.9)) if len(jumps) else 0.0, flags=flags[:10])


def main() -> None:
    print("=== Step 1: raw tick-level check on 2026-06-05 (前139+清潔天對照) ===")
    tk = tick_level_check("2026-06-05")
    print("2026-06-05 tick-jump detector:", {k: v for k, v in tk.items() if k != "flags"})
    print("first flags:", tk["flags"][:5])

    print("\n=== Step 2: bar-cache-level causal burst detector, full sample ===")
    all_days = []
    for src in SOURCES:
        for d in list_days(source=src):
            all_days.append((d, src))
    # de-dup by date (keep first source)
    seen = {}
    for d, src in all_days:
        seen.setdefault(d, src)
    all_days = sorted(seen.items())
    print(f"total unique days across 4 sources: {len(all_days)}")

    BASELINE_DAYS = 20
    day_cache = {}
    for date, src in all_days:
        bars = load_day(date, source=src)
        if bars:
            day_cache[date] = bars

    ordered_dates = list(day_cache.keys())  # already sorted by construction
    results = []
    for idx, date in enumerate(ordered_dates):
        bars = day_cache[date]
        prior_dates = ordered_dates[max(0, idx - BASELINE_DAYS):idx]
        prior_jumps = {"night": [], "day": []}
        for pd_ in prior_dates:
            for sess in ("night", "day"):
                _, j = session_jumps(day_cache[pd_], sess)
                prior_jumps[sess].extend(j)
        r = detect_day(bars, prior_jumps)
        r["date"] = date
        r["source"] = seen[date]
        results.append(r)

    bursts = [r for r in results if r["burst"]]
    print(f"\nn_days_tested={len(results)}  n_burst_flagged={len(bursts)}  "
          f"FP-rate(among non-0605)={sum(1 for r in bursts if r['date'] != '2026-06-05') / max(1, len(results) - 1):.4f}")

    print("\nDays flagged with burst (date, source, n_flags, burst_window[min-of-session]):")
    for r in sorted(bursts, key=lambda x: -x["n_flags"]):
        print(f"  {r['date']}  {r['source']}  n_flags={r['n_flags']}  window={r['burst_window']}")

    target = next((r for r in results if r["date"] == "2026-06-05"), None)
    print("\n2026-06-05 detail:")
    if target:
        print(f"  n_flags={target['n_flags']}  burst={target['burst']}  window={target['burst_window']}")
        for t, sess, jump, thresh in target.get("flags", [])[:15]:
            print(f"    {t} {sess} jump={jump:.0f} thresh={thresh:.0f}")


if __name__ == "__main__":
    main()
