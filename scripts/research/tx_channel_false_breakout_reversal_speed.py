#!/usr/bin/env python3
"""Item #13: false breakout reversal speed as a feature.

Question: after a struct_break exit fires, does the SPEED of the tick-level
price reversal immediately after correlate with anything knowable BEFORE the
trigger (pre-trigger features only, to keep it causal/actionable)?

Method
------
1. Run PAPER_RECIPE (live, final_v1_4_0) over the w83 cache
   (tx_1m_fullnight_cache_full.json, 2026-04-01..07-31) via
   tmf_channel.engine.simulate() directly (harness.run_day only returns a
   summary, not the trade list).
2. Keep struct_break exits whose ENTRY and EXIT bars are both in the DAY
   session (sess == "day") so the exit bar's calendar date maps 1:1 onto a
   single front-month tick file (avoids the night-session midnight-crossing
   tick-file-splitting problem flagged in tx_channel_tick_validation.py's
   caveats -- not solved here, out of scope for this item).
3. For each such trade, pull ticks for that date via load_front_month_ticks,
   find the first tick at/after the bar's exit HH:MM, then walk forward to
   measure how fast price reverses back toward the ORIGINAL trade's
   favorable direction by a fixed threshold (revert_pts) after the exit
   price -- this is the "false breakout reversal speed".
4. Correlate reversal speed against PRE-TRIGGER features only: hold_b (bars
   held before struct_break fired), swing_distance (embedded in why string),
   rvol_e, regime_e, side, entry hour bucket, local 12-bar range at trigger
   (the exact window struct_break uses).
5. Day-clustered significance (one point per day, not per trade, since we
   often have <=1-2 struct_break/day -- report n_trades and n_days both).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

from tmf_channel.cache_store import list_days, load_day  # noqa: E402
from tmf_channel.engine import load_vixtwn_delta, simulate  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from order.tmf_channel_pv16_book import RECIPE_VERSION  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "channel_lab"
CACHE = "tx_1m_fullnight_cache_full.json"
REVERT_PTS = 10.0  # how far price must come back (in the trade's favor) to count as "reverted"
HORIZON_MIN = 10  # max minutes after exit to search for reversal


def _arrays(day, rows):
    O = [float(r["o"]) for r in rows]
    H = [float(r["h"]) for r in rows]
    L = [float(r["l"]) for r in rows]
    C = [float(r["c"]) for r in rows]
    V = [float(r.get("v") or 0) for r in rows]
    T = [f"{day}T{r.get('t')}:00.000+08:00" for r in rows]
    S = [str(r.get("sess") or "") for r in rows]
    return O, H, L, C, V, T, S


def _hhmm(ts) -> str:
    """Extract bare HH:MM out of a full '{day}T{HH:MM}:00.000+08:00' timestamp."""
    return str(ts).split("T")[-1][:5]


def collect_struct_break_day_trades():
    days = list_days(source=CACHE)
    vix = load_vixtwn_delta() or {}
    out = []
    for day in days:
        rows = load_day(day, source=CACHE)
        if not rows:
            continue
        O, H, L, C, V, T, S = _arrays(day, rows)
        recipe = dict(PAPER_RECIPE)
        recipe.setdefault("hang_anchor", "O")
        recipe.setdefault("recipe_version", RECIPE_VERSION)
        trades, events, ws, wl, rvol, regime, open_pos = simulate(
            O, H, L, C, V, T, recipe, vix_delta=vix
        )
        for tr in trades:
            if not str(tr.get("why", "")).startswith("struct_break"):
                continue
            eb, xb = tr["eb"], tr["xb"]
            if S[eb] != "day" or S[xb] != "day":
                continue
            why = tr["why"]
            try:
                swing = float(why.split("|")[1])
            except (IndexError, ValueError):
                swing = None
            look0 = max(0, xb - 12)
            local_range = (max(H[look0:xb]) - min(L[look0:xb])) if xb > look0 else None
            rec = dict(tr)
            rec["day"] = day
            rec["swing"] = swing
            rec["swing_distance"] = abs(tr["xp"] - swing) if swing is not None else None
            rec["local_range12"] = local_range
            rec["entry_hour"] = _hhmm(tr["et"])[:2] if tr.get("et") else None
            out.append(rec)
    return out


def measure_reversal(day: str, xt_hhmm: str, side: str, xp: float):
    """Return (seconds_to_revert or None, max_favorable_move_within_horizon)."""
    ticks = load_front_month_ticks(day)
    if ticks is None or ticks.empty:
        return None, None
    exit_dt = pd.Timestamp(f"{day} {xt_hhmm}:00")
    horizon_end = exit_dt + timedelta(minutes=HORIZON_MIN)
    window = ticks[(ticks["dt"] >= exit_dt) & (ticks["dt"] <= horizon_end)]
    if window.empty:
        return None, None
    prices = window["price"].to_numpy()
    times = window["dt"].to_numpy()
    if side == "L":
        # trade was long, exited on a break below swing low -> "reversal" = price
        # comes back UP above xp + REVERT_PTS
        favorable = prices - xp
    else:
        favorable = xp - prices
    max_fav = float(favorable.max()) if len(favorable) else None
    hit = np.where(favorable >= REVERT_PTS)[0]
    if len(hit) == 0:
        return None, max_fav
    t0 = pd.Timestamp(times[0])
    t_hit = pd.Timestamp(times[hit[0]])
    secs = (t_hit - t0).total_seconds()
    return secs, max_fav


def main():
    trades = collect_struct_break_day_trades()
    print(f"struct_break exits, entry+exit both in day session: {len(trades)}")
    days_present = sorted({t["day"] for t in trades})
    print(f"n_days with >=1 such trade: {len(days_present)}")

    rows = []
    for tr in trades:
        secs, max_fav = measure_reversal(tr["day"], _hhmm(tr["xt"]), tr["s"], tr["xp"])
        rows.append({**tr, "revert_secs": secs, "max_fav_10min": max_fav})

    df = pd.DataFrame(rows)
    n_ticked = df["revert_secs"].notna().sum() + 0  # includes those that never reverted (NaN kept)
    n_have_ticks = df["max_fav_10min"].notna().sum()
    print(f"trades with usable tick window: {n_have_ticks}/{len(df)}")
    print(f"of those, reverted by >= {REVERT_PTS}pt within {HORIZON_MIN}min: "
          f"{df['revert_secs'].notna().sum()}/{n_have_ticks}")

    out_path = OUT_DIR / "false_breakout_reversal_speed_result.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_json(out_path, orient="records", indent=2, force_ascii=False)
    print(f"saved raw trade-level table: {out_path}")

    return df


if __name__ == "__main__":
    main()
