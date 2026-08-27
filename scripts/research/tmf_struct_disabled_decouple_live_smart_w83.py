#!/usr/bin/env python3
"""Decouple struct_disabled from always_lo on the w83 window (2026-04-01..
2026-07-31, source tx_1m_fullnight_cache_full.json), replicating the EXACT
methodology of scripts/research/tmf_struct_disabled_decouple_live_smart_holdout3.py
(the already-completed, already-registered 3-holdout run for
H-STRUCT-DISABLED-DECOUPLED in config/research.yaml) -- the only new axis is
the window.

This is the missing leg named in that hypothesis's own next_experiment:
"(2) windows = w83 + julsep25 + octdec25 + janmar26 ... (4) if w83 -- must
first handle defect C (patch_nq_gate_for_backfill(lookback_days=60) -> use
500), otherwise the OOS first 50 days are structurally zero-trade."

Two things make w83 different from the 3 holdouts and require extra care
(both handled below, NOT by hand-rolling anything new):

1. w83 DOES have 00:00-04:59 post-midnight bars (unlike the 3 holdouts,
   which are structurally immune to defects A/B) -- so it actually
   exercises the fixed cache_store.load_day()/_chronological()/
   bar_timestamps() path. We use ONLY those fixed helpers -- never
   f"{day}T{t}:00..." string construction (see docs/tmf-channel-research-
   handoff-20260811.md Sec.5d anti-pattern warning).
2. Defect C (lookback_days=60 structurally truncates Yahoo NQ history to
   the last 60 days from *today*, zeroing baseline-arm trades for days
   older than that cutoff). We call patch_nq_gate_for_backfill(
   lookback_days=500) and explicitly verify per-day NQ coverage by
   counting how many days have zero baseline-arm trades (should be near
   zero, not the ~50-of-66 structural-zero pattern the defect note
   describes for OOS with lookback_days=60).

Read-only w.r.t. src/order/, src/tmf_channel/, config/order.yaml,
config/strategy.yaml, .env -- this script only imports them.

Outputs reports/research/channel_lab/tmf_struct_disabled_decouple_live_smart_w83_result.json
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import scipy.stats as sps
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from slow_cell_significance_helper import classify_significance  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

OUT = LAB / "tmf_struct_disabled_decouple_live_smart_w83_result.json"

SOURCE = "tx_1m_fullnight_cache_full.json"
LOOKBACK_DAYS = 500  # defect C: default/60 truncates Yahoo NQ to today-60d,
# which structurally zeroes out baseline-arm trading for the older ~50 of
# w83's 83 days (2026-04-01..2026-07-31). 500 matches the two known-good
# precedents named in the pre-registered next_experiment
# (tmf_channel_side_level_cell_rescan, tx_day_expandupdn_retune_scan).

# ---------------------------------------------------------------------------
# BUG-2 guard: confirm we are NOT touching the live-smart rail-selection
# functions at all (unlike the always_lo family, which monkeypatches these
# to spot+lo / spot-lo).
# ---------------------------------------------------------------------------
_ORIG_ABOVE = ce._pick_hang_above
_ORIG_BELOW = ce._pick_hang_below


def assert_unpatched_smart_baseline() -> None:
    assert ce._pick_hang_above is _ORIG_ABOVE, "live-smart _pick_hang_above got patched"
    assert ce._pick_hang_below is _ORIG_BELOW, "live-smart _pick_hang_below got patched"


def load_arrays(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)  # never hand-roll f"{day}T{t}"
    return O, H, L, C, V, T


def verify_w83_shape_and_exposure(source: str) -> dict:
    """Data-shape check per handoff Sec.2.2 lesson #2 (verify shape before
    statistics): confirm bar order is monotonic non-decreasing per day (i.e.
    _chronological() is actually doing its job), and report how many bars
    per day fall in 00:00-04:59 -- w83 is EXPECTED to have a large nonzero
    count here (unlike the 3 holdouts, which have zero -- see handoff
    Sec.2.1b), which is exactly why this window exercises defects A/B and
    needs the fixed load_day()/bar_timestamps() path."""
    days = list_days(source=source)
    midnight_bars = 0
    total_bars = 0
    non_monotonic_days = []
    tmin, tmax = None, None
    for d in days:
        rows = load_day(d, source=source)
        cals = [r.get("cal") for r in rows]
        ts = bar_timestamps(d, rows, source=source)
        total_bars += len(rows)
        for r in rows:
            t = r["t"]
            if t < "05:00":
                midnight_bars += 1
            tmin = t if tmin is None or t < tmin else tmin
            tmax = t if tmax is None or t > tmax else tmax
        # true chronological order must be monotonic on the REAL instants,
        # not on the raw 'HH:MM' clock-time string (which is what defect A
        # broke: lexicographic order hoists 00:00-04:59 to the front).
        if ts != sorted(ts):
            non_monotonic_days.append(d)
    return dict(
        source=source,
        n_days=len(days),
        total_bars=total_bars,
        midnight_bars=midnight_bars,
        avg_midnight_bars_per_day=round(midnight_bars / len(days), 1) if days else 0,
        non_monotonic_days=non_monotonic_days,
        t_min=tmin,
        t_max=tmax,
        chronological_order_ok=(not non_monotonic_days),
        exposed_to_defects_ab=(midnight_bars > 0),
    )


def run_day(arr, gate, recipe_base, vix, *, struct_disabled: bool):
    assert_unpatched_smart_baseline()
    recipe = deepcopy(recipe_base)
    recipe["session_side_gate"] = gate
    recipe["struct_disabled"] = bool(struct_disabled)
    O, H, L, C, V, T = arr
    trades, events, ws, wl, rvol, regime, open_pos = simulate(
        O, H, L, C, V, T, recipe, vix_delta=vix
    )
    assert_unpatched_smart_baseline()
    return trades, open_pos


def one_sample_test(x):
    x = np.asarray(x, dtype=float)
    n = len(x)
    m = float(np.mean(x)) if n else 0.0
    sd = float(np.std(x, ddof=1)) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else float("nan")
    t_stat = m / se if se > 0 else float("nan")
    df = n - 1
    p_t = float(2.0 * sps.t.sf(abs(t_stat), df)) if se > 0 and df > 0 else float("nan")
    return dict(n=n, mean=round(m, 4), sd=round(sd, 4), t=None if np.isnan(t_stat) else round(t_stat, 4),
                p_value_t=p_t)


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


def bug7_exit_reason_breakdown(trades: list[dict]) -> dict:
    by_reason: dict[str, dict] = {}
    for tr in trades:
        why_raw = str(tr.get("why", "?"))
        cat = why_raw.split("|", 1)[0]
        d = by_reason.setdefault(cat, {"n": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += float(tr["pnl"])
    for d in by_reason.values():
        d["pnl"] = round(d["pnl"], 1)
        d["pnl_per_trade"] = round(d["pnl"] / d["n"], 2) if d["n"] else 0.0
    total_pnl = round(sum(float(t["pnl"]) for t in trades), 1)
    return dict(total_pnl=total_pnl, n_trades=len(trades), by_reason_category=by_reason)


def middle_80pct_contribution(trades: list[dict]) -> dict:
    pnls = sorted(float(t["pnl"]) for t in trades)
    n = len(pnls)
    if n == 0:
        return dict(n_total=0, n_middle=0, middle_pnl=0.0, middle_pnl_per_trade=0.0,
                     total_pnl=0.0, middle_share_of_total=None)
    k = int(round(n * 0.10))
    middle = pnls[k: n - k] if n - 2 * k > 0 else pnls
    total = round(sum(pnls), 1)
    mid_sum = round(sum(middle), 1)
    return dict(
        n_total=n,
        n_trimmed_each_tail=k,
        n_middle=len(middle),
        middle_pnl=mid_sum,
        middle_pnl_per_trade=round(mid_sum / len(middle), 3) if middle else 0.0,
        total_pnl=total,
        middle_share_of_total=(round(mid_sum / total, 3) if total not in (0, 0.0) else None),
    )


def bug1_session_end_accounting(rows_trades_openpos: list[tuple[str, list[dict], dict | None]]) -> dict:
    n_days_with_open_pos = 0
    trades_only_net = 0.0
    honest_net = 0.0
    open_pos_days = []
    for day, trades, open_pos in rows_trades_openpos:
        day_trades_net = sum(float(t["pnl"]) for t in trades)
        trades_only_net += day_trades_net
        day_honest = day_trades_net
        if open_pos is not None:
            n_days_with_open_pos += 1
            u = float(open_pos.get("u_pnl") or 0.0)
            day_honest += u
            open_pos_days.append(dict(day=day, s=open_pos.get("s"), n=open_pos.get("n"),
                                       u_pnl=round(u, 1)))
        honest_net += day_honest
    return dict(
        n_days=len(rows_trades_openpos),
        n_days_with_open_pos_at_window_end=n_days_with_open_pos,
        trades_only_net=round(trades_only_net, 1),
        honest_net_incl_open_pos=round(honest_net, 1),
        gap=round(honest_net - trades_only_net, 1),
        open_pos_days=open_pos_days,
    )


def top_share_dominance_check(deltas_by_day: list[tuple[str, float]]) -> dict:
    """Single-day-domination check, replicated for w83 (already done for the
    3 holdouts per next_experiment point (5)/(3) -- 'pooled middle-80%-of-
    trades stayed positive, no tail-concentration pathology'). Reports what
    share of the summed delta the single largest-magnitude day accounts
    for, and the sign of that day, so a reader can judge whether the result
    is driven by one outlier session."""
    if not deltas_by_day:
        return dict(n_days=0, top_day=None, top_day_delta=0.0, sum_delta=0.0, top_share_of_sum=None)
    sum_delta = sum(d for _, d in deltas_by_day)
    top_day, top_delta = max(deltas_by_day, key=lambda kv: abs(kv[1]))
    share = (top_delta / sum_delta) if sum_delta not in (0, 0.0) else None
    return dict(
        n_days=len(deltas_by_day),
        top_day=top_day,
        top_day_delta=round(top_delta, 1),
        sum_delta=round(sum_delta, 1),
        top_share_of_sum=(round(share, 4) if share is not None else None),
    )


def main() -> None:
    t_start = time.time()
    patch_nq_gate_for_backfill(lookback_days=LOOKBACK_DAYS)  # avoid defect-C truncation
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    shape = verify_w83_shape_and_exposure(SOURCE)
    print(f"=== w83 ({SOURCE}) === chronological_order_ok={shape['chronological_order_ok']} "
          f"n_days={shape['n_days']} midnight_bars={shape['midnight_bars']} "
          f"(avg/day={shape['avg_midnight_bars_per_day']}) exposed_to_defects_ab="
          f"{shape['exposed_to_defects_ab']} t_range=[{shape['t_min']},{shape['t_max']}]")
    if not shape["chronological_order_ok"]:
        raise SystemExit(
            f"!!! non-monotonic bar order on {len(shape['non_monotonic_days'])} days -- "
            "defect A appears NOT fixed on this checkout, aborting"
        )

    out = dict(
        title="struct_disabled decoupled from always_lo, on live-smart "
        "(_pick_hang_above/_pick_hang_below unpatched) hang-rail baseline, "
        "w83 window (2026-04-01..2026-07-31) -- the missing leg named in "
        "H-STRUCT-DISABLED-DECOUPLED's next_experiment, matching the "
        "already-completed 3-holdout methodology exactly",
        recipe_version=recipe_base.get("recipe_version"),
        source=SOURCE,
        lookback_days=LOOKBACK_DAYS,
        shape_and_exposure_check=shape,
    )

    days = list_days(source=SOURCE)
    deltas = []
    deltas_by_day = []
    base_rows, cand_rows = [], []
    zero_trade_days_baseline = []
    for d in days:
        arr = load_arrays(d, SOURCE)
        if arr is None:
            continue
        gate = continuous_gate_for_day(d, arr[5], source=SOURCE)
        base_trades, base_open = run_day(arr, gate, recipe_base, vix, struct_disabled=False)
        cand_trades, cand_open = run_day(arr, gate, recipe_base, vix, struct_disabled=True)
        base_net = sum(float(t["pnl"]) for t in base_trades)
        cand_net = sum(float(t["pnl"]) for t in cand_trades)
        deltas.append(cand_net - base_net)
        deltas_by_day.append((d, cand_net - base_net))
        base_rows.append((d, base_trades, base_open))
        cand_rows.append((d, cand_trades, cand_open))
        if len(base_trades) == 0:
            zero_trade_days_baseline.append(d)

    # ---- explicit NQ coverage proof: count of days with zero baseline-arm
    # trades. Defect C's signature (lookback_days=60 on this window) was
    # documented as ~50-of-66 structurally-zero OOS days; with
    # lookback_days=500 this count should be near-zero (some genuine
    # zero-trade days are expected -- flat/quiet sessions -- but not a
    # majority-of-window structural pattern). ----
    n_zero = len(zero_trade_days_baseline)
    coverage = dict(
        n_days_total=len(days),
        n_days_zero_baseline_trades=n_zero,
        pct_zero_baseline_trades=round(100.0 * n_zero / len(days), 1) if days else None,
        zero_trade_days=zero_trade_days_baseline,
        verdict=(
            "coverage looks genuine (near-zero structural-zero days)"
            if n_zero <= max(3, round(0.10 * len(days)))
            else "SUSPICIOUS -- resembles the lookback_days=60 structural-zero "
            "pattern (~50-of-66 days); investigate before trusting results"
        ),
    )
    out["nq_coverage_check"] = coverage
    print(f"NQ coverage check: {n_zero}/{len(days)} days with zero baseline-arm trades "
          f"({coverage['pct_zero_baseline_trades']}%) -> {coverage['verdict']}")
    if zero_trade_days_baseline:
        print(f"  zero-trade days: {zero_trade_days_baseline}")

    naive = one_sample_test(deltas)
    ac1 = lag1_autocorr(deltas)
    hac = {str(L): newey_west_test(deltas, L) for L in (1, 5, 10, 20)}
    hac_clean = {k: v for k, v in hac.items() if not (isinstance(v, float) and np.isnan(v))}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"],
                                 hac_p_by_maxlags=hac_clean or {"1": naive["p_value_t"]})

    base_bug7 = bug7_exit_reason_breakdown([t for _, tr, _ in base_rows for t in tr])
    cand_bug7 = bug7_exit_reason_breakdown([t for _, tr, _ in cand_rows for t in tr])
    cand_mid80 = middle_80pct_contribution([t for _, tr, _ in cand_rows for t in tr])
    base_mid80 = middle_80pct_contribution([t for _, tr, _ in base_rows for t in tr])
    bug1_base = bug1_session_end_accounting(base_rows)
    bug1_cand = bug1_session_end_accounting(cand_rows)
    top_share = top_share_dominance_check(deltas_by_day)

    n_struct_break_baseline = sum(1 for _, tr, _ in base_rows for t in tr
                                   if str(t.get("why", "")).split("|", 1)[0] == "struct_break")
    n_struct_break_candidate = sum(1 for _, tr, _ in cand_rows for t in tr
                                    if str(t.get("why", "")).split("|", 1)[0] == "struct_break")
    mechanism_sanity = dict(
        n_struct_break_exits_baseline=n_struct_break_baseline,
        n_struct_break_exits_candidate=n_struct_break_candidate,
        ok=(n_struct_break_baseline > 0 and n_struct_break_candidate == 0),
    )
    out["mechanism_sanity_check"] = mechanism_sanity

    out["w83"] = dict(
        n_days=naive["n"],
        baseline_trades_net=round(sum(float(t["pnl"]) for _, tr, _ in base_rows for t in tr), 1),
        candidate_trades_net=round(sum(float(t["pnl"]) for _, tr, _ in cand_rows for t in tr), 1),
        baseline_n_trades=sum(len(tr) for _, tr, _ in base_rows),
        candidate_n_trades=sum(len(tr) for _, tr, _ in cand_rows),
        delta_naive=naive,
        delta_lag1_autocorr=round(ac1, 4) if not np.isnan(ac1) else None,
        delta_hac_p_by_maxlags=hac,
        delta_significance=sig,
        bug1_session_end_accounting=dict(baseline=bug1_base, candidate=bug1_cand),
        bug7_exit_reason_baseline=base_bug7,
        bug7_exit_reason_candidate=cand_bug7,
        middle80pct_baseline=base_mid80,
        middle80pct_candidate=cand_mid80,
        top_day_dominance_check=top_share,
    )

    print(f"=== w83 (n={naive['n']}) ===")
    print(f"mean_delta={naive['mean']:.3f} p_naive={naive['p_value_t']} lag1={ac1:.3f} "
          f"hac_p={hac} -> {sig['label']}")
    print(f"baseline net={out['w83']['baseline_trades_net']} n_trades={out['w83']['baseline_n_trades']} | "
          f"candidate net={out['w83']['candidate_trades_net']} n_trades={out['w83']['candidate_n_trades']}")
    print(f"mechanism sanity: struct_break exits baseline={n_struct_break_baseline} "
          f"candidate={n_struct_break_candidate} ok={mechanism_sanity['ok']}")
    print(f"middle-80% candidate: {cand_mid80}")
    print(f"middle-80% baseline:  {base_mid80}")
    print(f"top-day dominance: {top_share}")

    out["elapsed_s"] = round(time.time() - t_start, 1)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
