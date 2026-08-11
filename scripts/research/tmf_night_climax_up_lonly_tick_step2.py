#!/usr/bin/env python3
"""Step 2 (tick-validation task): for each night|climax_up L trade produced
by the candidate book (step1 output), fetch real tick data (front-month,
concatenated across the midnight file boundary via
tx_channel_tick_night_concat_validation.build_night_session) and check
whether the claimed entry (ep) and exit (xp/why) are achievable in the
correct tick-level time sequence. Flags same-bar entry+exit trades as
highest-risk for a bar-level artifact.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_night_concat_validation import _load_raw  # noqa: E402

# CALENDAR ATTRIBUTION (corrected 2026-08-11 -- supersedes this file's earlier
# "BUGFIX" note, whose premise was wrong for the main source).
#
# The earlier note claimed a session key is a *calendar* date whose 00:00-04:59
# block is "the tail of the PREVIOUS trading day's night session". That holds for
# tx_1m_tick_built_fullnight_aug (the AUG_DAYS source) but is FALSE for
# tx_1m_fullnight_cache_full.json (JULY_DAYS + the whole 66-day OOS window),
# where session key D spans D 08:45 -> D+1 04:59 and the 00:00-04:59 tail is
# calendar D+1. Verified against FinMind ticks: 83/83 sessions, 24,532 tail bars
# match D+1 ticks with max deviation 0.0pt, while matching them against D gives
# deviations up to ~1000pt (see docs handoff §5a's phantom "cache corruption",
# and scripts/research/audit_tx_1m_fullnight_cache_quality.py).
#
# So ticks must be keyed off the calendar date carried in the trade's own et/xt
# (step1 now emits real instants via cache_store.bar_timestamps), NOT off
# trade["day"]. Entry and exit can now fall on different calendar dates, because
# in true chronological order 23:59 is followed directly by 00:00 and a trade may
# straddle midnight.

TOL = 1.0  # points; tick-vs-bar level tolerance (repo precedent: avg 1.3pt bar/tick diff)
TIME_TOUCH_EXIT_REASONS_PREFIX = ("stop|", "struct_break|")  # adverse-touch exits
IN_PATH = "reports/research/channel_lab/tmf_night_climax_up_lonly_trades_for_tick_check.json"
OUT_PATH = "reports/research/channel_lab/tmf_night_climax_up_lonly_tick_check_result.json"


def bar_window(ts: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    # ts like '2026-07-08T15:03:00.000+08:00' -> minute start/end (naive, matches tick 'dt')
    date_part, time_part = ts.split("T")
    hhmm = time_part[:5]
    start = pd.Timestamp(f"{date_part} {hhmm}:00")
    end = start + pd.Timedelta(minutes=1)
    return start, end


def load_day_ticks_front_month(day: str) -> pd.DataFrame | None:
    """Whole calendar-day raw ticks (spread rows excluded), front-month picked
    separately for segment A (00:00-04:59) and segment C (15:00-23:59) since
    a rollover could in principle put different front months in each."""
    raw = _load_raw(day)
    if raw is None or raw.empty:
        return None
    hm = raw["dt"].dt.strftime("%H:%M:%S")
    segA = raw[hm < "05:00:00"]
    segC = raw[hm >= "15:00:00"]
    parts = []
    if not segA.empty:
        frontA = segA.groupby("contract_date")["volume"].sum().idxmax()
        parts.append(segA[segA["contract_date"] == frontA])
    if not segC.empty:
        frontC = segC.groupby("contract_date")["volume"].sum().idxmax()
        parts.append(segC[segC["contract_date"] == frontC])
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).sort_values("dt").reset_index(drop=True)


def check_trade(ticks_entry, trade: dict, ticks_exit=None) -> dict:
    """`ticks_entry` / `ticks_exit` are the raw tick frames of the CALENDAR dates
    of et / xt respectively (they differ for a midnight-straddling trade)."""
    if ticks_exit is None:
        ticks_exit = ticks_entry
    et_start, et_end = bar_window(trade["et"])
    xt_start, xt_end = bar_window(trade["xt"])
    ep, xp = trade["ep"], trade["xp"]
    why = trade["why"]

    empty = ticks_entry.iloc[0:0]
    entry_win = (
        ticks_entry[(ticks_entry["dt"] >= et_start) & (ticks_entry["dt"] < et_end)]
        if ticks_entry is not None else empty
    )
    exit_win = (
        ticks_exit[(ticks_exit["dt"] >= xt_start) & (ticks_exit["dt"] < xt_end)]
        if ticks_exit is not None else empty
    )

    result = dict(day=trade["day"], et=trade["et"], xt=trade["xt"], ep=ep, xp=xp,
                  why=why, pnl=trade["pnl"], hold=trade["hold"])

    if entry_win.empty:
        result["entry_status"] = "no_ticks_in_bar"
        entry_touch_t = None
    else:
        # L entry: buy at/through ep from above (touch when price <= ep)
        touches = entry_win[entry_win["price"] <= ep + TOL]
        if not touches.empty:
            result["entry_status"] = "confirmed"
            entry_touch_t = touches["dt"].iloc[0]
        else:
            result["entry_status"] = "NOT_CONFIRMED"
            result["entry_min_price"] = float(entry_win["price"].min())
            entry_touch_t = None

    is_touch_exit = any(str(why).startswith(p) for p in TIME_TOUCH_EXIT_REASONS_PREFIX)
    if exit_win.empty:
        result["exit_status"] = "no_ticks_in_bar"
        exit_touch_t = None
    elif is_touch_exit:
        # closing an L (long) on adverse move -> price touches down to xp
        touches = exit_win[exit_win["price"] <= xp + TOL]
        if not touches.empty:
            result["exit_status"] = "confirmed"
            exit_touch_t = touches["dt"].iloc[0]
        else:
            result["exit_status"] = "NOT_CONFIRMED"
            result["exit_min_price"] = float(exit_win["price"].min())
            exit_touch_t = None
    else:
        # rule/time-based close (max_hold, session_flat, vix_*) -> should be near
        # prevailing price at that bar, not a directional touch requirement
        near = exit_win[(exit_win["price"] >= xp - 3 * TOL) & (exit_win["price"] <= xp + 3 * TOL)]
        result["exit_status"] = "confirmed_near_prevailing" if not near.empty else "PRICE_FAR_FROM_TICKS"
        if not near.empty:
            result["exit_price_range_in_bar"] = (float(exit_win["price"].min()), float(exit_win["price"].max()))
        exit_touch_t = exit_win["dt"].iloc[-1] if not exit_win.empty else None

    same_bar = trade["eb"] == trade["xb"]
    result["same_bar"] = same_bar
    if same_bar and entry_touch_t is not None and exit_touch_t is not None:
        result["sequence_ok"] = bool(entry_touch_t <= exit_touch_t)
        result["entry_touch_time"] = str(entry_touch_t)
        result["exit_touch_time"] = str(exit_touch_t)
    elif same_bar:
        result["sequence_ok"] = None  # can't verify, one side unconfirmed/no ticks

    ok = (
        result["entry_status"] in ("confirmed",)
        and result["exit_status"] in ("confirmed", "confirmed_near_prevailing")
        and result.get("sequence_ok", True) is not False
    )
    result["tick_confirmed"] = ok
    result["tick_pnl"] = trade["pnl"] if ok else 0.0
    return result


def main() -> None:
    trades_by_window = json.loads(Path(IN_PATH).read_text())

    all_results = {}
    for label, trades in trades_by_window.items():
        # Key ticks by the CALENDAR date inside et/xt, not by trade["day"].
        cal_dates = sorted({t["et"][:10] for t in trades} | {t["xt"][:10] for t in trades})
        ticks_by_cal = {d: load_day_ticks_front_month(d) for d in cal_dates}

        results = []
        for t in trades:
            te = ticks_by_cal.get(t["et"][:10])
            tx = ticks_by_cal.get(t["xt"][:10])
            if te is None or te.empty or tx is None or tx.empty:
                r = dict(day=t["day"], et=t["et"], xt=t["xt"], ep=t["ep"], xp=t["xp"],
                          why=t["why"], pnl=t["pnl"], entry_status="no_tick_file",
                          exit_status="no_tick_file", tick_confirmed=False, tick_pnl=0.0)
            else:
                r = check_trade(te, t, tx)
            results.append(r)
        all_results[label] = results

        n = len(results)
        n_conf = sum(1 for r in results if r["tick_confirmed"])
        bar_total = sum(t["pnl"] for t in trades)
        tick_total = sum(r["tick_pnl"] for r in results)
        print(f"=== {label} ({n} trades) ===")
        print(f"  bar-sim total pnl = {bar_total:.1f}  tick-confirmed total pnl = {tick_total:.1f}")
        print(f"  trades unchanged/confirmed: {n_conf}/{n}")
        for r in results:
            flag = "OK" if r["tick_confirmed"] else "FLAG"
            print(f"  [{flag}] {r['day']} et={r['et'][11:19]} xt={r['xt'][11:19]} "
                  f"ep={r['ep']} xp={r['xp']} why={r['why']} pnl={r['pnl']} "
                  f"same_bar={r.get('same_bar')} entry={r['entry_status']} exit={r['exit_status']} "
                  f"seq_ok={r.get('sequence_ok')}")

    Path("reports/research/channel_lab").mkdir(parents=True, exist_ok=True)
    Path(OUT_PATH).write_text(json.dumps(all_results, indent=2, ensure_ascii=False, default=str))
    print(f"\nWrote {OUT_PATH}")

    # per-day tick-confirmed aggregation for significance re-check
    for label in ("IS_22d", "OOS_66d"):
        results = all_results.get(label, [])
        by_day = {}
        for r in results:
            by_day.setdefault(r["day"], 0.0)
            by_day[r["day"]] += r["tick_pnl"]
        vals = list(by_day.values())
        if vals:
            print(f"{label}: tick-confirmed per-day cell contributions (days with a trade): "
                  f"n={len(vals)} mean={st.mean(vals):.2f} sum={sum(vals):.1f}")


if __name__ == "__main__":
    main()
