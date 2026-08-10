"""Item #9: does an amp+volume forecast-model UNCERTAINTY score (distance from
the trailing training distribution) help gate/widen-stop struct_break trades?

Reconstructs the round-10 walk-forward amp+volume forecast model
(tx_channel_amp_vol_combined_forecast.py: forecast = OLS(wamp30, vlvl30) ->
forward_amp(h), 30-trading-day rolling refit, 30-day warmup) and builds a
per-bar uncertainty score from it:

  uncertainty(t) = Mahalanobis distance of (wamp30[t], vlvl30[t]) from the
  MEAN/COV of (wamp30, vlvl30) over the trailing 30 trading days used to fit
  the model that day -- i.e. "how far is right-now from anything the model
  was trained on". Fully causal (only past days' bars).

Step 1 (sanity check the score is doing its job at all): does higher
Mahalanobis distance predict larger |forecast residual| out of sample?
Day-clustered Spearman IC, per methodology rules.

Step 2 (the actual ask): apply the SAME uncertainty score, computed at
struct_break entry bars, as a causal real-time filter -- bucket struct_break
trades into high/low uncertainty using a trailing rolling percentile
threshold (never global), day-clustered bucket-mean-pnl test + per-day IC,
matching tx_channel_struct_break_amp_gate_walkforward.py's design (that
script found amplitude LEVEL alone is a dead end as a blanket entry filter;
this tests whether DISTRIBUTIONAL DISTANCE, a different quantity, does any
better).

Uses cache-based day-session bars (tmf_channel.cache_store) across all 4
chronologically contiguous windows (julsep25 -> octdec25 -> janmar26 -> w83),
same combined walk-forward as the existing struct_break amp-gate script.
PAPER_RECIPE as-is, not modified. Read-only research.
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

WINDOW = 30  # weighted_amp30 / vol_level30 lookback
DAY_START, DAY_END = "08:45", "13:45"
WARMUP_DAYS = 30
ROLL_WINDOW = 30  # trailing trading days for the walk-forward fit + Mahalanobis ref dist
FORECAST_H = 35  # minutes; representative mid horizon from round 10

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


def vol_level30(vol: np.ndarray) -> np.ndarray:
    n = len(vol)
    out = np.full(n, np.nan)
    for i in range(WINDOW - 1, n):
        out[i] = vol[i - WINDOW + 1 : i + 1].mean()
    return out


def forward_amp(ranges: np.ndarray, h: int) -> np.ndarray:
    n = len(ranges)
    out = np.full(n, np.nan)
    for i in range(n - h):
        out[i] = ranges[i + 1 : i + 1 + h].mean()
    return out


def day_bars_from_rows(rows: list[dict]) -> pd.DataFrame:
    day_rows = [r for r in rows if r.get("sess") == "day"]
    if not day_rows:
        return pd.DataFrame()
    df = pd.DataFrame(day_rows)
    df["h"] = df["h"].astype(float)
    df["l"] = df["l"].astype(float)
    df["v"] = df["v"].astype(float) if "v" in df.columns else 0.0
    return df.reset_index(drop=True)


def arrays_from_rows(rows: list[dict], day: str):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def _hhmm(ts: str) -> str:
    """Extract bare HH:MM from either a bare 'HH:MM' or a full ISO timestamp
    (e.g. '2025-07-01T08:45:00.000+08:00' -> '08:45')."""
    s = str(ts or "")
    return s.split("T", 1)[1][:5] if "T" in s else s[:5]


def build_bar_series() -> dict[str, dict]:
    """One entry per calendar day (across all 4 windows, chronological),
    holding day-session wamp/vlvl/forward_amp arrays + window label."""
    series = {}
    order_days = []
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            rows = load_day(d, source=cache)
            bars = day_bars_from_rows(rows)
            if bars.empty or len(bars) < WINDOW + FORECAST_H + 5:
                continue
            ranges = (bars["h"] - bars["l"]).to_numpy()
            vol = bars["v"].to_numpy()
            wamp = weighted_amp30(ranges)
            vlvl = vol_level30(vol)
            famp = forward_amp(ranges, FORECAST_H)
            series[d] = dict(bars=bars, wamp=wamp, vlvl=vlvl, famp=famp,
                              window=WINDOW_LABELS[cache])
            order_days.append(d)
    return series


def day_clustered_ttest(vals) -> tuple[float, float, int]:
    vals = np.asarray(vals, dtype=float)
    vals = vals[~np.isnan(vals)]
    if len(vals) < 3:
        return np.nan, np.nan, len(vals)
    t, p = sstats.ttest_1samp(vals, 0.0)
    return float(t), float(p), len(vals)


def struct_break_entries_for_day(date: str, cache_name: str, recipe, vix) -> list[dict]:
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
    t_to_idx = {t: i for i, t in enumerate(day_bars["t"].tolist())}
    out = []
    for tr in trades:
        hm = _hhmm(tr["et"])
        if not (DAY_START <= hm <= DAY_END):
            continue
        why = str(tr.get("why") or "")
        if why.split("|")[0] != "struct_break":
            continue
        idx = t_to_idx.get(hm)
        out.append(dict(day=date, et=hm, pnl=float(tr["pnl"]), idx=idx))
    return out


def main():
    print("=== building combined chronological bar series (4 windows) ===")
    series = build_bar_series()
    dates = sorted(series)
    print(f"n days with usable bar series: {len(dates)}")

    # ---------------- Step 1: does the uncertainty score track residual error? ----------------
    print("\n=== STEP 1: walk-forward forecast model + Mahalanobis uncertainty sanity check ===")
    per_day_ic = []
    per_day_mae = {"naive": [], "combined": []}
    for i, date in enumerate(dates):
        if i < WARMUP_DAYS:
            continue
        prior = dates[max(0, i - ROLL_WINDOW): i]
        if len(prior) < 15:
            continue
        Xtr, Ytr = [], []
        pooled_wamp, pooled_vlvl = [], []
        for p in prior:
            s = series[p]
            valid = ~(np.isnan(s["wamp"]) | np.isnan(s["vlvl"]) | np.isnan(s["famp"]))
            if valid.sum() == 0:
                continue
            Xtr.append(np.column_stack([np.ones(valid.sum()), s["wamp"][valid], s["vlvl"][valid]]))
            Ytr.append(s["famp"][valid])
            pooled_wamp.append(s["wamp"][valid])
            pooled_vlvl.append(s["vlvl"][valid])
        if not Xtr:
            continue
        Xtr = np.vstack(Xtr)
        Ytr = np.concatenate(Ytr)
        beta, *_ = np.linalg.lstsq(Xtr, Ytr, rcond=None)

        pooled_wamp = np.concatenate(pooled_wamp)
        pooled_vlvl = np.concatenate(pooled_vlvl)
        mu = np.array([pooled_wamp.mean(), pooled_vlvl.mean()])
        cov = np.cov(np.column_stack([pooled_wamp, pooled_vlvl]).T)
        try:
            cov_inv = np.linalg.inv(cov + np.eye(2) * 1e-6)
        except np.linalg.LinAlgError:
            continue

        s = series[date]
        valid = ~(np.isnan(s["wamp"]) | np.isnan(s["vlvl"]) | np.isnan(s["famp"]))
        if valid.sum() < 20:
            continue
        a, v, act = s["wamp"][valid], s["vlvl"][valid], s["famp"][valid]
        f_naive = a
        f_comb = beta[0] + beta[1] * a + beta[2] * v
        resid = np.abs(act - f_comb)

        diffs = np.column_stack([a - mu[0], v - mu[1]])
        maha = np.sqrt(np.einsum("ij,jk,ik->i", diffs, cov_inv, diffs))

        per_day_mae["naive"].append(np.abs(act - f_naive).mean())
        per_day_mae["combined"].append(np.abs(act - f_comb).mean())
        if len(maha) >= 10:
            ic, _ = sstats.spearmanr(maha, resid)
            if not np.isnan(ic):
                per_day_ic.append(ic)

    t_ic, p_ic, n_ic = day_clustered_ttest(np.array(per_day_ic))
    print(f"n scored days: {n_ic}")
    print(f"combined-model MAE vs naive-amp MAE: naive={np.mean(per_day_mae['naive']):.2f}  "
          f"combined={np.mean(per_day_mae['combined']):.2f}  (h={FORECAST_H})")
    print(f"day-clustered IC(Mahalanobis dist, |forecast residual|): mean={np.nanmean(per_day_ic):.3f} "
          f"n_days={n_ic} t={t_ic:.2f} p={p_ic:.4f}")

    # ---------------- Step 2: apply score to struct_break trades ----------------
    print("\n=== STEP 2: uncertainty score at struct_break entries (causal walk-forward bucket test) ===")
    vix = load_vixtwn_delta() or {}
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    rows = []
    for cache in CACHE_ORDER:
        for d in list_days(source=cache):
            if d not in series:
                continue
            entries = struct_break_entries_for_day(d, cache, recipe, vix)
            for e in entries:
                e["window"] = WINDOW_LABELS[cache]
                rows.append(e)
    df = pd.DataFrame(rows)
    print(f"total day-session struct_break entries (days with usable bar series): {len(df)}")

    # attach causal Mahalanobis uncertainty score computed from trailing ROLL_WINDOW days
    # (recompute mu/cov per day exactly as step 1, but only score entries -- cheaper to
    # cache per-day mu/cov/beta once)
    day_mu_cov = {}
    for i, date in enumerate(dates):
        if i < WARMUP_DAYS:
            continue
        prior = dates[max(0, i - ROLL_WINDOW): i]
        if len(prior) < 15:
            continue
        pooled_wamp, pooled_vlvl = [], []
        for p in prior:
            s = series[p]
            valid = ~(np.isnan(s["wamp"]) | np.isnan(s["vlvl"]))
            pooled_wamp.append(s["wamp"][valid])
            pooled_vlvl.append(s["vlvl"][valid])
        pooled_wamp = np.concatenate(pooled_wamp)
        pooled_vlvl = np.concatenate(pooled_vlvl)
        if len(pooled_wamp) < 50:
            continue
        mu = np.array([pooled_wamp.mean(), pooled_vlvl.mean()])
        cov = np.cov(np.column_stack([pooled_wamp, pooled_vlvl]).T)
        try:
            cov_inv = np.linalg.inv(cov + np.eye(2) * 1e-6)
        except np.linalg.LinAlgError:
            continue
        day_mu_cov[date] = (mu, cov_inv)

    def maha_for(date, idx):
        if date not in day_mu_cov or idx is None:
            return np.nan
        s = series[date]
        if idx >= len(s["wamp"]) or np.isnan(s["wamp"][idx]) or np.isnan(s["vlvl"][idx]):
            return np.nan
        mu, cov_inv = day_mu_cov[date]
        diff = np.array([s["wamp"][idx] - mu[0], s["vlvl"][idx] - mu[1]])
        return float(np.sqrt(diff @ cov_inv @ diff))

    df["uncertainty"] = df.apply(lambda r: maha_for(r["day"], r["idx"]), axis=1)
    df = df.dropna(subset=["uncertainty"])
    print(f"struct_break entries with scorable uncertainty (post-warmup days): {len(df)}")
    df.to_csv("/tmp/tx_struct_break_uncertainty_entries.csv", index=False)

    all_days = sorted(df["day"].unique())
    day_to_rows = {d: df[df["day"] == d] for d in all_days}

    for trail_days, pctl in [(30, 50), (30, 75), (50, 75)]:
        hist_unc, hist_day = [], []
        lo_means, hi_means, day_ics = [], [], []
        for i, d in enumerate(all_days):
            prior_days_seen = len(set(hist_day))
            if prior_days_seen >= 20 and hist_unc:
                recent_days = sorted(set(hist_day))[-trail_days:]
                mask = np.array([dd in set(recent_days) for dd in hist_day])
                trail_vals = np.array(hist_unc)[mask]
                if len(trail_vals) >= 15:
                    thresh = np.percentile(trail_vals, pctl)
                    today = day_to_rows[d]
                    hi = today[today["uncertainty"] > thresh]
                    lo = today[today["uncertainty"] <= thresh]
                    if len(hi) >= 1 and len(lo) >= 1:
                        lo_means.append(lo["pnl"].mean())
                        hi_means.append(hi["pnl"].mean())
                    if len(today) >= 3:
                        ic, _ = sstats.spearmanr(today["uncertainty"], today["pnl"])
                        if not np.isnan(ic):
                            day_ics.append(ic)
            today_rows = day_to_rows[d]
            hist_unc.extend(today_rows["uncertainty"].tolist())
            hist_day.extend([d] * len(today_rows))

        lo_means, hi_means = np.array(lo_means), np.array(hi_means)
        if len(lo_means) < 3:
            print(f"trail={trail_days}d p{pctl}: insufficient scored days (n={len(lo_means)})")
            continue
        diff = lo_means - hi_means  # expect >0 if low-uncertainty bucket less bad
        t_diff, p_diff = sstats.ttest_1samp(diff, 0.0)
        t_ic2, p_ic2, n_ic2 = day_clustered_ttest(np.array(day_ics))
        print(f"trail={trail_days}d p{pctl}: n_scored_days={len(lo_means)}  "
              f"lo_unc_mean_pnl={lo_means.mean():.1f} hi_unc_mean_pnl={hi_means.mean():.1f} "
              f"diff={diff.mean():.1f} t={t_diff:.2f} p={p_diff:.4f}  |  "
              f"per-day IC(uncertainty,pnl): mean={np.nanmean(day_ics):.3f} n_days={n_ic2} t={t_ic2:.2f} p={p_ic2:.4f}")

    # naive (non-causal, full-sample) sanity replication for reference only
    print("\n--- naive (non-causal, full-sample) sanity replication, day-clustered, for reference only ---")
    ics = []
    for d in all_days:
        sub = df[df["day"] == d]
        if len(sub) >= 3:
            ic, _ = sstats.spearmanr(sub["uncertainty"], sub["pnl"])
            if not np.isnan(ic):
                ics.append(ic)
    t, p, n = day_clustered_ttest(np.array(ics))
    print(f"pooled-window per-day IC (uncertainty vs struct_break pnl): mean={np.nanmean(ics):.3f} n_days={n} t={t:.2f} p={p:.4f}")


if __name__ == "__main__":
    main()
