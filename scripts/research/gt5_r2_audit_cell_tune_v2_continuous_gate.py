#!/usr/bin/env python3
"""R2 (2026-08-11): re-run the CELL_TUNE_V2 5/5-significance audit under the
CURRENT full NQ-gate mechanism (fix A day/night anchor split + fix B
continuous per-bar-timestamp anchor + fix C min_age=1h forming-bar
exclusion), instead of the frozen-at-session-open / stale-cache gate that
``scripts/research/audit_cell_tune_v2_significance.py`` used.

Diagnostic only. Does NOT touch config/strategy.yaml, config/strategies.yaml,
src/order/*.py, src/tmf_channel/*.py, launchd/, or .env. Read-only re-audit;
does not change CELL_TUNE_V2 itself.

Same paired day-level (v1_3_0_cell_tune_v2 - v1_2_0_baseline) methodology,
same 5 periods, same HAC/lag-1/pooled-OOS treatment as
audit_cell_tune_v2_significance.py -- the ONLY change is how
`session_side_gate` is built:

  OLD (audit_cell_tune_v2_significance.py): ov.build_nq_gate(days) -- one
  value per CALENDAR DAY, anchored once at night-open (or day 08:45),
  computed via r5_synth_p0p1_vs_baseline.py's ORIGINAL futures_overnight_at
  (no min_age, reads the static frozen
  nikkei_us_intraday_1h_cache.json snapshot) -- i.e. none of fixes A/B/C
  actually reach this harness's gate at all; it is fully decoupled from the
  live nq_gate.py / nq_signal.py code path.

  NEW (this script): one value per 1-MINUTE BAR TIMESTAMP, recomputed fresh
  at each bar via tmf_channel.nq_signal.futures_overnight_at() (the frozen
  in-package module actually imported by src/order/ live code, which now
  carries fix C's min_age=NQ_ES_1H_MIN_AGE=1h), fed from a wide one-shot
  Yahoo fetch (patch_nq_gate_for_backfill, same pattern
  tmf_continuous_gate_vs_frozen_anchor.py uses for its fix-B validation) so
  historical days outside nq_gate.py's live 10-day rolling window still
  resolve real NQ/ES data instead of "none". causal_engine.py's
  session_side_gate lookup already supports per-bar-timestamp keys (falls
  back to per-day keys) -- see SessionSideGatePerBarKeyTest.

Read-only. Writes:
  reports/research/channel_lab/gt5_r2_audit_cell_tune_v2_continuous_gate.json
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
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import r_cost_aware_cell_tune as ht  # noqa: E402
from order.tmf_channel_pv16_book import (  # noqa: E402
    CELL_TUNE_V2_PATCHES,
    SPECIALIZED_PATCHES,
    freeze_cell_book,
)
from slow_cell_significance_helper import classify_significance  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402
from tmf_channel import nq_signal  # noqa: E402
import tmf_channel.nq_gate as nq_gate_mod  # noqa: E402

ov = ht.ov
eng = ht.eng
OUT = LAB / "gt5_r2_audit_cell_tune_v2_continuous_gate.json"
_TZ = ZoneInfo("Asia/Taipei")

# Earliest period start is julsep25 (2025-07-01); today is well into
# 2026-08. Wide enough to cover every period below with margin, without
# trying to reach further back than Yahoo's 1h-interval history actually
# goes (found 2026-08-10: Yahoo serves 1h back to ~2025-05 for a wide
# enough single request).
NQ_LOOKBACK_DAYS = 440


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


def build_continuous_gate(days: list[str], tx: dict) -> dict[str, str]:
    """One session_side_gate value per 1-min BAR (not per day), recomputed
    fresh at each bar's own timestamp via the live-code nq_signal module
    (fix A anchor split + fix C min_age already baked into
    nq_signal.futures_overnight_at itself -- no extra plumbing needed here).
    Bundle is fetched once (get_cached, 30min throttle) and reused for
    every bar/day in this call.
    """
    bundle = nq_gate_mod.get_cached(
        "nq_futures_1h", 1800.0, nq_gate_mod._load_futures_bundle
    )
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    gate: dict[str, str] = {}
    for day in days:
        bars = tx.get(day) or []
        for b in bars:
            t = f"{day}T{b['t']}:00.000+08:00"
            dt_tw = datetime.fromisoformat(t).astimezone(_TZ)
            snap = nq_signal.futures_overnight_at(
                dt_tw, nq_1h=nq_1h, es_1h=es_1h, nq_d=nq_d, es_d=es_d,
                us_dates=us_dates,
            )
            nq = None if snap is None else snap.get("nq_overnight_pct")
            side = nq_signal.bias_side(nq)
            gate[t] = {"up": "L", "down": "S"}.get(side, "none")
    return gate


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
    patch_nq_gate_for_backfill(lookback_days=NQ_LOOKBACK_DAYS)

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

    out = dict(
        title="R2 (2026-08-11): CELL_TUNE_V2 5/5 significance re-audit under "
        "current full NQ-gate mechanism (fix A anchor split + fix B "
        "continuous anchor + fix C min_age=1h)",
        nq_lookback_days=NQ_LOOKBACK_DAYS,
        periods={},
    )
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

        t0 = time.time()
        gate = build_continuous_gate(days, tx)
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
              f"hac_p={hac} -> {sig['label']} ({time.time()-t0:.1f}s)", flush=True)

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
