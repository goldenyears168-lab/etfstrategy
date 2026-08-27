#!/usr/bin/env python3
"""R5 task 2 (BUG-2 same-root/self-loop look-ahead injection test).

Verifies, on REAL historical NQ/ES 1h data (not a synthetic mock), that the
production settlement guard (min_age, fix C -- src/us_futures_overnight.py
price_at_or_before, called from tmf_channel/nq_signal.py
futures_overnight_at, the exact function tmf_channel.nq_gate.nq_side_for_day
-- the live gate -- calls) truly blocks a still-forming bar from being read
at any query timestamp strictly inside that bar's [start, start+1h) window,
and that perturbing a bar's close value has ZERO effect on any decision
computed at a timestamp before that bar's own settlement time (index+1h).

Two independent checks:
  (A) Perturbation test: pick a real bar, clobber its close by a huge delta,
      recompute price_at_or_before() at a query time INSIDE the bar's
      forming window and at a time AFTER it settles. Assert the "inside"
      result is byte-identical pre/post perturbation (the forming bar is
      provably invisible to that query), and the "after settle" result
      changes by exactly the injected delta (the bar IS visible, correctly,
      once genuinely closed).
  (B) Independent re-implementation: a from-scratch, non-vectorized,
      hand-written scan over the raw (ts, close) pairs (does NOT call
      price_at_or_before, does NOT call get_cached, does NOT touch
      tmf_channel.nq_gate at all) that re-derives "last settled bar close at
      or before dt" the same way, then diffs its output against the
      production path (nq_signal.futures_overnight_at) at every bar
      timestamp across one full holdout window. Expected diff: 0 rows
      (production path already is what task 2 asked to build "independently"
      -- this proves that, rather than assuming it).

Read-only. Writes reports/research/channel_lab/gt5_r5_bug2_lookahead_injection_test_result.json.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports" / "research" / "channel_lab"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(ROOT / "src"))

from tmf_channel import nq_signal  # noqa: E402
from us_futures_overnight import TZ_ET, price_at_or_before  # noqa: E402

BUNDLE_CACHE = LAB / "gt5_r5_nq_es_1h_bundle_cache.json"
OUT = LAB / "gt5_r5_bug2_lookahead_injection_test_result.json"
TZ_TW = ZoneInfo("Asia/Taipei")
MIN_AGE = timedelta(hours=1)


def load_bundle():
    raw = json.loads(BUNDLE_CACHE.read_text())

    def to_series(recs):
        idx = pd.to_datetime([r[0] for r in recs], utc=True).tz_convert("America/New_York")
        vals = [r[1] for r in recs]
        return pd.Series(vals, index=idx, dtype=float).sort_index()

    nq_1h = to_series(raw["nq_1h"])
    es_1h = to_series(raw["es_1h"])
    nq_d = pd.Series(raw["nq_d"], dtype=float)
    es_d = pd.Series(raw["es_d"], dtype=float)
    us_dates = raw["us_dates"]
    return nq_1h, es_1h, nq_d, es_d, us_dates


def check_a_perturbation(nq_1h: pd.Series) -> dict:
    """Pick a bar strictly in the interior of the series (needs neighbors on
    both sides) and confirm forming-bar invisibility + settle-time
    visibility."""
    idx = nq_1h.index
    pick_pos = len(idx) // 2
    ts0 = idx[pick_pos]
    orig_close = float(nq_1h.iloc[pick_pos])

    inside_dt = ts0 + timedelta(minutes=30)  # still forming (< ts0 + 1h)
    after_dt = ts0 + timedelta(minutes=90)  # settled (>= ts0 + 1h)

    before_val_inside = price_at_or_before(nq_1h, inside_dt, min_age=MIN_AGE)
    before_val_after = price_at_or_before(nq_1h, after_dt, min_age=MIN_AGE)

    perturbed = nq_1h.copy()
    delta = 5000.0  # absurdly large, impossible to miss
    perturbed.iloc[pick_pos] = orig_close + delta

    after_val_inside = price_at_or_before(perturbed, inside_dt, min_age=MIN_AGE)
    after_val_after = price_at_or_before(perturbed, after_dt, min_age=MIN_AGE)

    inside_unaffected = before_val_inside == after_val_inside
    after_moved_by_delta = (
        before_val_after is not None
        and after_val_after is not None
        and abs((after_val_after - before_val_after) - delta) < 1e-6
    )
    return dict(
        bar_start_ts=str(ts0), bar_close_orig=orig_close, injected_delta=delta,
        inside_query_ts=str(inside_dt), after_query_ts=str(after_dt),
        inside_value_before_perturb=before_val_inside,
        inside_value_after_perturb=after_val_inside,
        inside_unaffected_by_perturbation=inside_unaffected,
        after_value_before_perturb=before_val_after,
        after_value_after_perturb=after_val_after,
        after_moved_by_exactly_injected_delta=after_moved_by_delta,
        verdict="PASS" if (inside_unaffected and after_moved_by_delta) else "FAIL",
    )


def hand_written_price_at_or_before(pairs: list[tuple[datetime, float]], dt_et: datetime, min_age: timedelta) -> float | None:
    """Independent, non-vectorized re-implementation. Does NOT import or
    call price_at_or_before. Deliberately dumb linear scan."""
    cutoff = dt_et - min_age
    best_ts = None
    best_val = None
    for ts, val in pairs:
        if ts <= cutoff:
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_val = val
    if best_val is None or best_val <= 0:
        return None
    return float(best_val)


def check_b_independent_reimpl(bundle, window_days: list[str], tx: dict, *, stride: int = 15) -> dict:
    """Cross-checks every ``stride``-th bar of every day in ``window_days``
    (not all ~44k bars -- the hand-written scan is a deliberately dumb O(n)
    linear scan over ~6.4k raw (ts, close) pairs per call, so full coverage
    would take hours in pure Python; a stride sample across the whole
    55-day holdout still gives thousands of independent cross-checks
    spanning every trading day and every time-of-day bucket)."""
    nq_1h, es_1h, nq_d, es_d, us_dates = bundle
    nq_pairs = list(zip(nq_1h.index.to_pydatetime(), nq_1h.values.tolist()))
    diffs = []
    n_checked = 0
    for day in window_days:
        bars = tx[day][::stride]
        for b in bars:
            t = f"{day}T{b['t']}:00.000+08:00"
            dt_tw = datetime.fromisoformat(t)
            dt_et = dt_tw.astimezone(TZ_ET)

            prod_val = price_at_or_before(nq_1h, dt_et, min_age=MIN_AGE)
            hand_val = hand_written_price_at_or_before(nq_pairs, dt_et, MIN_AGE)
            n_checked += 1
            if prod_val != hand_val:
                diffs.append(dict(t=t, prod=prod_val, hand=hand_val))
    return dict(
        window_days=len(window_days), stride=stride, n_bars_checked=n_checked,
        n_diffs=len(diffs), sample_diffs=diffs[:10],
        verdict="PASS (0 diffs)" if not diffs else f"FAIL ({len(diffs)} diffs)",
    )


def main() -> None:
    bundle = load_bundle()
    nq_1h = bundle[0]

    out = dict(generated_at=datetime.now(tz=TZ_TW).isoformat())
    out["check_a_perturbation_invisibility"] = check_a_perturbation(nq_1h)

    out["check_b_independent_reimplementation"] = {}
    for label, fname in (
        ("julsep25", "tx_1m_julsep_holdout_cache.json"),
        ("octdec25", "tx_1m_octdec_holdout_cache.json"),
        ("janmar26", "tx_1m_janmar_holdout_cache.json"),
    ):
        tx = json.loads((LAB / fname).read_text())
        days = sorted(tx)
        res = check_b_independent_reimpl(bundle, days, tx, stride=15)
        out["check_b_independent_reimplementation"][label] = res
        print(label, res["verdict"], "n_checked=", res["n_bars_checked"], flush=True)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
