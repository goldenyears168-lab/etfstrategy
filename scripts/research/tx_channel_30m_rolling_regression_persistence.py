"""Ad-hoc 2026-08-07 diagnostic: rolling 30-min tick/bar regression channel for TX/TMF.

Two parts:
1. Today (8/7) live day-session snapshot — pulled from the already-running
   tmf-sim-server HTTP API (read-only, avoids opening a second Fubon session
   alongside the live order worker).
2. Persistence study — how many minutes does the rolling 30-min regression
   channel (center line + avg amplitude) stay "valid" (no big jump) before a
   meaningful shift — using true tick data for the last 6 day sessions from
   the FinMind tick cache.

Not wired into any pipeline; scratch research script.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

TICK_CACHE = Path.home() / "goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day"
SIM_SERVER = "http://localhost:8770/api/state"
WINDOW = 30  # minutes


def load_today_day_bars() -> pd.DataFrame:
    with urllib.request.urlopen(SIM_SERVER, timeout=5) as resp:
        state = json.load(resp)
    bars = pd.DataFrame(state["bars"])
    bars["t"] = pd.to_datetime(bars["t"])
    day = bars[bars["sess"] == "day"].reset_index(drop=True)
    meta = {
        "symbol": state["symbol"],
        "name": state["name"],
        "trading_day": state["trading_day"],
        "asof": state["asof"],
        "n_day": state["n_day"],
        "n_night": state["n_night"],
    }
    return day, meta


def ticks_to_1m_bars(ticks: list[dict], date: str) -> pd.DataFrame:
    df = pd.DataFrame(ticks)
    df = df[df["futures_id"] == "TX"]
    df["date"] = pd.to_datetime(df["date"])
    lo = pd.Timestamp(f"{date} 08:45:00")
    hi = pd.Timestamp(f"{date} 13:45:00")
    df = df[(df["date"] >= lo) & (df["date"] <= hi)].sort_values("date")
    if df.empty:
        return df
    df = df.set_index("date")
    o = df["price"].resample("1min").first()
    h = df["price"].resample("1min").max()
    l = df["price"].resample("1min").min()
    c = df["price"].resample("1min").last()
    v = df["volume"].resample("1min").sum()
    bars = pd.DataFrame({"o": o, "h": h, "l": l, "c": c, "v": v}).dropna(subset=["c"])
    bars = bars.reset_index().rename(columns={"date": "t"})
    return bars


def rolling_channel(bars: pd.DataFrame) -> pd.DataFrame:
    """OLS regression of close vs bar index + mean(H-L) amplitude, trailing WINDOW bars."""
    closes = bars["c"].to_numpy()
    ranges = (bars["h"] - bars["l"]).to_numpy()
    n = len(bars)
    slope = np.full(n, np.nan)
    center = np.full(n, np.nan)
    amp = np.full(n, np.nan)
    x = np.arange(WINDOW)
    for i in range(WINDOW - 1, n):
        y = closes[i - WINDOW + 1 : i + 1]
        s, b = np.polyfit(x, y, 1)
        slope[i] = s
        center[i] = s * (WINDOW - 1) + b
        amp[i] = ranges[i - WINDOW + 1 : i + 1].mean()
    out = bars.copy()
    out["slope_pt_per_min"] = slope
    out["reg_center"] = center
    out["avg_amp30"] = amp
    out["upper"] = out["reg_center"] + out["avg_amp30"] / 2
    out["lower"] = out["reg_center"] - out["avg_amp30"] / 2
    return out


def persistence_stats(ch: pd.DataFrame, tol_frac: float = 0.15) -> dict:
    """Run-length (minutes) between 'significant' center-line jumps.

    Significant = |Δcenter| > tol_frac * avg_amp30 (channel moved by more
    than tol_frac of its own width in a single minute).
    """
    d = ch.dropna(subset=["reg_center", "avg_amp30"]).reset_index(drop=True)
    if len(d) < 3:
        return {"n_runs": 0}
    dc = d["reg_center"].diff().abs()
    thresh = tol_frac * d["avg_amp30"]
    breaks = (dc > thresh).to_numpy()
    breaks[: 1] = False
    run_lengths = []
    run = 1
    for b in breaks[1:]:
        if b:
            run_lengths.append(run)
            run = 1
        else:
            run += 1
    run_lengths.append(run)
    slope_sign = np.sign(d["slope_pt_per_min"].to_numpy())
    sign_flip_rate = float((np.diff(slope_sign) != 0).mean())
    return {
        "n_runs": len(run_lengths),
        "median_run_min": float(np.median(run_lengths)),
        "mean_run_min": float(np.mean(run_lengths)),
        "p25_run_min": float(np.percentile(run_lengths, 25)),
        "p75_run_min": float(np.percentile(run_lengths, 75)),
        "slope_sign_flip_rate_per_min": sign_flip_rate,
        "run_lengths": run_lengths,
    }


def predictive_decay(bars_by_day: dict[str, pd.DataFrame], horizons: list[int], cost_pts: float = 2.0) -> pd.DataFrame:
    """Out-of-sample predictive decay of the 30-min channel, per forward horizon h (minutes).

    Two hypotheses tested against forward return c[t+h]-c[t] (never using
    future data to fit the channel itself — channel at t only uses t-29..t):
      trend:     bet direction = sign(slope[t])            (channel says "keep going")
      reversion: bet direction = sign(center[t] - c[t])     (price pulls back to center)
    Reports pooled IC (Spearman), hit rate, mean net pts after cost_pts round-trip,
    and a HAC(maxlags=h)-adjusted t-stat on the net P&L (observations h minutes
    apart overlap by construction, so lag length is tied to the horizon).
    """
    import statsmodels.api as sm
    from scipy import stats as sstats

    rows = []
    for h in horizons:
        trend_ic, rev_ic = [], []
        trend_pts, rev_pts = [], []
        for date, bars in bars_by_day.items():
            ch = rolling_channel(bars)
            n = len(ch)
            valid = ch.dropna(subset=["reg_center", "slope_pt_per_min", "avg_amp30"]).index
            for i in valid:
                if i + h >= n:
                    continue
                c0 = ch["c"].iat[i]
                fwd = ch["c"].iat[i + h] - c0
                slope = ch["slope_pt_per_min"].iat[i]
                center = ch["reg_center"].iat[i]
                trend_dir = np.sign(slope)
                rev_dir = np.sign(center - c0)
                trend_ic.append((slope, fwd))
                rev_ic.append((center - c0, fwd))
                if trend_dir != 0:
                    trend_pts.append(trend_dir * fwd - cost_pts)
                if rev_dir != 0:
                    rev_pts.append(rev_dir * fwd - cost_pts)

        def _summ(pairs, pts):
            if len(pairs) < 20:
                return dict(n=len(pairs), ic=np.nan, hit=np.nan, mean_net=np.nan, hac_t=np.nan)
            x = np.array([p[0] for p in pairs])
            y = np.array([p[1] for p in pairs])
            ic, _ = sstats.spearmanr(x, y)
            pts = np.array(pts)
            hit = float((pts + cost_pts > 0).mean()) if len(pts) else np.nan
            mean_net = float(pts.mean()) if len(pts) else np.nan
            hac_t = np.nan
            if len(pts) > 30:
                m = sm.OLS(pts, np.ones((len(pts), 1))).fit(cov_type="HAC", cov_kwds={"maxlags": max(h, 1)})
                hac_t = float(m.tvalues[0])
            return dict(n=len(pairs), ic=ic, hit=hit, mean_net=mean_net, hac_t=hac_t)

        t_s = _summ(trend_ic, trend_pts)
        r_s = _summ(rev_ic, rev_pts)
        rows.append(dict(
            horizon_min=h,
            trend_n=t_s["n"], trend_ic=t_s["ic"], trend_hit=t_s["hit"], trend_net_pts=t_s["mean_net"], trend_hac_t=t_s["hac_t"],
            rev_n=r_s["n"], rev_ic=r_s["ic"], rev_hit=r_s["hit"], rev_net_pts=r_s["mean_net"], rev_hac_t=r_s["hac_t"],
        ))
    return pd.DataFrame(rows)


def main():
    print("=== Part 1: today (live, 1-min bars via tmf-sim-server) ===")
    day, meta = load_today_day_bars()
    print(meta)
    ch_today = rolling_channel(day)
    latest = ch_today.iloc[-1]
    print("latest bar:", latest[["t", "c", "slope_pt_per_min", "reg_center", "avg_amp30", "upper", "lower"]].to_dict())
    ch_today.to_csv("/tmp/tx_today_rolling_channel.csv", index=False)

    print("\n=== Part 2: persistence study on last 6 day sessions (true tick data) ===")
    files = sorted(TICK_CACHE.glob("2026-*.json"))
    recent = [f for f in files if f.stem not in ("2026-08-07",)][-6:]
    all_stats = []
    for f in recent:
        date = f.stem
        with open(f) as fh:
            ticks = json.load(fh)
        bars = ticks_to_1m_bars(ticks, date)
        if len(bars) < WINDOW + 10:
            print(date, "insufficient day-session bars:", len(bars))
            continue
        ch = rolling_channel(bars)
        stats = persistence_stats(ch)
        stats["date"] = date
        stats["n_bars"] = len(bars)
        all_stats.append(stats)
        print(date, stats)

    pooled_runs = [r for s in all_stats for r in s["run_lengths"]]
    df_stats = pd.DataFrame([{k: v for k, v in s.items() if k != "run_lengths"} for s in all_stats])
    df_stats.to_csv("/tmp/tx_channel_persistence_by_day.csv", index=False)
    print("\n--- pooled across days (per-day summary) ---")
    print("median run (min):", df_stats["median_run_min"].median())
    print("mean run (min):", df_stats["mean_run_min"].mean())
    print("mean slope sign flip rate/min:", df_stats["slope_sign_flip_rate_per_min"].mean())
    print(f"\n--- pooled RAW run-length distribution (n={len(pooled_runs)} runs, all days flattened) ---")
    arr = np.array(pooled_runs)
    for p in (10, 25, 50, 75, 90):
        print(f"p{p}:", np.percentile(arr, p), "min")
    print("mean:", arr.mean(), "min")

    print("\n=== Part 3: predictive decay (OOS forward-return IC by horizon) ===")
    bars_by_day = {}
    for f in recent:
        date = f.stem
        with open(f) as fh:
            ticks = json.load(fh)
        bars = ticks_to_1m_bars(ticks, date)
        if len(bars) >= WINDOW + 10:
            bars_by_day[date] = bars
    decay = predictive_decay(bars_by_day, horizons=[1, 2, 3, 5, 8, 12, 18, 25, 35])
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(decay.round(4).to_string(index=False))
    decay.to_csv("/tmp/tx_channel_predictive_decay.csv", index=False)


if __name__ == "__main__":
    main()
