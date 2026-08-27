"""Independent adversarial re-check of night|expand_dn wide-band finding.

Read-only. Re-derives everything from scratch (own arithmetic, own t-test,
own single-trade-removal), and additionally checks whether unblocking this
cell changes OTHER cells' trades on the same day via max_lots=1 slot
contention (portfolio-level side effect not checked by the original agent).
"""
from __future__ import annotations

import statistics
from copy import deepcopy

from scipy import stats as sstats

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}
CELL_SESSION, CELL_REGIME = "night", "expand_dn"
CANDIDATES = [("1.5x", 27.0, 48.0), ("2x", 36.0, 64.0), ("3x", 54.0, 96.0)]


def _arrays(rows):
    return (
        [float(r["o"]) for r in rows],
        [float(r["h"]) for r in rows],
        [float(r["l"]) for r in rows],
        [float(r["c"]) for r in rows],
        [float(r.get("v") or 0) for r in rows],
        [str(r.get("t") or "") for r in rows],
    )


def is_night(hm):
    return hm >= "15:00" or hm < "05:00"


def build_recipe(hang_lo, hang_hi):
    recipe = deepcopy(PAPER_RECIPE)
    cell = recipe["session_pv_book"][CELL_SESSION][CELL_REGIME]
    cell["block"] = []
    cell["hang_lo"] = hang_lo
    cell["hang_hi"] = hang_hi
    return recipe


def run_all(recipe, vix):
    """Return {day: all_trades} across all 4 windows, tagging window+day."""
    out = {}
    for wname, cache in WINDOWS.items():
        for day in list_days(source=cache):
            rows = load_day(day, source=cache)
            if not rows:
                continue
            O, H, L, C, V, T = _arrays(rows)
            trades, *_ = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
            out[(wname, day)] = trades or []
    return out


def day_ttest(day_pnls):
    n = len(day_pnls)
    if n < 2:
        return None, None, n
    mean = statistics.mean(day_pnls)
    sd = statistics.stdev(day_pnls)
    if sd == 0:
        return None, None, n
    t = mean / (sd / n**0.5)
    p = 2 * (1 - sstats.t.cdf(abs(t), df=n - 1))
    return t, p, n


def main():
    vix = load_vixtwn_delta() or {}
    baseline_recipe = deepcopy(PAPER_RECIPE)  # live: expand_dn blocked
    baseline = run_all(baseline_recipe, vix)

    for label, lo, hi in CANDIDATES:
        recipe = build_recipe(lo, hi)
        cand = run_all(recipe, vix)

        # (A) isolate expand_dn|night trades, own arithmetic
        target_pnls = []
        by_day = {}
        for key, trades in cand.items():
            for tr in trades:
                if tr.get("regime_e") != CELL_REGIME:
                    continue
                if not is_night(tr.get("et") or ""):
                    continue
                target_pnls.append(tr["pnl"])
                by_day.setdefault(key, []).append(tr["pnl"])
        day_sums = [sum(v) for v in by_day.values()]
        t, p, n_days = day_ttest(day_sums)
        print(f"\n=== {label} (lo={lo} hi={hi}) ===")
        print(f"  n_trades={len(target_pnls)} n_days={n_days} sum={sum(target_pnls):.1f} "
              f"mean={statistics.mean(target_pnls) if target_pnls else 0:.2f} "
              f"win_rate={sum(1 for x in target_pnls if x>0)/len(target_pnls):.1%}")
        print(f"  day-clustered t={t}, p={p}")

        if len(target_pnls) >= 2:
            srt = sorted(target_pnls)
            worst, best = srt[0], srt[-1]
            total = sum(target_pnls)
            print(f"  own single-trade removal: total={total:.1f} "
                  f"minus_best({best:.1f})={total-best:.1f} minus_worst({worst:.1f})={total-worst:.1f}")
            # top-2 removal for robustness
            srt2 = sorted(target_pnls, reverse=True)
            print(f"  minus top-2 best({srt2[0]:.1f}+{srt2[1]:.1f})={total-srt2[0]-srt2[1]:.1f}")

        # (B) portfolio side-effect: does unblocking expand_dn change OTHER
        # cells' trades via max_lots=1 slot contention?
        other_delta = []
        contended_days = 0
        for key in cand:
            base_trades = baseline.get(key, [])
            cand_trades = cand[key]
            base_other = [tr for tr in base_trades if not (
                tr.get("regime_e") == CELL_REGIME and is_night(tr.get("et") or ""))]
            cand_other = [tr for tr in cand_trades if not (
                tr.get("regime_e") == CELL_REGIME and is_night(tr.get("et") or ""))]
            base_sum = sum(tr["pnl"] for tr in base_other)
            cand_sum = sum(tr["pnl"] for tr in cand_other)
            base_n = len(base_other)
            cand_n = len(cand_other)
            if base_n != cand_n or abs(base_sum - cand_sum) > 1e-6:
                contended_days += 1
                other_delta.append(cand_sum - base_sum)
        print(f"  side-effect on OTHER cells: {contended_days} days changed, "
              f"net delta to other-cell pnl = {sum(other_delta):.1f} "
              f"(target cell isolated sum was {sum(target_pnls):.1f})")


if __name__ == "__main__":
    main()
