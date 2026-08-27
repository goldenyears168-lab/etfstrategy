"""Research-only: test widened (unblocked) hang bands for day|div_hh_weak_vol.

Cell under test: day|div_hh_weak_vol -- currently block=['L','S'] in the live
PAPER_RECIPE (CELL_TUNE_V3_PATCHES). Hypothesis under test: does placing the
resting entry order MUCH farther from anchor (so only rare extreme touches
fill) produce a different, possibly positive, expected value vs the cell's
(net-negative) unblocked average?

True re-simulation only (no post-hoc trade filtering). Copies
PAPER_RECIPE['session_pv_book'], unblocks ONLY day|div_hh_weak_vol with a
widened hang_lo/hang_hi, leaves every other cell (including night|div_hh_weak_vol,
which stays blocked) untouched. Because night|div_hh_weak_vol remains blocked in
every candidate, any trade with regime_e == "div_hh_weak_vol" is guaranteed to be
a day-session trade -- no separate session tag needed on the trade record.

Runs across the 4 sanctioned validation windows (w83, julsep25, octdec25,
janmar26), day-clustered t-test (rule #2), per-window breakdown (rule #5),
best/worst single-trade-removal check (rule #4).
"""
from __future__ import annotations

import json
import statistics as st
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

CELL = "div_hh_weak_vol"
SESSION = "day"

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

CANDIDATES = [
    ("1.5x", 23.0, 45.0),
    ("2x", 30.0, 60.0),
    ("3x", 45.0, 90.0),
]


def _arrays_from_rows(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def make_recipe(hang_lo: float, hang_hi: float) -> dict:
    recipe = deepcopy(PAPER_RECIPE)
    book = deepcopy(recipe["session_pv_book"])
    cell = book[SESSION][CELL]
    assert cell.get("block") == ["L", "S"], f"unexpected baseline: {cell}"
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    # Sanity: confirm night cell for same regime stays blocked (untouched).
    assert book["night"][CELL].get("block") == ["L", "S"]
    recipe["session_pv_book"] = book
    return recipe


def run_window(cache_name: str, recipe: dict, vix: dict) -> dict:
    days = list_days(cache_name)
    per_day = {}  # day -> list of trade pnl for our cell
    all_trades = []
    for day in days:
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays_from_rows(day, rows)
        trades, events, ws, wl, rvol, regime, open_pos = simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
        cell_trades = [t for t in trades if t.get("regime_e") == CELL]
        if cell_trades:
            per_day[day] = [float(t["pnl"]) for t in cell_trades]
            for t in cell_trades:
                t2 = dict(t)
                t2["day"] = day
                all_trades.append(t2)
    return {"per_day": per_day, "trades": all_trades}


def day_clustered_ttest(day_sums: list[float]):
    n = len(day_sums)
    if n < 2:
        return {"t": None, "p": None, "n_days": n}
    mean = st.mean(day_sums)
    sd = st.stdev(day_sums)
    if sd == 0:
        return {"t": None, "p": None, "n_days": n, "mean": mean}
    se = sd / (n ** 0.5)
    t = mean / se
    # two-sided p via normal approx fallback if scipy unavailable
    try:
        from scipy import stats as sstats

        p = 2 * (1 - sstats.t.cdf(abs(t), df=n - 1))
    except Exception:
        # crude normal approximation
        import math

        p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / (2 ** 0.5))))
    return {"t": round(t, 3), "p": round(float(p), 5), "n_days": n, "mean": round(mean, 2)}


def analyze_candidate(name: str, hang_lo: float, hang_hi: float, vix: dict) -> dict:
    recipe = make_recipe(hang_lo, hang_hi)
    windows_out = {}
    pooled_day_sums = []
    pooled_trades = []
    for wname, cache_name in WINDOWS.items():
        res = run_window(cache_name, recipe, vix)
        day_sums = [sum(v) for v in res["per_day"].values()]
        n_trades = sum(len(v) for v in res["per_day"].values())
        stats = day_clustered_ttest(day_sums)
        wr = 0.0
        if res["trades"]:
            wins = sum(1 for t in res["trades"] if t["pnl"] > 0)
            wr = round(100.0 * wins / len(res["trades"]), 1)
        windows_out[wname] = {
            "n_days": len(day_sums),
            "n_trades": n_trades,
            "sum_pnl": round(sum(day_sums), 1) if day_sums else 0.0,
            "mean_trade_pnl": round(sum(t["pnl"] for t in res["trades"]) / n_trades, 2)
            if n_trades
            else 0.0,
            "win_rate": wr,
            "trades": res["trades"],
        }
        pooled_day_sums.extend(day_sums)
        pooled_trades.extend(res["trades"])

    pooled_stats = day_clustered_ttest(pooled_day_sums)
    n_trades_total = len(pooled_trades)
    mean_trade = (
        round(sum(t["pnl"] for t in pooled_trades) / n_trades_total, 2)
        if n_trades_total
        else 0.0
    )
    wr_total = (
        round(100.0 * sum(1 for t in pooled_trades if t["pnl"] > 0) / n_trades_total, 1)
        if n_trades_total
        else 0.0
    )

    # Single-trade-removal check (best and worst)
    removal = {}
    if pooled_trades:
        pnls = [t["pnl"] for t in pooled_trades]
        best_idx = max(range(len(pnls)), key=lambda i: pnls[i])
        worst_idx = min(range(len(pnls)), key=lambda i: pnls[i])
        base_sum = sum(pnls)
        removal["best_trade_pnl"] = pnls[best_idx]
        removal["worst_trade_pnl"] = pnls[worst_idx]
        removal["sum_excl_best"] = round(base_sum - pnls[best_idx], 1)
        removal["sum_excl_worst"] = round(base_sum - pnls[worst_idx], 1)
        removal["sum_all"] = round(base_sum, 1)

    return {
        "candidate": name,
        "hang_lo": hang_lo,
        "hang_hi": hang_hi,
        "n_trades_total": n_trades_total,
        "n_days_total": pooled_stats.get("n_days"),
        "mean_trade_pnl": mean_trade,
        "win_rate": wr_total,
        "pooled_day_clustered": pooled_stats,
        "removal_check": removal,
        "per_window": {
            k: {kk: vv for kk, vv in v.items() if kk != "trades"}
            for k, v in windows_out.items()
        },
    }


def main():
    vix = load_vixtwn_delta() or {}
    results = []
    for name, lo, hi in CANDIDATES:
        r = analyze_candidate(name, lo, hi, vix)
        results.append(r)
        print(f"\n=== candidate {name} (hang_lo={lo}, hang_hi={hi}) ===")
        print(json.dumps(r, indent=2, default=str))
    out_path = (
        "/Users/jackm4/goldenstocks/reports/research/channel_lab/"
        "day_dhwv_wide_hang_results.json"
    )
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
