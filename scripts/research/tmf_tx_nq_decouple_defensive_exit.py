#!/usr/bin/env python3
"""NEW mechanism (2026-08-12): TX-NQ decoupling as a DEFENSIVE EXIT trigger
for an OPEN position -- not an entry-side mean-reversion signal.

Context (read docs/tmf-channel-research-handoff-20260811.md and
config/research.yaml topic tmf-tw-us-ma5h-deviation-spread FIRST): the SAME
tw_dev/us_dev/spread construction, used as an ENTRY-side gate, has been
researched extensively and found weak-to-collapsed after the cache_store
bar-order/timestamp defect fixes (IS all non-significant, only OOS 15/30min
IC survives at reduced magnitude; all three "turn it into a trading rule"
attempts -- replace-gate / intersect-gate / 2h-window -- failed to clear the
IS p<0.20 screening line). Despite that, WINDOW_MIN=100/THRESHOLD=0.1 from
src/tmf_channel/nq_1m_spread_gate.py is now the LIVE entry-side
session_side_gate (v1.6.0, commit 108e61d), user-authorized as a live trial
without having cleared the IS/OOS bar -- see that module's docstring.

This script does NOT touch the entry-side question. It asks a narrower,
different question: while a position is ALREADY OPEN, does an adverse
TX-NQ decoupling spike (same tw_dev/us_dev/spread computation, reused
UNMODIFIED via import from nq_1m_spread_gate) provide useful information as
an early-exit anomaly trigger, layered ON TOP of the existing live
exit_mode (hybrid_trail: trail_giveback + struct_break + max_hold + opp),
with NO changes to entry logic, cell book, or the causal_engine itself?

Why this might behave differently from the entry-side result (reasoned
explicitly, not just asserted):
  1. Entry needs to predict WHICH WAY TX reverts (directional mean-reversion
     call) to be worth anything -- a weak/inconsistent IC there kills the
     entry case outright. This exit mechanism only needs the FIRST HALF of
     that claim (something anomalous is happening) to be useful: it doesn't
     need spread to reliably predict reversion magnitude/timing, only that
     an extreme divergence-against-the-position co-occurs often enough with
     bad forward outcomes for an ALREADY-RISKED trade to be worth cutting.
  2. False-positive cost structure differs: an unnecessary ENTRY on a false
     mean-reversion signal risks a full adverse move from flat. An
     unnecessary DEFENSIVE EXIT on a false anomaly signal only forfeits the
     difference between the early-exit price and whatever the ALREADY-ARMED
     exit_mode (trail/struct/max_hold/opp) would have produced anyway -- a
     strictly smaller, bounded downside conditional on already being in the
     trade.
  3. These are genuinely both live possibilities and the base rate of
     "clever new exit idea beating struct_break" in this exact research
     line is LOW (~120 struct_break-mitigation variants, all failed --
     tmf-structbreak-mitigation topic). This is a DIFFERENT axis
     (velocity/cross-market divergence, not price-structure swing
     detection) so it is not a mechanical repeat of those, but it must
     clear the same bar of evidence, not a lower one.

METHOD: overlay, not engine fork. Baseline trades come from an UNMODIFIED
causal_engine.simulate() call (same live-smart recipe/cell-book construction
as tmf_struct_disabled_decouple_live_smart_holdout3.py). For each closed
trade we then walk bar-by-bar from entry+min_hold to the ORIGINAL exit bar,
computing spread(t) causally (tw_dev/us_dev/spread reused verbatim from
nq_1m_spread_gate, re-sliced to `< t+1` at every step -- see
perturbation_test_bug2() for the causal-truncation self-check); if the
divergence is ADVERSE TO THE HELD SIDE and crosses THRESHOLD, we mark an
early exit at that bar's close (same COST convention as the live engine)
and compare to the trade's actual outcome. This ISOLATES the marginal
contribution of the new trigger without editing src/tmf_channel/ or
re-deriving entries/other exits.

TWO DATA-RESOLUTION VARIANTS, stated honestly per data-source constraint
(nq_1m_spread_gate's own docstring: "only ~3 days of genuine TX+real-1m-NQ
overlap exist right now"):

  A. REAL 1-MINUTE NQ (the actual live-resolution mechanism). Uses the
     locally-accumulated NQ archive
     (/Users/jackm4/goldenstocks-data/cache/tmf_channel/nq_es_1m_archive,
     built by scripts/research/nq_es_1m_daily_accumulate.py, Yahoo's ~8-day
     trailing 1m cap) intersected against TX-bar-cache days that actually
     have coverage. As of this run that is a THIN handful of days -- exact
     count printed and stored, NOT rounded up. Any conclusion from this arm
     is a small-sample pilot signal at best, explicitly labeled as such.
  B. HOURLY-NQ FORWARD-FILLED PROXY (secondary, longer-history sanity
     check, NOT the live-resolution mechanism). Reuses the same
     nq_1m_spread_gate._us_dev() function unmodified, but fed a NQ series
     built by forward-filling Yahoo hourly closes to 1-minute frequency.
     This lets us evaluate on the three holdout windows (julsep25/
     octdec25/janmar26, 182 days, structurally immune to cache_store
     defects A/B -- no 00:00-04:59 bars) plus the 83-day w83 window. It is
     explicitly a CRUDER proxy: within any 100-minute trailing window there
     are only ~1-2 genuinely-distinct underlying hourly observations being
     held flat and averaged, not ~100 independent 1-minute prints -- so it
     answers "does an hourly-resolution version of this idea show a
     qualitatively similar effect over a much longer sample", not "does the
     live 1-minute mechanism work". A negative/null result here does not
     resolve variant A either way (different, coarser feature); a positive
     result here is suggestive but not confirmatory of the live-resolution
     mechanism.

Read-only w.r.t. src/order/, src/tmf_channel/, config/order.yaml,
config/strategy.yaml, .env -- this script only imports them. Outputs go to
reports/research/channel_lab/.
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import scipy.stats as sps

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from slow_cell_significance_helper import classify_significance  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import COST, load_vixtwn_delta, simulate  # noqa: E402
from tmf_channel.nq_1m_spread_gate import _tw_dev, _us_dev  # noqa: E402  -- reused, NOT reimplemented
from tmf_channel.nq_1m_spread_gate import WINDOW_MIN as SPREAD_WINDOW_MIN  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from tmf_struct_disabled_decouple_live_smart_holdout3 import (  # noqa: E402
    bug1_session_end_accounting,
    bug7_exit_reason_breakdown,
    lag1_autocorr,
    middle_80pct_contribution,
    newey_west_test,
    one_sample_test,
)

OUT = LAB / "tmf_tx_nq_decouple_defensive_exit_result.json"
ARCHIVE_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/nq_es_1m_archive")
_TZ_TPE = ZoneInfo("Asia/Taipei")

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}
W83_SOURCE = "tx_1m_fullnight_cache_full.json"
AUG_REAL_1M_SOURCE = "tx_1m_tick_built_fullnight_aug"  # per-source registered "same day" convention

MIN_HOLD_BARS = 3  # matches engine's min_hold_before_smart default (trail/struct eligibility)
THRESHOLD_GRID = [0.15, 0.20, 0.30]  # ~+1.1 / +2.0 / +3.0 sigma vs production
# nq_gate_debug distribution cited in nq_1m_spread_gate.py's own docstring
# (n=244 polls, mean=0.061, std=0.082) -- same feature, defensive threshold
# chosen to require a genuinely EXTREME reading, not the loose 0.1 entry gate.


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_arrays(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)
    return O, H, L, C, V, T


def build_hourly_ffill_proxy_series(start: date, end: date) -> pd.Series:
    """Variant B input: real Yahoo hourly NQ closes, forward-filled to 1min
    frequency, so _us_dev()'s trailing-WINDOW_MIN-minute mean is computed
    over a step function of the (few) real underlying hourly prints rather
    than a single stale point. Explicitly a coarser proxy -- see module
    docstring point B."""
    from us_futures_overnight import NQ_YAHOO, fetch_yahoo_intraday_closes

    raw = fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval="1h")
    if raw.empty:
        return raw
    raw = raw.sort_index()
    full_index = pd.date_range(raw.index.min(), raw.index.max(), freq="1min", tz=raw.index.tz)
    return raw.reindex(full_index).ffill()


def load_real_1m_archive() -> pd.Series:
    path = ARCHIVE_DIR / "NQ_1m.parquet"
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_parquet(path)
    return df["close"].sort_index()


def _to_et(ts: str) -> "pd.Timestamp":
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(_TZ_TPE)
    return t.tz_convert("America/New_York")


def _make_local_us_dev_fn(full_series: pd.Series, T: list[str]):
    """Perf wrapper, NOT a redefinition of ``_us_dev``: pre-slice the (huge)
    NQ series down to this day's session span (+/- a WINDOW_MIN-plus-margin
    buffer) ONCE per day, then call ``_us_dev`` UNMODIFIED on that small
    local slice for every bar. ``_us_dev`` itself only ever looks at rows
    with ``index <= dt_et`` and ``index > dt_et - WINDOW_MIN minutes``, so a
    slice that comfortably covers ``[session_start - WINDOW_MIN_minutes,
    session_end]`` is guaranteed to contain every row the unsliced call
    would have used -- bit-for-bit identical output, just without scanning
    hundreds of thousands of irrelevant rows on every single bar."""
    if full_series.empty or not T:
        return lambda t: None
    t0 = _to_et(T[0]) - pd.Timedelta(minutes=SPREAD_WINDOW_MIN + 5)
    t1 = _to_et(T[-1]) + pd.Timedelta(minutes=5)
    local = full_series[(full_series.index >= t0) & (full_series.index <= t1)]
    et_cache: dict[int, "pd.Timestamp"] = {}

    def fn(t: int) -> float | None:
        if t not in et_cache:
            et_cache[t] = _to_et(T[t])
        if local.empty:
            return None
        return _us_dev(local, et_cache[t].to_pydatetime())

    return fn


# ---------------------------------------------------------------------------
# Defensive-exit overlay (causal per-bar evaluation)
# ---------------------------------------------------------------------------
def defensive_spread_at(C: list[float], t: int, us_dev_fn) -> tuple[float | None, float | None, float | None]:
    """spread(t) computed using ONLY data available up to and including bar
    t. Perf note: ``_tw_dev`` only ever reads the LAST ``WINDOW_MIN+1``
    entries of whatever list it is given (``C[-1]`` and
    ``C[-(WINDOW_MIN+1):-1]``), so passing the trailing
    ``C[t-WINDOW_MIN : t+1]`` slice is bit-for-bit identical to passing the
    full ``C[:t+1]`` causal prefix (verified by perturbation_test_bug2(),
    which checks index<inject_at is unaffected using this same truncated
    call path) while being O(WINDOW_MIN) instead of O(t) per call -- this
    is a performance-only change, not a causality change: both forms only
    ever see bars <= t."""
    lo = max(0, t - SPREAD_WINDOW_MIN)
    tw_dev = _tw_dev(C[lo: t + 1])
    if tw_dev is None:
        return None, None, None
    us_dev = us_dev_fn(t)
    if us_dev is None:
        return tw_dev, None, None
    return tw_dev, us_dev, tw_dev - us_dev


def evaluate_trade_overlay(trade: dict, C: list[float], us_dev_fn, *, threshold: float, min_hold: int) -> dict:
    s = trade["s"]
    eb, xb = int(trade["eb"]), int(trade["xb"])
    fire_t = None
    fire_spread = None
    for t in range(eb + min_hold, xb):
        _, _, spread = defensive_spread_at(C, t, us_dev_fn)
        if spread is None:
            continue
        adverse = (s == "L" and spread >= threshold) or (s == "S" and spread <= -threshold)
        if adverse:
            fire_t = t
            fire_spread = spread
            break
    if fire_t is None:
        return dict(**trade, pnl_overlay=trade["pnl"], triggered=False)
    px = C[fire_t]
    ep = trade["ep"]
    pnl_overlay = round(((px - ep) if s == "L" else (ep - px)) - COST, 1)
    return dict(
        **trade,
        pnl_overlay=pnl_overlay,
        triggered=True,
        fire_bar=fire_t,
        fire_spread=round(fire_spread, 4),
        bars_saved=xb - fire_t,
        orig_why=trade["why"],
    )


# ---------------------------------------------------------------------------
# BUG-2 perturbation test (causal-truncation self-check of THIS wrapper, not
# of nq_1m_spread_gate._tw_dev/_us_dev themselves, which are frozen/reused
# production code out of scope for this audit).
# ---------------------------------------------------------------------------
def perturbation_test_bug2(C: list[float], us_dev_fn, *, t_lo: int, t_hi: int, inject_at: int) -> dict:
    assert t_lo < inject_at < t_hi, "perturbation point must be strictly inside the probed range"
    before = {t: defensive_spread_at(C, t, us_dev_fn) for t in range(t_lo, t_hi)}
    C2 = list(C)
    C2[inject_at] = C2[inject_at] + 500.0  # gross synthetic anomaly, TX points
    after = {t: defensive_spread_at(C2, t, us_dev_fn) for t in range(t_lo, t_hi)}
    first_changed = None
    for t in range(t_lo, t_hi):
        if before[t] != after[t]:
            first_changed = t
            break
    ok = (first_changed is None) or (first_changed >= inject_at)
    unchanged_before = all(before[t] == after[t] for t in range(t_lo, inject_at))
    return dict(
        t_lo=t_lo, t_hi=t_hi, inject_at=inject_at,
        first_changed_index=first_changed,
        all_prior_indices_unchanged=unchanged_before,
        ok=ok and unchanged_before,
    )


# ---------------------------------------------------------------------------
# Per-day raw evaluation (baseline simulate() + overlay), shared by the
# per-window and pooled summaries so both are computed from IDENTICAL
# per-day results (no separate re-run / re-derivation between them).
# ---------------------------------------------------------------------------
def run_one_day(day: str, source: str, recipe_base: dict, vix: dict, us_dev_fn_factory,
                 *, threshold: float, min_hold: int) -> dict | None:
    arr = load_arrays(day, source)
    if arr is None:
        return None
    O, H, L, C, V, T = arr
    gate = continuous_gate_for_day(day, T, source=source)
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    trades, events, ws, wl, rvol, regime, open_pos = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    if not trades:
        return None

    us_dev_fn = us_dev_fn_factory(day, T)
    day_had_coverage = False
    n_evaluable = 0
    overlay_trades = []
    for tr in trades:
        res = evaluate_trade_overlay(tr, C, us_dev_fn, threshold=threshold, min_hold=min_hold)
        mid_t = (int(tr["eb"]) + int(tr["xb"])) // 2
        _, us_mid, _ = defensive_spread_at(C, mid_t, us_dev_fn)
        if us_mid is not None:
            day_had_coverage = True
            n_evaluable += 1
        overlay_trades.append(res)

    base_net = sum(float(t["pnl"]) for t in trades)
    overlay_net = sum(float(t["pnl_overlay"]) for t in overlay_trades)
    return dict(
        day=day, source=source, trades=trades, overlay_trades=overlay_trades, open_pos=open_pos,
        delta=overlay_net - base_net, had_coverage=day_had_coverage, n_evaluable=n_evaluable,
    )


def summarize_days(day_results: list[dict], *, threshold: float, min_hold: int, label: str) -> dict:
    pooled_delta = [d["delta"] for d in day_results]
    pooled_base_trades = [t for d in day_results for t in d["trades"]]
    pooled_overlay_trades = [t for d in day_results for t in d["overlay_trades"]]
    pooled_base_rows = [(d["day"], d["trades"], d["open_pos"]) for d in day_results]
    n_days_with_real_nq_coverage = sum(1 for d in day_results if d["had_coverage"])
    n_trades_evaluable = sum(d["n_evaluable"] for d in day_results)

    n = len(pooled_delta)
    naive = one_sample_test(pooled_delta) if n else dict(n=0, mean=0.0, sd=0.0, t=None, p_value_t=float("nan"))
    ac1 = lag1_autocorr(pooled_delta) if n >= 3 else float("nan")
    hac = {str(mlag): newey_west_test(pooled_delta, mlag) for mlag in (1, 5, 10, 20)} if n else {}
    hac_clean = {k: v for k, v in hac.items() if not (isinstance(v, float) and np.isnan(v))}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"],
                                 hac_p_by_maxlags=hac_clean or {"1": naive["p_value_t"]})

    triggered = [t for t in pooled_overlay_trades if t.get("triggered")]
    bug7_triggered_orig_reason = bug7_exit_reason_breakdown(
        [dict(pnl=t["pnl"], why=t["orig_why"]) for t in triggered]
    ) if triggered else dict(total_pnl=0.0, n_trades=0, by_reason_category={})
    bug7_baseline_all = bug7_exit_reason_breakdown(pooled_base_trades)
    mid80_overlay = middle_80pct_contribution([dict(pnl=t["pnl_overlay"]) for t in pooled_overlay_trades])
    bug1 = bug1_session_end_accounting(pooled_base_rows)

    trig_pnl_orig = round(sum(float(t["pnl"]) for t in triggered), 1)
    trig_pnl_overlay = round(sum(float(t["pnl_overlay"]) for t in triggered), 1)
    bars_saved = [t["bars_saved"] for t in triggered]

    return dict(
        label=label,
        threshold=threshold,
        min_hold=min_hold,
        n_days_run=n,
        n_days_with_real_nq_coverage_at_probe=n_days_with_real_nq_coverage,
        n_trades_total=len(pooled_overlay_trades),
        n_trades_evaluable_probe=n_trades_evaluable,
        n_trades_triggered=len(triggered),
        trigger_rate=round(len(triggered) / len(pooled_overlay_trades), 4) if pooled_overlay_trades else None,
        baseline_net=round(sum(float(t["pnl"]) for t in pooled_base_trades), 1),
        overlay_net=round(sum(float(t["pnl_overlay"]) for t in pooled_overlay_trades), 1),
        triggered_orig_pnl_sum=trig_pnl_orig,
        triggered_overlay_pnl_sum=trig_pnl_overlay,
        triggered_pnl_saved=round(trig_pnl_overlay - trig_pnl_orig, 1),
        avg_bars_saved_when_triggered=round(float(np.mean(bars_saved)), 2) if bars_saved else None,
        delta_naive=naive,
        delta_lag1_autocorr=round(ac1, 4) if not np.isnan(ac1) else None,
        delta_hac_p_by_maxlags=hac,
        delta_significance=sig,
        bug7_triggered_trades_original_exit_reason=bug7_triggered_orig_reason["by_reason_category"] if triggered else {},
        bug7_baseline_all_exit_reasons=bug7_baseline_all["by_reason_category"],
        bug1_session_end_accounting=bug1,
        middle80pct_overlay=mid80_overlay,
    )


# ---------------------------------------------------------------------------
# Main sweep over one variant across a set of (window_label, day, source)
# triples, at one threshold: per-window summaries + a pooled summary, all
# derived from the SAME per-day run_one_day() results (BUG-3/A2 fold
# consistency -- do not just report the pooled number).
# ---------------------------------------------------------------------------
def run_variant(window_days_sources: list[tuple[str, str, str]], recipe_base: dict, vix: dict,
                 us_dev_fn_factory, *, threshold: float, min_hold: int, label: str) -> dict:
    by_window: dict[str, list[dict]] = {}
    for window, day, source in window_days_sources:
        res = run_one_day(day, source, recipe_base, vix, us_dev_fn_factory, threshold=threshold, min_hold=min_hold)
        if res is None:
            continue
        by_window.setdefault(window, []).append(res)

    windows_out = {
        w: summarize_days(rows, threshold=threshold, min_hold=min_hold, label=f"{label}_{w}")
        for w, rows in by_window.items()
    }
    all_rows = [r for rows in by_window.values() for r in rows]
    pooled_out = summarize_days(all_rows, threshold=threshold, min_hold=min_hold, label=f"{label}_pooled")
    # sign/direction consistency across independent windows (A2/BUG-3 style check)
    per_window_mean_delta = {w: s["delta_naive"]["mean"] for w, s in windows_out.items()}
    signs = {w: (1 if m > 0 else (-1 if m < 0 else 0)) for w, m in per_window_mean_delta.items()}
    n_positive = sum(1 for v in signs.values() if v > 0)
    return dict(
        windows=windows_out,
        pooled=pooled_out,
        per_window_mean_delta=per_window_mean_delta,
        n_windows_positive=n_positive,
        n_windows_total=len(signs),
        majority_windows_positive=(n_positive > len(signs) / 2) if signs else None,
    )


def main() -> None:
    t_start = time.time()
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()
    recipe_base["struct_disabled"] = False  # current live default (unadopted candidate stays off)

    out = dict(
        title="TX-NQ decoupling defensive EXIT trigger, overlaid on live exit_mode "
        "(struct_break/trail/max_hold/opp unchanged) -- marginal-contribution isolation",
        spread_window_min=SPREAD_WINDOW_MIN,
        min_hold_bars=MIN_HOLD_BARS,
        threshold_grid=THRESHOLD_GRID,
    )

    # ---------------- Variant B: hourly-NQ ffill proxy, long history -------
    print("Fetching hourly NQ proxy series (wide range)...")
    hourly_proxy = build_hourly_ffill_proxy_series(date(2025, 6, 20), date(2026, 8, 12))
    print(f"  hourly proxy series: {len(hourly_proxy)} rows, "
          f"{hourly_proxy.index.min()}..{hourly_proxy.index.max()}")

    def proxy_us_dev_fn_factory(day: str, T: list[str]):
        return _make_local_us_dev_fn(hourly_proxy, T)

    proxy_window_days_sources = []
    for wlabel, source in HOLDOUT_SOURCES.items():
        for d in list_days(source=source):
            proxy_window_days_sources.append((wlabel, d, source))
    for d in list_days(source=W83_SOURCE):
        proxy_window_days_sources.append(("w83", d, W83_SOURCE))
    print(f"variant B day count (holdout x3 + w83): {len(proxy_window_days_sources)}")

    out["variant_B_hourly_proxy_long_history"] = {}
    for thr in THRESHOLD_GRID:
        res = run_variant(proxy_window_days_sources, recipe_base, vix, proxy_us_dev_fn_factory,
                           threshold=thr, min_hold=MIN_HOLD_BARS, label=f"proxy_thr{thr}")
        out["variant_B_hourly_proxy_long_history"][str(thr)] = res
        p = res["pooled"]
        print(f"[B thr={thr}] pooled n_days={p['n_days_run']} triggers={p['n_trades_triggered']}"
              f"/{p['n_trades_total']} baseline_net={p['baseline_net']} "
              f"overlay_net={p['overlay_net']} mean_delta={p['delta_naive']['mean']} "
              f"p_naive={p['delta_naive']['p_value_t']} -> {p['delta_significance']['label']} | "
              f"windows_positive={res['n_windows_positive']}/{res['n_windows_total']} "
              f"per_window_mean_delta={res['per_window_mean_delta']}")

    # ---------------- Variant A: real 1-minute NQ archive, thin -----------
    print("\nLoading real 1m NQ archive...")
    real_1m = load_real_1m_archive()
    print(f"  real 1m archive: {len(real_1m)} rows, "
          f"{real_1m.index.min() if len(real_1m) else None}..{real_1m.index.max() if len(real_1m) else None}")

    def real_us_dev_fn_factory(day: str, T: list[str]):
        return _make_local_us_dev_fn(real_1m, T)

    real_window_days_sources = [("aug26", d, AUG_REAL_1M_SOURCE) for d in list_days(source=AUG_REAL_1M_SOURCE)]
    print(f"variant A candidate TX days (all of aug tick-built cache): {len(real_window_days_sources)} -> "
          f"{[d for _, d, _ in real_window_days_sources]}")

    out["variant_A_real_1m_thin"] = {}
    for thr in THRESHOLD_GRID:
        res = run_variant(real_window_days_sources, recipe_base, vix, real_us_dev_fn_factory,
                           threshold=thr, min_hold=MIN_HOLD_BARS, label=f"real1m_thr{thr}")
        out["variant_A_real_1m_thin"][str(thr)] = res
        p = res["pooled"]
        print(f"[A thr={thr}] n_days={p['n_days_run']} coverage_days={p['n_days_with_real_nq_coverage_at_probe']} "
              f"triggers={p['n_trades_triggered']}/{p['n_trades_total']} "
              f"baseline_net={p['baseline_net']} overlay_net={p['overlay_net']} "
              f"mean_delta={p['delta_naive']['mean']} p_naive={p['delta_naive']['p_value_t']} "
              f"-> {p['delta_significance']['label']}")

    # ---------------- BUG-2 perturbation test ------------------------------
    print("\nBUG-2 perturbation test...")
    pert_day, pert_source = W83_SOURCE and list_days(source=W83_SOURCE)[10], W83_SOURCE
    arr = load_arrays(pert_day, pert_source)
    O, H, L, C, V, T = arr
    fn = proxy_us_dev_fn_factory(pert_day, T)
    n_bars = len(C)
    t_lo = SPREAD_WINDOW_MIN + 5
    t_hi = min(n_bars - 5, t_lo + 200)
    inject_at = t_lo + (t_hi - t_lo) // 2
    pert = perturbation_test_bug2(C, fn, t_lo=t_lo, t_hi=t_hi, inject_at=inject_at)
    pert["day"] = pert_day
    out["bug2_perturbation_test"] = pert
    print(f"  {pert}")

    out["elapsed_s"] = round(time.time() - t_start, 1)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
