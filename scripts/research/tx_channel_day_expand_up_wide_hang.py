"""Research-only: does widening (not un-blocking-as-is) day|expand_up hang bands
turn a hard-blocked, net-loss PV8 cell into a positive-EV rare-touch setup?

Cell under test: PAPER_RECIPE['session_pv_book']['day']['expand_up']
  live: hang_lo=18, hang_hi=33, block=['L','S']  (fully blocked, never fills)

Task base (unblocked-cell-style) distance given by operator: hang_lo=15, hang_hi=30.
Candidates tested (deepcopy recipe, unblock ONLY this cell, all other cells
untouched):
    C1: hang_lo=23, hang_hi=45   (1.5x base)
    C2: hang_lo=30, hang_hi=60   (2x base)
    C3: hang_lo=45, hang_hi=90   (3x base)

Method:
  - TRUE re-simulation via tmf_channel.engine.simulate() per day, per candidate.
  - Primary metric = trades whose regime_e == "expand_up" AND entry timestamp
    falls in the DAY session (hm in [05:00, 15:00)) -- night also has an
    "expand_up" cell (already unblocked, different band) so we must exclude
    night trades sharing the same regime tag.
  - Day-clustered stats: one number per trading day (sum of PnL for matching
    trades that day, 0 if none that day among days that HAVE >=1 matching
    trade -- n_days = count of days with >=1 matching trade, per methodology).
  - One-sample t-test (scipy if available, else manual) on those day sums.
  - Per-window breakdown across the 4 sanctioned validation windows.
  - Single best/worst trade removal check.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

CELL = "expand_up"
SESS = "day"

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

CANDIDATES = {
    "C1_1.5x_23_45": (23.0, 45.0),
    "C2_2x_30_60": (30.0, 60.0),
    "C3_3x_45_90": (45.0, 90.0),
}


def _hhmm(ts: str) -> str:
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]


def _is_night(hm: str) -> bool:
    return hm >= "15:00" or hm < "05:00"


def _arrays(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def build_recipe(hang_lo: float, hang_hi: float) -> dict:
    r = deepcopy(PAPER_RECIPE)
    cell = dict(r["session_pv_book"][SESS][CELL])
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    r["session_pv_book"][SESS][CELL] = cell
    return r


def run_window(cache_name: str, recipe: dict, vix: dict) -> list[dict]:
    """Return list of matching-cell trade dicts, each tagged with day."""
    out = []
    for day in list_days(cache_name):
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays(day, rows)
        trades, *_rest = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        for tr in trades or []:
            if tr.get("regime_e") != CELL:
                continue
            et = tr.get("et") or ""
            if _is_night(_hhmm(et)):
                continue
            tr2 = dict(tr)
            tr2["day"] = day
            tr2["window"] = None  # filled by caller
            out.append(tr2)
    return out


def t_test_1samp(vals: list[float]):
    n = len(vals)
    if n < 2:
        return float("nan"), float("nan")
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se == 0:
        return float("inf") if mean != 0 else 0.0, 0.0 if mean != 0 else 1.0
    t = mean / se
    try:
        from scipy import stats

        p = 2 * stats.t.sf(abs(t), df=n - 1)
    except Exception:
        # crude normal-approx fallback
        p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return t, p


def day_sums(trades: list[dict]) -> list[float]:
    by_day: dict[str, float] = {}
    for tr in trades:
        by_day[tr["day"]] = by_day.get(tr["day"], 0.0) + float(tr["pnl"])
    return list(by_day.values())


def analyze(trades: list[dict], label: str) -> dict:
    n_trades = len(trades)
    pnls = [float(t["pnl"]) for t in trades]
    dsums = day_sums(trades)
    n_days = len(dsums)
    t, p = t_test_1samp(dsums)
    wr = 100.0 * sum(1 for x in pnls if x > 0) / n_trades if n_trades else float("nan")
    mean_pnl = sum(pnls) / n_trades if n_trades else float("nan")
    res = dict(
        label=label,
        n_trades=n_trades,
        n_days=n_days,
        t=t,
        p=p,
        mean_pnl=round(mean_pnl, 2) if pnls else None,
        win_rate=round(wr, 1) if pnls else None,
        net=round(sum(pnls), 1),
    )
    # single trade removal check
    if n_trades >= 2:
        best_i = max(range(n_trades), key=lambda i: pnls[i])
        worst_i = min(range(n_trades), key=lambda i: pnls[i])
        wo_best = [t for i, t in enumerate(trades) if i != best_i]
        wo_worst = [t for i, t in enumerate(trades) if i != worst_i]
        res["excl_best"] = dict(
            trade_pnl=pnls[best_i],
            net_without=round(sum(pnls) - pnls[best_i], 1),
            mean_without=round(
                (sum(pnls) - pnls[best_i]) / (n_trades - 1), 2
            ),
        )
        res["excl_worst"] = dict(
            trade_pnl=pnls[worst_i],
            net_without=round(sum(pnls) - pnls[worst_i], 1),
            mean_without=round(
                (sum(pnls) - pnls[worst_i]) / (n_trades - 1), 2
            ),
        )
    return res


def main():
    vix = load_vixtwn_delta() or {}
    all_results = {}
    for cand_label, (lo, hi) in CANDIDATES.items():
        recipe = build_recipe(lo, hi)
        assert recipe["session_pv_book"][SESS][CELL]["block"] == []
        assert recipe["session_pv_book"][SESS][CELL]["hang_lo"] == lo
        assert recipe["session_pv_book"][SESS][CELL]["hang_hi"] == hi
        per_window = {}
        pooled_trades = []
        for wlabel, cache_name in WINDOWS.items():
            trades = run_window(cache_name, recipe, vix)
            for tr in trades:
                tr["window"] = wlabel
            per_window[wlabel] = analyze(trades, f"{cand_label}/{wlabel}")
            pooled_trades.extend(trades)
        pooled = analyze(pooled_trades, f"{cand_label}/POOLED")
        all_results[cand_label] = dict(
            hang_lo=lo, hang_hi=hi, per_window=per_window, pooled=pooled,
            pooled_trades=[
                dict(day=t["day"], window=t["window"], s=t["s"], pnl=t["pnl"],
                     et=t["et"], xt=t["xt"], why=t["why"])
                for t in pooled_trades
            ],
        )
        print(f"\n=== {cand_label} (hang_lo={lo}, hang_hi={hi}) ===")
        for wlabel in WINDOWS:
            r = per_window[wlabel]
            print(
                f"  {wlabel:9s} n_trades={r['n_trades']:3d} n_days={r['n_days']:2d} "
                f"net={r['net']:8.1f} mean={r['mean_pnl']} wr={r['win_rate']}"
            )
        print(
            f"  POOLED    n_trades={pooled['n_trades']:3d} n_days={pooled['n_days']:2d} "
            f"net={pooled['net']:8.1f} mean={pooled['mean_pnl']} wr={pooled['win_rate']} "
            f"t={pooled['t']:.3f} p={pooled['p']:.4f}"
        )
        if "excl_best" in pooled:
            print(f"  excl_best : {pooled['excl_best']}")
            print(f"  excl_worst: {pooled['excl_worst']}")

    out_path = (
        "reports/research/channel_lab/day_expand_up_wide_hang_results.json"
    )
    import os

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
