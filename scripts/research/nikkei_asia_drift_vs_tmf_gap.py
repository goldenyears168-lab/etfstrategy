"""One-off research script (observe-only): fetch historical Nikkei 225 data via
yfinance, cache it offline, and test correlation between Asia-morning drift
(08:00-08:45 Taipei, the one genuinely new info window before TMF's 08:45 day
open) and the TMF day_open_gap series in
reports/research/channel_lab/session_transition_gap_lab.json.

Two proxy definitions are used depending on data availability, and NEVER
silently mixed in a single correlation number:

  - intraday_true: replicates src/morning_gate_brief.py._asia_early_drift()
    exactly — 30m bars, drift = close(<=08:30 TW)/open(08:00 TW) - 1, in %.
    Only available where yfinance still has 30m history (~last 60 days from
    whenever this script is run).
  - daily_fallback: whole Nikkei session open->close return (%) for the same
    calendar date, from DAILY OHLC (available for the full window). This is
    a coarser, different-window proxy (09:00-15:00 JST, not 08:00-08:45 TW)
    and is labeled separately.

Outputs:
  reports/research/channel_lab/nikkei_historical_cache.json  (raw+derived cache)
  stdout: correlation report (separate r for each subset + a flagged mixed pool)
"""
from __future__ import annotations

import json
import statistics
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
GAP_LAB_PATH = REPO_ROOT / "reports/research/channel_lab/session_transition_gap_lab.json"
CACHE_PATH = REPO_ROOT / "reports/research/channel_lab/nikkei_historical_cache.json"

TW = ZoneInfo("Asia/Taipei")
START = "2026-04-01"
END = "2026-08-02"  # exclusive-ish upper bound covering through 2026-07-31 session


def fetch_intraday_true() -> dict[str, float]:
    """30m bars -> _asia_early_drift()-equivalent drift, keyed by TW calendar date str."""
    t = yf.Ticker("^N225")
    h = t.history(period="60d", interval="30m")
    if h.empty:
        return {}
    h.index = pd.to_datetime(h.index).tz_convert(TW)
    out: dict[str, float] = {}
    for d, sub in h.groupby(h.index.date):
        sub = sub.sort_index()
        times = sub.index.strftime("%H:%M")
        opn = float(sub["Open"].iloc[0])
        pre = sub["Close"][times <= "08:30"]
        if pre.empty or not opn:
            continue
        drift = (float(pre.iloc[-1]) / opn - 1) * 100
        out[str(d)] = drift
    return out


def fetch_daily_fallback() -> dict[str, float]:
    """Daily OHLC -> whole-session open->close %, keyed by calendar date str."""
    t = yf.Ticker("^N225")
    h = t.history(start=START, end=END, interval="1d")
    if h.empty:
        return {}
    out: dict[str, float] = {}
    for ts, row in h.iterrows():
        d = str(ts.date())
        opn, cls = float(row["Open"]), float(row["Close"])
        if opn:
            out[d] = (cls / opn - 1) * 100
    return out


def pearson(xs: list[float], ys: list[float]) -> tuple[float | None, int]:
    n = len(xs)
    if n < 3:
        return None, n
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None, n
    r = cov / (vx**0.5 * vy**0.5)
    return r, n


def main() -> None:
    gap_lab = json.loads(GAP_LAB_PATH.read_text())
    day_open_gap_detail = gap_lab["day_open_gap_detail"]
    gap_by_date = {row["cur_session"]: row["gap"] for row in day_open_gap_detail}

    intraday_true = fetch_intraday_true()
    daily_fallback = fetch_daily_fallback()

    cache = {
        "fetched_at": pd.Timestamp.now(tz=TW).isoformat(),
        "symbol": "^N225",
        "window": {"start": START, "end": END},
        "intraday_true": {
            "method": "30m bars; drift = close(<=08:30 TW)/open(08:00 TW) - 1, in % "
                      "(replicates src/morning_gate_brief.py._asia_early_drift() exactly)",
            "n": len(intraday_true),
            "date_range": [min(intraday_true), max(intraday_true)] if intraday_true else None,
            "values": intraday_true,
        },
        "daily_fallback": {
            "method": "daily OHLC; proxy = close/open - 1, in % (whole 09:00-15:00 JST "
                      "session return, NOT the 08:00-08:45 TW morning-drift window — "
                      "coarser & different-window proxy)",
            "n": len(daily_fallback),
            "date_range": [min(daily_fallback), max(daily_fallback)] if daily_fallback else None,
            "values": daily_fallback,
        },
    }
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=False))

    # Align by cur_session date. Prefer intraday_true where available for a date;
    # otherwise use daily_fallback. Track which subset each pair belongs to.
    true_pairs, fallback_pairs, mixed_pairs = [], [], []
    for d, gap in gap_by_date.items():
        if d in intraday_true:
            true_pairs.append((intraday_true[d], gap, d))
            mixed_pairs.append((intraday_true[d], gap, d, "intraday_true"))
        elif d in daily_fallback:
            fallback_pairs.append((daily_fallback[d], gap, d))
            mixed_pairs.append((daily_fallback[d], gap, d, "daily_fallback"))

    r_true, n_true = pearson([p[0] for p in true_pairs], [p[1] for p in true_pairs])
    r_fb, n_fb = pearson([p[0] for p in fallback_pairs], [p[1] for p in fallback_pairs])
    r_mixed, n_mixed = pearson([p[0] for p in mixed_pairs], [p[1] for p in mixed_pairs])

    report = {
        "gap_lab_n": len(gap_by_date),
        "intraday_true_subset": {
            "n": n_true,
            "r": round(r_true, 4) if r_true is not None else None,
            "date_range": [min(p[2] for p in true_pairs), max(p[2] for p in true_pairs)] if true_pairs else None,
        },
        "daily_fallback_subset": {
            "n": n_fb,
            "r": round(r_fb, 4) if r_fb is not None else None,
            "date_range": [min(p[2] for p in fallback_pairs), max(p[2] for p in fallback_pairs)] if fallback_pairs else None,
        },
        "mixed_pooled_ALL_DATES_flagged_not_apples_to_apples": {
            "n": n_mixed,
            "r": round(r_mixed, 4) if r_mixed is not None else None,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\ncache written: {CACHE_PATH}")


if __name__ == "__main__":
    main()
