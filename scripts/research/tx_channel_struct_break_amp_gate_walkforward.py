"""Causal, day-clustered walk-forward validation of the "pre-entry 30-min
amplitude predicts struct_break severity" lead.

Background (see SHARED_CONTEXT this session handed to this script — not
re-derived here): a post-hoc, full-sample analysis found weighted_amp30
(linear-recency-weighted 30-bar H-L range), evaluated AT the entry bar index
BEFORE a trade opens, has Spearman IC = -0.72 with struct_break trade pnl
(p<0.0001, but computed with a GLOBAL/full-sample threshold and pooled trades
— exactly the two mistakes this session's methodology rules warn against).
This script redoes it properly:

  1. CAUSAL rolling threshold: percentile cutoffs (p50/p75) for "is pre-entry
     amp high right now" computed ONLY from a trailing 30-50 trading day
     window of PRIOR struct_break entries' pre-entry-amp values — never the
     whole sample.
  2. Day-clustered significance: one number per trading day, t-test across
     days (n = n_days), never pooled trades treated as independent.
  3. Robustness across all 4 available holdout windows. Because julsep25 ->
     octdec25 -> janmar26 -> w83 are contiguous chronologically (2025-07-01
     .. 2026-07-31, no gaps > a few days), this script runs ONE combined
     walk-forward across all four caches in true calendar order, rather than
     four separate look-ups — this is both more principled (a single
     expanding/rolling threshold that never peeks forward) and gives more
     days of post-warmup evaluation. Per-window (per-cache) breakdowns are
     reported as a subset of the combined run. w25 = last 25 trading days of
     w83 (2026-06-26..2026-07-31, confirmed via
     reports/research/channel_lab/r_pv16_specialized_strict_reval.json
     "windows" block) — not a separate cache, sliced from w83 here.
  4. Tick-level rebuild check on w83 specifically (front-month tick cache
     available 2026-01-02..2026-08-06): rebuild day-session bars from
     load_front_month_ticks() instead of the engine's own 1m cache, recompute
     pre-entry amp + struct_break IC, see if it survives.

Engine: PAPER_RECIPE as-is (already includes CELL_TUNE_V3_PATCHES day/normal
+ day/div_hh_weak_vol full block from earlier this session) — not modified.
Pre-entry amplitude is computed ONLY from day-session bars (load_day(...,
source=X), rows with sess=='day'), matching how the original lead was scoped.
Only day-session struct_break trades are analyzed (the original finding's
scope); night session is out of scope for this pass.

Read-only research; does not touch src/ or config/. Frozen engine untouched.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from scipy import stats as sstats

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

WINDOW = 30  # weighted_amp30 lookback, matches tx_channel_amp_volume_interaction.py
DAY_START, DAY_END = "08:45", "13:45"


def _hhmm(ts: str) -> str:
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]

# chronological order — julsep25 -> octdec25 -> janmar26 -> w83 (contiguous)
CACHE_ORDER = [
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
    "tx_1m_janmar_holdout_cache.json",
    "tx_1m_fullnight_cache_full.json",
]
WINDOW_LABELS = {
    "tx_1m_julsep_holdout_cache.json": "julsep25",
    "tx_1m_octdec_holdout_cache.json": "octdec25",
    "tx_1m_janmar_holdout_cache.json": "janmar26",
    "tx_1m_fullnight_cache_full.json": "w83",
}


def weighted_amp30(ranges: np.ndarray) -> np.ndarray:
    n = len(ranges)
    w = np.arange(1, WINDOW + 1)
    wsum = w.sum()
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        out[i] = (ranges[i - WINDOW + 1 : i + 1] * w).sum() / wsum
    return out


def day_bars_from_rows(rows: list[dict]) -> pd.DataFrame:
    day_rows = [r for r in rows if r.get("sess") == "day"]
    if not day_rows:
        return pd.DataFrame()
    df = pd.DataFrame(day_rows)
    df["h"] = df["h"].astype(float)
    df["l"] = df["l"].astype(float)
    return df.reset_index(drop=True)


def arrays_from_rows(rows: list[dict], day: str):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    # full ISO date+time — bare "HH:MM" breaks causal_engine._day(ts)=str(ts)[:10],
    # which vix_session_bias relies on for date-keyed lookups at session boundaries.
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def struct_break_entries_for_day(date: str, cache_name: str, recipe, vix) -> list[dict]:
    """Run the engine for one day, return day-session struct_break trades with
    pre-entry amp attached (pre_amp=nan if not computable / insufficient warmup)."""
    rows = load_day(date, source=cache_name)
    if not rows:
        return []
    O, H, L, C, V, T = arrays_from_rows(rows, date)
    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    if not trades:
        return []
    day_bars = day_bars_from_rows(rows)
    if day_bars.empty:
        return []
    ranges = (day_bars["h"] - day_bars["l"]).to_numpy()
    amp = weighted_amp30(ranges)
    t_to_idx = {t: i for i, t in enumerate(day_bars["t"].tolist())}

    out = []
    for tr in trades:
        et_hm = _hhmm(tr["et"])
        if not (DAY_START <= et_hm <= DAY_END):
            continue  # night trade, out of scope
        why = str(tr.get("why") or "")
        if why.split("|")[0] != "struct_break":
            continue
        idx = t_to_idx.get(et_hm)
        pre_amp = amp[idx] if idx is not None else np.nan
        out.append(dict(day=date, et=et_hm, pnl=float(tr["pnl"]), pre_amp=pre_amp))
    return out


def collect_all(cache_order=CACHE_ORDER) -> pd.DataFrame:
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    rows = []
    for cache in cache_order:
        days = list_days(source=cache)
        for d in days:
            entries = struct_break_entries_for_day(d, cache, recipe, vix)
            for e in entries:
                e["window"] = WINDOW_LABELS[cache]
                rows.append(e)
    return pd.DataFrame(rows)


def day_clustered_ttest(vals: np.ndarray) -> tuple[float, float, int]:
    vals = np.asarray(vals, dtype=float)
    vals = vals[~np.isnan(vals)]
    if len(vals) < 3:
        return np.nan, np.nan, len(vals)
    t, p = sstats.ttest_1samp(vals, 0.0)
    return float(t), float(p), len(vals)


def walk_forward_bucket_test(df: pd.DataFrame, trail_days: int, pctl: float, warmup_days: int = 30):
    """For each trading day (in the combined chronological day list), the
    threshold = percentile `pctl` of pre_amp among struct_break entries from
    the trailing `trail_days` PRIOR trading days (never same-day, never
    future). Days before `warmup_days` of history are skipped entirely
    (no scoring). Returns per-day mean-pnl for the 'high pre_amp' bucket vs
    'low pre_amp' bucket (only on days where both buckets are non-empty),
    plus per-day IC (Spearman, pre_amp vs pnl) for days with >=3 entries."""
    df = df.dropna(subset=["pre_amp"]).copy()
    all_days = sorted(df["day"].unique())
    day_to_rows = {d: df[df["day"] == d] for d in all_days}

    hist_amp: list[float] = []
    hist_day: list[str] = []
    day_lo_mean, day_hi_mean, day_ic, day_n = [], [], [], []
    per_day_rows = []

    for i, d in enumerate(all_days):
        cutoff_day = all_days[i - 1] if i > 0 else None
        # trailing window of prior days' struct_break entries only
        if cutoff_day is not None:
            prior_days_seen = len(set(hist_day))
        else:
            prior_days_seen = 0
        if prior_days_seen >= warmup_days and hist_amp:
            # keep only the most recent trail_days worth of *days* of history
            recent_days = sorted(set(hist_day))[-trail_days:]
            mask = [dd in set(recent_days) for dd in hist_day]
            trail_vals = np.array(hist_amp)[np.array(mask)]
            if len(trail_vals) >= 20:
                thresh = np.percentile(trail_vals, pctl)
                today = day_to_rows[d]
                hi = today[today["pre_amp"] > thresh]
                lo = today[today["pre_amp"] <= thresh]
                if len(hi) >= 1 and len(lo) >= 1:
                    day_hi_mean.append(hi["pnl"].mean())
                    day_lo_mean.append(lo["pnl"].mean())
                if len(today) >= 3:
                    ic, _ = sstats.spearmanr(today["pre_amp"], today["pnl"])
                    if not np.isnan(ic):
                        day_ic.append(ic)
                per_day_rows.append(dict(day=d, n=len(today), thresh=thresh,
                                          n_hi=len(hi), n_lo=len(lo),
                                          hi_mean=hi["pnl"].mean() if len(hi) else np.nan,
                                          lo_mean=lo["pnl"].mean() if len(lo) else np.nan))
        # push today's entries into history AFTER scoring (causal)
        today_rows = day_to_rows[d]
        hist_amp.extend(today_rows["pre_amp"].tolist())
        hist_day.extend([d] * len(today_rows))

    return (np.array(day_lo_mean), np.array(day_hi_mean), np.array(day_ic),
            pd.DataFrame(per_day_rows))


def tick_level_check_w83(df_bar: pd.DataFrame):
    """Rebuild w83 day-session bars from load_front_month_ticks() (tick cache
    covers 2026-01-02..2026-08-06, so all 83 w83 days are in range), recompute
    pre-entry amp for the SAME struct_break trades (same et/day from the
    bar-cache run — re-simulating with tick-built bars would require
    reconstructing the whole engine input array, out of scope; this checks
    whether the amp SIGNAL itself, not the trade list, survives a tick-built
    rebuild of its only input, H-L range)."""
    w83 = df_bar[df_bar["window"] == "w83"].dropna(subset=["pre_amp"]).copy()
    days = sorted(w83["day"].unique())
    recomputed = []
    n_missing = 0
    for d in days:
        ticks = load_front_month_ticks(d)
        if ticks is None or ticks.empty:
            n_missing += 1
            continue
        ticks = ticks[(ticks["dt"].dt.strftime("%H:%M") >= DAY_START) &
                       (ticks["dt"].dt.strftime("%H:%M") <= DAY_END)]
        if ticks.empty:
            n_missing += 1
            continue
        tk = ticks.set_index("dt")
        h = tk["price"].resample("1min").max()
        l = tk["price"].resample("1min").min()
        bars = pd.DataFrame({"h": h, "l": l}).dropna()
        bars = bars.reset_index().rename(columns={"dt": "ts"})
        bars["t"] = bars["ts"].dt.strftime("%H:%M")
        ranges = (bars["h"] - bars["l"]).to_numpy()
        amp = weighted_amp30(ranges)
        t_to_idx = {t: i for i, t in enumerate(bars["t"].tolist())}
        day_trades = w83[w83["day"] == d]
        for _, tr in day_trades.iterrows():
            idx = t_to_idx.get(tr["et"])
            tick_amp = amp[idx] if idx is not None else np.nan
            recomputed.append(dict(day=d, et=tr["et"], pnl=tr["pnl"],
                                    pre_amp_bar=tr["pre_amp"], pre_amp_tick=tick_amp))
    return pd.DataFrame(recomputed), n_missing


def main():
    print("=== collecting struct_break entries across all 4 windows (combined chronological walk-forward) ===")
    df = collect_all()
    df.to_csv("/tmp/tx_struct_break_amp_gate_entries.csv", index=False)
    print(f"total day-session struct_break entries: {len(df)}  across windows: {df['window'].value_counts().to_dict()}")

    # w25 slice
    w83_days = sorted(df[df["window"] == "w83"]["day"].unique())
    w25_days = set(w83_days[-25:]) if len(w83_days) >= 25 else set(w83_days)
    df["window2"] = df.apply(lambda r: "w25" if (r["window"] == "w83" and r["day"] in w25_days) else r["window"], axis=1)

    print("\n--- naive (non-causal, full-sample) sanity replication, day-clustered ---")
    # per-day IC using the WHOLE sample as reference (for comparison only, NOT the final verdict)
    all_days = sorted(df["day"].unique())
    ics = []
    for d in all_days:
        sub = df[df["day"] == d]
        if len(sub) >= 3:
            ic, _ = sstats.spearmanr(sub["pre_amp"], sub["pnl"])
            if not np.isnan(ic):
                ics.append(ic)
    t, p, n = day_clustered_ttest(np.array(ics))
    print(f"pooled-window per-day IC (pre_amp vs struct_break pnl): mean={np.nanmean(ics):.3f} n_days={n} t={t:.2f} p={p:.4f}")

    print("\n=== CAUSAL walk-forward, day-clustered (this is the real test) ===")
    for trail_days in (30, 50):
        for pctl in (50, 75):
            lo, hi, ic, per_day = walk_forward_bucket_test(df, trail_days=trail_days, pctl=pctl, warmup_days=30)
            if len(lo) < 3:
                print(f"trail={trail_days} p{pctl}: insufficient scored days (n={len(lo)})")
                continue
            diff = lo - hi  # expect diff > 0 if low-amp (pre-entry quiet) is less bad than high-amp
            t_diff, p_diff = sstats.ttest_1samp(diff, 0.0)
            t_ic, p_ic, n_ic = day_clustered_ttest(ic)
            print(f"trail={trail_days}d p{pctl}: n_scored_days={len(lo)}  "
                  f"lo_mean={lo.mean():.1f} hi_mean={hi.mean():.1f} diff={diff.mean():.1f} "
                  f"t={t_diff:.2f} p={p_diff:.4f}  |  per-day IC: mean={np.nanmean(ic):.3f} n_days={n_ic} t={t_ic:.2f} p={p_ic:.4f}")
            per_day.to_csv(f"/tmp/tx_struct_break_amp_gate_walkforward_trail{trail_days}_p{pctl}.csv", index=False)

    print("\n=== per-window (per-season) breakdown, causal trail=30d p75 ===")
    lo, hi, ic, per_day = walk_forward_bucket_test(df, trail_days=30, pctl=75, warmup_days=30)
    per_day["window"] = per_day["day"].map(lambda d: df[df["day"] == d]["window2"].iloc[0] if (df["day"] == d).any() else None)
    for w, g in per_day.groupby("window"):
        diff = g["lo_mean"] - g["hi_mean"]
        diff = diff.dropna()
        if len(diff) >= 3:
            t_, p_ = sstats.ttest_1samp(diff, 0.0)
            print(f"  {w:10s} n_scored_days={len(diff)}  mean_diff(lo-hi)={diff.mean():.1f}  t={t_:.2f} p={p_:.4f}")
        else:
            print(f"  {w:10s} n_scored_days={len(diff)}  (too few for t-test)")

    print("\n=== tick-level rebuild sanity check (w83 only) ===")
    tick_df, n_missing = tick_level_check_w83(df)
    print(f"w83 struct_break entries with tick coverage: {len(tick_df)}  (missing/no-tick days skipped: {n_missing})")
    if len(tick_df) >= 10:
        tick_df = tick_df.dropna(subset=["pre_amp_tick"])
        corr_bar_tick, _ = sstats.spearmanr(tick_df["pre_amp_bar"], tick_df["pre_amp_tick"])
        print(f"agreement bar-vs-tick pre_amp (Spearman): {corr_bar_tick:.3f}  n={len(tick_df)}")
        by_day = tick_df.groupby("day")
        ics_tick = []
        for d, g in by_day:
            if len(g) >= 3:
                ic, _ = sstats.spearmanr(g["pre_amp_tick"], g["pnl"])
                if not np.isnan(ic):
                    ics_tick.append(ic)
        t_, p_, n_ = day_clustered_ttest(np.array(ics_tick))
        print(f"tick-rebuilt per-day IC (pre_amp_tick vs struct_break pnl): mean={np.nanmean(ics_tick):.3f} n_days={n_} t={t_:.2f} p={p_:.4f}")
        tick_df.to_csv("/tmp/tx_struct_break_amp_gate_tick_check.csv", index=False)


if __name__ == "__main__":
    main()
