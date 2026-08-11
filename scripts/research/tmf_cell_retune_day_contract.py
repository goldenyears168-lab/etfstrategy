#!/usr/bin/env python3
"""TMF channel · retune day|contract cell under the new CONTINUOUS session_side_gate.

Assigned cell: day|contract. Current live default (specialized_cell_book(),
CELL_TUNE_V2 layer, later-wins over SPECIALIZED_PATCHES) =
hang_lo=10.0, hang_hi=25.0, early_fill_gamma=11.0, max_hold_bars=38, block=[].

Methodology: see docs/agent-brief.md task instructions (2026-08-10 continuous-
gate cell retune campaign). Re-simulate 22 in-sample days with the CURRENT
16-cell book as baseline vs candidate books that vary ONLY day|contract, day-
clustered net-pnl delta over trades where regime_e=="contract" and entry
session=="day" (08:45..13:45), paired t-test. Best in-sample candidate (if
any) validated once on the 66-day OOS window.

Does not touch src/order/*.py, src/tmf_channel/causal_engine.py,
src/tmf_channel/nq_gate.py, config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from tmf_continuous_gate_vs_frozen_anchor import (  # noqa: E402
    continuous_gate_for_day,
    JULY_DAYS,
    AUG_DAYS,
    SOURCE_FOR_DAY,
)
from tmf_order_layer_aware_replay import patch_nq_gate_for_backfill  # noqa: E402

SESSION = "day"
PV = "contract"

CURRENT_DEFAULT = dict(hang_lo=10.0, hang_hi=25.0, early_fill_gamma=11.0, max_hold_bars=38)

# 6-12 candidates: hang_lo/hang_hi sweep (wider + narrower than current),
# max_hold_bars variants, early_fill_gamma variants, and full block.
CANDIDATES: list[tuple[str, dict]] = [
    ("wider_lo8_hi30", dict(hang_lo=8.0, hang_hi=30.0, early_fill_gamma=11.0, max_hold_bars=38)),
    ("narrower_lo13_hi22", dict(hang_lo=13.0, hang_hi=22.0, early_fill_gamma=11.0, max_hold_bars=38)),
    ("freeze_default_lo15_hi30_mh30", dict(hang_lo=15.0, hang_hi=30.0, early_fill_gamma=8.0, max_hold_bars=30)),
    ("pre_v2_lo13_hi28", dict(hang_lo=13.0, hang_hi=28.0, early_fill_gamma=11.0, max_hold_bars=38)),
    ("wider_hi_lo10_hi32", dict(hang_lo=10.0, hang_hi=32.0, early_fill_gamma=11.0, max_hold_bars=38)),
    ("shorter_hold_mh25", dict(hang_lo=10.0, hang_hi=25.0, early_fill_gamma=11.0, max_hold_bars=25)),
    ("longer_hold_mh50", dict(hang_lo=10.0, hang_hi=25.0, early_fill_gamma=11.0, max_hold_bars=50)),
    ("gamma_off", dict(hang_lo=10.0, hang_hi=25.0, early_fill_gamma=0.0, max_hold_bars=38)),
    ("gamma_up16", dict(hang_lo=10.0, hang_hi=25.0, early_fill_gamma=16.0, max_hold_bars=38)),
    ("full_block", dict(block=["L", "S"])),
]

IS_DAYS = JULY_DAYS + AUG_DAYS


def in_day_session(t_iso: str) -> bool:
    hm = t_iso.split("T", 1)[1][:5]
    return "08:45" <= hm < "13:45"


def cell_pnl(trades: list[dict]) -> float:
    return round(
        sum(
            t["pnl"]
            for t in trades
            if t.get("regime_e") == PV and in_day_session(t.get("et", ""))
        ),
        1,
    )


def build_book(overrides: dict) -> dict:
    book = deepcopy(specialized_cell_book())
    book[SESSION][PV].update(deepcopy(overrides))
    return book


def run_day_all_books(day: str, books: dict[str, dict], vix: dict) -> dict[str, float] | None:
    source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = bar_timestamps(day, rows, source=source)

    gate = continuous_gate_for_day(day, T, source=source)

    out = {}
    for name, book in books.items():
        recipe = dict(PAPER_RECIPE)
        recipe.setdefault("hang_anchor", "O")
        recipe["session_pv_book"] = book
        recipe["session_side_gate"] = gate
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        out[name] = cell_pnl(trades)
    return out


def paired_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    mean_d = st.mean(deltas) if n else 0.0
    std_d = st.stdev(deltas) if n > 1 else 0.0
    t_stat = mean_d / (std_d / (n ** 0.5)) if std_d > 0 else 0.0
    try:
        from scipy import stats as sp

        p_val = float(2 * (1 - sp.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else None
    except Exception:
        p_val = None
    return dict(n=n, mean=mean_d, std=std_d, t=t_stat, p=p_val)


def main():
    patch_nq_gate_for_backfill(lookback_days=60)
    vix = load_vixtwn_delta() or {}

    baseline_book = specialized_cell_book()
    books = {"baseline": baseline_book}
    for name, overrides in CANDIDATES:
        books[name] = build_book(overrides)

    per_day_rows = []
    n_appearances_baseline = 0
    for day in IS_DAYS:
        r = run_day_all_books(day, books, vix)
        if r is None:
            print(f"{day}: skipped (no rows)")
            continue
        per_day_rows.append((day, r))
        print(day, json.dumps(r))

    print(f"\nIn-sample days used: {len(per_day_rows)}")

    # Count trade appearances for baseline to sanity-check sample thinness.
    # (Re-derive count, not just pnl.)
    appearances = 0
    for day in IS_DAYS:
        source = SOURCE_FOR_DAY.get(day, "tx_1m_fullnight_cache_full.json")
        rows = load_day(day, source=source)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]
        H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]
        C = [float(r["c"]) for r in rows]
        V = [float(r.get("v") or 0) for r in rows]
        T = bar_timestamps(day, rows, source=source)
        gate = continuous_gate_for_day(day, T, source=source)
        recipe = dict(PAPER_RECIPE)
        recipe.setdefault("hang_anchor", "O")
        recipe["session_pv_book"] = baseline_book
        recipe["session_side_gate"] = gate
        trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        appearances += sum(
            1 for t in trades if t.get("regime_e") == PV and in_day_session(t.get("et", ""))
        )
    print(f"baseline day|contract trade appearances across {len(per_day_rows)} IS days: {appearances}")

    results = {}
    for name, _ in CANDIDATES:
        deltas = [r[name] - r["baseline"] for _, r in per_day_rows]
        stats = paired_stats(deltas)
        # Single-day-artifact check: exclude the largest-|delta| day.
        if len(deltas) > 1:
            idx_max = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
            excl = deltas[:idx_max] + deltas[idx_max + 1:]
            stats_excl = paired_stats(excl)
        else:
            stats_excl = None
        results[name] = dict(stats=stats, stats_excl_max_day=stats_excl,
                              sum_delta=round(sum(deltas), 1))
        print(f"\n[{name}] {results[name]}")

    out_path = "/tmp/tmf_cell_retune_day_contract_is.json"
    try:
        with open(out_path, "w") as f:
            json.dump(dict(appearances=appearances, per_day=per_day_rows, results=results), f,
                       indent=2, ensure_ascii=False)
        print(f"\nWrote {out_path}")
    except Exception as e:
        print(f"(could not write {out_path}: {e})")


if __name__ == "__main__":
    main()
