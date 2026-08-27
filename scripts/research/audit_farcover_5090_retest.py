"""Research-only audit: re-test far_cover_lo/hi=50/90 (the runner-up candidate
from the original 2026-08-05 farcover retune sweep) against the CURRENT live
v1.4.0 recipe (order.tmf_channel_config.PAPER_RECIPE, far_cover 65/105),
across all 4 current sanctioned windows (w83/julsep25/octdec25/janmar26),
with day-clustered significance -- the stricter bar the original 5-window
adoption study did not use (it reported raw per-window net-P&L deltas, no
p-values, and its windows predate w83 entirely).

TRUE re-simulation: deepcopy PAPER_RECIPE, only far_cover_lo/hi changed,
everything else (including the full 16-cell session_pv_book) untouched.
Full day P&L (sum of ALL trade pnl that day), not filtered to any subset --
this dimension changes exit behavior for every open position, so its effect
is on total day P&L, not one regime cell.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

WINDOWS = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "julsep25": "tx_1m_julsep_holdout_cache.json",
    "octdec25": "tx_1m_octdec_holdout_cache.json",
    "janmar26": "tx_1m_janmar_holdout_cache.json",
}

CANDIDATES = {
    "baseline_65_105": (65.0, 105.0),
    "cand_50_90": (50.0, 90.0),
}

# 2026-06-05 (w83 window) is a known-contaminated session from the original
# retune audit; kept in the raw pooled_days but excluded from the headline
# "clean" summary stats below (see module docstring / audit history).
EXCLUDE_DAYS = {"w83|2026-06-05"}


def _arrays(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def build_recipe(flo: float, fhi: float) -> dict:
    r = deepcopy(PAPER_RECIPE)
    r["far_cover_lo"] = flo
    r["far_cover_hi"] = fhi
    return r


def day_pnls(cache_name: str, recipe: dict, vix: dict) -> dict[str, float]:
    out = {}
    for day in list_days(cache_name):
        rows = load_day(day, source=cache_name)
        if not rows:
            continue
        O, H, L, C, V, T = _arrays(day, rows)
        trades, *_rest = simulate(O, H, L, C, V, T, recipe, vix_delta=vix)
        out[day] = round(sum(float(t.get("pnl") or 0.0) for t in (trades or [])), 1)
    return out


def t_test_1samp(vals: list[float]):
    n = len(vals)
    if n < 2:
        return float("nan"), float("nan")
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se == 0:
        return (float("inf") if mean != 0 else 0.0), (0.0 if mean != 0 else 1.0)
    t = mean / se
    try:
        from scipy import stats

        pval = 2 * (1 - stats.t.cdf(abs(t), df=n - 1))
    except Exception:
        # normal approx
        pval = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return t, pval


def std(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (n - 1))


def summarize(pooled_days: dict[str, float], per_window: dict[str, dict[str, float]]):
    pooled_vals = list(pooled_days.values())
    n = len(pooled_vals)
    mean = sum(pooled_vals) / n
    sd = std(pooled_vals)
    t, p = t_test_1samp(pooled_vals)
    sharpe = mean / sd if sd else float("nan")
    win_rate = sum(1 for v in pooled_vals if v > 0) / n

    # single-day artifact check: exclude best and worst day
    srt = sorted(pooled_vals)
    excl_best = srt[:-1]
    excl_worst = srt[1:]
    mean_excl_best = sum(excl_best) / len(excl_best)
    mean_excl_worst = sum(excl_worst) / len(excl_worst)

    per_window_stats = {}
    for wname, dp in per_window.items():
        vals = list(dp.values())
        wn = len(vals)
        wmean = sum(vals) / wn if wn else float("nan")
        wsd = std(vals) if wn > 1 else float("nan")
        wwin = sum(1 for v in vals if v > 0) / wn if wn else float("nan")
        per_window_stats[wname] = {
            "n": wn,
            "mean": round(wmean, 1),
            "std": round(wsd, 1),
            "win_rate": round(wwin, 3),
            "sum": round(sum(vals), 1),
        }

    return {
        "n_days": n,
        "mean": round(mean, 2),
        "std": round(sd, 2),
        "sharpe_like": round(sharpe, 4) if sharpe == sharpe else None,
        "sum": round(sum(pooled_vals), 1),
        "win_rate": round(win_rate, 3),
        "t_stat": round(t, 3) if t == t else None,
        "p_value": round(p, 4) if p == p else None,
        "mean_excl_best_day": round(mean_excl_best, 2),
        "mean_excl_worst_day": round(mean_excl_worst, 2),
        "per_window": per_window_stats,
    }


def main():
    vix = load_vixtwn_delta()
    result = {}
    for cand_name, (flo, fhi) in CANDIDATES.items():
        recipe = build_recipe(flo, fhi)
        per_window = {}
        pooled_days = {}
        for wname, cache in WINDOWS.items():
            dp = day_pnls(cache, recipe, vix)
            per_window[wname] = dp
            for d, v in dp.items():
                pooled_days[f"{wname}|{d}"] = v
        result[cand_name] = {"per_window": per_window, "pooled_days": pooled_days}

    # summarize: headline "all_days" (raw pooled) + "excl_contaminated"
    # (drops EXCLUDE_DAYS, e.g. the 2026-06-05 w83 outlier session)
    summary = {}
    for cand_name, data in result.items():
        pooled_all = data["pooled_days"]
        per_window_excl = {
            wname: {
                d: v
                for d, v in dp.items()
                if f"{wname}|{d}" not in EXCLUDE_DAYS
            }
            for wname, dp in data["per_window"].items()
        }
        pooled_excl = {
            k: v for k, v in pooled_all.items() if k not in EXCLUDE_DAYS
        }
        summary[cand_name] = {
            "all_days": summarize(pooled_all, data["per_window"]),
            "excl_contaminated": summarize(pooled_excl, per_window_excl),
            "excluded_days": sorted(EXCLUDE_DAYS & pooled_all.keys()),
        }

    with open(
        "reports/research/channel_lab/audit_farcover_5090_retest.json", "w"
    ) as f:
        json.dump({"summary": summary, "raw": result}, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
