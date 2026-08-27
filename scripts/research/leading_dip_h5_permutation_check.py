#!/usr/bin/env python3
"""Permutation / randomization check for H5 (gap-up-then-dip) vs H3 (plain dip).

Item D of the creative-combination research plan (2026-08-07).

Reuses the EXACT event engine from the archived `leading-stage2-gap-fade`
topic (`scripts/research/archive/run_leading_stage2_crash_entry_backtest.py`
+ `run_leading_stage2_gap_fade_wfa.py`) — same signal definitions, same
OOS 70/30 chronological session split — and asks one question the original
writeup never answered: is H5's reported OOS edge (n=42, win=73.8%,
med=+15.65%) distinguishable from what you'd get by drawing a random
same-size subset of the same H3-qualified event pool (n=114)?

Read-only DB access. No writes to config/order.yaml, config/strategy.yaml,
src/order/, or launchd/. Does not commit/push.

Permutation design
-------------------
H5 is, by construction, a strict subset of the OOS H3 pool: every H5 event
already satisfies leading + Stage-2 + Minervini>=7 + gate-off + -6% dip
entry; H5 additionally requires an intraday gap-up-then-fade
(day_high >= prev_close * 1.01) before the dip trigger. So the sharpest
test of "does the gap-up-fade filter add real information, or did we just
get lucky drawing 42 events out of 114" is a label-permutation test:
randomly relabel which 42-of-114 OOS H3 events are "H5" (matching the
observed sample size and preserving the exact return values / event pool),
rebuild the null distribution of median return and win rate over many
reps, and see how often a random draw beats or matches what was actually
observed for the gap-up-fade subset.

This is PIT-safe: no new prices, no look-ahead is introduced by the
shuffle — we only permute which already-realized events get pooled
together, never change entry/exit timing or price data.

Two null constructions are run for robustness:
  1. Event-level permutation: random subset of 42 events drawn from the
     114-event OOS H3 pool (session/stock ties ignored).
  2. Session-block permutation: same idea but drawing whole sessions (all
     H3 events on a session move together), which respects same-day
     cross-sectional correlation among concurrently-triggered events.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research" / "archive"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from run_leading_stage2_crash_entry_backtest import (  # noqa: E402
    BENCH,
    IX,
    run_backtest,
)
from run_leading_stage2_gap_fade_wfa import (  # noqa: E402
    IS_RATIO,
    _h3_events,
    _h5_events,
    _split_ratio,
    _unique_sessions,
)

REPORT_DIR = ROOT / "reports" / "research" / "leading_dip_h5_permutation_check"
_TZ = ZoneInfo("Asia/Taipei")
SEED = 20260807
N_REPS = 5000
DIP = 0.06
HOLD = 5
# The archived report (leading_stage2_gap_fade_wfa_20260629.json) was generated
# 2026-06-29 against a 96-session H3 universe. The DB has since grown (161
# sessions as of this rerun), which shifts the chronological 70/30 OOS cut and
# changes which events fall in "OOS". To test the ORIGINAL headline number
# (not a different number produced by a larger, later dataset), we replicate
# the original session universe by capping sessions at this date before
# re-deriving the IS/OOS split with the same 0.7 ratio.
HISTORICAL_CUTOFF = "2026-06-29"
EVENTS_CACHE = REPORT_DIR / "_events_cache.pkl"


def _stats(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0, "win_pct": None, "med_pct": None, "avg_pct": None}
    return {
        "n": len(rets),
        "win_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 2),
        "med_pct": round(median(rets), 3),
        "avg_pct": round(sum(rets) / len(rets), 3),
    }


def _get_events(use_cache: bool):
    import pickle

    if use_cache and EVENTS_CACHE.exists():
        print(f"Loading cached events from {EVENTS_CACHE} …")
        with open(EVENTS_CACHE, "rb") as fh:
            return pickle.load(fh)

    conn = connect(DEFAULT_DB_PATH)
    stock_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_kbar_1m ORDER BY stock_id"
        ).fetchall()
        if r[0] not in {BENCH, IX}
    ]
    print(f"Collecting events (exact reuse of archived engine) · {len(stock_ids)} names …")
    events = run_backtest(conn, stock_ids)
    conn.close()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_CACHE, "wb") as fh:
        pickle.dump(events, fh)
    print(f"Cached {len(events)} raw events to {EVENTS_CACHE}")
    return events


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--use-cache", action="store_true")
    args = parser.parse_args()

    rng = np.random.default_rng(SEED)

    events = _get_events(args.use_cache)

    h3_all_current = _h3_events(events, dip=DIP, hold=HOLD)
    # Replicate the ORIGINAL 2026-06-29 session universe (96 sessions) rather
    # than the current (grown) universe, so the permutation test targets the
    # actual reported headline number instead of a different number produced
    # by more recent data that didn't exist when H5/H3 was first written up.
    h3_all = [e for e in h3_all_current if e.session <= HISTORICAL_CUTOFF]
    h5_all = _h5_events(h3_all)
    sessions = _unique_sessions(h3_all)
    is_sess, oos_sess = _split_ratio(sessions, IS_RATIO)
    oos_set = set(oos_sess)

    h3_oos = [e for e in h3_all if e.session in oos_set]
    h5_oos = [e for e in h3_oos if e.gap_up_fade]
    not_h5_oos = [e for e in h3_oos if not e.gap_up_fade]

    obs_h3 = _stats([e.ret_pct for e in h3_oos])
    obs_h5 = _stats([e.ret_pct for e in h5_oos])

    print(f"[sanity] historical session universe: {len(sessions)} (original report: 96)")
    print(f"[sanity] current (un-capped) session universe: {len(_unique_sessions(h3_all_current))}")
    print(f"OOS sessions: {len(oos_sess)} (of {len(sessions)} total, IS ratio={IS_RATIO})")
    print(f"H3 OOS pool: n={obs_h3['n']} win={obs_h3['win_pct']}% med={obs_h3['med_pct']}% "
          f"(original report: n=114 win=61.4% med=3.87%)")
    print(f"H5 OOS subset: n={obs_h5['n']} win={obs_h5['win_pct']}% med={obs_h5['med_pct']}% "
          f"(original report: n=42 win=73.8% med=15.65%)")

    n_pool = len(h3_oos)
    n_h5 = len(h5_oos)
    pool_rets = np.array([e.ret_pct for e in h3_oos])
    pool_sessions = np.array([e.session for e in h3_oos])

    # --- Null 1: event-level permutation (draw n_h5 events at random from pool) ---
    null_med_evt = np.empty(N_REPS)
    null_win_evt = np.empty(N_REPS)
    idx_all = np.arange(n_pool)
    for r in range(N_REPS):
        draw = rng.choice(idx_all, size=n_h5, replace=False)
        sample = pool_rets[draw]
        null_med_evt[r] = np.median(sample)
        null_win_evt[r] = np.mean(sample > 0) * 100

    # --- Null 2: session-block permutation ---
    # Group OOS H3 events by session; randomly select whole sessions until
    # cumulative event count is >= n_h5 (then trim to exactly n_h5 by
    # random subsample within the last included session), replicating the
    # fact that H5 events cluster on specific "gap-up-then-dip" days.
    by_session: dict[str, list[float]] = defaultdict(list)
    for e in h3_oos:
        by_session[e.session].append(e.ret_pct)
    sess_keys = list(by_session.keys())
    sess_sizes = np.array([len(by_session[s]) for s in sess_keys])

    null_med_blk = np.empty(N_REPS)
    null_win_blk = np.empty(N_REPS)
    sess_idx_all = np.arange(len(sess_keys))
    for r in range(N_REPS):
        order = rng.permutation(sess_idx_all)
        picked: list[float] = []
        for si in order:
            if len(picked) >= n_h5:
                break
            picked.extend(by_session[sess_keys[si]])
        sample = np.array(picked[:n_h5]) if len(picked) >= n_h5 else np.array(picked)
        if sample.size == 0:
            null_med_blk[r] = np.nan
            null_win_blk[r] = np.nan
            continue
        null_med_blk[r] = np.median(sample)
        null_win_blk[r] = np.mean(sample > 0) * 100

    def _pvals(null_arr: np.ndarray, observed: float) -> dict[str, float]:
        valid = null_arr[~np.isnan(null_arr)]
        p_ge = float(np.mean(valid >= observed))
        return {
            "p_one_sided_null_ge_observed": round(p_ge, 4),
            "null_mean": round(float(np.mean(valid)), 3),
            "null_std": round(float(np.std(valid)), 3),
            "null_p5": round(float(np.percentile(valid, 5)), 3),
            "null_p50": round(float(np.percentile(valid, 50)), 3),
            "null_p95": round(float(np.percentile(valid, 95)), 3),
            "n_reps_valid": int(valid.size),
        }

    med_p_evt = _pvals(null_med_evt, obs_h5["med_pct"])
    win_p_evt = _pvals(null_win_evt, obs_h5["win_pct"])
    med_p_blk = _pvals(null_med_blk, obs_h5["med_pct"])
    win_p_blk = _pvals(null_win_blk, obs_h5["win_pct"])

    payload = {
        "topic_id": "leading-stage2-gap-fade",
        "item": "creative_combo_2026-08-07 item D",
        "generated_at": datetime.now(_TZ).replace(microsecond=0).isoformat(),
        "source_scripts": [
            "scripts/research/archive/run_leading_stage2_crash_entry_backtest.py",
            "scripts/research/archive/run_leading_stage2_gap_fade_wfa.py",
        ],
        "params": {
            "dip_pct": DIP,
            "hold_days": HOLD,
            "is_ratio": IS_RATIO,
            "seed": SEED,
            "n_reps": N_REPS,
            "historical_cutoff": HISTORICAL_CUTOFF,
            "note": "events capped at historical_cutoff to replicate the original "
            "2026-06-29 report's 96-session universe; DB has since grown "
            "(more sessions), which would otherwise shift the chronological "
            "70/30 OOS split and change which events land in OOS.",
        },
        "original_report_2026_06_29": {
            "artifact": "reports/research/leading-stage2-gap-fade/leading_stage2_gap_fade_wfa_20260629.json",
            "universe_sessions": 96,
            "h3_oos": {"n": 114, "win_pct": 61.4, "med_pct": 3.87},
            "h5_oos": {"n": 42, "win_pct": 73.8, "med_pct": 15.65},
        },
        "observed": {
            "session_universe_replicated": len(sessions),
            "h3_oos_pool": obs_h3,
            "h5_oos_subset": obs_h5,
            "not_h5_oos_subset": _stats([e.ret_pct for e in not_h5_oos]),
        },
        "permutation_event_level": {
            "median_return_pct": {**med_p_evt, "observed": obs_h5["med_pct"]},
            "win_rate_pct": {**win_p_evt, "observed": obs_h5["win_pct"]},
        },
        "permutation_session_block": {
            "median_return_pct": {**med_p_blk, "observed": obs_h5["med_pct"]},
            "win_rate_pct": {**win_p_blk, "observed": obs_h5["win_pct"]},
            "n_unique_oos_sessions_in_h3_pool": len(sess_keys),
        },
        "h5_events_trade_list": [
            {"stock_id": e.stock_id, "session": e.session, "ret_pct": round(e.ret_pct, 3)}
            for e in sorted(h5_oos, key=lambda e: (e.session, e.stock_id))
        ],
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = REPORT_DIR / "permutation_check.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_json}")

    print("\n=== Event-level permutation (n=5000, draw 42-of-114) ===")
    print(f"  median return: observed={obs_h5['med_pct']}% | null p50={med_p_evt['null_p50']}% "
          f"p95={med_p_evt['null_p95']}% | p(null>=obs)={med_p_evt['p_one_sided_null_ge_observed']}")
    print(f"  win rate:      observed={obs_h5['win_pct']}% | null p50={win_p_evt['null_p50']}% "
          f"p95={win_p_evt['null_p95']}% | p(null>=obs)={win_p_evt['p_one_sided_null_ge_observed']}")

    print("\n=== Session-block permutation (n=5000) ===")
    print(f"  median return: observed={obs_h5['med_pct']}% | null p50={med_p_blk['null_p50']}% "
          f"p95={med_p_blk['null_p95']}% | p(null>=obs)={med_p_blk['p_one_sided_null_ge_observed']}")
    print(f"  win rate:      observed={obs_h5['win_pct']}% | null p50={win_p_blk['null_p50']}% "
          f"p95={win_p_blk['null_p95']}% | p(null>=obs)={win_p_blk['p_one_sided_null_ge_observed']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
