#!/usr/bin/env python3
"""BUG-4 formal significance pass for the CELL_TUNE_V2_PATCHES re-audit.

Computes paired day-level (v1_3_0_cell_tune_v2 - v1_2_0_baseline) delta series
per period, naive one-sample t-test, lag-1 autocorrelation, and Newey-West HAC
p-values at maxlags in (1,5,10,20) -- symmetric gate regardless of sign, reusing
this lab's existing slow_cell_significance_helper.classify_significance().

Also pools the three genuinely-independent OOS quarters (julsep25/octdec25/
janmar26 -- non-overlapping calendar windows) into one combined series, since
w83_IS and w25_IS are NOT independent of each other (w25 subset of w83) and
both are in-sample (the window cell-tune v2's 16 per-cell patches were
selected on) -- counting them as 2 of "5 independent periods" overstates the
independent-evidence count (checklist Appendix A8 pattern).

Read-only. Writes: reports/research/channel_lab/audit_cell_tune_v2_significance.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import statsmodels.api as sm
import scipy.stats as sps

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))

import r_cost_aware_cell_tune as ht  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    freeze_cell_book,
    SPECIALIZED_PATCHES,
    CELL_TUNE_V2_PATCHES,
)
from slow_cell_significance_helper import classify_significance  # noqa: E402

ov = ht.ov
eng = ht.eng
OUT = LAB / "audit_cell_tune_v2_significance.json"


def build_v1_2_0():
    from copy import deepcopy
    book = freeze_cell_book()
    for sess, reg, upd in SPECIALIZED_PATCHES:
        book[sess][reg].update(upd)
    return book


def build_v1_3_0_v2only():
    from copy import deepcopy
    book = build_v1_2_0()
    for sess, reg, upd in CELL_TUNE_V2_PATCHES:
        book[sess][reg].update(deepcopy(upd))
    return book


def day_nets_for_book(days, tx, vix, gate, v1m, book):
    ht.set_cost(ht.LIVE)
    cfg = ov.base_recipe(
        hang_anchor="O", tick_native=False, gap_fill_improve=False,
        session_side_gate=gate, hang_lo=15.0, hang_hi=30.0,
        night_hang_lo=18.0, night_hang_hi=32.0, early_fill_gamma=5.0,
        max_hold_bars=25, skip_quiet_mode="dry", place_every=ht.PLACE_EVERY,
        vixtwn_1m=v1m, vixtwn_calib="blend", vixtwn_calib_gamma=5.0,
        us_vix_calib="none", nq_calib="none", session_pv_book=book,
    )
    out = []
    for day in days:
        tr, *_ = eng.simulate(*ov.day_arrays(day, tx[day]), cfg, vix_delta=vix, tick_index=None)
        out.append(sum(float(t["pnl"]) for t in tr))
    return out


def one_sample_test(x):
    x = np.asarray(x, dtype=float)
    n = len(x)
    m = float(np.mean(x))
    sd = float(np.std(x, ddof=1)) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else float("nan")
    t_stat = m / se if se > 0 else float("nan")
    df = n - 1
    p_t = float(2.0 * sps.t.sf(abs(t_stat), df)) if se > 0 else float("nan")
    return dict(n=n, mean=round(m, 3), sd=round(sd, 3), p_value_t=p_t)


def lag1_autocorr(x):
    x = np.asarray(x, dtype=float)
    xm = x - x.mean()
    num = float(np.sum(xm[:-1] * xm[1:]))
    den = float(np.sum(xm * xm))
    return num / den if den > 0 else float("nan")


def newey_west_test(x, maxlags):
    x = np.asarray(x, dtype=float)
    Xc = np.ones((len(x), 1))
    model = sm.OLS(x, Xc).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    return dict(maxlags=maxlags, p_value_hac=float(model.pvalues[0]))


def main() -> None:
    ht.set_cost(ht.LIVE)
    vix = ht.load_vixtwn_delta()
    v1m = eng.load_vixtwn_1m()
    v1_2_0 = build_v1_2_0()
    v1_3_0 = build_v1_3_0_v2only()

    tx_main = json.loads(ov.TX_PATH.read_text())
    days_main = sorted(tx_main)
    periods = {
        "julsep25": ("cache", LAB / "tx_1m_julsep_holdout_cache.json"),
        "octdec25": ("cache", LAB / "tx_1m_octdec_holdout_cache.json"),
        "janmar26": ("cache", LAB / "tx_1m_janmar_holdout_cache.json"),
        "w83_IS": ("main", days_main[-83:]),
        "w25_IS": ("main", days_main[-25:]),
    }

    out = dict(title="BUG-4 significance re-audit for cell-tune v2 5/5 claim", periods={})
    pooled_oos_delta = []
    for pname, (kind, src) in periods.items():
        if kind == "cache":
            if not src.exists():
                continue
            tx = json.loads(src.read_text())
            days = sorted(tx)
        else:
            tx = tx_main
            days = src
        gate = ov.build_nq_gate(days)
        t0 = time.time()
        base_nets = day_nets_for_book(days, tx, vix, gate, v1m, v1_2_0)
        v2_nets = day_nets_for_book(days, tx, vix, gate, v1m, v1_3_0)
        delta = [b - a for a, b in zip(base_nets, v2_nets)]
        if pname in ("julsep25", "octdec25", "janmar26"):
            pooled_oos_delta.extend(delta)

        naive = one_sample_test(delta)
        ac1 = lag1_autocorr(delta)
        hac = {str(L): newey_west_test(delta, L)["p_value_hac"] for L in (1, 5, 10, 20)}
        sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"], hac_p_by_maxlags=hac)
        out["periods"][pname] = dict(
            n_days=len(days), naive=naive, lag1_autocorr=round(ac1, 3),
            hac_p_by_maxlags=hac, significance=sig,
            elapsed_s=round(time.time() - t0, 1),
        )
        print(f"{pname:10s} n={len(days):3d} mean_delta={naive['mean']:8.2f} "
              f"p_naive={naive['p_value_t']:.4f} lag1={ac1:+.3f} "
              f"hac_p={hac} -> {sig['label']}", flush=True)

    # pooled genuinely-independent OOS quarters
    naive = one_sample_test(pooled_oos_delta)
    ac1 = lag1_autocorr(pooled_oos_delta)
    hac = {str(L): newey_west_test(pooled_oos_delta, L)["p_value_hac"] for L in (1, 5, 10, 20)}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"], hac_p_by_maxlags=hac)
    out["pooled_oos_3quarters"] = dict(
        note="julsep25+octdec25+janmar26 concatenated (non-overlapping calendar "
        "quarters, genuinely independent of the w83/w25 in-sample tuning window)",
        n_days=len(pooled_oos_delta), naive=naive, lag1_autocorr=round(ac1, 3),
        hac_p_by_maxlags=hac, significance=sig,
    )
    print(f"POOLED OOS (3 quarters) n={len(pooled_oos_delta)} mean_delta={naive['mean']:.2f} "
          f"p_naive={naive['p_value_t']:.4f} lag1={ac1:+.3f} hac_p={hac} -> {sig['label']}", flush=True)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"Wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
