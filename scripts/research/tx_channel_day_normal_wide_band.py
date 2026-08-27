"""Research (read-only): does widening the hang distance on the hard-blocked
day|normal PV16 cell (instead of blocking it outright) find a positive-EV
tail regime?

Assigned cell: day|normal (live PAPER_RECIPE has block=['L','S'] there).
Candidate widened bands (base day session hang_lo=15/hang_hi=30 per task spec):
  1.5x -> hang_lo=23, hang_hi=45
  2x   -> hang_lo=30, hang_hi=60
  3x   -> hang_lo=45, hang_hi=90

For each candidate: deepcopy PAPER_RECIPE['session_pv_book'], set ONLY
day.normal.block=[] and hang_lo/hang_hi to the candidate, run TRUE
re-simulation via causal_engine.simulate() across all 4 sanctioned validation
windows, and report day-clustered stats restricted to trades whose
regime_e == "normal" and session == "day" (derived from entry time et,
day session = 05:00 <= et < 15:00, matching the engine's own is_night rule
`hm >= "15:00" or hm < "05:00"`).

This file is scratch research; it does not modify any live config or engine
code. It only imports and calls the frozen causal_engine via
tmf_channel.engine / tmf_channel.harness.
"""

from __future__ import annotations

import statistics
from copy import deepcopy
from typing import Any

from order.tmf_channel_config import PAPER_RECIPE
from order.tmf_channel_pv16_book import RECIPE_VERSION
from tmf_channel.cache_store import load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

CELL_SESSION = "day"
CELL_REGIME = "normal"

CANDIDATES = {
    "1.5x (23/45)": (23.0, 45.0),
    "2x (30/60)": (30.0, 60.0),
    "3x (45/90)": (45.0, 90.0),
}


def _arrays_from_rows(day: str, rows: list[dict[str, Any]]):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def build_recipe(hang_lo: float, hang_hi: float) -> dict[str, Any]:
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe.setdefault("recipe_version", RECIPE_VERSION)
    spb = recipe["session_pv_book"]
    cell = spb[CELL_SESSION][CELL_REGIME]
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    return recipe


def is_day_session(et: str) -> bool:
    # Matches engine's own rule: night = hm >= "15:00" or hm < "05:00"
    s = str(et)
    hm = s.split("T", 1)[1][:5] if "T" in s else s[:5]  # tolerate full ISO or bare "HH:MM"
    return not (hm >= "15:00" or hm < "05:00")


def run_window(cache_name: str, recipe: dict[str, Any], vix: dict) -> dict[str, list]:
    """Return {day: [trade_dict, ...]} restricted to our cell's trades."""
    from tmf_channel.cache_store import list_days

    out: dict[str, list] = {}
    for day in list_days(source=cache_name):
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(day, rows)
        trades, *_rest = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        cell_trades = [
            t
            for t in (trades or [])
            if t.get("regime_e") == CELL_REGIME and is_day_session(str(t.get("et") or ""))
        ]
        if cell_trades:
            out[day] = cell_trades
    return out


def day_clustered_ttest(day_pnls: list[float]):
    n = len(day_pnls)
    if n < 2:
        return None, None
    mean = statistics.mean(day_pnls)
    sd = statistics.stdev(day_pnls)
    if sd == 0:
        return None, None
    se = sd / (n**0.5)
    t = mean / se
    # two-sided p-value via t distribution using scipy if available, else normal approx
    try:
        from scipy import stats as sps

        p = 2 * (1 - sps.t.cdf(abs(t), df=n - 1))
    except Exception:
        from math import erf, sqrt

        p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))  # normal approx fallback
    return t, p


def analyze_candidate(label: str, hang_lo: float, hang_hi: float):
    recipe = build_recipe(hang_lo, hang_hi)
    vix = load_vixtwn_delta() or {}

    per_window_trades: dict[str, list] = {}
    day_pnl_map: dict[str, float] = {}
    for wname, cache_name in WINDOWS.items():
        day_map = run_window(cache_name, recipe, vix)
        flat_trades = [tr for trs in day_map.values() for tr in trs]
        per_window_trades[wname] = flat_trades
        for day, trs in day_map.items():
            key = f"{wname}|{day}"
            day_pnl_map[key] = sum(tr["pnl"] for tr in trs)

    all_trades = [tr for trs in per_window_trades.values() for tr in trs]
    n_trades = len(all_trades)

    day_pnls = list(day_pnl_map.values())
    n_days = len(day_pnls)
    t, p = day_clustered_ttest(day_pnls)

    pnls = [tr["pnl"] for tr in all_trades]
    mean_pnl = statistics.mean(pnls) if pnls else float("nan")
    win_rate = (sum(1 for x in pnls if x > 0) / n_trades) if n_trades else float("nan")

    # single-trade removal check
    removal_note = "n/a (fewer than 2 trades)"
    if n_trades >= 2:
        best_i = max(range(n_trades), key=lambda i: pnls[i])
        worst_i = min(range(n_trades), key=lambda i: pnls[i])
        sum_all = sum(pnls)
        sum_wo_best = sum_all - pnls[best_i]
        sum_wo_worst = sum_all - pnls[worst_i]
        removal_note = (
            f"sum_all={sum_all:+.1f} sum_excl_best({pnls[best_i]:+.1f})={sum_wo_best:+.1f} "
            f"sum_excl_worst({pnls[worst_i]:+.1f})={sum_wo_worst:+.1f}"
        )

    print(f"\n=== Candidate {label}: hang_lo={hang_lo} hang_hi={hang_hi} ===")
    print(
        f"n_trades={n_trades} n_days={n_days} mean_pnl={mean_pnl:+.2f} "
        f"win_rate={win_rate:.1%} t={t} p={p}"
    )
    print(f"removal_check: {removal_note}")
    for wname in WINDOWS:
        trs = per_window_trades[wname]
        wpnls = [tr["pnl"] for tr in trs]
        wsum = sum(wpnls)
        wmean = statistics.mean(wpnls) if wpnls else float("nan")
        print(
            f"  [{wname}] n_trades={len(trs)} sum_pnl={wsum:+.1f} mean_pnl={wmean:+.2f} "
            f"trades={[(tr['s'], tr['pnl'], tr['et'], tr['xt']) for tr in trs]}"
        )

    return {
        "label": label,
        "hang_lo": hang_lo,
        "hang_hi": hang_hi,
        "n_trades": n_trades,
        "n_days": n_days,
        "mean_pnl": mean_pnl,
        "win_rate": win_rate,
        "t": t,
        "p": p,
        "per_window_trades": per_window_trades,
    }


def main():
    results = []
    for label, (hang_lo, hang_hi) in CANDIDATES.items():
        results.append(analyze_candidate(label, hang_lo, hang_hi))

    print("\n\n=== SUMMARY (day|normal widened-band candidates) ===")
    for r in results:
        print(
            f"{r['label']:16s} n_trades={r['n_trades']:3d} n_days={r['n_days']:2d} "
            f"mean_pnl={r['mean_pnl']:+8.2f} win_rate={r['win_rate']:.1%} "
            f"t={r['t']} p={r['p']}"
        )


if __name__ == "__main__":
    main()
