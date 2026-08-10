"""Ad-hoc: does 30-min (recency-weighted) amplitude persist, and does trailing
volume trend add anything beyond the amplitude itself — esp. for the cases
where realized forward amplitude deviates from the naive "stays the same"
expectation?

Uses true tick data (FinMind tick cache) for as many day sessions as are
cached (~1 month), day-session ticks only (08:45-13:45). Day-clustered
significance (n = number of days), not pooled-minute HAC — lesson from the
regression-channel study earlier in this thread: pooled/minute-level
significance overstates confidence when all the correlation structure comes
from a handful of trading days.

Not wired into any pipeline; scratch research script.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sstats

TICK_CACHE = Path.home() / "goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day"
WINDOW = 30
HORIZONS = [1, 2, 3, 5, 8, 12, 18, 25, 35]


def ticks_to_1m_bars(ticks: list[dict], date: str) -> pd.DataFrame:
    """NOTE: the FinMind tick cache lumps every TX-labeled contract together —
    different outright months (different absolute price levels) AND calendar
    SPREAD trades (contract_date like "202606/202607", price = the spread
    differential, ~tens of points instead of ~46000) under the same
    futures_id=="TX". Mixing those in creates huge spurious price jumps.
    Must restrict to outright (no "/") ticks and pick the single most-traded
    contract month for the day (the front/active month), never all of "TX"."""
    if not ticks:
        return pd.DataFrame()
    df = pd.DataFrame(ticks)
    if "futures_id" not in df.columns or "contract_date" not in df.columns:
        return pd.DataFrame()
    df = df[df["futures_id"] == "TX"]
    df = df[~df["contract_date"].astype(str).str.contains("/")]
    df["date"] = pd.to_datetime(df["date"])
    lo = pd.Timestamp(f"{date} 08:45:00")
    hi = pd.Timestamp(f"{date} 13:45:00")
    df = df[(df["date"] >= lo) & (df["date"] <= hi)].sort_values("date")
    if df.empty:
        return df
    front_month = df["contract_date"].value_counts().idxmax()
    df = df[df["contract_date"] == front_month]
    df = df.set_index("date")
    o = df["price"].resample("1min").first()
    h = df["price"].resample("1min").max()
    l = df["price"].resample("1min").min()
    c = df["price"].resample("1min").last()
    v = df["volume"].resample("1min").sum()
    bars = pd.DataFrame({"o": o, "h": h, "l": l, "c": c, "v": v}).dropna(subset=["c"])
    bars["v"] = bars["v"].fillna(0.0)
    return bars.reset_index().rename(columns={"date": "t"})


def load_all_day_bars() -> dict[str, pd.DataFrame]:
    files = sorted(TICK_CACHE.glob("2026-*.json"))
    out = {}
    for f in files:
        date = f.stem
        if date == "2026-08-07":
            continue  # today: day session not cached yet (job runs pre-open)
        with open(f) as fh:
            ticks = json.load(fh)
        bars = ticks_to_1m_bars(ticks, date)
        if len(bars) >= WINDOW + max(HORIZONS) + 5:
            out[date] = bars
    return out


def weighted_amp30(bars: pd.DataFrame) -> np.ndarray:
    ranges = (bars["h"] - bars["l"]).to_numpy()
    n = len(bars)
    w = np.arange(1, WINDOW + 1)  # oldest -> 1 ... newest -> WINDOW
    wsum = w.sum()
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        out[i] = (ranges[i - WINDOW + 1 : i + 1] * w).sum() / wsum
    return out


def vol_trend30(bars: pd.DataFrame) -> np.ndarray:
    """OLS slope of volume vs minute index over trailing WINDOW bars, normalized
    by the window's mean volume -> relative volume trend, %/min. Positive =
    volume accelerating into "now"; negative = decelerating."""
    vol = bars["v"].to_numpy()
    n = len(bars)
    x = np.arange(WINDOW)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        seg = vol[i - WINDOW + 1 : i + 1]
        m = seg.mean()
        if m <= 0:
            continue
        s, _ = np.polyfit(x, seg, 1)
        out[i] = s / m
    return out


def forward_amp(bars: pd.DataFrame, h: int) -> np.ndarray:
    ranges = (bars["h"] - bars["l"]).to_numpy()
    n = len(bars)
    out = np.full(n, np.nan)
    for i in range(n - h):
        out[i] = ranges[i + 1 : i + 1 + h].mean()
    return out


def day_clustered(vals: np.ndarray) -> tuple[float, float]:
    vals = vals[~np.isnan(vals)]
    if len(vals) < 3:
        return np.nan, np.nan
    t, p = sstats.ttest_1samp(vals, 0.0)
    return float(t), float(p)


def main():
    bars_by_day = load_all_day_bars()
    print(f"usable day sessions: {len(bars_by_day)} -> {list(bars_by_day)}\n")

    rows = []
    per_day_detail = {h: [] for h in HORIZONS}
    for h in HORIZONS:
        ic_amp_list, ic_volchg_list, ic_surprise_volchg_list = [], [], []
        r2_amp_list, r2_combined_list = [], []
        for date, bars in bars_by_day.items():
            wamp = weighted_amp30(bars)
            vtrend = vol_trend30(bars)
            famp = forward_amp(bars, h)
            valid = ~(np.isnan(wamp) | np.isnan(vtrend) | np.isnan(famp))
            if valid.sum() < 30:
                continue
            x1, x2, y = wamp[valid], vtrend[valid], famp[valid]
            surprise = y - x1  # actual forward amp minus naive "stays the same" expectation

            ic_amp, _ = sstats.spearmanr(x1, y)
            ic_volchg, _ = sstats.spearmanr(x2, y)
            ic_surprise_volchg, _ = sstats.spearmanr(surprise, x2)

            # R^2: amp-only vs amp+volume-trend (within-day OLS, simple comparison)
            X1 = np.column_stack([np.ones_like(x1), x1])
            b1, *_ = np.linalg.lstsq(X1, y, rcond=None)
            resid1 = y - X1 @ b1
            r2_amp = 1 - resid1.var() / y.var()

            X2 = np.column_stack([np.ones_like(x1), x1, x2])
            b2, *_ = np.linalg.lstsq(X2, y, rcond=None)
            resid2 = y - X2 @ b2
            r2_combined = 1 - resid2.var() / y.var()

            ic_amp_list.append(ic_amp)
            ic_volchg_list.append(ic_volchg)
            ic_surprise_volchg_list.append(ic_surprise_volchg)
            r2_amp_list.append(r2_amp)
            r2_combined_list.append(r2_combined)
            per_day_detail[h].append((date, ic_amp, ic_volchg, ic_surprise_volchg, r2_amp, r2_combined))

        ic_amp_arr = np.array(ic_amp_list)
        ic_volchg_arr = np.array(ic_volchg_list)
        ic_surprise_arr = np.array(ic_surprise_volchg_list)
        r2_amp_arr = np.array(r2_amp_list)
        r2_comb_arr = np.array(r2_combined_list)

        t_amp, p_amp = day_clustered(ic_amp_arr)
        t_volchg, p_volchg = day_clustered(ic_volchg_arr)
        t_surprise, p_surprise = day_clustered(ic_surprise_arr)
        t_dr2, p_dr2 = day_clustered(r2_comb_arr - r2_amp_arr)

        rows.append(dict(
            h=h, n_days=len(ic_amp_arr),
            ic_amp_mean=ic_amp_arr.mean(), ic_amp_t=t_amp, ic_amp_p=p_amp,
            ic_volchg_mean=ic_volchg_arr.mean(), ic_volchg_t=t_volchg, ic_volchg_p=p_volchg,
            ic_surprise_volchg_mean=ic_surprise_arr.mean(), ic_surprise_t=t_surprise, ic_surprise_p=p_surprise,
            r2_amp_mean=r2_amp_arr.mean(), r2_combined_mean=r2_comb_arr.mean(),
            dR2_mean=(r2_comb_arr - r2_amp_arr).mean(), dR2_t=t_dr2, dR2_p=p_dr2,
        ))

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print(df.round(4).to_string(index=False))
    df.to_csv("/tmp/tx_amp_volume_interaction.csv", index=False)

    print("\n--- per-day detail @ h=12 ---")
    for row in per_day_detail[12]:
        print(f"  {row[0]}: ic_amp={row[1]:.3f} ic_volchg={row[2]:.3f} ic_surprise_volchg={row[3]:.3f} r2_amp={row[4]:.3f} r2_comb={row[5]:.3f}")


if __name__ == "__main__":
    main()
