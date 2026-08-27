#!/usr/bin/env python3
"""Item #4: contract-regime confirm-bars isolated test (overnight research run).

Context (read first): struct_confirm_bars_design_lab.py / _variant.py ran an
all-regime-mixed campaign on the OLD recipe (Final v1.1.3, far_cover 65/105,
NOT the current v1.4.0) and found requiring N consecutive bar-close
confirmations of the struct_break adverse-swing condition reduces struct
losses but shifts them into the stop-loss bucket (net STILL improves
monotonically toward N=inf, i.e. STRUCT_DISABLED was the actual best in that
campaign: -16030 (N1) -> -10667 (N2) -> -9575 (N3) -> -8959 (disabled), 83
days). NOT adopted into config/strategy.yaml.

This script asks the narrower question assigned: does confirm-bars behave
differently specifically WITHIN the "contract" regime (dominant post-v3
regime) on the CURRENT PAPER_RECIPE (v1.4.0)?

Method (APPROXIMATION, not a true re-simulation — causal_engine.py has no
struct_confirm_mode/struct_confirm_bars knobs; modifying it is out of scope
tonight):
  1. Run current PAPER_RECIPE via tmf_channel.engine.simulate() across all 4
     available bar caches (w83 + 3 holdouts = 265 days) to get the REAL trade
     list, then isolate struct_break exits whose regime_e == "contract".
  2. For each such trade, starting at the ORIGINAL exit bar, bar-walk FORWARD
     using the same day's real 1-minute OHLC bars (not synthetic), replaying
     the engine's own stop_pts/trail_arm/trail_giveback/struct_exit_look
     logic bar-by-bar: stop/trail touch checks use the bar's actual traded
     H/L extremes (real ticks, not close-only — satisfies the "check true
     touch level" rule for those two conditions); the struct bar-close
     confirmation streak counter uses bar CLOSE (this literally IS what
     bar_close confirm mode evaluates — "once per bar, at that bar's last
     real tick" ~= close). This finds, for N=2 and N=3, what the ACTUAL
     alternate exit reason/price/pnl would have been, not just "did it
     eventually re-breach."
  3. Day-clustered: pnl delta aggregated per calendar day, then a one-sample
     t-test across days (n = number of contract-struct-break-bearing days).

Caveat printed in output: order-of-events WITHIN a single bar (stop vs
struct both eligible in the same bar) is approximated by evaluating stop
before trail before struct, matching engine code order — but exact
sub-minute sequencing is not resolved without full tick replay of every
forward-walked bar (in scope for a spot-check subsample only, see PART 2).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, "src")

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import simulate, load_vixtwn_delta, COST  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402

CACHES = [
    "tx_1m_fullnight_cache_full.json",
    "tx_1m_janmar_holdout_cache.json",
    "tx_1m_julsep_holdout_cache.json",
    "tx_1m_octdec_holdout_cache.json",
]

LOOK = int(PAPER_RECIPE.get("struct_exit_look", 12))
MIN_SMART = int(PAPER_RECIPE.get("min_hold_before_smart", 3))
MIN_STOP = int(PAPER_RECIPE.get("min_hold_before_stop", 12))
STOP_PTS = float(PAPER_RECIPE.get("stop_pts", 150.0))
ARM = float(PAPER_RECIPE.get("trail_arm_pts", 50.0))
GIVE = float(PAPER_RECIPE.get("trail_giveback_pts", 40.0))


def day_arrays(rows, day):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def confirm_walk(side, ep0, eb, t0, H, L, C, n_confirm):
    """Bar-forward walk from t0 (original struct exit bar) replaying
    stop/trail/struct(confirm N) each bar. Returns dict with alt exit info.

    peak_fav is seeded from the REAL bar range [eb, t0) (favorable extreme
    reached before the original struct trigger bar) — required because
    trail_arm/giveback depends on the running max favorable excursion since
    entry, not just what happens after t0. streak is correctly seeded at 0:
    the original recipe uses tick_pierce (fires on ANY tick breach), so the
    fact the real trade survived to t0 proves no earlier bar's CLOSE (a
    subset of its ticks) breached either — bar-close-confirm streak has no
    hidden history to inherit.
    """
    peak_fav = None
    for i in range(eb, t0):
        fx = (H[i] - ep0) if side == "L" else (ep0 - L[i])
        peak_fav = fx if peak_fav is None else max(peak_fav, fx)
    streak = 0
    for t in range(t0, len(C)):
        hold_b = t - eb
        fav_extreme = (H[t] - ep0) if side == "L" else (ep0 - L[t])
        peak_fav = fav_extreme if peak_fav is None else max(peak_fav, fav_extreme)
        adverse_extreme = (ep0 - L[t]) if side == "L" else (H[t] - ep0)
        # stop
        if hold_b >= MIN_STOP and adverse_extreme >= STOP_PTS:
            xp = ep0 - STOP_PTS if side == "L" else ep0 + STOP_PTS
            pnl = ((xp - ep0) if side == "L" else (ep0 - xp)) - COST
            return dict(reason="stop", xb=t, xp=xp, pnl=pnl, hold=hold_b)
        # trail
        fav_now_close = (C[t] - ep0) if side == "L" else (ep0 - C[t])
        if hold_b >= MIN_SMART and peak_fav >= ARM and (peak_fav - fav_now_close) >= GIVE:
            xp = C[t]
            pnl = ((xp - ep0) if side == "L" else (ep0 - xp)) - COST
            return dict(reason="trail", xb=t, xp=xp, pnl=pnl, hold=hold_b)
        # struct (bar-close confirm streak)
        a0 = max(0, t - LOOK)
        if hold_b >= MIN_SMART and t - a0 >= 3:
            if side == "L":
                window = L[a0:t]
                swing = min(window) if window else None
                breach = swing is not None and C[t] < swing
            else:
                window = H[a0:t]
                swing = max(window) if window else None
                breach = swing is not None and C[t] > swing
            if breach:
                streak += 1
                if streak >= n_confirm:
                    xp = C[t]
                    pnl = ((xp - ep0) if side == "L" else (ep0 - xp)) - COST
                    return dict(reason="struct", xb=t, xp=xp, pnl=pnl, hold=hold_b)
            else:
                streak = 0
    return dict(reason="unresolved_eod", xb=len(C) - 1, xp=C[-1], pnl=None, hold=len(C) - 1 - eb)


def tstat(diffs):
    n = len(diffs)
    if n < 2:
        return None, None, n
    m = mean(diffs)
    sd = stdev(diffs)
    if sd == 0:
        return m, None, n
    t = m / (sd / (n ** 0.5))
    return m, t, n


def main():
    vix = load_vixtwn_delta() or {}
    all_trades = []
    per_day_bars = {}
    n_days = 0

    for cache in CACHES:
        days = list_days(source=cache)
        for d in days:
            rows = load_day(d, source=cache)
            if not rows:
                continue
            O, H, L, C, V, T = day_arrays(rows, d)
            trades, *_ = simulate(O, H, L, C, V, T, PAPER_RECIPE, vix_delta=vix)
            n_days += 1
            key = f"{cache}:{d}"
            per_day_bars[key] = (H, L, C)
            for t in trades:
                t["_day"] = key
                all_trades.append(t)

    contract_struct = [
        t for t in all_trades
        if str(t["why"]).split("|", 1)[0] == "struct_break" and t.get("regime_e") == "contract"
    ]
    print(f"n_days={n_days}  contract struct_break trades={len(contract_struct)}")

    results = {2: [], 3: []}
    for N in (2, 3):
        per_day_delta = defaultdict(float)
        reason_counts = defaultdict(int)
        n_unresolved = 0
        n_better = n_worse = n_same_sign = 0
        for tr in contract_struct:
            H, L, C = per_day_bars[tr["_day"]]
            side = tr["s"]
            ep0 = tr["ep"]
            eb = tr["eb"]
            t0 = tr["xb"]  # original exit bar; bar-walk forward from here
            alt = confirm_walk(side, ep0, eb, t0, H, L, C, N)
            reason_counts[alt["reason"]] += 1
            if alt["pnl"] is None:
                n_unresolved += 1
                continue
            delta = alt["pnl"] - tr["pnl"]
            per_day_delta[tr["_day"]] += delta
            if delta > 0:
                n_better += 1
            elif delta < 0:
                n_worse += 1
            results[N].append(dict(day=tr["_day"], orig_pnl=tr["pnl"], alt_pnl=alt["pnl"],
                                    alt_reason=alt["reason"], delta=delta, hold_orig=tr["hold"],
                                    hold_alt=alt["hold"]))

        diffs = list(per_day_delta.values())
        m, t_stat, n_d = tstat(diffs)
        print(f"\n=== N={N} confirm bars ===")
        print(f"  n_trades={len(contract_struct)}  unresolved(eod-of-cache-day)={n_unresolved}")
        print(f"  alt exit reasons: {dict(reason_counts)}")
        print(f"  trade-level: better={n_better} worse={n_worse} "
              f"(of {len(contract_struct)-n_unresolved} resolved)")
        total_delta = sum(t["pnl"] for t in contract_struct if True) * 0  # placeholder
        resolved_orig_sum = sum(tr["pnl"] for tr in contract_struct)
        resolved_alt_sum = resolved_orig_sum + sum(diffs)
        print(f"  day-clustered: n_days_with_contract_struct={n_d}  mean_delta/day={m:.2f}  "
              f"t={t_stat if t_stat is None else round(t_stat,3)}")
        print(f"  pnl sum: orig(all contract struct)={resolved_orig_sum:.1f}  "
              f"alt_total (approx, unresolved kept at orig)={resolved_alt_sum:.1f}")

    out_path = Path("reports/research/channel_lab/tx_channel_contract_confirm_bars_item4.json")
    out_path.write_text(json.dumps({
        "n_days": n_days,
        "n_contract_struct_break": len(contract_struct),
        "N2_trades": results[2],
        "N3_trades": results[3],
    }, indent=2, default=str))
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
