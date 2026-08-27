#!/usr/bin/env python3
"""Final combined 16-cell 30m-primary/1m-calib book, true re-simulation
(2026-08-09) -- synthesizes the 16 independent per-cell tuning agents'
verdicts into ONE book and re-validates it end-to-end.

For each of the 16 session x PV8 cells: if that cell's agent verdict was
ADOPT, use its winning_params; otherwise (NO_IMPROVEMENT / OOS_FAILED /
INSUFFICIENT_DATA) keep order.tmf_channel_pv16_book.specialized_cell_book()'s
own current-live-equivalent value for that cell untouched.

Only 3 of 16 cells were ADOPT: day|climax_up, day|climax_dn, day|normal.

Runs BOTH the 22-day in-sample set and the 66-day OOS set through:
  (a) the CURRENT LIVE 1-minute-primary recipe (unmodified engine, no
      monkeypatch -- classify_pv computed from 1-min bars as today)
  (b) the FINAL COMBINED 30-min-primary/1-min-calib book (regime feed from
      build_pv30_series + patched_classify_pv_factory, mechanics unchanged)
on the SAME days, and reports day-clustered paired stats (delta = b - a)
for each window, plus trade-count/day comparison.

Does NOT touch src/order/*.py, src/tmf_channel/causal_engine.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml. Reads (never writes) reports/research/channel_lab/
except for the one designated final-result JSON this task asked for.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from copy import deepcopy

sys.path.insert(0, "src")

import tmf_channel.causal_engine as ce  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import specialized_cell_book  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta  # noqa: E402

sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    JULY_DAYS,
    AUG_DAYS,
    SOURCE_FOR_DAY,
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

IN_SAMPLE_DAYS = JULY_DAYS + AUG_DAYS  # 22 days
OOS_DAYS = [d for d in list_days(source="tx_1m_fullnight_cache_full.json") if d < "2026-07-08"]

# ---------------------------------------------------------------------------
# Step 1: assemble the FINAL combined 16-cell book.
# ---------------------------------------------------------------------------
CELL_VERDICTS = {
    ("day", "climax_up"): "ADOPT",
    ("day", "climax_dn"): "ADOPT",
    ("day", "expand_up"): "NO_IMPROVEMENT",
    ("day", "expand_dn"): "NO_IMPROVEMENT",
    ("day", "contract"): "OOS_FAILED",
    ("day", "dry"): "INSUFFICIENT_DATA",
    ("day", "normal"): "ADOPT",
    ("day", "div_hh_weak_vol"): "INSUFFICIENT_DATA",
    ("night", "climax_up"): "INSUFFICIENT_DATA",
    ("night", "climax_dn"): "INSUFFICIENT_DATA",
    ("night", "expand_up"): "OOS_FAILED",
    ("night", "expand_dn"): "NO_IMPROVEMENT",
    ("night", "contract"): "NO_IMPROVEMENT",
    ("night", "dry"): "INSUFFICIENT_DATA",
    ("night", "normal"): "OOS_FAILED",
    ("night", "div_hh_weak_vol"): "NO_IMPROVEMENT",
}

ADOPTED_PARAMS = {
    ("day", "climax_up"): dict(
        hang_lo=45, hang_hi=90, max_hold_bars=30, early_fill_gamma=0, block=[],
        skip_quiet_mode="dry", bias=True, vixtwn_calib="blend", vixtwn_calib_gamma=5,
    ),
    ("day", "climax_dn"): dict(
        hang_lo=12, hang_hi=27, early_fill_gamma=11, max_hold_bars=38, block=["L", "S"],
    ),
    ("day", "normal"): dict(
        hang_lo=12, hang_hi=27, early_fill_gamma=13, max_hold_bars=38, block=["L", "S"],
        skip_quiet_mode="dry", bias=True, vixtwn_calib="blend", vixtwn_calib_gamma=5,
    ),
}


def build_final_book():
    base = deepcopy(specialized_cell_book())
    for (sess, pv), verdict in CELL_VERDICTS.items():
        if verdict == "ADOPT":
            base[sess][pv].update(deepcopy(ADOPTED_PARAMS[(sess, pv)]))
        # else: leave current-live-equivalent default untouched
    return base


FINAL_BOOK = build_final_book()


# ---------------------------------------------------------------------------
# Step 2: per-day dual run (baseline 1m-primary vs final 30m-primary book).
# ---------------------------------------------------------------------------
def _load_arrays(day: str, source: str):
    rows = load_day(day, source=source)
    if not rows:
        return None
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def run_baseline_1m(day: str, source: str, vix: dict) -> dict:
    arrs = _load_arrays(day, source)
    if arrs is None:
        return dict(day=day, n_trades=0, sum_pnl=0.0, skipped=True)
    O, H, L, C, V, T = arrs
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    # PAPER_RECIPE already embeds specialized_cell_book() as session_pv_book
    trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    return dict(day=day, n_trades=len(trades), sum_pnl=round(sum(t["pnl"] for t in trades), 1))


def run_final_30m(day: str, source: str, vix: dict) -> dict:
    arrs = _load_arrays(day, source)
    if arrs is None:
        return dict(day=day, n_trades=0, sum_pnl=0.0, skipped=True)
    O, H, L, C, V, T = arrs
    recipe = deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")
    recipe["session_pv_book"] = deepcopy(FINAL_BOOK)

    pv_series = build_pv30_series(T, O, H, L, C, V)
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, *_ = ce.simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    return dict(day=day, n_trades=len(trades), sum_pnl=round(sum(t["pnl"] for t in trades), 1))


def day_clustered_stats(deltas: list[float]) -> dict:
    n = len(deltas)
    if n < 2:
        return dict(n=n, mean=(deltas[0] if deltas else 0.0), std=0.0, t=0.0, p=1.0)
    mean = st.mean(deltas)
    std = st.stdev(deltas)
    if std == 0:
        t = 0.0
    else:
        t = mean / (std / (n ** 0.5))
    # two-sided p-value via normal approx (no scipy dependency assumed available)
    try:
        from scipy import stats as _sp

        p = float(2 * (1 - _sp.t.cdf(abs(t), df=n - 1)))
    except Exception:
        import math

        p = float(math.erfc(abs(t) / math.sqrt(2)))
    return dict(n=n, mean=round(mean, 3), std=round(std, 3), t=round(t, 4), p=round(p, 5))


def run_window(days: list[str], source_for_day) -> dict:
    vix = load_vixtwn_delta() or {}
    rows = []
    for day in days:
        source = source_for_day(day)
        base = run_baseline_1m(day, source, vix)
        new = run_final_30m(day, source, vix)
        rows.append(dict(day=day, baseline=base, final30m=new))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    valid = [r for r in rows if not r["baseline"].get("skipped") and not r["final30m"].get("skipped")]
    deltas = [r["final30m"]["sum_pnl"] - r["baseline"]["sum_pnl"] for r in valid]
    base_trades = [r["baseline"]["n_trades"] for r in valid]
    new_trades = [r["final30m"]["n_trades"] for r in valid]

    stats = day_clustered_stats(deltas)
    return dict(
        rows=rows,
        n_days=len(valid),
        vs_live_1m=stats,
        baseline_total_trades=sum(base_trades),
        baseline_mean_trades_per_day=round(st.mean(base_trades), 2) if base_trades else 0,
        baseline_total_pnl=round(sum(r["baseline"]["sum_pnl"] for r in valid), 1),
        final30m_total_trades=sum(new_trades),
        final30m_mean_trades_per_day=round(st.mean(new_trades), 2) if new_trades else 0,
        final30m_total_pnl=round(sum(r["final30m"]["sum_pnl"] for r in valid), 1),
    )


def main():
    is_source = lambda d: SOURCE_FOR_DAY[d]  # noqa: E731
    oos_source = lambda d: "tx_1m_fullnight_cache_full.json"  # noqa: E731

    print("=== IN-SAMPLE (22 days) ===", flush=True)
    is_result = run_window(IN_SAMPLE_DAYS, is_source)

    print("\n=== OOS (66 days) ===", flush=True)
    oos_result = run_window(OOS_DAYS, oos_source)

    print("\n=== SUMMARY ===")
    print("IN-SAMPLE vs live-1m:", json.dumps(is_result["vs_live_1m"]))
    print("  baseline trades/day:", is_result["baseline_mean_trades_per_day"],
          "final30m trades/day:", is_result["final30m_mean_trades_per_day"])
    print("  baseline total pnl:", is_result["baseline_total_pnl"],
          "final30m total pnl:", is_result["final30m_total_pnl"])
    print("OOS vs live-1m:", json.dumps(oos_result["vs_live_1m"]))
    print("  baseline trades/day:", oos_result["baseline_mean_trades_per_day"],
          "final30m trades/day:", oos_result["final30m_mean_trades_per_day"])
    print("  baseline total pnl:", oos_result["baseline_total_pnl"],
          "final30m total pnl:", oos_result["final30m_total_pnl"])

    out = dict(
        cell_verdicts={f"{s}|{p}": v for (s, p), v in CELL_VERDICTS.items()},
        final_book=FINAL_BOOK,
        in_sample_stats=is_result,
        oos_stats=oos_result,
        vs_live_1m_in_sample=is_result["vs_live_1m"],
        vs_live_1m_oos=oos_result["vs_live_1m"],
        trade_count_comparison=dict(
            in_sample=dict(
                baseline_total=is_result["baseline_total_trades"],
                baseline_per_day=is_result["baseline_mean_trades_per_day"],
                final30m_total=is_result["final30m_total_trades"],
                final30m_per_day=is_result["final30m_mean_trades_per_day"],
            ),
            oos=dict(
                baseline_total=oos_result["baseline_total_trades"],
                baseline_per_day=oos_result["baseline_mean_trades_per_day"],
                final30m_total=oos_result["final30m_total_trades"],
                final30m_per_day=oos_result["final30m_mean_trades_per_day"],
            ),
        ),
    )
    out_path = "reports/research/channel_lab/tmf_30m_primary_16cell_tuned_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
