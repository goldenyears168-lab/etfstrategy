"""Research-only, read-only test: night|expand_dn hard-blocked cell, does placing
the resting entry order much farther from anchor ("wide band") produce positive EV
on the rare fills that occur, vs the cell's blocked (net-loss) baseline?

Assigned cell: night|expand_dn (live PAPER_RECIPE has block=['L','S']).
Candidates (hang_lo, hang_hi): (27,48) 1.5x, (36,64) 2x, (54,96) 3x of the
task-specified base (18,32).

TRUE re-simulation per methodology: deepcopy PAPER_RECIPE['session_pv_book'],
override ONLY night.expand_dn's block/hang_lo/hang_hi, run simulate() per day
across all 4 sanctioned validation windows, and pull trades whose regime_e ==
"expand_dn" AND entry bar time is in the night session (hm >= "15:00" or
hm < "05:00").

Does NOT touch src/order/*, causal_engine.py, or any live config. Pure read-only
research script under scripts/research/.
"""

from __future__ import annotations

import statistics
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

CELL_SESSION = "night"
CELL_REGIME = "expand_dn"

CANDIDATES = [
    ("1.5x", 27.0, 48.0),
    ("2x", 36.0, 64.0),
    ("3x", 54.0, 96.0),
]


def _arrays_from_rows(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def _is_night(hm: str) -> bool:
    return hm >= "15:00" or hm < "05:00"


def build_recipe(hang_lo: float, hang_hi: float) -> dict:
    recipe = deepcopy(PAPER_RECIPE)
    spb = recipe["session_pv_book"]
    cell = spb[CELL_SESSION][CELL_REGIME]
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    return recipe


def run_window(recipe: dict, cache_name: str, vix: dict) -> list[dict]:
    """Run every day in this cache, return list of matching trades w/ day tag."""
    out = []
    for day in list_days(source=cache_name):
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(day, rows)
        trades, events, ws, wl, rvol, regime, open_pos = simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
        for tr in trades or []:
            if tr.get("regime_e") != CELL_REGIME:
                continue
            et = tr.get("et") or ""
            if not _is_night(et):
                continue
            out.append({**tr, "day": day})
    return out


def day_clustered_ttest(day_pnls: list[float]):
    """One-sample t-test of per-day summed pnl vs 0. Returns (t, p, n_days)."""
    n = len(day_pnls)
    if n < 2:
        return None, None, n
    mean = statistics.mean(day_pnls)
    sd = statistics.stdev(day_pnls)
    if sd == 0:
        return None, None, n
    se = sd / (n**0.5)
    t = mean / se
    try:
        from scipy import stats as sstats

        p = 2 * (1 - sstats.t.cdf(abs(t), df=n - 1))
    except Exception:
        p = None
    return t, p, n


def analyze(trades: list[dict], label: str):
    print(f"\n=== {label} ===")
    if not trades:
        print("  n_trades=0 (no fills in any window)")
        return

    n_trades = len(trades)
    pnls = [tr["pnl"] for tr in trades]
    mean_pnl = statistics.mean(pnls)
    wins = sum(1 for x in pnls if x > 0)
    win_rate = wins / n_trades

    by_day: dict[str, float] = {}
    for tr in trades:
        by_day[tr["day"]] = by_day.get(tr["day"], 0.0) + tr["pnl"]
    day_pnls = list(by_day.values())
    t, p, n_days = day_clustered_ttest(day_pnls)

    print(
        f"  pooled: n_trades={n_trades} n_days={n_days} mean_pnl={mean_pnl:.1f} "
        f"win_rate={win_rate:.1%} sum_pnl={sum(pnls):.1f}"
    )
    print(f"  day-clustered t-test: t={t}, p={p}")
    if n_trades < 20:
        print(
            "  ** SAMPLE-SIZE WARNING: n_trades < 20 -> suggestive at best, "
            "cannot be treated as validated regardless of sign/p-value. **"
        )

    # per-window breakdown
    by_window: dict[str, list[dict]] = {}
    for tr in trades:
        by_window.setdefault(tr["window"], []).append(tr)
    for w, wtrades in by_window.items():
        wp = [x["pnl"] for x in wtrades]
        wwin = sum(1 for x in wp if x > 0) / len(wp)
        print(
            f"    [{w}] n={len(wtrades)} sum={sum(wp):.1f} mean={statistics.mean(wp):.1f} "
            f"win_rate={wwin:.1%}"
        )

    # single-trade removal check
    if n_trades >= 2:
        best_i = max(range(n_trades), key=lambda i: pnls[i])
        worst_i = min(range(n_trades), key=lambda i: pnls[i])
        without_best = [x for i, x in enumerate(pnls) if i != best_i]
        without_worst = [x for i, x in enumerate(pnls) if i != worst_i]
        print(
            f"  best trade removed: sum={sum(without_best):.1f} mean={statistics.mean(without_best):.1f} "
            f"(best trade pnl={pnls[best_i]:.1f})"
        )
        print(
            f"  worst trade removed: sum={sum(without_worst):.1f} mean={statistics.mean(without_worst):.1f} "
            f"(worst trade pnl={pnls[worst_i]:.1f})"
        )
        flips_best = (sum(pnls) > 0) != (sum(without_best) > 0)
        flips_worst = (sum(pnls) > 0) != (sum(without_worst) > 0)
        if flips_best or flips_worst:
            print("  ** CONCLUSION FLIPS when removing single best/worst trade **")


def main():
    vix = load_vixtwn_delta() or {}
    for label, hang_lo, hang_hi in CANDIDATES:
        recipe = build_recipe(hang_lo, hang_hi)
        all_trades = []
        for window_name, cache_name in WINDOWS.items():
            wt = run_window(recipe, cache_name, vix)
            for tr in wt:
                tr["window"] = window_name
            all_trades.extend(wt)
        analyze(all_trades, f"night|expand_dn hang_lo={hang_lo} hang_hi={hang_hi} ({label})")


if __name__ == "__main__":
    main()
