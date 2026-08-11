#!/usr/bin/env python3
"""Side-level rescan of ALL 16 live cells (session x PV8), current live book
unmodified -- look for a (cell, side) that is net NEGATIVE in BOTH IS_DAYS
and OOS_66d, i.e. an already-live, fully-unblocked cell where one side is a
drag that a NEW partial block could remove. One simulate() per day (baseline
book only); trades tagged post-hoc by (session, regime_e, side) using the
exact same session split the engine itself uses (hm >= "15:00" or hm <
"05:00" -> night, else day), applied to each trade's entry time (et).

Read-only research. Does not touch causal_engine.py / src/order/*.
"""
from __future__ import annotations

import statistics as st
import sys
from collections import defaultdict
from copy import deepcopy
from math import erf, sqrt

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import continuous_gate_for_day  # noqa: E402
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

JULY_DAYS = [
    "2026-07-08", "2026-07-09", "2026-07-13", "2026-07-14", "2026-07-15",
    "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21", "2026-07-22",
    "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
    "2026-07-30", "2026-07-31",
]
AUG_DAYS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
IS_DAYS = JULY_DAYS + AUG_DAYS
SOURCE_FOR_DAY = {d: "tx_1m_fullnight_cache_full.json" for d in JULY_DAYS}
SOURCE_FOR_DAY.update({d: "tx_1m_tick_built_fullnight_aug" for d in AUG_DAYS})

OOS_DAYS = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

PV8 = (
    "climax_up", "climax_dn", "expand_up", "expand_dn",
    "contract", "dry", "normal", "div_hh_weak_vol",
)
CELLS = [(sess, pv) for sess in ("day", "night") for pv in PV8]


def _hhmm(ts: str) -> str:
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]


def _sess_of(ts: str) -> str:
    hm = _hhmm(ts)
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def run_day(day: str, source: str, vix: dict, book: dict):
    """One baseline simulate() call; returns list of (sess, pv, side, pnl)."""
    rows = load_day(day, source=source)
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0.0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_side_gate"] = continuous_gate_for_day(day, T, source=SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json"))
    recipe["session_pv_book"] = deepcopy(book)

    trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix or {})
    out = []
    for tr in trades:
        sess = _sess_of(tr.get("et") or T[tr["eb"]])
        pv = tr.get("regime_e", "?")
        side = tr.get("s")
        out.append((sess, pv, side, float(tr["pnl"])))
    return out


def summarize(per_day_records: dict[str, list[tuple]]):
    """per_day_records: day -> list of (sess, pv, side, pnl).
    Returns cell_side -> {"pnl": total, "n": count, "per_day": {day: sum_pnl}}
    """
    agg: dict[tuple, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0, "per_day": defaultdict(float)})
    for day, recs in per_day_records.items():
        for sess, pv, side, pnl in recs:
            key = (sess, pv, side)
            agg[key]["pnl"] += pnl
            agg[key]["n"] += 1
            agg[key]["per_day"][day] += pnl
    return agg


def ttest_1samp(vals: list[float]):
    n = len(vals)
    if n < 2:
        return None, None
    mean = st.mean(vals)
    sd = st.stdev(vals)
    if sd == 0:
        return mean, (0.0 if mean == 0 else 1.0)
    se = sd / sqrt(n)
    t = mean / se
    # two-sided p via normal approx of Student-t CDF is not exact; use erf-based
    # incomplete approach only as fallback -- prefer scipy if available.
    try:
        from scipy.stats import t as student_t
        p = 2 * (1 - student_t.cdf(abs(t), df=n - 1))
    except Exception:
        # crude normal approximation fallback
        p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return t, p


def main():
    print("Patching NQ gate for backfill (lookback_days=500)...")
    patch_nq_gate_for_backfill(lookback_days=500)
    vix = load_vixtwn_delta() or {}
    book = specialized_cell_book()

    print(f"IS_DAYS n={len(IS_DAYS)}  OOS_DAYS n={len(OOS_DAYS)}")

    is_records: dict[str, list[tuple]] = {}
    for d in IS_DAYS:
        is_records[d] = run_day(d, SOURCE_FOR_DAY[d], vix, book)
        print(f"  IS {d}: {len(is_records[d])} trades")

    oos_records: dict[str, list[tuple]] = {}
    for d in OOS_DAYS:
        oos_records[d] = run_day(d, "tx_1m_fullnight_cache_full.json", vix, book)
    print(f"OOS done: {sum(len(v) for v in oos_records.values())} trades across {len(oos_records)} days")

    is_agg = summarize(is_records)
    oos_agg = summarize(oos_records)

    print("\n=== cell,side summary (IS vs OOS) ===")
    print(f"{'cell':<28}{'side':<5}{'IS_pnl':>10}{'IS_n':>6}{'OOS_pnl':>10}{'OOS_n':>6}  block?")
    flagged = []
    for sess, pv in CELLS:
        cur_block = book[sess][pv].get("block") or []
        for side in ("L", "S"):
            key = (sess, pv, side)
            isd = is_agg.get(key, {"pnl": 0.0, "n": 0})
            oosd = oos_agg.get(key, {"pnl": 0.0, "n": 0})
            blocked_flag = "BLOCKED" if side in cur_block else ("" if not cur_block else "")
            print(
                f"{sess+'|'+pv:<28}{side:<5}{isd['pnl']:>10.1f}{isd['n']:>6}"
                f"{oosd['pnl']:>10.1f}{oosd['n']:>6}  {blocked_flag}"
            )
            if (
                not cur_block  # cell fully unblocked (block=[]) -- the task's target set
                and isd["n"] >= 10
                and oosd["n"] >= 10
                and isd["pnl"] < 0
                and oosd["pnl"] < 0
            ):
                flagged.append((sess, pv, side, isd, oosd))

    print("\n=== FLAGGED (cell fully unblocked block=[], one side net-negative BOTH IS and OOS, n>=10 each) ===")
    if not flagged:
        print("NONE")
    for sess, pv, side, isd, oosd in flagged:
        print(f"{sess}|{pv} side={side}  IS pnl={isd['pnl']:.1f} n={isd['n']}  OOS pnl={oosd['pnl']:.1f} n={oosd['n']}")

    if not flagged:
        return

    # pick single most negative (by combined IS+OOS pnl) for full significance test
    flagged.sort(key=lambda x: x[3]["pnl"] + x[4]["pnl"])
    sess, pv, side, isd, oosd = flagged[0]
    print(f"\n=== Full significance test: block side={side} in {sess}|{pv} ===")

    cand_book = deepcopy(book)
    cur_block = list(cand_book[sess][pv].get("block") or [])
    if side not in cur_block:
        cur_block.append(side)
    cand_book[sess][pv]["block"] = cur_block

    for label, days, source_map, base_records in (
        ("IS", IS_DAYS, SOURCE_FOR_DAY, is_records),
        ("OOS", OOS_DAYS, {d: "tx_1m_fullnight_cache_full.json" for d in OOS_DAYS}, oos_records),
    ):
        deltas = []
        for d in days:
            src = source_map[d]
            base_net = sum(pnl for (_, _, _, pnl) in base_records[d])
            cand_recs = run_day(d, src, vix, cand_book)
            cand_net = sum(pnl for (_, _, _, pnl) in cand_recs)
            deltas.append(cand_net - base_net)
        n = len(deltas)
        mean = st.mean(deltas)
        t, p = ttest_1samp(deltas)
        top_idx = max(range(n), key=lambda i: abs(deltas[i]))
        excl = [d for i, d in enumerate(deltas) if i != top_idx]
        excl_mean = st.mean(excl) if excl else float("nan")
        print(
            f"{label}: n={n} mean_delta/day={mean:.2f}pt t={t:.3f} p={p:.4f} "
            f"excl_top_day_mean={excl_mean:.2f}pt (top day delta={deltas[top_idx]:.2f})"
        )


if __name__ == "__main__":
    main()
