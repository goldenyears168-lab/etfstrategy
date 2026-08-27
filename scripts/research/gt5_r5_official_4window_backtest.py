#!/usr/bin/env python3
"""R5 task 1: official 4-window CELL_TUNE_V2 backtest under the CURRENT full
production gate mechanism (continuous per-bar anchor [fix B, 2026-08-10] +
1h-bar min_age settlement guard [fix C, 2026-08-10 workspace-only]).

Why a new script instead of reusing audit_cell_tune_v2_significance.py
verbatim: that script's gate came from
``r_strict_paper_bias_overlay.build_nq_gate()``, which (a) anchors ONCE per
session (night-open bar, or 08:45 for day-only days) -- the pre-fix-B frozen
anchor -- and (b) loads NQ/ES 1h via ``r5_synth_p0p1_vs_baseline.load_futures_1h()``,
a static on-disk JSON snapshot with no ``min_age`` settlement guard at all
(pre-fix-C). Its "5/5 significant" claim (recorded in config/strategy.yaml's
graduation note for final_v1_3_0_pv16_celltune_v2) has never been reproduced
under fix B+C. This script swaps in a per-bar-timestamp gate built from
``tmf_channel.nq_signal.futures_overnight_at`` (the exact function
``tmf_channel.nq_gate.nq_side_for_day`` -- the live production gate -- calls),
which already has NQ_ES_1H_MIN_AGE=1h baked in (see git diff of
src/tmf_channel/nq_signal.py, workspace-uncommitted fix C), recomputed fresh
at every bar's own timestamp (fix B's continuous-anchor design, matching
scripts/research/tmf_continuous_gate_vs_frozen_anchor.py's
continuous_gate_for_day()) instead of once per session.

Read-only. Writes reports/research/channel_lab/gt5_r5_official_4window_backtest_result.json.
"""
from __future__ import annotations

import json
import sys
import time
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

import r_cost_aware_cell_tune as ht  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from slow_cell_significance_helper import classify_significance  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
from us_futures_overnight import (  # noqa: E402
    ES_YAHOO,
    NQ_YAHOO,
    fetch_yahoo_daily_closes,
    fetch_yahoo_intraday_closes,
)

ov = ht.ov
eng = ht.eng
TZ_TW = ZoneInfo("Asia/Taipei")

OUT = LAB / "gt5_r5_official_4window_backtest_result.json"
BUNDLE_CACHE = LAB / "gt5_r5_nq_es_1h_bundle_cache.json"


def build_v1_2_0():
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


def _load_or_fetch_bundle():
    """One wide Yahoo fetch covering julsep25..today, cached to our own
    gt5_ scratch file (never touches nq_gate's live 30-min cache or the
    frozen research snapshot nq_signal.NQ_ES_1H_CACHE reads)."""
    if BUNDLE_CACHE.exists():
        raw = json.loads(BUNDLE_CACHE.read_text())
        import pandas as pd

        def to_series(recs):
            idx = pd.to_datetime([r[0] for r in recs], utc=True).tz_convert("America/New_York")
            vals = [r[1] for r in recs]
            return pd.Series(vals, index=idx, dtype=float).sort_index()

        nq_1h = to_series(raw["nq_1h"])
        es_1h = to_series(raw["es_1h"])
        nq_d = None
        es_d = None
        import pandas as pd

        nq_d = pd.Series(raw["nq_d"], dtype=float)
        es_d = pd.Series(raw["es_d"], dtype=float)
        us_dates = raw["us_dates"]
        print(f"loaded cached bundle: nq_1h n={len(nq_1h)} range={nq_1h.index.min()}..{nq_1h.index.max()}")
        return nq_1h, es_1h, nq_d, es_d, us_dates

    end = datetime.now(tz=TZ_TW).date()
    start = end.replace(year=end.year - 1, month=6, day=25)  # a few days before julsep25 starts
    print(f"fetching Yahoo NQ/ES 1h+daily {start}..{end} (one-shot, will cache)...")
    nq_1h = fetch_yahoo_intraday_closes(NQ_YAHOO, start, end, interval="1h")
    es_1h = fetch_yahoo_intraday_closes(ES_YAHOO, start, end, interval="1h")
    nq_d = fetch_yahoo_daily_closes(NQ_YAHOO, start, end)
    es_d = fetch_yahoo_daily_closes(ES_YAHOO, start, end)
    us_dates = sorted(set(nq_d.index.astype(str)) | set(es_d.index.astype(str)))
    BUNDLE_CACHE.write_text(
        json.dumps(
            dict(
                nq_1h=[[ts.isoformat(), float(v)] for ts, v in nq_1h.items()],
                es_1h=[[ts.isoformat(), float(v)] for ts, v in es_1h.items()],
                nq_d={k: float(v) for k, v in nq_d.items()},
                es_d={k: float(v) for k, v in es_d.items()},
                us_dates=us_dates,
            )
        )
    )
    print(f"fetched+cached: nq_1h n={len(nq_1h)} range={nq_1h.index.min()}..{nq_1h.index.max()}")
    return nq_1h, es_1h, nq_d, es_d, us_dates


def continuous_gate_for_days(days_bars: dict, bundle) -> dict[str, str]:
    """Per-bar-timestamp gate: recompute nq_signal.futures_overnight_at()
    (production function, min_age already applied inside it) fresh at every
    bar's own T[t] timestamp -- the fix-B design, matching
    tmf_continuous_gate_vs_frozen_anchor.py's continuous_gate_for_day()."""
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    out: dict[str, str] = {}
    for day, bars in days_bars.items():
        for b in bars:
            t = f"{day}T{b['t']}:00.000+08:00"
            dt_tw = datetime.fromisoformat(t)
            snap = nq_signal.futures_overnight_at(
                dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d, us_dates=us_dates
            )
            nq = None if snap is None else snap.get("nq_overnight_pct")
            side = nq_signal.bias_side(nq)
            out[t] = {"up": "L", "down": "S"}.get(side, "none")
    return out


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
    return float(model.pvalues[0])


def main() -> None:
    ht.set_cost(ht.LIVE)
    vix = ht.load_vixtwn_delta()
    v1m = eng.load_vixtwn_1m()
    v1_2_0 = build_v1_2_0()
    v1_3_0 = build_v1_3_0_v2only()
    bundle = _load_or_fetch_bundle()

    tx_main = json.loads(ov.TX_PATH.read_text())
    days_main = sorted(tx_main)
    periods = {
        "julsep25": ("cache", LAB / "tx_1m_julsep_holdout_cache.json"),
        "octdec25": ("cache", LAB / "tx_1m_octdec_holdout_cache.json"),
        "janmar26": ("cache", LAB / "tx_1m_janmar_holdout_cache.json"),
        "w83_IS": ("main", days_main[-83:]),
        "w25_IS": ("main", days_main[-25:]),
    }

    out = dict(
        title="R5 task1: CELL_TUNE_V2 4-window backtest, continuous per-bar "
        "gate + min_age (current production mechanism, fix B+C)",
        generated_at=datetime.now(tz=TZ_TW).isoformat(),
        periods={},
    )
    pooled_oos_delta = []
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
        gate = continuous_gate_for_days({d: tx[d] for d in days}, bundle)
        base_nets = day_nets_for_book(days, tx, vix, gate, v1m, v1_2_0)
        v2_nets = day_nets_for_book(days, tx, vix, gate, v1m, v1_3_0)
        delta = [b2 - b1 for b1, b2 in zip(base_nets, v2_nets)]
        if pname in ("julsep25", "octdec25", "janmar26"):
            pooled_oos_delta.extend(delta)

        naive = one_sample_test(delta)
        ac1 = lag1_autocorr(delta)
        hac = {str(L): newey_west_test(delta, L) for L in (1, 5, 10, 20)}
        sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"], hac_p_by_maxlags=hac)
        n_gate_l = sum(1 for v in gate.values() if v == "L")
        n_gate_s = sum(1 for v in gate.values() if v == "S")
        n_gate_none = sum(1 for v in gate.values() if v == "none")
        out["periods"][pname] = dict(
            n_days=len(days),
            net_v1_2_0=round(sum(base_nets), 1),
            net_v1_3_0=round(sum(v2_nets), 1),
            naive=naive, lag1_autocorr=round(ac1, 3),
            hac_p_by_maxlags=hac, significance=sig,
            gate_bar_counts=dict(L=n_gate_l, S=n_gate_s, none=n_gate_none),
            elapsed_s=round(time.time() - t0, 1),
        )
        print(
            f"{pname:10s} n={len(days):3d} v1.2.0={sum(base_nets):9.1f} "
            f"v1.3.0={sum(v2_nets):9.1f} mean_delta={naive['mean']:8.2f} "
            f"p_naive={naive['p_value_t']:.4f} lag1={ac1:+.3f} "
            f"hac_p={hac} -> {sig['label']}",
            flush=True,
        )

    naive = one_sample_test(pooled_oos_delta)
    ac1 = lag1_autocorr(pooled_oos_delta)
    hac = {str(L): newey_west_test(pooled_oos_delta, L) for L in (1, 5, 10, 20)}
    sig = classify_significance(mean=naive["mean"], p_naive=naive["p_value_t"], hac_p_by_maxlags=hac)
    out["pooled_oos_3quarters"] = dict(
        note="julsep25+octdec25+janmar26 concatenated (non-overlapping calendar "
        "quarters, independent of w83/w25 in-sample tuning window)",
        n_days=len(pooled_oos_delta), naive=naive, lag1_autocorr=round(ac1, 3),
        hac_p_by_maxlags=hac, significance=sig,
    )
    print(
        f"POOLED OOS (3 quarters) n={len(pooled_oos_delta)} "
        f"mean_delta={naive['mean']:.2f} p_naive={naive['p_value_t']:.4f} "
        f"lag1={ac1:+.3f} hac_p={hac} -> {sig['label']}",
        flush=True,
    )

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
