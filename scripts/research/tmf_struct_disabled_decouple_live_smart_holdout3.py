#!/usr/bin/env python3
"""Decouple struct_disabled from always_lo: run the LIVE-smart hang-rail
baseline (specialized_cell_book()/PAPER_RECIPE, i.e. real
_pick_hang_above/_pick_hang_below structure selection -- NOT the always_lo
monkeypatch) twice, toggling ONLY recipe['struct_disabled'], on the three
holdout windows that are structurally immune to defects A/B (no
00:00-04:59 bars -- see docs/tmf-channel-research-handoff-20260811.md
Sec.2.1b, reconfirmed by this script at runtime, not just trusted from the
handoff).

This directly answers handoff Sec.3 row 3 warning (a) / Sec.6 todo #1: the
published -11.48 pt/day three-window number bundles always_lo (independently
found to have flipped negative, IS -42.14 p=0.647) together with
struct_disabled, so it cannot be attributed to either alone. This script
isolates struct_disabled by NOT touching ce._pick_hang_above/_pick_hang_below
at all -- the only difference between the two runs is
recipe['struct_disabled'].

Read-only w.r.t. src/order/, src/tmf_channel/, config/order.yaml,
config/strategy.yaml, .env -- this script only imports them.

Outputs reports/research/channel_lab/tmf_struct_disabled_decouple_live_smart_holdout3_result.json
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

OUT = LAB / "tmf_struct_disabled_decouple_live_smart_holdout3_result.json"

HOLDOUT_SOURCES = {
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

# ---------------------------------------------------------------------------
# BUG-2 guard: confirm we are NOT touching the live-smart rail-selection
# functions at all (unlike tmf_always_lo_struct_disabled_holdout3_validate.py
# / tmf_always_lo_struct_disabled_test.py, which monkeypatch these to
# spot+lo / spot-lo). This script must never call these patchers.
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


def verify_holdout_immune_to_defects_ab(source: str) -> dict:
    """Per handoff Sec.2.1b's stated verification method: confirm the cache
    has zero 00:00-04:59 bars (defect A has nothing to reorder, defect B has
    nothing to mis-timestamp), and that bar order is monotonic non-decreasing
    within each day (data-shape check per Sec.2.2 lesson #2)."""
    days = list_days(source=source)
    midnight_bars = 0
    total_bars = 0
    non_monotonic_days = []
    tmin, tmax = None, None
    for d in days:
        rows = load_day(d, source=source)
        ts = [r["t"] for r in rows]
        total_bars += len(ts)
        for t in ts:
            if t < "05:00":
                midnight_bars += 1
            tmin = t if tmin is None or t < tmin else tmin
            tmax = t if tmax is None or t > tmax else tmax
        if ts != sorted(ts):
            non_monotonic_days.append(d)
    return dict(
        source=source,
        n_days=len(days),
        total_bars=total_bars,
        midnight_bars=midnight_bars,
        non_monotonic_days=non_monotonic_days,
        t_min=tmin,
        t_max=tmax,
        immune=(midnight_bars == 0 and not non_monotonic_days),
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
    """Bucket by exit-reason CATEGORY (prefix before '|' -- e.g. 'trail|63->17'
    and 'trail|54->10' both bucket to 'trail'; the raw why-string is near-unique
    per trade for trail/stop/vix_*_flat reasons since it embeds the actual
    peak/giveback/stop level, which would make a literal-string breakdown
    useless (~every row its own singleton bucket))."""
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
    """Sort trades by pnl, drop the top 10% and bottom 10% (by rank, not by
    sign), report the middle 80%'s pnl contribution -- same diagnostic the
    handoff used to show always_lo's P&L was tail-concentrated (Sec.3 row 3
    warning (b))."""
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
    """For each day, compare trades-only net vs honest net (trades net +
    end-of-window open position's unrealized pnl). PAPER_RECIPE has
    eod_flatten=False (poll must not flatten mid-session); the 3 holdout
    caches truncate each session at 23:59 with no continuation into
    00:00-04:59, so any lot still open at bar[-1] is genuinely dropped from
    `net` unless we add open_pos['u_pnl'] back in ourselves."""
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


def main() -> None:
    t_start = time.time()
    patch_nq_gate_for_backfill(lookback_days=500)  # avoid defect-C truncation
    vix = load_vixtwn_delta() or {}
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()

    out = dict(
        title="struct_disabled decoupled from always_lo, on live-smart "
        "(_pick_hang_above/_pick_hang_below unpatched) hang-rail baseline, "
        "3 holdout windows",
        recipe_version=recipe_base.get("recipe_version"),
        windows={},
    )

    pooled_delta = []
    pooled_base_trades = []
    pooled_cand_trades = []
    pooled_base_rows = []  # (day, trades, open_pos) for BUG-1, baseline
    pooled_cand_rows = []  # (day, trades, open_pos) for BUG-1, candidate

    for label, source in HOLDOUT_SOURCES.items():
        immune = verify_holdout_immune_to_defects_ab(source)
        print(f"=== {label} ({source}) === immune_to_A_B={immune['immune']} "
              f"n_days={immune['n_days']} midnight_bars={immune['midnight_bars']} "
              f"t_range=[{immune['t_min']},{immune['t_max']}]")
        if not immune["immune"]:
            print(f"  !!! {label} FAILED immunity check -- see immune dict for detail")

        days = list_days(source=source)
        deltas = []
        base_rows, cand_rows = [], []
        for d in days:
            arr = load_arrays(d, source)
            if arr is None:
                continue
            gate = continuous_gate_for_day(d, arr[5], source=source)
            base_trades, base_open = run_day(arr, gate, recipe_base, vix, struct_disabled=False)
            cand_trades, cand_open = run_day(arr, gate, recipe_base, vix, struct_disabled=True)
            base_net = sum(float(t["pnl"]) for t in base_trades)
            cand_net = sum(float(t["pnl"]) for t in cand_trades)
            deltas.append(cand_net - base_net)
            base_rows.append((d, base_trades, base_open))
            cand_rows.append((d, cand_trades, cand_open))
            pooled_base_trades.extend(base_trades)
            pooled_cand_trades.extend(cand_trades)

        pooled_delta.extend(deltas)
        pooled_base_rows.extend(base_rows)
        pooled_cand_rows.extend(cand_rows)

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

        out["windows"][label] = dict(
            immunity_check=immune,
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
        )
        print(f"  n={naive['n']} mean_delta={naive['mean']:.2f} p_naive={naive['p_value_t']} "
              f"lag1={ac1:.3f} hac_p={hac} -> {sig['label']}")
        print(f"  baseline net={out['windows'][label]['baseline_trades_net']} "
              f"n_trades={out['windows'][label]['baseline_n_trades']} | "
              f"candidate net={out['windows'][label]['candidate_trades_net']} "
              f"n_trades={out['windows'][label]['candidate_n_trades']}")
        print()

    # ---- pooled across all 3 holdout windows (n=182, independent calendar
    # quarters, never touched by IS(22d)/OOS(66d) tuning) ----
    naive = one_sample_test(pooled_delta)
    ac1 = lag1_autocorr(pooled_delta)
    hac = {str(L): newey_west_test(pooled_delta, L) for L in (1, 5, 10, 20)}
    hac_clean = {k: v for k, v in hac.items() if not (isinstance(v, float) and np.isnan(v))}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"],
                                 hac_p_by_maxlags=hac_clean or {"1": naive["p_value_t"]})

    pooled_base_bug7 = bug7_exit_reason_breakdown(pooled_base_trades)
    pooled_cand_bug7 = bug7_exit_reason_breakdown(pooled_cand_trades)
    pooled_cand_mid80 = middle_80pct_contribution(pooled_cand_trades)
    pooled_base_mid80 = middle_80pct_contribution(pooled_base_trades)
    pooled_bug1_base = bug1_session_end_accounting(pooled_base_rows)
    pooled_bug1_cand = bug1_session_end_accounting(pooled_cand_rows)

    # BUG-2-adjacent mechanism sanity: struct_disabled must remove ONLY
    # struct_break exits, nothing else -- candidate trades should contain
    # zero struct_break exits; baseline should contain a nonzero number
    # (otherwise the toggle isn't actually engaging the mechanism under test).
    n_struct_break_baseline = sum(1 for t in pooled_base_trades if str(t.get("why", "")).split("|", 1)[0] == "struct_break")
    n_struct_break_candidate = sum(1 for t in pooled_cand_trades if str(t.get("why", "")).split("|", 1)[0] == "struct_break")
    mechanism_sanity = dict(
        n_struct_break_exits_baseline=n_struct_break_baseline,
        n_struct_break_exits_candidate=n_struct_break_candidate,
        ok=(n_struct_break_baseline > 0 and n_struct_break_candidate == 0),
    )
    out["mechanism_sanity_check"] = mechanism_sanity
    print(f"mechanism sanity: struct_break exits baseline={n_struct_break_baseline} "
          f"candidate={n_struct_break_candidate} ok={mechanism_sanity['ok']}")

    out["pooled_3holdout"] = dict(
        note="julsep25+octdec25+janmar26 concatenated, n=182 independent trading "
        "days, structurally immune to defects A/B (verified at runtime above)",
        n_days=len(pooled_delta),
        delta_naive=naive,
        delta_lag1_autocorr=round(ac1, 4) if not np.isnan(ac1) else None,
        delta_hac_p_by_maxlags=hac,
        delta_significance=sig,
        bug1_session_end_accounting=dict(baseline=pooled_bug1_base, candidate=pooled_bug1_cand),
        bug7_exit_reason_baseline=pooled_base_bug7,
        bug7_exit_reason_candidate=pooled_cand_bug7,
        middle80pct_baseline=pooled_base_mid80,
        middle80pct_candidate=pooled_cand_mid80,
    )
    print(f"=== POOLED 3-holdout (n={len(pooled_delta)}) ===")
    print(f"mean_delta={naive['mean']:.3f} p_naive={naive['p_value_t']} lag1={ac1:.3f} "
          f"hac_p={hac} -> {sig['label']}")
    print(f"BUG-1 baseline: {pooled_bug1_base}")
    print(f"BUG-1 candidate: {pooled_bug1_cand}")
    print(f"BUG-7 candidate exit reasons: {pooled_cand_bug7['by_reason_category']}")
    print(f"BUG-7 baseline exit reasons:  {pooled_base_bug7['by_reason_category']}")
    print(f"middle-80% candidate: {pooled_cand_mid80}")
    print(f"middle-80% baseline:  {pooled_base_mid80}")

    out["elapsed_s"] = round(time.time() - t_start, 1)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
