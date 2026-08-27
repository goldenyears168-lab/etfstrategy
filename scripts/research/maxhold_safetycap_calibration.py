"""Item #3: 90-min safety cap calibration research.

Pure read-only research: build the true holding-time distribution of every
trade under the CURRENT live PAPER_RECIPE (final_v1_4_0_pv16_normal_dhwv_block)
across w83 + 3 holdout windows, via simulate(). Compare tail (p99, max) vs the
90-minute flat safety-net cap implemented tonight in
check_max_hold_safety_net(), and vs a per-cell-informed cap based on each
cell's own max_hold_bars (16-38) x a safety multiplier.

Does NOT touch src/order/tmf_channel_order.py or any live path.
"""
from __future__ import annotations

import sys
sys.path.insert(0, "scripts/research")

import json
import statistics
from collections import defaultdict

from order.tmf_channel_config import PAPER_RECIPE
from tmf_channel.cache_store import list_days, load_day
from tmf_channel.engine import load_vixtwn_delta, simulate

SOURCES = {
    "w83": "tx_1m_fullnight_cache_full.json",
    "janmar_holdout": "tx_1m_janmar_holdout_cache.json",
    "julsep_holdout": "tx_1m_julsep_holdout_cache.json",
    "octdec_holdout": "tx_1m_octdec_holdout_cache.json",
}


def _arrays_from_rows(day, rows):
    """Found 2026-08-08: bare 'HH:MM' timestamps (no date) make every
    _day()-keyed lookup in causal_engine.py (vix_session_bias etc.) silently
    fail its date-string match, which does NOT disable vix_session_bias --
    it makes its `dlt is None` branch fire on every poll, force-flattening
    all open lots at every session boundary instead of the intended
    selective flatten. That artificially truncates the holding-time tail
    this script exists to measure. Fixed to match the known-safe
    day_arrays() convention (reports/research/channel_lab/
    r_strict_paper_bias_overlay.py) used by the rest of the research
    ecosystem: full ISO timestamp, date prefix from the cache's own trading-
    day label.
    """
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    return O, H, L, C, V, T


def session_of(hm: str) -> str:
    return "night" if (hm >= "15:00" or hm < "05:00") else "day"


def cell_max_hold(recipe, sess: str, regime: str) -> int:
    spb = recipe.get("session_pv_book") or {}
    cfg = (spb.get(sess) or {}).get(regime) or {}
    mh = cfg.get("max_hold_bars")
    if mh is None:
        mh = recipe.get("max_hold_bars") or 0
    return int(mh)


def main():
    vix = load_vixtwn_delta() or {}
    all_trades = []
    per_window_days = {}
    for label, cache_name in SOURCES.items():
        days = list_days(source=cache_name)
        per_window_days[label] = len(days)
        for day in days:
            rows = load_day(day, source=cache_name)
            if not rows:
                continue
            O, H, L, C, V, T = _arrays_from_rows(day, rows)
            recipe = dict(PAPER_RECIPE)
            recipe.setdefault("hang_anchor", "O")
            trades, events, ws, wl, rvol, regime, open_pos = simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
            for tr in trades:
                sess = session_of(tr["et"])
                mh = cell_max_hold(recipe, sess, tr.get("regime_e", "?"))
                tr2 = dict(tr)
                tr2["window"] = label
                tr2["day"] = day
                tr2["sess"] = sess
                tr2["cell_max_hold_bars"] = mh
                all_trades.append(tr2)

    holds = [t["hold"] for t in all_trades]
    holds_sorted = sorted(holds)
    n = len(holds_sorted)

    def pct(p):
        if n == 0:
            return None
        idx = min(n - 1, int(round(p / 100.0 * (n - 1))))
        return holds_sorted[idx]

    print(f"n_trades={n} across windows={per_window_days}")
    print(
        f"hold(min): mean={statistics.mean(holds):.1f} median={statistics.median(holds)} "
        f"p90={pct(90)} p95={pct(95)} p99={pct(99)} p99.5={pct(99.5)} max={max(holds)}"
    )

    # top 20 longest holds with context
    top = sorted(all_trades, key=lambda t: -t["hold"])[:20]
    print("\nTop 20 longest holds (window, day, sess, regime_e, hold_bars, cell_max_hold_bars, why, pnl):")
    for t in top:
        print(
            f"  {t['window']:14s} {t['day']} {t['sess']:5s} {t['regime_e']:16s} "
            f"hold={t['hold']:4d} cell_cap={t['cell_max_hold_bars']:3d} "
            f"why={t['why']:20s} pnl={t['pnl']}"
        )

    # how many trades exceed 90 min flat cap, and how many would exceed
    # cell_cap * multiplier for multiplier in {2,3,4}
    exceed_90 = sum(1 for h in holds if h > 90)
    print(f"\ntrades with hold>90min: {exceed_90} ({100.0*exceed_90/n:.3f}%)")
    for mult in (2, 3, 4, 5):
        exceed = sum(
            1 for t in all_trades if t["hold"] > mult * max(1, t["cell_max_hold_bars"])
        )
        print(
            f"trades with hold > {mult}x cell_max_hold_bars: {exceed} "
            f"({100.0*exceed/n:.3f}%)"
        )

    # distribution of hold vs cell_max_hold_bars ratio, at the tail
    ratios = [t["hold"] / max(1, t["cell_max_hold_bars"]) for t in all_trades]
    ratios_sorted = sorted(ratios)

    def pctr(p):
        idx = min(n - 1, int(round(p / 100.0 * (n - 1))))
        return ratios_sorted[idx]

    print(
        f"\nhold/cell_max_hold_bars ratio: p90={pctr(90):.2f} p95={pctr(95):.2f} "
        f"p99={pctr(99):.2f} p99.5={pctr(99.5):.2f} max={max(ratios):.2f}"
    )

    # breakdown by why (exit reason) for trades with hold > 60
    long_trades = [t for t in all_trades if t["hold"] > 60]
    why_counts = defaultdict(int)
    for t in long_trades:
        cat = t["why"].split("|")[0]
        why_counts[cat] += 1
    print(f"\ntrades with hold>60min: {len(long_trades)}; by exit-category:")
    for k, v in sorted(why_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")

    # eod_flatten specific: hold times for eod_flatten-exited trades (expected long by design)
    eod_holds = [t["hold"] for t in all_trades if t["why"].split("|")[0] == "eod_flatten"]
    if eod_holds:
        eod_sorted = sorted(eod_holds)
        print(
            f"\neod_flatten trades: n={len(eod_holds)} mean={statistics.mean(eod_holds):.1f} "
            f"p50={eod_sorted[len(eod_sorted)//2]} p90={eod_sorted[int(0.9*(len(eod_sorted)-1))]} "
            f"max={max(eod_holds)}"
        )

    non_eod_holds = [t["hold"] for t in all_trades if t["why"].split("|")[0] != "eod_flatten"]
    if non_eod_holds:
        s2 = sorted(non_eod_holds)
        nn = len(s2)
        print(
            f"non-eod_flatten trades: n={nn} mean={statistics.mean(non_eod_holds):.1f} "
            f"p90={s2[int(0.9*(nn-1))]} p99={s2[int(0.99*(nn-1))]} max={max(non_eod_holds)}"
        )

    out = {
        "n_trades": n,
        "per_window_days": per_window_days,
        "hold_stats": {
            "mean": statistics.mean(holds),
            "median": statistics.median(holds),
            "p90": pct(90),
            "p95": pct(95),
            "p99": pct(99),
            "p99_5": pct(99.5),
            "max": max(holds),
        },
        "exceed_90min_count": exceed_90,
        "top20_longest": [
            {k: t[k] for k in ("window", "day", "sess", "regime_e", "hold", "cell_max_hold_bars", "why", "pnl")}
            for t in top
        ],
        "eod_flatten_hold_stats": {
            "n": len(eod_holds),
            "mean": statistics.mean(eod_holds) if eod_holds else None,
            "max": max(eod_holds) if eod_holds else None,
        },
        "non_eod_hold_stats": {
            "n": len(non_eod_holds),
            "mean": statistics.mean(non_eod_holds) if non_eod_holds else None,
            "p99": s2[int(0.99 * (len(s2) - 1))] if non_eod_holds else None,
            "max": max(non_eod_holds) if non_eod_holds else None,
        },
    }
    out_path = "reports/research/channel_lab/maxhold_safetycap_calibration_result.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
