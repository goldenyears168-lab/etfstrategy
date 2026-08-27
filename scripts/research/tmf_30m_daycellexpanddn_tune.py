#!/usr/bin/env python3
"""Cell tuning: day|expand_dn under the 30-min-primary/1-min-calib PV8
architecture (see scripts/research/tmf_30m_primary_1m_calib_prototype.py
for the prototyped mechanism -- this script reuses it verbatim and does
NOT re-derive it).

Assigned cell: day|expand_dn. Only this cell's params are varied; all other
15 cells stay at the current-live-equivalent default
(order.tmf_channel_pv16_book.specialized_cell_book()).

Methodology: day-clustered paired comparison (candidate cell trades minus
baseline cell trades, net pnl, per day) across the 22-day in-sample window,
then OOS validation of the single winning candidate on the 66-day
pre-2026-07-08 window. See module docstring in the orchestrating prompt for
full spec; not reproduced here.

Does NOT touch src/tmf_channel/causal_engine.py, src/order/*.py,
config/order.yaml, .env, launchd/, scripts/order/, config/strategy.yaml,
config/strategies.yaml.
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

# Reuse the prototype's PIT-safe 30m PV8 series builder + monkeypatch factory
# verbatim (no re-derivation).
sys.path.insert(0, "scripts/research")
from tmf_30m_primary_1m_calib_prototype import (  # noqa: E402
    build_pv30_series,
    patched_classify_pv_factory,
)

_ORIG_CLASSIFY_PV = ce.classify_pv

SESS = "day"
PV = "expand_dn"

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
for d in OOS_DAYS:
    SOURCE_FOR_DAY[d] = "tx_1m_fullnight_cache_full.json"

CURRENT_DEFAULT = dict(
    hang_lo=15.0, hang_hi=30.0, early_fill_gamma=8.0, max_hold_bars=30,
    block=["L"], skip_quiet_mode="dry", bias=True, vixtwn_calib="blend",
    vixtwn_calib_gamma=5.0,
)

CANDIDATES: dict[str, dict] = {
    "baseline_sanity": dict(hang_lo=15.0, hang_hi=30.0, early_fill_gamma=8.0, max_hold_bars=30, block=["L"]),
    "wide_b": dict(hang_lo=20.0, hang_hi=38.0, early_fill_gamma=8.0, max_hold_bars=30, block=["L"]),
    "wide_c_longhold": dict(hang_lo=22.0, hang_hi=42.0, early_fill_gamma=8.0, max_hold_bars=45, block=["L"]),
    "samehang_longhold": dict(hang_lo=15.0, hang_hi=30.0, early_fill_gamma=8.0, max_hold_bars=45, block=["L"]),
    "wide_e_longhold": dict(hang_lo=20.0, hang_hi=38.0, early_fill_gamma=8.0, max_hold_bars=45, block=["L"]),
    "very_wide_f": dict(hang_lo=25.0, hang_hi=48.0, early_fill_gamma=8.0, max_hold_bars=60, block=["L"]),
    "wide_b_gamma0": dict(hang_lo=20.0, hang_hi=38.0, early_fill_gamma=0.0, max_hold_bars=30, block=["L"]),
    "full_block": dict(hang_lo=20.0, hang_hi=38.0, early_fill_gamma=8.0, max_hold_bars=30, block=["L", "S"]),
}


def _bars_for_day(day: str):
    source = SOURCE_FOR_DAY[day]
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


def _session_of(et: str) -> str:
    hm = et[11:16]
    return "day" if "08:45" <= hm < "13:45" else "night"


def _cell_sum(trades, sess: str, pv: str) -> tuple[float, int]:
    tot, n = 0.0, 0
    for tr in trades:
        if tr.get("regime_e") != pv:
            continue
        if _session_of(tr["et"]) != sess:
            continue
        tot += float(tr["pnl"])
        n += 1
    return round(tot, 1), n


def _run(bars, pv_series, recipe, vix):
    ce.classify_pv = patched_classify_pv_factory(pv_series)
    try:
        trades, *_ = ce.simulate(*bars, recipe, vix_delta=vix)
    finally:
        ce.classify_pv = _ORIG_CLASSIFY_PV
    return trades


def evaluate(days: list[str], cand_params: dict, vix, cache: dict) -> dict:
    """Day-clustered baseline vs candidate net-pnl delta for SESS|PV."""
    base_book = specialized_cell_book()
    cand_book = deepcopy(base_book)
    cand_book[SESS][PV].update(cand_params)

    base_recipe = deepcopy(PAPER_RECIPE)
    base_recipe.setdefault("hang_anchor", "O")
    base_recipe["session_pv_book"] = base_book
    cand_recipe = deepcopy(PAPER_RECIPE)
    cand_recipe.setdefault("hang_anchor", "O")
    cand_recipe["session_pv_book"] = cand_book

    deltas = []
    base_ns, cand_ns = [], []
    per_day = []
    for day in days:
        if day not in cache:
            bars = _bars_for_day(day)
            if bars is None:
                cache[day] = None
                continue
            pv_series = build_pv30_series(bars[5], *bars[:5])
            cache[day] = (bars, pv_series)
        entry = cache[day]
        if entry is None:
            continue
        bars, pv_series = entry
        base_trades = _run(bars, pv_series, base_recipe, vix)
        cand_trades = _run(bars, pv_series, cand_recipe, vix)
        base_sum, base_n = _cell_sum(base_trades, SESS, PV)
        cand_sum, cand_n = _cell_sum(cand_trades, SESS, PV)
        deltas.append(round(cand_sum - base_sum, 1))
        base_ns.append(base_n)
        cand_ns.append(cand_n)
        per_day.append(dict(day=day, base_sum=base_sum, base_n=base_n, cand_sum=cand_sum, cand_n=cand_n, delta=round(cand_sum - base_sum, 1)))

    n = len(deltas)
    mean = st.mean(deltas) if n else 0.0
    std = st.stdev(deltas) if n > 1 else 0.0
    t_stat = (mean / (std / (n ** 0.5))) if (n > 1 and std > 0) else 0.0
    # two-sided p-value via normal approx (no scipy dependency assumed)
    try:
        from scipy import stats as sps  # type: ignore
        p_val = float(2 * (1 - sps.t.cdf(abs(t_stat), df=n - 1))) if n > 1 else 1.0
    except Exception:
        import math
        p_val = float(2 * (1 - 0.5 * (1 + math.erf(abs(t_stat) / (2 ** 0.5))))) if n > 1 else 1.0

    return dict(
        n=n, mean=round(mean, 2), std=round(std, 2), t=round(t_stat, 3), p=round(p_val, 4),
        total_base_n=sum(base_ns), total_cand_n=sum(cand_ns),
        per_day=per_day,
    )


def main():
    vix = load_vixtwn_delta() or {}
    cache_is: dict = {}
    results = {}
    for name, params in CANDIDATES.items():
        stats = evaluate(IS_DAYS, params, vix, cache_is)
        results[name] = stats
        print(f"{name}: params={params}")
        print(f"  IS n={stats['n']} mean={stats['mean']} std={stats['std']} t={stats['t']} p={stats['p']} "
              f"base_trades={stats['total_base_n']} cand_trades={stats['total_cand_n']}")
        # largest-|delta| day exclusion check
        if stats["per_day"]:
            biggest = max(stats["per_day"], key=lambda r: abs(r["delta"]))
            rest = [r["delta"] for r in stats["per_day"] if r["day"] != biggest["day"]]
            rest_mean = round(st.mean(rest), 2) if rest else 0.0
            print(f"  largest-|delta| day={biggest['day']} delta={biggest['delta']} "
                  f"(mean excl. it={rest_mean} vs full mean={stats['mean']})")

    # NB: reports/research/channel_lab/ is READ-ONLY for this task; write
    # our own result artifact elsewhere.
    out_path = "/tmp/tmf_30m_day_expand_dn_tune_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
