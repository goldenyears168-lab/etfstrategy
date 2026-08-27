#!/usr/bin/env python3
"""Item #17: session-transition-gap cross-check with max_hold bug.

Read-only research. Uses tmf_channel.engine.simulate() directly (not live)
against PAPER_RECIPE (v1.4.0) across w83 + 3 holdout caches to count trades
entered within the last 10 minutes of the day session that carry into the
night session, and to compute their realized pnl vs. all-trades baseline.

Does NOT touch src/order/, config/order.yaml, .env, launchd/, scripts/order/.
"""
from __future__ import annotations

import json
import statistics as st
import sys

sys.path.insert(0, "src")

from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402

SOURCES = [
    "tx_1m_fullnight_cache_full.json",  # w83, 2026-04-01..07-31
    "tx_1m_janmar_holdout_cache.json",  # 2026 Q1
    "tx_1m_julsep_holdout_cache.json",  # 2025-07..09
    "tx_1m_octdec_holdout_cache.json",  # 2025-10..12
]

DAY_LAST10_START = "13:35"  # day session ends ~13:44/13:45; last 10 min window


def _arrays(day, rows):
    """Found 2026-08-08: bare 'HH:MM' timestamps (no date) make every
    _day()-keyed lookup in causal_engine.py (vix_session_bias) silently
    mismatch, which forces an unconditional flatten at every session
    boundary instead of the intended selective one -- directly undermining
    this script's own premise (trades carrying across the boundary). Fixed
    to match the known-safe day_arrays() convention (reports/research/
    channel_lab/r_strict_paper_bias_overlay.py).
    """
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    S = [str(r.get("sess") or "") for r in rows]
    return O, H, L, C, V, T, S


def main():
    vix = load_vixtwn_delta() or {}
    all_trades = []  # (source, day, trade, entry_sess, exit_sess)
    per_source_days = {}

    for source in SOURCES:
        days = list_days(source)
        per_source_days[source] = len(days)
        for day in days:
            rows = load_day(day, source=source)
            if not rows:
                continue
            O, H, L, C, V, T, S = _arrays(day, rows)
            recipe = dict(PAPER_RECIPE)
            recipe.setdefault("hang_anchor", "O")
            trades, events, ws, wl, rvol, regime, open_pos = simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
            for tr in trades:
                eb = tr.get("eb")
                xb = tr.get("xb")
                entry_sess = S[eb] if eb is not None and eb < len(S) else None
                exit_sess = S[xb] if xb is not None and xb < len(S) else None
                all_trades.append((source, day, tr, entry_sess, exit_sess))

    total_n = len(all_trades)
    total_pnl = sum(tr["pnl"] for _, _, tr, _, _ in all_trades)

    # day-session entries in the last 10 minutes that carry into night
    carry = []
    for source, day, tr, entry_sess, exit_sess in all_trades:
        et = tr.get("et") or ""
        if entry_sess == "day" and et >= DAY_LAST10_START and exit_sess == "night":
            carry.append((source, day, tr))

    # also: any day-entry (regardless of time) whose exit lands in night session
    # (broader "spans the transition" population, for context)
    spans_any = [
        (source, day, tr)
        for source, day, tr, entry_sess, exit_sess in all_trades
        if entry_sess == "day" and exit_sess == "night"
    ]

    def stats(trs):
        pnls = [t["pnl"] for _, _, t in trs]
        if not pnls:
            return {"n": 0}
        wins = sum(1 for p in pnls if p > 0)
        return {
            "n": len(pnls),
            "net": round(sum(pnls), 1),
            "mean": round(st.mean(pnls), 1),
            "median": round(st.median(pnls), 1),
            "win_rate_pct": round(100.0 * wins / len(pnls), 1),
            "worst": round(min(pnls), 1),
            "best": round(max(pnls), 1),
        }

    result = {
        "sources": per_source_days,
        "total_trades_all_sources": total_n,
        "total_pnl_all_sources": round(total_pnl, 1),
        "baseline_per_trade_mean_pnl": round(total_pnl / total_n, 2) if total_n else None,
        "carry_last10min_into_night": {
            "definition": f"entry bar sess=='day', entry time >= {DAY_LAST10_START}, exit bar sess=='night'",
            **stats(carry),
            "detail": [
                {
                    "source": s,
                    "day": d,
                    "et": t.get("et"),
                    "xt": t.get("xt"),
                    "hold": t.get("hold"),
                    "side": t.get("s"),
                    "pnl": round(t.get("pnl", 0.0), 1),
                    "why": t.get("why"),
                }
                for s, d, t in carry
            ],
        },
        "spans_day_to_night_any_entry_time": {
            "definition": "entry bar sess=='day' (any time), exit bar sess=='night'",
            **stats(spans_any),
        },
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
