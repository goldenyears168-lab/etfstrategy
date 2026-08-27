#!/usr/bin/env python3
"""Companion diagnostics for tmf_tx_nq_decouple_defensive_exit.py -- NOT a
separate mechanism, just two follow-up checks run after the main sweep
surfaced a question the summary JSON alone couldn't answer:

1. bug7_by_orig_exit_reason(): the main script's bug7_exit_reason_breakdown
   only reports (n, total pnl) per exit-reason CATEGORY, aggregated
   separately for baseline-all vs triggered-original-reason. It does NOT
   report the PAIRED (orig_pnl, overlay_pnl) delta per category for the
   trades our new trigger actually touched -- which is the number that
   actually answers "is this genuinely rescuing losers, or just clipping
   winners short?" This reruns the identical variant-B threshold=0.15 sweep
   (reusing run_one_day/build_hourly_ffill_proxy_series unmodified, no
   engine changes) and accumulates that paired delta per category.

2. spread_variance_decomposition(): sanity-checks a mechanism concern raised
   by (1)'s result (opp_cover-bound trades get hurt by early exit) -- is the
   hourly-ffilled proxy's ``spread`` actually dominated by tw_dev alone
   (i.e. degenerating into "TX overextended from its own MA", a
   price-structure signal indistinguishable in spirit from the already-studied
   and already-rejected ~120 struct_break-mitigation variants) rather than a
   genuine cross-market divergence? Computes sd(tw_dev), sd(us_dev), and
   corr(tw_dev, us_dev) over real bars in both variants.

Read-only w.r.t. src/order/, src/tmf_channel/, config/order.yaml,
config/strategy.yaml, .env.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
import tmf_tx_nq_decouple_defensive_exit as mod  # noqa: E402

OUT = LAB / "tmf_tx_nq_decouple_exit_diag_result.json"
DIAG_THRESHOLD = 0.15


def _recipe_base():
    recipe_base = deepcopy(PAPER_RECIPE)
    recipe_base.setdefault("hang_anchor", "O")
    recipe_base["session_pv_book"] = specialized_cell_book()
    recipe_base["struct_disabled"] = False
    return recipe_base


def bug7_by_orig_exit_reason(hourly_proxy, recipe_base, vix, *, threshold: float) -> dict:
    def factory(day, T):
        return mod._make_local_us_dev_fn(hourly_proxy, T)

    windows = []
    for wlabel, source in mod.HOLDOUT_SOURCES.items():
        for d in list_days(source=source):
            windows.append((wlabel, d, source))
    for d in list_days(source=mod.W83_SOURCE):
        windows.append(("w83", d, mod.W83_SOURCE))

    by_cat: dict[str, dict] = {}
    n_total_triggered = 0
    for wlabel, day, source in windows:
        res = mod.run_one_day(day, source, recipe_base, vix, factory, threshold=threshold, min_hold=mod.MIN_HOLD_BARS)
        if res is None:
            continue
        for tr in res["overlay_trades"]:
            if not tr.get("triggered"):
                continue
            n_total_triggered += 1
            cat = str(tr["orig_why"]).split("|", 1)[0]
            d = by_cat.setdefault(cat, dict(n=0, orig_pnl=0.0, overlay_pnl=0.0))
            d["n"] += 1
            d["orig_pnl"] += float(tr["pnl"])
            d["overlay_pnl"] += float(tr["pnl_overlay"])

    for d in by_cat.values():
        d["delta"] = round(d["overlay_pnl"] - d["orig_pnl"], 1)
        d["delta_per_trade"] = round(d["delta"] / d["n"], 2) if d["n"] else 0.0
        d["orig_pnl_per_trade"] = round(d["orig_pnl"] / d["n"], 2) if d["n"] else 0.0
        d["overlay_pnl_per_trade"] = round(d["overlay_pnl"] / d["n"], 2) if d["n"] else 0.0

    net_delta_check = round(sum(d["delta"] for d in by_cat.values()), 1)
    return dict(threshold=threshold, n_total_triggered=n_total_triggered,
                by_orig_exit_reason=by_cat, net_delta_sum_check=net_delta_check)


def spread_variance_decomposition(days_sources, us_series, *, label: str, n_days_sample: int = 15) -> dict:
    tw_all, us_all = [], []
    for day, source in days_sources[:n_days_sample]:
        arr = mod.load_arrays(day, source)
        if arr is None:
            continue
        O, H, L, C, V, T = arr
        fn = mod._make_local_us_dev_fn(us_series, T)
        for t in range(mod.SPREAD_WINDOW_MIN, len(C)):
            tw, us, sp = mod.defensive_spread_at(C, t, fn)
            if tw is None or us is None:
                continue
            tw_all.append(tw)
            us_all.append(us)
    if len(tw_all) < 5:
        return dict(label=label, n=len(tw_all), note="insufficient points")
    n = len(tw_all)
    sd_tw, sd_us = st.pstdev(tw_all), st.pstdev(us_all)
    mtw, mus = sum(tw_all) / n, sum(us_all) / n
    cov = sum((a - mtw) * (b - mus) for a, b in zip(tw_all, us_all)) / n
    corr = cov / (sd_tw * sd_us) if sd_tw > 0 and sd_us > 0 else None
    return dict(label=label, n=n, sd_tw_dev=round(sd_tw, 4), sd_us_dev=round(sd_us, 4),
                ratio_tw_over_us=round(sd_tw / sd_us, 3) if sd_us > 0 else None,
                corr_tw_us=round(corr, 3) if corr is not None else None)


def main() -> None:
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    recipe_base = _recipe_base()

    print("Building hourly ffill proxy series...")
    hourly_proxy = mod.build_hourly_ffill_proxy_series(date(2025, 6, 20), date(2026, 8, 12))
    print("Loading real 1m NQ archive...")
    real_1m = mod.load_real_1m_archive()

    print(f"Running bug7-by-orig-exit-reason paired delta (threshold={DIAG_THRESHOLD}, variant B)...")
    bug7 = bug7_by_orig_exit_reason(hourly_proxy, recipe_base, vix, threshold=DIAG_THRESHOLD)
    for cat, d in sorted(bug7["by_orig_exit_reason"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {cat:12s} n={d['n']:5d} orig={d['orig_pnl_per_trade']:8.2f}/tr "
              f"overlay={d['overlay_pnl_per_trade']:8.2f}/tr delta={d['delta_per_trade']:8.2f}/tr "
              f"(sum {d['delta']})")
    print(f"  net_delta_sum_check={bug7['net_delta_sum_check']}")

    print("\nSpread variance decomposition (sd(tw_dev) vs sd(us_dev), corr) ...")
    w83_ds = [(d, mod.W83_SOURCE) for d in list_days(source=mod.W83_SOURCE)]
    aug_ds = [(d, mod.AUG_REAL_1M_SOURCE) for d in list_days(source=mod.AUG_REAL_1M_SOURCE)]
    var_b = spread_variance_decomposition(w83_ds, hourly_proxy, label="variant_B_hourly_ffill_proxy_w83_first15")
    var_a = spread_variance_decomposition(aug_ds, real_1m, label="variant_A_real_1m_aug_all", n_days_sample=6)
    print(f"  {var_b}")
    print(f"  {var_a}")

    out = dict(diag_threshold=DIAG_THRESHOLD, bug7_paired_by_orig_exit_reason=bug7,
               spread_variance_decomposition=dict(variant_B=var_b, variant_A=var_a))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
