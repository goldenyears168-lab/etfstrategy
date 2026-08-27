#!/usr/bin/env python3
"""Item #18: contract regime hang_lo/hang_hi re-sweep (day-contract, night-contract).

True re-simulation via tmf_channel.harness.run_days against current live
PAPER_RECIPE (final_v1_4_0_pv16_normal_dhwv_block), modifying
recipe['session_pv_book']['day']['contract'] / ['night']['contract'] only.
Windows: w83 + 3 holdouts (janmar26, julsep25, octdec25).
Day-clustered significance: per-day net(candidate) - net(baseline), paired
one-sample t-test across all matched days (n = number of days where BOTH
baseline and candidate produced a valid day row).
"""
from __future__ import annotations

import json
import statistics as st
from copy import deepcopy
from pathlib import Path

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days
from tmf_channel.harness import run_days, summarize_days

OUT = Path("reports/research/channel_lab/tx_contract_hang_band_resweep.json")

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
}


def days_for(cache_name: str) -> list[str]:
    return list_days(cache_name)


def net_by_day(rows: list[dict]) -> dict[str, float]:
    return {r["day"]: float(r["net"]) for r in rows if r.get("ok")}


def run_all_windows(recipe: dict) -> dict[str, dict[str, float]]:
    out = {}
    for label, cache in WINDOWS.items():
        d = days_for(cache)
        rows = run_days(d, recipe=recipe, cache_name=cache)
        out[label] = net_by_day(rows)
    return out


def paired_ttest(base: dict[str, dict[str, float]], cand: dict[str, dict[str, float]]):
    """Pool per-day deltas across all windows, day-clustered (n=n_days)."""
    deltas = []
    for label in WINDOWS:
        b = base[label]
        c = cand[label]
        for day in b:
            if day in c:
                deltas.append(c[day] - b[day])
    n = len(deltas)
    if n < 2:
        return {"n_days": n, "mean_delta": 0.0, "t": 0.0, "p": 1.0}
    mean = sum(deltas) / n
    sd = st.stdev(deltas)
    if sd == 0:
        t = 0.0
    else:
        t = mean / (sd / (n ** 0.5))
    # two-sided p via normal approx (n is large enough across 4 windows, ~265 days)
    from math import erf, sqrt

    p = 2.0 * (1.0 - 0.5 * (1.0 + erf(abs(t) / sqrt(2))))
    return {
        "n_days": n,
        "sum_delta": round(sum(deltas), 1),
        "mean_delta": round(mean, 2),
        "t": round(t, 3),
        "p": round(p, 4),
    }


def summarize_window_set(nbd: dict[str, dict[str, float]]) -> dict[str, dict]:
    out = {}
    for label, dd in nbd.items():
        vals = list(dd.values())
        out[label] = {
            "n_days": len(vals),
            "net": round(sum(vals), 1),
            "mean": round(sum(vals) / len(vals), 1) if vals else 0.0,
        }
    out["ALL"] = {
        "n_days": sum(v["n_days"] for v in out.values()),
        "net": round(sum(v["net"] for k, v in out.items() if k != "ALL"), 1),
    }
    return out


def make_recipe(day_contract=None, night_contract=None) -> dict:
    r = deepcopy(PAPER_RECIPE)
    book = r["session_pv_book"]
    if day_contract:
        book["day"]["contract"].update(day_contract)
    if night_contract:
        book["night"]["contract"].update(night_contract)
    return r


def main():
    print("Baseline day-contract:", PAPER_RECIPE["session_pv_book"]["day"]["contract"])
    print("Baseline night-contract:", PAPER_RECIPE["session_pv_book"]["night"]["contract"])

    base_nbd = run_all_windows(PAPER_RECIPE)
    base_summary = summarize_window_set(base_nbd)
    print("BASELINE", json.dumps(base_summary, indent=2))

    results = {"baseline": base_summary}

    # --- day-contract sweep ---
    day_candidates = []
    # vary lo, width fixed at 15 (current width)
    for lo in (6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0):
        day_candidates.append({"hang_lo": lo, "hang_hi": lo + 15.0})
    # vary width, lo fixed at current 10
    for hi in (18.0, 20.0, 22.0, 28.0, 32.0, 36.0):
        day_candidates.append({"hang_lo": 10.0, "hang_hi": hi})

    day_results = []
    for cand in day_candidates:
        recipe = make_recipe(day_contract=cand)
        nbd = run_all_windows(recipe)
        summ = summarize_window_set(nbd)
        stat = paired_ttest(base_nbd, nbd)
        day_results.append({"cand": cand, "summary": summ, "vs_baseline": stat})
        print("DAY-CONTRACT", cand, "ALL_net=", summ["ALL"]["net"], stat)

    # --- night-contract sweep ---
    night_candidates = []
    for lo in (10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 24.0, 28.0):
        night_candidates.append({"hang_lo": lo, "hang_hi": lo + 14.0})
    for hi in (24.0, 26.0, 32.0, 36.0, 40.0):
        night_candidates.append({"hang_lo": 16.0, "hang_hi": hi})

    night_results = []
    for cand in night_candidates:
        recipe = make_recipe(night_contract=cand)
        nbd = run_all_windows(recipe)
        summ = summarize_window_set(nbd)
        stat = paired_ttest(base_nbd, nbd)
        night_results.append({"cand": cand, "summary": summ, "vs_baseline": stat})
        print("NIGHT-CONTRACT", cand, "ALL_net=", summ["ALL"]["net"], stat)

    results["day_contract_sweep"] = day_results
    results["night_contract_sweep"] = night_results

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
