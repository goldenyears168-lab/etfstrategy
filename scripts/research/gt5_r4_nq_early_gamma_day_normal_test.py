#!/usr/bin/env python3
"""R4 (2026-08-11): re-test the 2026-08-06 Round-1 "NQ overnight direction
modulates day|normal early_fill_gamma" hypothesis, driven by PRODUCTION
tmf_channel.nq_gate / tmf_channel.nq_signal (fix A day/night anchor split +
fix B continuous per-bar-timestamp anchor + fix C min_age=1h forming-bar
exclusion, all already baked into nq_signal.futures_overnight_at itself),
instead of Round 1's self-built nq_ov_pct feature (r_negcell_usvix_vixtwn_clues.py
load_nq_1h() -- reads the static, frozen nikkei_us_intraday_1h_cache.json with
no min_age handling -- the same bar-start-timestamp bug fix C addresses, found
by Round-1's own reviewer in 27/265 days).

Mechanism under test: causal_engine.py's nq_conf_enable / nq_on_1m /
nq_conf_early_k / nq_conf_early_div_bonus / nq_conf_early_same_penalty --
already-live PRODUCTION CODE in tmf_channel/causal_engine.py (nq_early_gamma_scale,
lines ~600-616 and ~2405-2459), but never wired into the live order path
(src/order/tmf_channel_order.py only ever sets session_side_gate, never
nq_conf_enable/nq_on_1m) -- i.e. a real, already-built production knob that
has sat dormant/unused. This script is the first time it is driven by
correctly-anchored data end to end.

Isolation: early_fill_gamma is a GLOBAL recipe switch in causal_engine.py --
turning on nq_conf_enable would also modulate day|climax_dn (eg=11),
day|contract (eg=11) and night|expand_up (eg=8) in the live CELL_TUNE_V2 book,
confounding a "day|normal only" test. Both the control and treatment book
here zero early_fill_gamma on every cell except day|normal (identical
zeroing in both arms), isolating the day-level PnL delta to exactly the
day|normal cell's early-fill behavior -- verified by construction: since
every other cell is byte-identical between control and treatment, its
contribution cancels exactly in the paired delta.

Both arms also carry the SAME continuous (per-bar) session_side_gate
(fix A+B+C -- "today's live mechanism") so the only difference between
control and treatment is nq_conf_enable + nq_on_1m for day|normal's
early_fill_gamma. This directly answers: "given today's already-fixed NQ
gate mechanism, does modulating day|normal's early-fill eagerness by NQ
overnight direction help?"

4 historical windows (julsep25/octdec25/janmar26 OOS quarters + w83 IS),
paired day-level delta (treatment - control), naive t-test + lag-1
autocorrelation + Newey-West HAC p-values at maxlags in (1,5,10,20),
pooled independent-OOS (julsep+octdec+janmar) test, plus a small
neighbor-parameter grid on nq_conf_early_k (the main "how hard does NQ
direction push gamma" lever) to check sign/significance stability.

Diagnostic only. Does NOT touch config/strategy.yaml, config/strategies.yaml,
src/order/*.py, src/tmf_channel/*.py, launchd/, or .env. Read-only re-test;
does not adopt or apply anything.

Run:
  cd /Users/jackm4/goldenstocks && PYTHONPATH=src:reports/research/channel_lab \
    nice -n 15 .venv/bin/python scripts/research/gt5_r4_nq_early_gamma_day_normal_test.py

Writes:
  reports/research/channel_lab/gt5_r4_nq_early_gamma_day_normal_test_result.json
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import scipy.stats as sps
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import r_cost_aware_cell_tune as ht  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from slow_cell_significance_helper import classify_significance  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402

ov = ht.ov
eng = ht.eng
OUT = LAB / "gt5_r4_nq_early_gamma_day_normal_test_result.json"
_TZ = ZoneInfo("Asia/Taipei")

# Same margin R2 already validated live 2026-08-11 (julsep25 starts
# 2025-07-01, ~406 days back from today; Yahoo serves 1h data back to
# ~2025-05 for a wide-enough single request).
NQ_LOOKBACK_DAYS = 440

# nq_conf_* defaults already hard-coded as fallbacks in causal_engine.py
# itself (nq_conf_early_k=0.5, div_bonus=0.35, same_penalty=0.25,
# eps_nq=0.05, eps_tx=20.0) -- treated here as the "center" cell of the
# robustness grid, not invented by this script.
CENTER_K = 0.5
CENTER_DB = 0.35
CENTER_SP = 0.25
K_GRID = [0.3, 0.5, 0.7]
DB_GRID = [0.25, 0.35, 0.45]
SP_GRID = [0.15, 0.25, 0.35]


def isolate_day_normal(book: dict) -> dict:
    """Deep-copy `book`, zero early_fill_gamma on every cell except
    day|normal, so nq_conf_enable's global early-fill modulation can only
    ever change day|normal's trades -- see module docstring."""
    b = deepcopy(book)
    for sess in ("day", "night"):
        for pv, cell in b[sess].items():
            if not (sess == "day" and pv == "normal"):
                cell["early_fill_gamma"] = 0.0
    return b


def build_continuous_gate_and_nq_on_1m(
    days: list[str], tx: dict
) -> tuple[dict[str, str], dict[str, dict[str, float]]]:
    """One session_side_gate value AND one raw nq_overnight_pct value per
    1-min bar, both recomputed fresh at each bar's own timestamp via the
    live-code nq_signal module (fix A anchor split + fix C min_age already
    baked into nq_signal.futures_overnight_at itself -- no extra plumbing
    needed here). Bundle fetched once (get_cached, 30min throttle) and
    reused for every bar/day in this call -- same pattern as
    gt5_r2_audit_cell_tune_v2_continuous_gate.py's build_continuous_gate,
    extended to also keep the raw pct (not just the L/S/none side) for
    nq_on_1m."""
    bundle = nq_gate_mod.get_cached(
        "nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle
    )
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    gate: dict[str, str] = {}
    nq_on_1m: dict[str, dict[str, float]] = {}
    for day in days:
        bars = tx.get(day) or []
        day_pct: dict[str, float] = {}
        for b in bars:
            hm = str(b["t"])
            t = f"{day}T{hm}:00.000+08:00"
            dt_tw = datetime.fromisoformat(t).astimezone(_TZ)
            snap = nq_signal.futures_overnight_at(
                dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d,
                us_dates=us_dates,
            )
            pct = None if snap is None else snap.get("nq_overnight_pct")
            side = nq_signal.bias_side(pct)
            gate[t] = {"up": "L", "down": "S"}.get(side, "none")
            if pct is not None:
                day_pct[hm] = float(pct)
        nq_on_1m[day] = day_pct
    return gate, nq_on_1m


def make_cfg(gate, v1m, vix, book, *, nq_conf: dict | None):
    kw = dict(
        hang_anchor="O", tick_native=False, gap_fill_improve=False,
        session_side_gate=gate, hang_lo=15.0, hang_hi=30.0,
        night_hang_lo=18.0, night_hang_hi=32.0, early_fill_gamma=5.0,
        max_hold_bars=25, skip_quiet_mode="dry", place_every=ht.PLACE_EVERY,
        vixtwn_1m=v1m, vixtwn_calib="blend", vixtwn_calib_gamma=5.0,
        us_vix_calib="none", nq_calib="none", session_pv_book=book,
    )
    if nq_conf:
        kw.update(nq_conf)
    return ov.base_recipe(**kw)


def day_nets(days, tx, vix, cfg) -> list[float]:
    ht.set_cost(ht.LIVE)
    out = []
    for day in days:
        tr, *_ = eng.simulate(*ov.day_arrays(day, tx[day]), cfg, vix_delta=vix, tick_index=None)
        out.append(round(sum(float(t["pnl"]) for t in tr), 2))
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


def full_stats(delta: list[float]) -> dict:
    naive = one_sample_test(delta)
    ac1 = lag1_autocorr(delta)
    hac = {str(L): newey_west_test(delta, L)["p_value_hac"] for L in (1, 5, 10, 20)}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"], hac_p_by_maxlags=hac)
    return dict(n_days=len(delta), naive=naive, lag1_autocorr=round(ac1, 3),
                hac_p_by_maxlags=hac, significance=sig)


def main() -> None:
    patch_nq_gate_for_backfill(lookback_days=NQ_LOOKBACK_DAYS)

    vix = ht.load_vixtwn_delta()
    v1m = eng.load_vixtwn_1m()
    book_isolated = isolate_day_normal(specialized_cell_book())

    tx_main = json.loads(ov.TX_PATH.read_text())
    days_main = sorted(tx_main)
    periods = {
        "julsep25": ("cache", LAB / "tx_1m_julsep_holdout_cache.json"),
        "octdec25": ("cache", LAB / "tx_1m_octdec_holdout_cache.json"),
        "janmar26": ("cache", LAB / "tx_1m_janmar_holdout_cache.json"),
        "w83_IS": ("main", days_main),
    }

    out = dict(
        title="R4 (2026-08-11): NQ overnight direction -> day|normal "
        "early_fill_gamma modulation (nq_conf_enable), production "
        "nq_gate/nq_signal (fix A+B+C), day|normal isolated by zeroing "
        "early_fill_gamma on every other cell in both arms",
        nq_lookback_days=NQ_LOOKBACK_DAYS,
        center_params=dict(k=CENTER_K, div_bonus=CENTER_DB, same_penalty=CENTER_SP),
        original_round1_claim="+0.2~1.1 pt/day, noise-level (self-built nq_ov_pct "
        "feature, 27/265 days bar-start-timestamp misclassified per reviewer)",
        periods={},
        param_grid={},
    )

    per_period_cache: dict[str, dict] = {}
    pooled_oos_center_delta: list[float] = []

    for pname, (kind, src) in periods.items():
        if kind == "cache":
            if not src.exists():
                print(f"skip {pname}: {src} missing")
                continue
            tx = json.loads(src.read_text())
            days = sorted(tx)
        else:
            tx = tx_main
            days = src

        t0 = time.time()
        gate, nq_on_1m = build_continuous_gate_and_nq_on_1m(days, tx)
        cfg_control = make_cfg(gate, v1m, vix, book_isolated, nq_conf=None)
        control_nets = day_nets(days, tx, vix, cfg_control)

        cfg_center = make_cfg(
            gate, v1m, vix, book_isolated,
            nq_conf=dict(
                nq_conf_enable=True, nq_on_1m=nq_on_1m,
                nq_conf_early_k=CENTER_K,
                nq_conf_early_div_bonus=CENTER_DB,
                nq_conf_early_same_penalty=CENTER_SP,
            ),
        )
        center_nets = day_nets(days, tx, vix, cfg_center)
        delta = [round(t - c, 2) for c, t in zip(control_nets, center_nets)]
        if pname in ("julsep25", "octdec25", "janmar26"):
            pooled_oos_center_delta.extend(delta)

        stats = full_stats(delta)
        out["periods"][pname] = dict(
            total_days_in_window=len(days),
            control_net_sum=round(sum(control_nets), 1),
            center_net_sum=round(sum(center_nets), 1),
            elapsed_s=round(time.time() - t0, 1),
            **stats,
        )
        print(
            f"{pname:10s} n={len(days):3d} mean_delta={stats['naive']['mean']:8.3f} "
            f"p_naive={stats['naive']['p_value_t']:.4f} lag1={stats['lag1_autocorr']:+.3f} "
            f"-> {stats['significance']['label']}  ({time.time()-t0:.1f}s)",
            flush=True,
        )
        per_period_cache[pname] = dict(days=days, tx=tx, gate=gate, nq_on_1m=nq_on_1m, control_nets=control_nets)

    naive = one_sample_test(pooled_oos_center_delta)
    ac1 = lag1_autocorr(pooled_oos_center_delta)
    hac = {str(L): newey_west_test(pooled_oos_center_delta, L)["p_value_hac"] for L in (1, 5, 10, 20)}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"], hac_p_by_maxlags=hac)
    out["pooled_oos_3quarters_center"] = dict(
        note="julsep25+octdec25+janmar26 concatenated (non-overlapping "
        "calendar quarters, independent of the w83 in-sample window)",
        n_days=len(pooled_oos_center_delta), naive=naive, lag1_autocorr=round(ac1, 3),
        hac_p_by_maxlags=hac, significance=sig,
    )
    print(
        f"POOLED OOS (3 quarters, center k={CENTER_K}) n={len(pooled_oos_center_delta)} "
        f"mean_delta={naive['mean']:.3f} p_naive={naive['p_value_t']:.4f} "
        f"lag1={ac1:+.3f} hac_p={hac} -> {sig['label']}",
        flush=True,
    )

    # --- neighbor-parameter robustness (pooled OOS 3 quarters only) ---
    def run_variant(k, db, sp):
        pooled = []
        for pname in ("julsep25", "octdec25", "janmar26"):
            if pname not in per_period_cache:
                continue
            c = per_period_cache[pname]
            cfg_v = make_cfg(
                c["gate"], v1m, vix, book_isolated,
                nq_conf=dict(
                    nq_conf_enable=True, nq_on_1m=c["nq_on_1m"],
                    nq_conf_early_k=k, nq_conf_early_div_bonus=db,
                    nq_conf_early_same_penalty=sp,
                ),
            )
            v_nets = day_nets(c["days"], c["tx"], vix, cfg_v)
            pooled.extend(round(t - ctl, 2) for ctl, t in zip(c["control_nets"], v_nets))
        return pooled

    for k in K_GRID:
        if k == CENTER_K:
            continue
        pooled = run_variant(k, CENTER_DB, CENTER_SP)
        stats = full_stats(pooled)
        out["param_grid"][f"k={k}"] = stats
        print(f"grid k={k:.2f} db={CENTER_DB} sp={CENTER_SP} n={stats['n_days']} "
              f"mean={stats['naive']['mean']:.3f} p={stats['naive']['p_value_t']:.4f} "
              f"-> {stats['significance']['label']}", flush=True)

    for db in DB_GRID:
        if db == CENTER_DB:
            continue
        pooled = run_variant(CENTER_K, db, CENTER_SP)
        stats = full_stats(pooled)
        out["param_grid"][f"div_bonus={db}"] = stats
        print(f"grid k={CENTER_K} db={db:.2f} sp={CENTER_SP} n={stats['n_days']} "
              f"mean={stats['naive']['mean']:.3f} p={stats['naive']['p_value_t']:.4f} "
              f"-> {stats['significance']['label']}", flush=True)

    for sp in SP_GRID:
        if sp == CENTER_SP:
            continue
        pooled = run_variant(CENTER_K, CENTER_DB, sp)
        stats = full_stats(pooled)
        out["param_grid"][f"same_penalty={sp}"] = stats
        print(f"grid k={CENTER_K} db={CENTER_DB} sp={sp:.2f} n={stats['n_days']} "
              f"mean={stats['naive']['mean']:.3f} p={stats['naive']['p_value_t']:.4f} "
              f"-> {stats['significance']['label']}", flush=True)

    # center k=0.5 counts as one of the grid points too, for a complete table
    out["param_grid"][f"k={CENTER_K}(center)"] = full_stats(pooled_oos_center_delta)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nWrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
