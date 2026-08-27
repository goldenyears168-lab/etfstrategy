#!/usr/bin/env python3
"""H-VEL-ACCEL-STOP: a NEW tick-level velocity/acceleration adverse-move exit,
tested as an ADDITIONAL trigger layered on top of the current live exit_mode
(hybrid_trail: stop + trail + struct_break all still active), across w83
(2026-04-01..2026-07-31, patch_nq_gate_for_backfill(lookback_days=500)) and
the three holdout windows (julsep25/octdec25/janmar26).

READ-ONLY w.r.t. src/order/, src/tmf_channel/, config/order.yaml,
config/strategy.yaml, .env -- this script only imports
tmf_channel.causal_engine / tmf_channel.engine.simulate and existing
research-line helpers; causal_engine.py is never edited or monkeypatched.

--------------------------------------------------------------------------
WHY "PARALLEL TICK-REPLAY" RATHER THAN MONKEY-PATCHING causal_engine
--------------------------------------------------------------------------
causal_engine.simulate()'s real per-tick exit loop (stop/trail/struct_break,
`for k in range(start, end): px = tk_px[k]` around line 1597) lives inside a
nested closure (`_tick_walk_bar`) defined INSIDE simulate()'s function body.
It is not a module-level attribute, so there is nothing to monkeypatch from
outside without editing causal_engine.py itself (prohibited). The clean
alternative -- and the one this script takes -- is:

  1. Run the REAL, unmodified causal_engine.simulate() once per day with the
     live-smart recipe (PAPER_RECIPE + specialized_cell_book() +
     continuous_gate_for_day, matching every other script in this research
     line) to get the OFFICIAL baseline trades: entries, fills, and exits
     exactly as today's live hybrid_trail (stop+trail+struct_break) produces
     them. This is the "SAME entry logic/fills from causal_engine.simulate()"
     required by the task -- we never re-derive entries ourselves.
  2. For each closed trade (max_lots=1 in PAPER_RECIPE, so each trade is one
     clean open->close lot, ep/eb/xb/s/why/pnl), and for the day's dangling
     open position (if any, since PAPER_RECIPE has eod_flatten=False),
     replay REAL tick-by-tick data (FinMind front-month TX ticks,
     ~/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day/) over the
     window strictly AFTER the entry bar through strictly BEFORE the bar in
     which the baseline itself exited, and ask: does the NEW velocity/accel
     trigger fire at some real tick in that window? If yes, the trade closes
     EARLY at that tick's real price (an unambiguous improvement/deterioration
     the baseline mechanism could not see, because the baseline never looked
     at intra-bar tick sequencing for this new signal). If no, the trade is
     left completely untouched -- candidate pnl == baseline pnl exactly.

This design can only ever make a trade exit EARLIER than the baseline's own
bar-level exit; it deliberately never contests bar xb itself (the bar in
which the baseline mechanism already exited), because we don't have exact
intra-bar timing for the ORIGINAL stop/trail/struct_break trigger and do not
want to guess at intrabar ordering between two different mechanisms on the
same bar. This is a conservative simplification (documented, not hidden) --
it can only UNDERCOUNT the new mechanism's potential effect, never overcount
it via a look-ahead-tainted "was I first" race on the same bar.

Also: entry bar `eb` itself is EXCLUDED from the new-trigger's eligible
window (monitoring starts at bar eb+1) because we do not know the exact
intra-bar tick at which the baseline's own entry fill actually happened, and
do not want to fabricate a "trigger" before the position genuinely existed.

--------------------------------------------------------------------------
THE METRIC: adverse ACCELERATION (fast-window velocity minus slow-window
velocity), signed against the open position, thresholded by an ADAPTIVE
(N-sigma-of-recent-volatility) bound -- not a fixed pts/sec constant.
--------------------------------------------------------------------------
Alternatives considered and why they were NOT chosen as primary:
  (a) Fixed pts/sec velocity threshold. Rejected: TX/TMF trades in wildly
      different vol regimes (quiet Asian afternoon lull vs a US-session
      crash night); a fixed threshold either never fires in quiet regimes
      or fires constantly in violent ones. An adaptive threshold is
      necessary for the metric to mean the same thing across day/night and
      across calm/turbulent stretches.
  (b) Plain velocity (not acceleration) with an N-sigma threshold. Not
      chosen as primary because plain "currently moving fast against me" is
      closely related to what trail's peak/giveback and struct_break's
      swing-break already respond to (both are ultimately triggered by
      price having moved a certain DISTANCE against the position -- trail
      via giveback-from-peak, struct_break via breaching a swing level).  A
      pure velocity trigger risks being a near-duplicate of those two,
      just re-expressed in points/second instead of points. Acceleration
      (change in the RATE of adverse movement) targets something distinct:
      a SUDDEN SPEED-UP relative to the position's own recent local trend,
      which can fire before price has traveled far enough to breach a swing
      level or exhaust a trail giveback -- e.g. an air-pocket/flash-move
      that starts moving much faster than the preceding ~90s, even if the
      total adverse distance so far is still small. This is examined
      explicitly in the BUG-7 section below (does it catch genuinely
      different trades, or just fire slightly before struct_break/trail on
      the SAME trades).
  (c) Acceleration with a FIXED threshold. Rejected for the same reason as
      (a).

Exact definition (all strictly causal -- see the BUG-2 perturbation test in
`_bug2_perturbation_self_check()`, run once before the main backtest and
required to PASS or the script aborts):
  - v_fast(k)  = (price[k] - price[idx_fast(k)]) / (t[k] - t[idx_fast(k)])
                 window: real ticks in (t[k]-W1, t[k]], W1=20s
  - v_slow(k)  = same construction, W2=90s
  - accel(k)   = v_fast(k) - v_slow(k)      (side-agnostic, pts/sec)
  - adverse_accel(k) = accel(k) if side=="S" else -accel(k)
        (short: upward acceleration is adverse; long: downward is adverse)
  - sigma_ref(k) = std of accel(.) sampled over ticks in
        [t[k]-W2-REF_WIN, t[k]-W2), REF_WIN=600s -- i.e. a reference window
        that ENDS exactly where v_slow(k)'s own window BEGINS, giving zero
        overlap with any tick that feeds v_fast(k)/v_slow(k)/accel(k) at k.
        This is deliberate: a reference volatility that could see the very
        tick(s) being judged would inflate itself in response to the same
        anomaly it's supposed to be judging against, which would silently
        raise the bar exactly when it should be lowered (a subtle
        self-referential variant of BUG-2). Requires >=30 valid accel
        samples in the reference window or sigma_ref(k) is undefined (no
        trigger that tick).
  - fires at k when: adverse_accel(k) > K_SIGMA * sigma_ref(k)
        AND the fast-window velocity is ALSO actually adverse right now
        ( -v_fast(k) if L else v_fast(k) > 0 ) -- guards against the
        pathological case of accel exceeding threshold while price is
        still net favorable (decelerating less than its recent trend, but
        still moving the right way), which is not what "stop out on
        adverse velocity" should mean.
  K_SIGMA swept over {2.5, 3.5, 5.0} (BUG-4: report all three, no cherry-
  picking a single "best" threshold after the fact).

Outputs reports/research/channel_lab/tmf_tick_velocity_accel_stop_v1_result.json
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.stats as sps
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from slow_cell_significance_helper import classify_significance  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_channel.legacy_helpers import COST  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

OUT = LAB / "tmf_tick_velocity_accel_stop_v1_result.json"

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}
LOOKBACK_DAYS = 500  # defect C guard (handoff Sec.5c) -- same as every other
# script in this research line that touches w83's OOS-shaped date range.

# ---- metric parameters (fixed a priori, not fit on this backtest's own
# outcome -- see module docstring "alternatives considered") ----
W1 = 20.0          # fast window, seconds
W2 = 90.0          # slow window, seconds
REF_WIN = 600.0    # sigma reference window, seconds
MIN_FRAC = 0.5     # require >= MIN_FRAC*window of real elapsed time to be valid
MIN_REF_N = 30     # minimum accel samples in the reference window
K_SIGMA_SWEEP = (2.5, 3.5, 5.0)
EPS = 1e-9


# ===========================================================================
# tick-side machinery
# ===========================================================================
def load_session_ticks(T: list[str]) -> pd.DataFrame | None:
    """Real front-month TX ticks spanning every REAL calendar date this
    session's bars actually touch (derived from the already-fixed
    cache_store.bar_timestamps() output -- never hand-rolled date
    arithmetic, per handoff Sec.5d anti-pattern warning). For the 3
    holdout sources (no 00:00-04:59 bars) this is always a single calendar
    date; for w83 (session convention, post-midnight bars = day+1) it is
    two calendar dates on nights that touch after 00:00."""
    cal_dates = sorted({t[:10] for t in T})
    frames = []
    for cal in cal_dates:
        f = load_front_month_ticks(cal)
        if f is not None and not f.empty:
            frames.append(f)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).sort_values("dt").reset_index(drop=True)
    df = df.drop_duplicates(subset="dt", keep="last")
    return df


def bar_open_ts(iso: str) -> pd.Timestamp:
    """iso like '2026-04-02T00:36:00.000+08:00' -> naive local pd.Timestamp
    matching the tick file's naive local 'dt' column (both Taiwan local
    time, no tz math needed -- same convention as
    struct_break_bar_vs_tick_swing.py's to_ts())."""
    return pd.Timestamp(f"{iso[:10]} {iso[11:16]}:00")


def compute_velocity_accel(t_epoch: np.ndarray, px: np.ndarray) -> dict:
    """All arrays indexed by real tick k, strictly causal (index k only
    ever uses ticks with index <= k). See module docstring for exact
    definitions and the BUG-2 non-overlap design of sigma_ref."""
    n = len(t_epoch)
    idx_fast = np.searchsorted(t_epoch, t_epoch - W1, side="left")
    idx_slow = np.searchsorted(t_epoch, t_epoch - W2, side="left")
    span_fast = t_epoch - t_epoch[idx_fast]
    span_slow = t_epoch - t_epoch[idx_slow]
    valid_fast = span_fast >= (W1 * MIN_FRAC)
    valid_slow = span_slow >= (W2 * MIN_FRAC)

    v_fast = np.where(valid_fast, (px - px[idx_fast]) / np.maximum(span_fast, EPS), np.nan)
    v_slow = np.where(valid_slow, (px - px[idx_slow]) / np.maximum(span_slow, EPS), np.nan)
    accel = v_fast - v_slow
    accel_valid = valid_fast & valid_slow
    accel_for_sigma = np.where(accel_valid, accel, 0.0)

    # ref window = [t[k]-W2-REF_WIN, t[k]-W2) -- ends exactly where the
    # v_slow window begins, i.e. zero overlap with anything feeding
    # v_fast/v_slow/accel at k.
    ref_end_idx = idx_slow  # first idx with t >= t[k]-W2
    ref_start_idx = np.searchsorted(t_epoch, t_epoch - W2 - REF_WIN, side="left")

    cnt = accel_valid.astype(np.float64)
    cs1 = np.concatenate([[0.0], np.cumsum(accel_for_sigma)])
    cs2 = np.concatenate([[0.0], np.cumsum(accel_for_sigma ** 2)])
    csn = np.concatenate([[0.0], np.cumsum(cnt)])

    sigma_ref = np.full(n, np.nan)
    sigma_ref_n = np.zeros(n, dtype=np.int64)
    a = ref_start_idx
    b = ref_end_idx
    n_in = csn[b] - csn[a]
    ok = n_in >= MIN_REF_N
    s1 = cs1[b] - cs1[a]
    s2 = cs2[b] - cs2[a]
    with np.errstate(invalid="ignore"):
        mean_ = np.where(ok, s1 / np.maximum(n_in, 1), np.nan)
        var_ = np.where(ok, np.maximum(s2 / np.maximum(n_in, 1) - mean_ ** 2, 0.0), np.nan)
    sigma_ref = np.where(ok, np.sqrt(var_), np.nan)
    sigma_ref_n = n_in.astype(np.int64)

    return dict(
        t_epoch=t_epoch, px=px,
        v_fast=v_fast, v_slow=v_slow, accel=accel, accel_valid=accel_valid,
        sigma_ref=sigma_ref, sigma_ref_n=sigma_ref_n,
        idx_fast=idx_fast, idx_slow=idx_slow,
        ref_start_idx=ref_start_idx, ref_end_idx=ref_end_idx,
    )


def find_trigger(arrs: dict, lo: int, hi: int, side: str, k_sigma: float) -> int | None:
    """First tick index in [lo, hi) where the adverse-acceleration trigger
    fires, or None. side: 'L' or 'S'."""
    if hi <= lo:
        return None
    accel = arrs["accel"][lo:hi]
    accel_valid = arrs["accel_valid"][lo:hi]
    sigma_ref = arrs["sigma_ref"][lo:hi]
    v_fast = arrs["v_fast"][lo:hi]
    sign = 1.0 if side == "S" else -1.0
    adverse_accel = sign * accel
    adverse_v_fast = sign * v_fast
    valid = accel_valid & ~np.isnan(sigma_ref)
    fires = valid & (adverse_accel > k_sigma * sigma_ref) & (adverse_v_fast > 0.0)
    hit = np.argmax(fires) if fires.any() else None
    return (lo + int(hit)) if hit is not None else None


# ===========================================================================
# BUG-2 perturbation self-check (must PASS before the real backtest runs)
# ===========================================================================
def _bug2_perturbation_self_check() -> dict:
    rng = np.random.default_rng(20260812)
    n = 4000
    t_epoch = np.cumsum(rng.exponential(scale=2.0, size=n)).astype(float)  # irregular ticks, ~2s apart
    px = 46000.0 + np.cumsum(rng.normal(0, 1.5, size=n))

    base = compute_velocity_accel(t_epoch, px)

    t0 = 2500  # perturb well inside the series, away from both edges
    px_pert = px.copy()
    px_pert[t0] += 800.0  # large synthetic spike, one tick only
    pert = compute_velocity_accel(t_epoch, px_pert)

    checks = {}

    # 1) everything strictly before t0 must be BIT-EXACT identical.
    def arrs_equal_before(key):
        a, b = base[key][:t0], pert[key][:t0]
        if np.issubdtype(a.dtype, np.floating):
            eq = np.array_equal(a, b, equal_nan=True)
        else:
            eq = np.array_equal(a, b)
        return bool(eq)

    for key in ("v_fast", "v_slow", "accel", "sigma_ref"):
        checks[f"{key}_unchanged_before_t0"] = arrs_equal_before(key)

    # 2) v_fast/v_slow/accel: earliest affected index must be EXACTLY t0
    #    (the tick itself is legitimately allowed to change -- at real time
    #    t0 the price at t0 is genuinely "now", not future information).
    diff_accel = ~np.isclose(base["accel"], pert["accel"], equal_nan=True)
    first_affected_accel = int(np.argmax(diff_accel)) if diff_accel.any() else None
    checks["accel_first_affected_index"] = first_affected_accel
    checks["accel_first_affected_is_exactly_t0"] = (first_affected_accel == t0)

    # 3) sigma_ref(t0) itself must be UNCHANGED (buffer design: the
    #    reference window for k=t0 ends at idx_slow(t0), which is < t0, so
    #    it never sees the perturbed tick at t0 -- this is the key
    #    self-referential-contamination guard described in the module
    #    docstring).
    sr_t0_unchanged = bool(
        np.isclose(base["sigma_ref"][t0], pert["sigma_ref"][t0], equal_nan=True)
    )
    checks["sigma_ref_at_t0_unchanged"] = sr_t0_unchanged

    # 4) sigma_ref DOES eventually change (confirms the perturbation
    #    actually propagates somewhere, i.e. checks 1-3 aren't vacuously
    #    true because nothing ever depends on accel[t0] at all).
    diff_sr = ~np.isclose(base["sigma_ref"], pert["sigma_ref"], equal_nan=True)
    first_affected_sr = int(np.argmax(diff_sr)) if diff_sr.any() else None
    checks["sigma_ref_first_affected_index"] = first_affected_sr
    checks["sigma_ref_eventually_changes"] = diff_sr.any()
    checks["sigma_ref_first_affected_after_t0_plus_W2"] = (
        first_affected_sr is not None and t_epoch[first_affected_sr] >= t_epoch[t0] + W2 - 1e-6
    )

    all_pass = all(
        checks[k]
        for k in (
            "v_fast_unchanged_before_t0", "v_slow_unchanged_before_t0",
            "accel_unchanged_before_t0", "sigma_ref_unchanged_before_t0",
            "accel_first_affected_is_exactly_t0", "sigma_ref_at_t0_unchanged",
            "sigma_ref_eventually_changes", "sigma_ref_first_affected_after_t0_plus_W2",
        )
    )
    checks["ALL_PASS"] = bool(all_pass)
    return checks


# ===========================================================================
# stats / decomposition helpers (same pattern as
# tmf_struct_disabled_decouple_live_smart_w83.py -- this research line's
# established convention for these checks)
# ===========================================================================
def one_sample_test(x):
    x = np.asarray(x, dtype=float)
    n = len(x)
    m = float(np.mean(x)) if n else 0.0
    sd = float(np.std(x, ddof=1)) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else float("nan")
    t_stat = m / se if se > 0 else float("nan")
    df = n - 1
    p_t = float(2.0 * sps.t.sf(abs(t_stat), df)) if se > 0 and df > 0 else float("nan")
    return dict(n=n, mean=round(m, 4), sd=round(sd, 4),
                t=None if np.isnan(t_stat) else round(t_stat, 4), p_value_t=p_t)


def lag1_autocorr(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 3:
        return float("nan")
    xm = x - x.mean()
    num = float(np.sum(xm[:-1] * xm[1:]))
    den = float(np.sum(xm * xm))
    return num / den if den > 0 else float("nan")


def newey_west_test(x, maxlags):
    x = np.asarray(x, dtype=float)
    if len(x) < max(3, maxlags + 2):
        return float("nan")
    Xc = np.ones((len(x), 1))
    model = sm.OLS(x, Xc).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    return float(model.pvalues[0])


def bug1_session_end_accounting(rows) -> dict:
    """rows: list of (day, trades_net, honest_net) already includes any
    open-position unrealized pnl / new-trigger-preempted-open-position
    pnl computed by the caller."""
    trades_only = sum(r[1] for r in rows)
    honest = sum(r[2] for r in rows)
    return dict(
        n_days=len(rows),
        trades_only_net=round(trades_only, 1),
        honest_net=round(honest, 1),
        gap=round(honest - trades_only, 1),
    )


def bug7_breakdown(records: list[dict]) -> dict:
    """records: dicts with 'orig_why_cat', 'preempted' (bool), 'delta'
    (candidate_pnl - baseline_pnl for this trade, 0.0 if not preempted)."""
    n_total = len(records)
    n_preempted = sum(1 for r in records if r["preempted"])
    by_orig_cat_all = {}
    by_orig_cat_preempted = {}
    for r in records:
        cat = r["orig_why_cat"]
        by_orig_cat_all[cat] = by_orig_cat_all.get(cat, 0) + 1
        if r["preempted"]:
            by_orig_cat_preempted[cat] = by_orig_cat_preempted.get(cat, 0) + 1
    preempted_delta_sum = round(sum(r["delta"] for r in records if r["preempted"]), 1)
    preempted_delta_pos = sum(1 for r in records if r["preempted"] and r["delta"] > 0)
    preempted_delta_neg = sum(1 for r in records if r["preempted"] and r["delta"] < 0)
    return dict(
        n_total_eligible_trades=n_total,
        n_preempted_by_new_trigger=n_preempted,
        pct_preempted=(round(100.0 * n_preempted / n_total, 2) if n_total else None),
        orig_exit_reason_share_ALL=by_orig_cat_all,
        orig_exit_reason_share_PREEMPTED=by_orig_cat_preempted,
        preempted_delta_sum_pnl=preempted_delta_sum,
        preempted_n_delta_positive=preempted_delta_pos,
        preempted_n_delta_negative=preempted_delta_neg,
    )


# ===========================================================================
# per-day evaluation
# ===========================================================================
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


def evaluate_day(day: str, source: str, recipe_base: dict, vix: dict, k_sigma_list: tuple[float, ...]):
    arr = load_arrays(day, source)
    if arr is None:
        return None
    O, H, L, C, V, T = arr
    gate = continuous_gate_for_day(day, T, source=source)
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    trades, events, ws, wl, rvol, regime, open_pos = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)

    tdf = load_session_ticks(T)
    if tdf is None or tdf.empty:
        # no real tick coverage this day -> new trigger structurally cannot
        # fire; candidate == baseline for every threshold.
        return dict(
            day=day, baseline_net=round(sum(t["pnl"] for t in trades), 1),
            candidate_net_by_k={k: round(sum(t["pnl"] for t in trades), 1) for k in k_sigma_list},
            records_by_k={k: [dict(orig_why_cat=str(t.get("why", "?")).split("|", 1)[0],
                                    preempted=False, delta=0.0) for t in trades]
                          for k in k_sigma_list},
            open_pos_baseline_u_pnl=(float(open_pos["u_pnl"]) if open_pos else 0.0),
            open_pos_candidate_u_pnl_by_k={k: (float(open_pos["u_pnl"]) if open_pos else 0.0)
                                           for k in k_sigma_list},
            has_open_pos=(open_pos is not None),
            n_tick_covered=0,
        )

    t_epoch = tdf["dt"].to_numpy(dtype="datetime64[s]").astype(np.int64)
    px_arr = tdf["price"].to_numpy(dtype=float)
    tick_arrs = compute_velocity_accel(t_epoch, px_arr)

    def tick_idx_at_or_after(ts: pd.Timestamp) -> int:
        ep = int(np.datetime64(ts, "s").astype(np.int64))
        return int(np.searchsorted(t_epoch, ep, side="left"))

    out_records = {k: [] for k in k_sigma_list}
    out_candidate_net = {k: 0.0 for k in k_sigma_list}

    n_bars = len(T)
    for tr in trades:
        eb, xb, side = int(tr["eb"]), int(tr["xb"]), tr["s"]
        ep_price = float(tr["ep"])
        baseline_pnl = float(tr["pnl"])
        orig_cat = str(tr.get("why", "?")).split("|", 1)[0]

        window_ok = (eb + 1 < n_bars) and (eb + 1 <= xb - 1) if xb - 1 >= eb + 1 else False
        window_ok = (eb + 1 < n_bars) and (xb > eb + 1)
        lo = hi = None
        if window_ok:
            lo = tick_idx_at_or_after(bar_open_ts(T[eb + 1]))
            hi = tick_idx_at_or_after(bar_open_ts(T[xb]))

        for k_sigma in k_sigma_list:
            trig_idx = find_trigger(tick_arrs, lo, hi, side, k_sigma) if (lo is not None and hi is not None) else None
            if trig_idx is None:
                out_candidate_net[k_sigma] += baseline_pnl
                out_records[k_sigma].append(dict(orig_why_cat=orig_cat, preempted=False, delta=0.0))
            else:
                exit_px = float(tick_arrs["px"][trig_idx])
                new_pnl = ((exit_px - ep_price) if side == "L" else (ep_price - exit_px)) - COST
                out_candidate_net[k_sigma] += new_pnl
                out_records[k_sigma].append(
                    dict(orig_why_cat=orig_cat, preempted=True, delta=round(new_pnl - baseline_pnl, 1))
                )

    baseline_net = round(sum(t["pnl"] for t in trades), 1)

    # dangling open position at session cutoff (PAPER_RECIPE eod_flatten=False)
    open_pos_baseline_u = 0.0
    open_pos_cand_u_by_k = {k: 0.0 for k in k_sigma_list}
    if open_pos is not None:
        open_pos_baseline_u = float(open_pos["u_pnl"])
        # recover eb by matching et string against T (small arrays, linear scan ok)
        try:
            op_eb = T.index(open_pos["et"])
        except ValueError:
            op_eb = None
        for k_sigma in k_sigma_list:
            if op_eb is not None and op_eb + 1 < n_bars:
                lo2 = tick_idx_at_or_after(bar_open_ts(T[op_eb + 1]))
                hi2 = len(t_epoch)
                trig_idx = find_trigger(tick_arrs, lo2, hi2, open_pos["s"], k_sigma)
                if trig_idx is not None:
                    exit_px = float(tick_arrs["px"][trig_idx])
                    side_op = open_pos["s"]
                    ep_op = float(open_pos["ep"])
                    new_u = ((exit_px - ep_op) if side_op == "L" else (ep_op - exit_px)) - COST
                    open_pos_cand_u_by_k[k_sigma] = new_u
                    continue
            open_pos_cand_u_by_k[k_sigma] = open_pos_baseline_u

    return dict(
        day=day, baseline_net=baseline_net,
        candidate_net_by_k={k: round(v, 1) for k, v in out_candidate_net.items()},
        records_by_k=out_records,
        open_pos_baseline_u_pnl=round(open_pos_baseline_u, 1),
        open_pos_candidate_u_pnl_by_k={k: round(v, 1) for k, v in open_pos_cand_u_by_k.items()},
        has_open_pos=(open_pos is not None),
        n_tick_covered=len(t_epoch),
    )


def main() -> None:
    t_start = time.time()

    print("=== BUG-2 perturbation self-check (synthetic series) ===")
    pcheck = _bug2_perturbation_self_check()
    for k, v in pcheck.items():
        print(f"  {k}: {v}")
    if not pcheck["ALL_PASS"]:
        raise SystemExit("!!! BUG-2 perturbation self-check FAILED -- aborting before running "
                          "any real backtest. See pcheck dict above for which assertion failed.")
    print("BUG-2 perturbation self-check: ALL PASS\n")

    patch_nq_gate_for_backfill(lookback_days=LOOKBACK_DAYS)
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    out = dict(
        title="H-VEL-ACCEL-STOP: tick-level adverse-acceleration exit trigger, "
        "layered on top of live hybrid_trail (stop+trail+struct_break all "
        "active), w83 + 3 holdout windows",
        recipe_version=recipe_base.get("recipe_version"),
        metric_params=dict(W1_sec=W1, W2_sec=W2, REF_WIN_sec=REF_WIN,
                            MIN_FRAC=MIN_FRAC, MIN_REF_N=MIN_REF_N,
                            k_sigma_sweep=list(K_SIGMA_SWEEP)),
        bug2_perturbation_self_check=pcheck,
        windows={},
    )

    pooled_holdout_delta_by_k = {k: [] for k in K_SIGMA_SWEEP}
    pooled_holdout_records_by_k = {k: [] for k in K_SIGMA_SWEEP}
    pooled_holdout_rows_by_k = {k: [] for k in K_SIGMA_SWEEP}  # (day, base_net, honest_net)

    for label, source in WINDOWS.items():
        days = list_days(source=source)
        print(f"=== {label} ({source}) n_days={len(days)} ===")
        day_results = []
        for d in days:
            r = evaluate_day(d, source, recipe_base, vix, K_SIGMA_SWEEP)
            if r is not None:
                day_results.append(r)

        n_zero_tick = sum(1 for r in day_results if r["n_tick_covered"] == 0)
        win_out = dict(
            n_days=len(day_results),
            n_days_zero_tick_coverage=n_zero_tick,
            baseline_net_total=round(sum(r["baseline_net"] for r in day_results), 1),
            k_sigma_results={},
        )

        for k_sigma in K_SIGMA_SWEEP:
            deltas = []
            all_records = []
            rows_for_bug1 = []
            for r in day_results:
                base_trades_net = r["baseline_net"]
                cand_trades_net = r["candidate_net_by_k"][k_sigma]
                deltas.append(cand_trades_net - base_trades_net)
                all_records.extend(r["records_by_k"][k_sigma])
                base_honest = base_trades_net + r["open_pos_baseline_u_pnl"]
                cand_honest = cand_trades_net + r["open_pos_candidate_u_pnl_by_k"][k_sigma]
                rows_for_bug1.append((r["day"], base_trades_net, base_honest))
                rows_for_bug1.append((r["day"] + "|cand", cand_trades_net, cand_honest))

            naive = one_sample_test(deltas)
            ac1 = lag1_autocorr(deltas)
            hac = {str(L): newey_west_test(deltas, L) for L in (1, 5, 10, 20)}
            hac_clean = {k: v for k, v in hac.items() if not (isinstance(v, float) and np.isnan(v))}
            sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"],
                                         hac_p_by_maxlags=hac_clean or {"1": naive["p_value_t"]})
            bug7 = bug7_breakdown(all_records)
            bug1_base = bug1_session_end_accounting([(r["day"], r["baseline_net"], r["baseline_net"] + r["open_pos_baseline_u_pnl"]) for r in day_results])
            bug1_cand = bug1_session_end_accounting([(r["day"], r["candidate_net_by_k"][k_sigma], r["candidate_net_by_k"][k_sigma] + r["open_pos_candidate_u_pnl_by_k"][k_sigma]) for r in day_results])

            win_out["k_sigma_results"][str(k_sigma)] = dict(
                delta_naive=naive,
                delta_lag1_autocorr=round(ac1, 4) if not np.isnan(ac1) else None,
                delta_hac_p_by_maxlags=hac,
                delta_significance=sig,
                bug7_exit_reason_decomposition=bug7,
                bug1_session_end_accounting=dict(baseline=bug1_base, candidate=bug1_cand),
            )
            print(f"  k_sigma={k_sigma}: mean_delta={naive['mean']:.3f} p_naive={naive['p_value_t']:.4f} "
                  f"lag1={ac1:.3f} -> {sig['label']} | preempted={bug7['n_preempted_by_new_trigger']}/"
                  f"{bug7['n_total_eligible_trades']} ({bug7['pct_preempted']}%) "
                  f"preempted_delta_sum={bug7['preempted_delta_sum_pnl']}")

            if label != "w83":
                pooled_holdout_delta_by_k[k_sigma].extend(deltas)
                pooled_holdout_records_by_k[k_sigma].extend(all_records)
                pooled_holdout_rows_by_k[k_sigma].extend(
                    [(r["day"], r["baseline_net"], r["baseline_net"] + r["open_pos_baseline_u_pnl"]) for r in day_results]
                )

        out["windows"][label] = win_out
        print()

    # ---- pooled across the 3 independent holdout windows (n=182 days,
    # structurally immune to defects A/B per handoff Sec.2.1b; w83 reported
    # separately above, matching this research line's established
    # convention -- see tmf_struct_disabled_decouple_live_smart_w83.py vs
    # _holdout3.py being two separate scripts/outputs, not pooled) ----
    out["pooled_3holdout"] = dict(
        note="julsep25+octdec25+janmar26, n=182 independent trading days",
        k_sigma_results={},
    )
    for k_sigma in K_SIGMA_SWEEP:
        deltas = pooled_holdout_delta_by_k[k_sigma]
        naive = one_sample_test(deltas)
        ac1 = lag1_autocorr(deltas)
        hac = {str(L): newey_west_test(deltas, L) for L in (1, 5, 10, 20)}
        hac_clean = {k: v for k, v in hac.items() if not (isinstance(v, float) and np.isnan(v))}
        sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"],
                                     hac_p_by_maxlags=hac_clean or {"1": naive["p_value_t"]})
        bug7 = bug7_breakdown(pooled_holdout_records_by_k[k_sigma])
        out["pooled_3holdout"]["k_sigma_results"][str(k_sigma)] = dict(
            n_days=len(deltas),
            delta_naive=naive,
            delta_lag1_autocorr=round(ac1, 4) if not np.isnan(ac1) else None,
            delta_hac_p_by_maxlags=hac,
            delta_significance=sig,
            bug7_exit_reason_decomposition=bug7,
        )
        print(f"POOLED 3-holdout k_sigma={k_sigma}: n={len(deltas)} mean_delta={naive['mean']:.3f} "
              f"p_naive={naive['p_value_t']:.4f} lag1={ac1:.3f} -> {sig['label']}")
        print(f"  BUG-7: {bug7}")

    out["elapsed_s"] = round(time.time() - t_start, 1)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
