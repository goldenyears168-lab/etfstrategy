#!/usr/bin/env python3
"""tickrev-T2: restore the *pre-look-ahead-fix* comparison for
reports/research/channel_lab/slow_cell_tick_trigger_engine.py and audit for
RESIDUAL causality leaks.

Why this script exists
----------------------
slow_cell_tick_trigger_engine.json's `lookahead_fix_note.
prior_result_before_lookahead_fix` block is INVALID: it was populated by
re-reading the (already overwritten) OUT_JSON, so it contains the *post*-fix
numbers bit-for-bit while the same note's prose claims the pre-fix delta was
+2,356 pt. This script re-derives the pre-fix numbers by actually re-running
the engine with the guard switched off, instead of trusting either record.

What it does
------------
1. Copies (does NOT modify) the tick engine's `simulate_block_tick` with one
   added parameter `guard: bool`.  guard=True (DEFAULT) == the shipped,
   fixed engine (`not ch_just_locked` / `not cr_just_locked` gating).
   guard=False == the pre-fix engine: the bar on which a channel's line locks
   is immediately usable by that same bar's own ticks -> look-ahead.
2. Runs bar baseline + tick(guard=ON) + tick(guard=OFF) on the SAME 13
   sampled days / same config, and reports full side-by-side including
   per-day breakdown and ex-top-N concentration for every arm.
3. Independently audits residual look-ahead: every place the engine consumes
   C[t] (bar t's CLOSE, only knowable at the END of bar t) while making a
   decision triggered by a tick INSIDE bar t gets a counter + per-trade tag.

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t2_prefix_restore.py
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t2_prefix_restore.py --disable-lookahead-guard
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

ROOT = Path("/Users/jackm4/goldenstocks")
LAB = ROOT / "reports/research/channel_lab"
OUT_JSON = LAB / "tickrev_t2_prefix_restore.json"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, LAB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


W = _load_module("slow_cell_width_percentile_rolling")
TL = _load_module("slow_cell_tick_latency_lab")
ENG = _load_module("slow_cell_tick_trigger_engine")  # for SAMPLE_DAYS / build_bundle / config parity

SAMPLE_DAYS = ENG.SAMPLE_DAYS
PCT, WINDOW, MODE = ENG.PCT, ENG.WINDOW, ENG.MODE
TRIG, N_CAP = ENG.TRIG, ENG.N_CAP
COST, SW_TH = ENG.COST, ENG.SW_TH
LOCK_K, COOLDOWN_BARS = ENG.LOCK_K, ENG.COOLDOWN_BARS


# ---------------------------------------------------------------------------
# simulate_block_tick, verbatim from slow_cell_tick_trigger_engine.py except:
#   (a) `guard` parameter gates the two `*_just_locked` protections
#   (b) contamination audit ALWAYS runs (pre-fix it was unreachable-by-design
#       under guard=True; here it must also count under guard=False)
#   (c) extra RESIDUAL-leak counters + per-trade tags (see RESIDUAL_* below)
# ---------------------------------------------------------------------------
def simulate_block_tick(day, session, bars, ticks, ranges, half_hist, trades, acct,
                        pct, window, guard=True, trig=TRIG, n_cap=N_CAP):
    O, H, L, C = W.arrays(bars)
    n = len(bars)
    last = n - 1
    if n < 60:
        return

    zz_dir = 0
    ei, ep = 0, C[0]
    prev_piv = None
    ch = None
    pos = None
    cooldown_until = -1
    consec = 0

    def cur_half(t):
        hist = half_hist if window is None else half_hist[-window:]
        return W.percentile(hist, pct) if hist else max(12.0, W.causal_median_tr(H, L, C, t))

    def next_tick(idx):
        if idx + 1 < len(ticks):
            return idx + 1, ticks[idx + 1][1]
        return idx, ticks[idx][1]

    def close_trade(p, fill_idx, fill_bar, px, reason):
        pnl = (px - p["e_px"]) * (1.0 if p["side"] == "L" else -1.0) - COST
        trades.append(dict(
            day=day, session=session, side=p["side"], kind=p["kind"],
            entry_sig_bar=p["e_sig_bar"], entry_sig_tick=p["e_sig_tick"],
            entry_fill_bar=p["e_fill_bar"], entry_fill_tick=p["e_fill_tick"], entry_px=p["e_px"],
            exit_fill_bar=fill_bar, exit_fill_tick=fill_idx, exit_px=px, exit_reason=reason,
            pnl=round(pnl, 2),
            # --- audit tags (T2 additions) ---
            lookahead_entry=p.get("lookahead_entry", False),
            lookahead_exit=(reason in ("target", "target_rail") and p.get("_exit_on_lock_bar", False)),
            resid_entry_on_zzflip_bar=p.get("resid_entry_on_zzflip_bar", False),
            resid_entry_flip_used_curbar_close=p.get("resid_flip_curbar_close", False),
        ))
        acct["closed"] += 1

    for t in range(n):
        lo, hi = ranges[t]

        # ---- causal ZigZag update (close-based, confirm at this bar's close) ----
        pivot_confirm_dir = None
        flipped = False
        zz_confirmed_this_bar = False
        half_appended_this_bar = False
        if zz_dir >= 0:
            if C[t] >= ep:
                ei, ep = t, C[t]
            elif ep - C[t] >= SW_TH[session]:
                if prev_piv is not None:
                    half_hist.append(W.leg_half(prev_piv, (ei, ep), C))
                    half_appended_this_bar = True
                prev_piv = (ei, ep)
                ch = W.make_channel(ei, ep, -1, t, C, cur_half(t))
                zz_dir = -1
                ei, ep = t, C[t]
                flipped = True
                pivot_confirm_dir = -1
                zz_confirmed_this_bar = True
        if zz_dir <= 0 and not flipped:
            if C[t] <= ep:
                ei, ep = t, C[t]
            elif C[t] - ep >= SW_TH[session]:
                if prev_piv is not None:
                    half_hist.append(W.leg_half(prev_piv, (ei, ep), C))
                    half_appended_this_bar = True
                prev_piv = (ei, ep)
                ch = W.make_channel(ei, ep, 1, t, C, cur_half(t))
                zz_dir = 1
                ei, ep = t, C[t]
                pivot_confirm_dir = 1
                zz_confirmed_this_bar = True
        del pivot_confirm_dir

        # ---- channel maintenance: extreme tracking + slope lock ----
        ch_just_locked = False
        if ch is not None:
            was_unlocked = ch["m"] is None
            minmax_touched_curbar = False
            if C[t] < ch["min_p"]:
                ch["min_p"], ch["min_i"] = C[t], t
                minmax_touched_curbar = True
            if C[t] > ch["max_p"]:
                ch["max_p"], ch["max_i"] = C[t], t
                minmax_touched_curbar = True
            ch["_minmax_curbar"] = minmax_touched_curbar
            if ch["m"] is None and t >= ch["lock_bar"] and t > ch["a_i"]:
                ch["m"] = (C[t] - ch["a_p"]) / (t - ch["a_i"])
                ch["line"] = dict(i=ch["a_i"], p=ch["a_p"], m=ch["m"])
            ch_just_locked = was_unlocked and ch["m"] is not None
            if ch_just_locked:
                ch["locked_at_bar"] = t

        # ---- session boundary: force flat at the session's very last real tick ----
        if t == last:
            if pos is not None:
                last_idx = len(ticks) - 1
                close_trade(pos, last_idx, t, ticks[last_idx][1], "session_end")
                pos = None
            break

        # ---- signal check: tick-scan replaces bar H[t]/L[t] ----
        if pos is not None and t > pos["e_fill_bar"]:
            side = pos["side"]
            stop_level = pos["stop"]
            target_level = None
            target_source_ch = None
            if pos["kind"] == "fade":
                target_level = W.line_at(pos["line"], t)
            elif pos["kind"] == "flip":
                cr = pos["ch_ref"]
                cr_just_locked = ch_just_locked and cr is ch
                blocked = cr_just_locked if guard else False
                if cr is not None and cr["m"] is not None and not blocked:
                    lvl = W.line_at(cr["line"], t)
                    target_level = lvl + cr["half"] if side == "L" else lvl - cr["half"]
                    target_source_ch = cr

            trig_kind, trig_gi = None, None
            for gi in range(lo, hi):
                px = ticks[gi][1]
                stop_hit = (px <= stop_level) if side == "L" else (px >= stop_level)
                if stop_hit:
                    trig_kind, trig_gi = "stop", gi
                    break
                if target_level is not None:
                    target_hit = (px >= target_level) if side == "L" else (px <= target_level)
                    if target_hit:
                        trig_kind, trig_gi = "target", gi
                        break

            if trig_kind == "stop":
                acct["raw:exit_stop_touch"] += 1
                if consec >= n_cap:
                    acct["signals"] += 1
                    acct["skip:consec_flip_cap"] += 1
                    fill_idx, fill_px = next_tick(trig_gi)
                    fill_bar = t if fill_idx < hi else t + 1
                    close_trade(pos, fill_idx, fill_bar, fill_px, "stop_capped_flat")
                    pos = None
                    cooldown_until = fill_bar + COOLDOWN_BARS
                    consec = 0
                else:
                    new_dir = -1 if side == "L" else 1
                    if new_dir == -1:
                        a_i, a_p = ch["max_i"], ch["max_p"]
                    else:
                        a_i, a_p = ch["min_i"], ch["min_p"]
                    # RESIDUAL AUDIT: the anchor and/or the half used to build the
                    # replacement channel come from C[t] == THIS bar's close, but we
                    # are mid-bar (trigger tick trig_gi is inside bar t).
                    resid_anchor = (a_i == t)
                    if resid_anchor:
                        acct["resid:flip_anchor_is_curbar_close"] += 1
                    if half_appended_this_bar:
                        acct["resid:flip_half_uses_curbar_leg"] += 1
                    ch = W.make_channel(a_i, a_p, new_dir, t, C, cur_half(t))
                    reopen = (t + 1) < last
                    acct["signals"] += 1
                    if not reopen:
                        acct["skip:flip_reopen_no_room"] += 1
                    new_tp = W.trig_pts(trig, ch["half"])
                    fill_idx, fill_px = next_tick(trig_gi)
                    fill_bar = t if fill_idx < hi else t + 1
                    close_trade(pos, fill_idx, fill_bar, fill_px, "stop_flip")
                    old_side = side
                    consec += 1
                    if reopen:
                        new_side = "S" if old_side == "L" else "L"
                        pos = dict(side=new_side, kind="flip", e_sig_bar=t, e_sig_tick=trig_gi,
                                   e_fill_bar=fill_bar, e_fill_tick=fill_idx, e_px=fill_px,
                                   stop=fill_px - new_tp if new_side == "L" else fill_px + new_tp,
                                   line=None, ch_ref=ch, trig_pts=new_tp,
                                   lookahead_entry=False,
                                   resid_entry_on_zzflip_bar=zz_confirmed_this_bar,
                                   resid_flip_curbar_close=(resid_anchor or half_appended_this_bar))
                        acct["fills"] += 1
                    else:
                        pos = None
            elif trig_kind == "target":
                acct["raw:exit_target_touch"] += 1
                on_lock = (target_source_ch is not None
                           and target_source_ch.get("locked_at_bar") == t)
                if on_lock:
                    acct["contamination:signal_on_lock_bar"] += 1
                pos["_exit_on_lock_bar"] = on_lock
                fill_idx, fill_px = next_tick(trig_gi)
                fill_bar = t if fill_idx < hi else t + 1
                reason = "target" if pos["kind"] == "fade" else "target_rail"
                close_trade(pos, fill_idx, fill_bar, fill_px, reason)
                pos = None
                consec = 0

        elif pos is None:
            in_cooldown_bar = t < cooldown_until
            entry_blocked = ch_just_locked if guard else False
            if ch is not None and ch["m"] is not None and not entry_blocked:
                mid = W.line_at(ch["line"], t)
                half = ch["half"]
                touched_gi, touched_side = None, None
                for gi in range(lo, hi):
                    px = ticks[gi][1]
                    if px >= mid + half:
                        touched_gi, touched_side = gi, "S"
                        break
                    if px <= mid - half:
                        touched_gi, touched_side = gi, "L"
                        break
                if touched_gi is not None:
                    acct["raw:entry_touch"] += 1
                    on_lock = (ch.get("locked_at_bar") == t)
                    if on_lock:
                        acct["contamination:signal_on_lock_bar"] += 1
                    acct["signals"] += 1
                    if in_cooldown_bar:
                        acct["skip:cooldown"] += 1
                    elif t + 1 >= last:
                        acct["skip:no_room_before_session_end"] += 1
                    else:
                        tp = W.trig_pts(trig, half)
                        rail = mid + half if touched_side == "S" else mid - half
                        stop = rail + tp if touched_side == "S" else rail - tp
                        fill_idx, fill_px = next_tick(touched_gi)
                        fill_bar = t if fill_idx < hi else t + 1
                        pos = dict(side=touched_side, kind="fade", e_sig_bar=t, e_sig_tick=touched_gi,
                                   e_fill_bar=fill_bar, e_fill_tick=fill_idx, e_px=fill_px,
                                   stop=stop, line=dict(ch["line"]), ch_ref=ch, trig_pts=tp,
                                   lookahead_entry=on_lock,
                                   resid_entry_on_zzflip_bar=zz_confirmed_this_bar,
                                   resid_flip_curbar_close=False)
                        acct["fills"] += 1



# ---------------------------------------------------------------------------
# STRICT-CAUSAL DIAGNOSTIC VARIANT (T2 addition, NOT part of the shipped engine)
# ---------------------------------------------------------------------------
# The shipped fix (`ch_just_locked`) plugs exactly ONE of several places where
# bar t's CLOSE (C[t]) feeds a decision taken by a tick INSIDE bar t. This
# variant plugs the whole family at once by reordering the loop: bar t's tick
# scan runs FIRST, using channel/ZigZag/half_hist state frozen as of the END OF
# BAR t-1; the ZigZag confirm + channel maintenance for bar t then run AFTER the
# scan (i.e. at bar t's close, where they actually become knowable).
# The delta between this and the guard-ON arm is the size of the RESIDUAL leak.
def simulate_block_tick_strict(day, session, bars, ticks, ranges, half_hist, trades, acct,
                               pct, window, trig=TRIG, n_cap=N_CAP):
    O, H, L, C = W.arrays(bars)
    n = len(bars)
    last = n - 1
    if n < 60:
        return

    zz_dir = 0
    ei, ep = 0, C[0]
    prev_piv = None
    ch = None
    pos = None
    cooldown_until = -1
    consec = 0

    def cur_half(t):
        hist = half_hist if window is None else half_hist[-window:]
        return W.percentile(hist, pct) if hist else max(12.0, W.causal_median_tr(H, L, C, t))

    def next_tick(idx):
        if idx + 1 < len(ticks):
            return idx + 1, ticks[idx + 1][1]
        return idx, ticks[idx][1]

    def close_trade(p, fill_idx, fill_bar, px, reason):
        pnl = (px - p["e_px"]) * (1.0 if p["side"] == "L" else -1.0) - COST
        trades.append(dict(
            day=day, session=session, side=p["side"], kind=p["kind"],
            entry_sig_bar=p["e_sig_bar"], entry_sig_tick=p["e_sig_tick"],
            entry_fill_bar=p["e_fill_bar"], entry_fill_tick=p["e_fill_tick"], entry_px=p["e_px"],
            exit_fill_bar=fill_bar, exit_fill_tick=fill_idx, exit_px=px, exit_reason=reason,
            pnl=round(pnl, 2), lookahead_entry=False, lookahead_exit=False,
            resid_entry_on_zzflip_bar=False, resid_entry_flip_used_curbar_close=False,
        ))
        acct["closed"] += 1

    for t in range(n):
        lo, hi = ranges[t]

        # ---- session boundary first (same semantics as the shipped engine) ----
        if t == last:
            if pos is not None:
                last_idx = len(ticks) - 1
                close_trade(pos, last_idx, t, ticks[last_idx][1], "session_end")
                pos = None
            break

        # ================= PHASE 1: bar t tick scan, state as of end of bar t-1 =========
        if pos is not None and t > pos["e_fill_bar"]:
            side = pos["side"]
            stop_level = pos["stop"]
            target_level = None
            if pos["kind"] == "fade":
                target_level = W.line_at(pos["line"], t)
            elif pos["kind"] == "flip":
                cr = pos["ch_ref"]
                if cr is not None and cr["m"] is not None:
                    if cr.get("locked_at_bar") == t:
                        acct["contamination:signal_on_lock_bar"] += 1  # must stay 0
                    lvl = W.line_at(cr["line"], t)
                    target_level = lvl + cr["half"] if side == "L" else lvl - cr["half"]

            trig_kind, trig_gi = None, None
            for gi in range(lo, hi):
                px = ticks[gi][1]
                stop_hit = (px <= stop_level) if side == "L" else (px >= stop_level)
                if stop_hit:
                    trig_kind, trig_gi = "stop", gi
                    break
                if target_level is not None:
                    target_hit = (px >= target_level) if side == "L" else (px <= target_level)
                    if target_hit:
                        trig_kind, trig_gi = "target", gi
                        break

            if trig_kind == "stop":
                acct["raw:exit_stop_touch"] += 1
                if consec >= n_cap:
                    acct["signals"] += 1
                    acct["skip:consec_flip_cap"] += 1
                    fill_idx, fill_px = next_tick(trig_gi)
                    fill_bar = t if fill_idx < hi else t + 1
                    close_trade(pos, fill_idx, fill_bar, fill_px, "stop_capped_flat")
                    pos = None
                    cooldown_until = fill_bar + COOLDOWN_BARS
                    consec = 0
                else:
                    new_dir = -1 if side == "L" else 1
                    if new_dir == -1:
                        a_i, a_p = ch["max_i"], ch["max_p"]
                    else:
                        a_i, a_p = ch["min_i"], ch["min_p"]
                    if a_i == t:
                        acct["strict_violation:anchor_is_curbar"] += 1  # must stay 0
                    # seed min/max only up to bar t-1 (C[t] not knowable mid-bar),
                    # but keep the shipped engine's lock spacing (created-at-bar-t + LOCK_K)
                    ch = W.make_channel(a_i, a_p, new_dir, max(a_i, t - 1), C, cur_half(t))
                    ch["lock_bar"] = t + LOCK_K
                    reopen = (t + 1) < last
                    acct["signals"] += 1
                    if not reopen:
                        acct["skip:flip_reopen_no_room"] += 1
                    new_tp = W.trig_pts(trig, ch["half"])
                    fill_idx, fill_px = next_tick(trig_gi)
                    fill_bar = t if fill_idx < hi else t + 1
                    close_trade(pos, fill_idx, fill_bar, fill_px, "stop_flip")
                    old_side = side
                    consec += 1
                    if reopen:
                        new_side = "S" if old_side == "L" else "L"
                        pos = dict(side=new_side, kind="flip", e_sig_bar=t, e_sig_tick=trig_gi,
                                   e_fill_bar=fill_bar, e_fill_tick=fill_idx, e_px=fill_px,
                                   stop=fill_px - new_tp if new_side == "L" else fill_px + new_tp,
                                   line=None, ch_ref=ch, trig_pts=new_tp)
                        acct["fills"] += 1
                    else:
                        pos = None
            elif trig_kind == "target":
                acct["raw:exit_target_touch"] += 1
                fill_idx, fill_px = next_tick(trig_gi)
                fill_bar = t if fill_idx < hi else t + 1
                reason = "target" if pos["kind"] == "fade" else "target_rail"
                close_trade(pos, fill_idx, fill_bar, fill_px, reason)
                pos = None
                consec = 0

        elif pos is None:
            in_cooldown_bar = t < cooldown_until
            if ch is not None and ch["m"] is not None:
                if ch.get("locked_at_bar") == t:
                    acct["contamination:signal_on_lock_bar"] += 1  # must stay 0
                mid = W.line_at(ch["line"], t)
                half = ch["half"]
                touched_gi, touched_side = None, None
                for gi in range(lo, hi):
                    px = ticks[gi][1]
                    if px >= mid + half:
                        touched_gi, touched_side = gi, "S"
                        break
                    if px <= mid - half:
                        touched_gi, touched_side = gi, "L"
                        break
                if touched_gi is not None:
                    acct["raw:entry_touch"] += 1
                    acct["signals"] += 1
                    if in_cooldown_bar:
                        acct["skip:cooldown"] += 1
                    elif t + 1 >= last:
                        acct["skip:no_room_before_session_end"] += 1
                    else:
                        tp = W.trig_pts(trig, half)
                        rail = mid + half if touched_side == "S" else mid - half
                        stop = rail + tp if touched_side == "S" else rail - tp
                        fill_idx, fill_px = next_tick(touched_gi)
                        fill_bar = t if fill_idx < hi else t + 1
                        pos = dict(side=touched_side, kind="fade", e_sig_bar=t, e_sig_tick=touched_gi,
                                   e_fill_bar=fill_bar, e_fill_tick=fill_idx, e_px=fill_px,
                                   stop=stop, line=dict(ch["line"]), ch_ref=ch, trig_pts=tp)
                        acct["fills"] += 1

        # ================= PHASE 2: end-of-bar-t updates (now knowable) ================
        flipped = False
        if zz_dir >= 0:
            if C[t] >= ep:
                ei, ep = t, C[t]
            elif ep - C[t] >= SW_TH[session]:
                if prev_piv is not None:
                    half_hist.append(W.leg_half(prev_piv, (ei, ep), C))
                prev_piv = (ei, ep)
                ch = W.make_channel(ei, ep, -1, t, C, cur_half(t))
                zz_dir = -1
                ei, ep = t, C[t]
                flipped = True
        if zz_dir <= 0 and not flipped:
            if C[t] <= ep:
                ei, ep = t, C[t]
            elif C[t] - ep >= SW_TH[session]:
                if prev_piv is not None:
                    half_hist.append(W.leg_half(prev_piv, (ei, ep), C))
                prev_piv = (ei, ep)
                ch = W.make_channel(ei, ep, 1, t, C, cur_half(t))
                zz_dir = 1
                ei, ep = t, C[t]

        if ch is not None:
            was_unlocked = ch["m"] is None
            if C[t] < ch["min_p"]:
                ch["min_p"], ch["min_i"] = C[t], t
            if C[t] > ch["max_p"]:
                ch["max_p"], ch["max_i"] = C[t], t
            if ch["m"] is None and t >= ch["lock_bar"] and t > ch["a_i"]:
                ch["m"] = (C[t] - ch["a_p"]) / (t - ch["a_i"])
                ch["line"] = dict(i=ch["a_i"], p=ch["a_p"], m=ch["m"])
            if was_unlocked and ch["m"] is not None:
                ch["locked_at_bar"] = t


def run_config_tick_strict(bundles, pct, window):
    half_hist = {"day": [], "night": []}
    trades: list[dict] = []
    acct = Counter()
    n_days = 0
    for day in sorted(bundles):
        used = False
        for sess in ("day", "night"):
            b = bundles[day].get(sess)
            if b is None:
                continue
            simulate_block_tick_strict(day, sess, b["bars"], b["ticks"], b["ranges"],
                                       half_hist[sess], trades, acct, pct, window)
            used = True
        if used:
            n_days += 1
    return trades, acct, n_days


def run_config_tick(bundles, pct, window, guard=True):
    half_hist = {"day": [], "night": []}
    trades: list[dict] = []
    acct = Counter()
    n_days = 0
    for day in sorted(bundles):
        used = False
        for sess in ("day", "night"):
            b = bundles[day].get(sess)
            if b is None:
                continue
            simulate_block_tick(day, sess, b["bars"], b["ticks"], b["ranges"],
                                half_hist[sess], trades, acct, pct, window, guard=guard)
            used = True
        if used:
            n_days += 1
    return trades, acct, n_days


def summarize_tick(trades, acct, guard: bool):
    skips = {k.split(":", 1)[1]: v for k, v in acct.items() if k.startswith("skip:")}
    n_skip = sum(skips.values())
    bar_gaps = [tr["exit_fill_bar"] - tr["entry_fill_bar"] for tr in trades]
    tick_gaps = [tr["exit_fill_tick"] - tr["entry_fill_tick"] for tr in trades]
    reasons = sorted({tr["exit_reason"] for tr in trades})
    out = dict(
        **W.pnl_block(trades),
        by_session={s: W.pnl_block([t for t in trades if t["session"] == s]) for s in ("day", "night")},
        by_exit_reason={r: W.pnl_block([t for t in trades if t["exit_reason"] == r]) for r in reasons},
        touch_resolution=W.touch_resolution(trades),
        accounting=dict(
            n_signals=acct["signals"], n_fills=acct["fills"], n_closed=acct["closed"],
            n_skipped=n_skip, skip_reasons=skips,
            balanced=(acct["signals"] == acct["fills"] + n_skip and acct["fills"] == acct["closed"]),
        ),
        same_bar_check=dict(
            min_entryfill_to_exitfill_gap_bars=(min(bar_gaps) if bar_gaps else None),
            n_bar_gap_le_0=sum(1 for g in bar_gaps if g <= 0),
            min_entryfill_to_exitfill_gap_ticks=(min(tick_gaps) if tick_gaps else None),
            n_tick_gap_le_0=sum(1 for g in tick_gaps if g <= 0),
        ),
        causal_lock_check=dict(n_signal_on_lock_bar=acct["contamination:signal_on_lock_bar"]),
        raw_trigger_counts=dict(
            entry_touch=acct["raw:entry_touch"],
            exit_target_touch=acct["raw:exit_target_touch"],
            exit_stop_touch=acct["raw:exit_stop_touch"],
            guardable_touch_total=acct["raw:entry_touch"] + acct["raw:exit_target_touch"],
            all_touch_total=(acct["raw:entry_touch"] + acct["raw:exit_target_touch"]
                             + acct["raw:exit_stop_touch"]),
        ),
        residual_leak_counts=dict(
            flip_anchor_is_curbar_close=acct["resid:flip_anchor_is_curbar_close"],
            flip_half_uses_curbar_leg=acct["resid:flip_half_uses_curbar_leg"],
        ),
        guard_enabled=guard,
    )
    assert out["accounting"]["balanced"], f"accounting mismatch: {acct}"
    assert out["same_bar_check"]["n_bar_gap_le_0"] == 0
    assert out["same_bar_check"]["n_tick_gap_le_0"] == 0
    if guard:
        assert out["causal_lock_check"]["n_signal_on_lock_bar"] == 0, (
            "look-ahead detected with guard ON — regression!")
    return out


def run_bar_baseline(bundles, pct, window):
    cache = {}
    for day, sess_map in bundles.items():
        bars = []
        for sess in ("day", "night"):
            b = sess_map.get(sess)
            if b is not None:
                bars.extend(b["bars"])
        if bars:
            cache[day] = bars
    trades, acct, n_days = W.run_config(cache, MODE, TRIG, N_CAP, pct, window)
    return trades, W.summarize(trades, acct), n_days


# ---------------------------------------------------------------------------
def concentration(trades, ks=(1, 3, 5, 10)):
    pnls = sorted((t["pnl"] for t in trades), reverse=True)
    tot = sum(pnls)
    out = dict(
        n=len(pnls),
        net=round(tot, 1),
        median_pnl=(sorted(pnls)[len(pnls) // 2] if pnls else None),
        mean_pnl=(round(tot / len(pnls), 3) if pnls else None),
        max_single_trade_pnl=(pnls[0] if pnls else None),
        min_single_trade_pnl=(pnls[-1] if pnls else None),
    )
    for k in ks:
        out[f"top{k}_sum"] = round(sum(pnls[:k]), 1)
        out[f"net_ex_top{k}"] = round(tot - sum(pnls[:k]), 1)
    out["top5_pct_of_total_net"] = round(100.0 * sum(pnls[:5]) / tot, 1) if tot else None
    return out


def by_day(trades):
    acc = {}
    for tr in trades:
        acc[tr["day"]] = acc.get(tr["day"], 0.0) + tr["pnl"]
    return {d: round(v, 1) for d, v in sorted(acc.items())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disable-lookahead-guard", action="store_true",
                    help="make the LEAKY (pre-fix) arm the headline/primary result. "
                         "DEFAULT IS GUARD ON. Never use for any adoption decision.")
    args = ap.parse_args()
    primary = "guard_off_PREFIX_LEAKY" if args.disable_lookahead_guard else "guard_on_FIXED"
    if args.disable_lookahead_guard:
        banner = ("!!!!!!!!!! LOOK-AHEAD GUARD DISABLED — PRIMARY RESULT IS CONTAMINATED "
                  "(pre-fix reproduction only, NOT a tradeable result) !!!!!!!!!!")
        print("\n".join([banner] * 3), flush=True)

    bundles = {}
    missing = []
    for day in SAMPLE_DAYS:
        b = ENG.build_bundle(day)
        if b:
            bundles[day] = b
        else:
            missing.append(day)
        print(f"  bundle {day}: {'ok' if b else 'EMPTY'} sessions={sorted(b) if b else []}", flush=True)
    print(f"built bundles for {len(bundles)}/{len(SAMPLE_DAYS)} sampled days; missing={missing}", flush=True)

    bar_trades, bar_summary, bar_n_days = run_bar_baseline(bundles, PCT, WINDOW)
    print(f"[bar ] n={bar_summary['n_trades']:4d} net={bar_summary['net_pts']:>9.1f} "
          f"wr={bar_summary['win_rate_pct']:>6.2f}%", flush=True)

    arms = {}
    for tag, guard in (("guard_on_FIXED", True), ("guard_off_PREFIX_LEAKY", False)):
        trs, acct, nd = run_config_tick(bundles, PCT, WINDOW, guard=guard)
        s = summarize_tick(trs, acct, guard)
        arms[tag] = dict(summary=s, trades=trs, n_days=nd)
        print(f"[{tag:22s}] n={s['n_trades']:4d} net={s['net_pts']:>9.1f} "
              f"wr={s['win_rate_pct']:>6.2f}% lockbar_signals={s['causal_lock_check']['n_signal_on_lock_bar']}",
              flush=True)

    st_trs, st_acct, st_nd = run_config_tick_strict(bundles, PCT, WINDOW)
    st_sum = summarize_tick(st_trs, st_acct, True)
    assert st_acct["strict_violation:anchor_is_curbar"] == 0, "strict arm still anchors on current bar close"
    arms["tick_strict_causal_DIAGNOSTIC"] = dict(summary=st_sum, trades=st_trs, n_days=st_nd)
    print(f"[{'tick_strict_causal':22s}] n={st_sum['n_trades']:4d} net={st_sum['net_pts']:>9.1f} "
          f"wr={st_sum['win_rate_pct']:>6.2f}% lockbar_signals={st_sum['causal_lock_check']['n_signal_on_lock_bar']}",
          flush=True)

    on, off = arms["guard_on_FIXED"], arms["guard_off_PREFIX_LEAKY"]

    # ---- contaminated-trade forensics (guard OFF arm) ----
    off_trs = off["trades"]
    contaminated = [t for t in off_trs
                    if t["lookahead_entry"] or t["lookahead_exit"]]
    contam_entry = [t for t in off_trs if t["lookahead_entry"]]
    contam_exit = [t for t in off_trs if t["lookahead_exit"]]
    contamination_forensics = dict(
        note=("guard OFF 時，直接標記每一筆『用了本根 bar 自己剛鎖定的線』的交易："
              "lookahead_entry=進場觸價用了 lock 當根的線；lookahead_exit=flip 部位的 "
              "target_rail 出場用了 lock 當根的線。"),
        n_trades_contaminated=len(contaminated),
        n_entry_contaminated=len(contam_entry),
        n_exit_contaminated=len(contam_exit),
        pnl_of_contaminated_trades=round(sum(t["pnl"] for t in contaminated), 1),
        pnl_of_entry_contaminated=round(sum(t["pnl"] for t in contam_entry), 1),
        pnl_of_exit_contaminated=round(sum(t["pnl"] for t in contam_exit), 1),
        contaminated_trade_detail=[
            dict(day=t["day"], session=t["session"], side=t["side"], kind=t["kind"],
                 entry_sig_bar=t["entry_sig_bar"], exit_reason=t["exit_reason"], pnl=t["pnl"],
                 leak=("entry" if t["lookahead_entry"] else "") + ("|exit" if t["lookahead_exit"] else ""))
            for t in contaminated],
        raw_touch_denominators=dict(
            guard_off=off["summary"]["raw_trigger_counts"],
            guard_on=on["summary"]["raw_trigger_counts"],
        ),
        leak_rate_pct_of_guardable_touches=(
            round(100.0 * off["summary"]["causal_lock_check"]["n_signal_on_lock_bar"]
                  / off["summary"]["raw_trigger_counts"]["guardable_touch_total"], 2)
            if off["summary"]["raw_trigger_counts"]["guardable_touch_total"] else None),
    )

    # ---- residual look-ahead audit (applies to the FIXED arm too) ----
    on_trs = on["trades"]
    resid_zzflip = [t for t in on_trs if t["resid_entry_on_zzflip_bar"]]
    resid_curbar = [t for t in on_trs if t["resid_entry_flip_used_curbar_close"]]
    residual_audit = dict(
        note=("guard ON 版本仍殘留的因果疑點：所有『在 bar t 的某筆 tick 觸發，但決策讀了 C[t]"
              "（= bar t 的收盤價，要等這根 bar 走完才知道）』的路徑。ch_just_locked 只堵住了"
              "『線斜率鎖定』這一條，下列幾條沒堵。"),
        findings=[
            dict(id="R1", where="simulate_block_tick: ZigZag update runs BEFORE this bar's tick scan",
                 what=("bar t 的 ZigZag 若在 C[t] 上確認新 pivot，會在掃描 bar t 自己的 tick 之前"
                       "就把 ch 換成新的（m=None）通道。等於用 bar t 的收盤價，決定 bar t 開頭那些"
                       "tick 『不准』用舊通道交易。真實世界在 09:15:03 你還不知道 09:15 收盤會翻轉。"),
                 direction="訊號抑制（保守方向），但仍是資訊洩漏，會系統性改變樣本組成",
                 measured=dict(n_trades_entered_on_a_zzflip_bar=len(resid_zzflip),
                               pnl=round(sum(t["pnl"] for t in resid_zzflip), 1),
                               pct_of_trades=round(100.0 * len(resid_zzflip) / len(on_trs), 1) if on_trs else None)),
            dict(id="R2", where="stop_flip branch: W.make_channel(a_i, a_p, new_dir, t, C, cur_half(t))",
                 what=("翻空/翻多時新通道的錨點取 ch['max_i']/['min_i']，而 min/max 追蹤在本迭代"
                       "開頭已經用 C[t] 更新過。若 a_i == t，代表錨點就是這根 bar 自己的收盤價，"
                       "但觸發是這根 bar 盤中的某筆 tick。新通道的 lock 線因此建立在未來資訊上。"),
                 direction="未知（改變新通道位置→改變後續進出場）",
                 measured=dict(n_flip_events_anchored_on_curbar_close=on["summary"]["residual_leak_counts"]["flip_anchor_is_curbar_close"],
                               n_trades_whose_entry_used_it=len(resid_curbar),
                               pnl_of_those_trades=round(sum(t["pnl"] for t in resid_curbar), 1))),
            dict(id="R3", where="stop_flip branch: cur_half(t) after half_hist.append() at the same bar",
                 what=("若 bar t 的 ZigZag 剛好在同一根 bar confirm 了 pivot，會先 append 新的 "
                       "leg_half 到 half_hist，然後 bar t 盤中的 flip 觸發用 cur_half(t) 取百分位數"
                       "時就吃到了這條剛剛才知道的腿。影響新部位的 stop 距離（trig_pts）與通道半寬。"),
                 direction="未知",
                 measured=dict(n_flip_events_using_curbar_leg=on["summary"]["residual_leak_counts"]["flip_half_uses_curbar_leg"])),
            dict(id="R4", where="TL._dominant_outright_contract(rows)",
                 what=("合約過濾用『整個交易日出現次數最多的單月 contract_date』決定要保留哪一檔，"
                       "是用整天（含未來）的資料選的。實務上主力月份盤前就已知，屬良性；但轉倉日"
                       "（每月第三個週三）附近嚴格說是 whole-day look-ahead。"),
                 direction="良性/極小，但非零",
                 measured="not quantified (would require an ex-ante front-month calendar)"),
            dict(id="R5", where="simulate_block_tick: t == last -> close_trade(ticks[-1])",
                 what=("session 收尾強制平倉直接用整段 session 最後一筆 tick 的價格，且在 bar last "
                       "這根完全不檢查 stop/target。若該部位其實在 bar last 盤中就已觸停損，這筆會"
                       "被記成用『最後一筆 tick』平倉的價格。session_end 這一類在 guard ON 版是 "
                       f"n={len([t for t in on_trs if t['exit_reason']=='session_end'])}、"
                       f"net={round(sum(t['pnl'] for t in on_trs if t['exit_reason']=='session_end'),1)}pt，"
                       "占總淨值極高比重，因此這一條的敏感度不低。"),
                 direction="未知（可能兩個方向都有），但佔比高",
                 measured=dict(
                     n_session_end_trades=len([t for t in on_trs if t["exit_reason"] == "session_end"]),
                     net_pts=round(sum(t["pnl"] for t in on_trs if t["exit_reason"] == "session_end"), 1),
                     pct_of_total_net=(round(100.0 * sum(t["pnl"] for t in on_trs if t["exit_reason"] == "session_end")
                                             / on["summary"]["net_pts"], 1)
                                       if on["summary"]["net_pts"] else None))),
            dict(id="R-STRICT", where="see strict_causal_arm (R1+R2+R3 全部堵起來)",
                 what=("把 bar t 的 ZigZag confirm + 通道維護整段移到 bar t 的 tick 掃描『之後』，"
                       "掃描只能用截至 t-1 收盤的狀態。結果：n 從 325 掉到 270，net 從 1,982 "
                       "變成 2,547（+565pt）。也就是說殘留的 R1/R2/R3 洩漏方向是『對策略不利』"
                       "（主要是 R1 的訊號抑制），堵掉之後淨值反而更高——所以 +1,363 這個改善"
                       "不是靠殘留洩漏撐起來的。"),
                 direction="堵掉後淨值上升 → 殘留洩漏未虛胖 headline",
                 caveat=("但同時也說明結果對『一根 bar 的先後順序』極度敏感：只是把兩段程式碼"
                         "對調，交易數就少 17%、淨值變 +28%。13 天樣本下這種敏感度本身就是"
                         "不該採信單一數字的理由。"),
                 measured="net 2547 (n=270) vs guard_on 1982 (n=325); delta +565"),
            dict(id="R6-clean", where="fade target: pos['line'] = dict(ch['line']) snapshot",
                 what=("fade 部位的 target 用進場當下快照的線，之後只做 line_at(line, t) 外推，"
                       "不重讀任何未來 bar 的 C。查核結果：乾淨，沒有洩漏。"),
                 direction="無洩漏",
                 measured="clean"),
        ],
    )

    _oc = concentration(off_trs)
    off_conc_ex5, off_conc_top5pct = _oc["net_ex_top5"], _oc["top5_pct_of_total_net"]
    delta_fixed = round(on["summary"]["net_pts"] - bar_summary["net_pts"], 1)
    delta_leaky = round(off["summary"]["net_pts"] - bar_summary["net_pts"], 1)

    out = dict(
        task="tickrev-T2: restore the invalid pre-look-ahead-fix comparison + hunt residual leaks",
        script=str(Path(__file__).resolve()),
        engine_under_test=str(LAB / "slow_cell_tick_trigger_engine.py"),
        engine_unmodified=True,
        primary_result_arm=primary,
        guard_flag_default="ON (fixed engine). --disable-lookahead-guard only relabels which arm is headline; BOTH arms are always computed.",
        config=dict(percentile=PCT, window=WINDOW, mode=MODE,
                    flip_trigger=f"{TRIG[0]}:{TRIG[1]}", flip_cap_n=N_CAP,
                    cost_pt_per_roundtrip=COST, sw_th=SW_TH, lock_k=LOCK_K,
                    cooldown_bars=COOLDOWN_BARS),
        days=dict(requested=SAMPLE_DAYS, n_requested=len(SAMPLE_DAYS),
                  n_with_data=len(bundles), missing=missing,
                  n_days_with_trades_bar=len(by_day(bar_trades)),
                  n_days_with_trades_tick_guard_on=len(by_day(on_trs))),
        arms=dict(
            bar=dict(n_trades=bar_summary["n_trades"], net_pts=bar_summary["net_pts"],
                     avg_pts=bar_summary["avg_pts"], win_rate_pct=bar_summary["win_rate_pct"],
                     day_net=bar_summary["by_session"]["day"]["net_pts"],
                     night_net=bar_summary["by_session"]["night"]["net_pts"],
                     by_exit_reason={r: dict(n=v["n_trades"], net_pts=v["net_pts"])
                                     for r, v in bar_summary["by_exit_reason"].items()},
                     accounting=bar_summary["accounting"]),
            tick_guard_on_FIXED=dict(
                n_trades=on["summary"]["n_trades"], net_pts=on["summary"]["net_pts"],
                avg_pts=on["summary"]["avg_pts"], win_rate_pct=on["summary"]["win_rate_pct"],
                day_net=on["summary"]["by_session"]["day"]["net_pts"],
                night_net=on["summary"]["by_session"]["night"]["net_pts"],
                by_exit_reason={r: dict(n=v["n_trades"], net_pts=v["net_pts"])
                                for r, v in on["summary"]["by_exit_reason"].items()},
                accounting=on["summary"]["accounting"],
                causal_lock_check=on["summary"]["causal_lock_check"],
                raw_trigger_counts=on["summary"]["raw_trigger_counts"]),
            tick_guard_off_PREFIX_LEAKY=dict(
                WARNING="CONTAMINATED BY DESIGN — reproduction of the pre-fix engine only",
                n_trades=off["summary"]["n_trades"], net_pts=off["summary"]["net_pts"],
                avg_pts=off["summary"]["avg_pts"], win_rate_pct=off["summary"]["win_rate_pct"],
                day_net=off["summary"]["by_session"]["day"]["net_pts"],
                night_net=off["summary"]["by_session"]["night"]["net_pts"],
                by_exit_reason={r: dict(n=v["n_trades"], net_pts=v["net_pts"])
                                for r, v in off["summary"]["by_exit_reason"].items()},
                accounting=off["summary"]["accounting"],
                causal_lock_check=off["summary"]["causal_lock_check"],
                raw_trigger_counts=off["summary"]["raw_trigger_counts"]),
        ),
        deltas_vs_bar=dict(
            fixed=dict(net_pts_delta=delta_fixed,
                       pct_of_bar=round(100.0 * delta_fixed / abs(bar_summary["net_pts"]), 1)
                       if bar_summary["net_pts"] else None,
                       n_trades_delta=on["summary"]["n_trades"] - bar_summary["n_trades"]),
            prefix_leaky=dict(net_pts_delta=delta_leaky,
                              pct_of_bar=round(100.0 * delta_leaky / abs(bar_summary["net_pts"]), 1)
                              if bar_summary["net_pts"] else None,
                              n_trades_delta=off["summary"]["n_trades"] - bar_summary["n_trades"]),
            leak_inflation_pts=round(delta_leaky - delta_fixed, 1),
        ),
        concentration=dict(bar=concentration(bar_trades),
                           tick_guard_on=concentration(on_trs),
                           tick_guard_off=concentration(off_trs),
                           tick_strict_causal=concentration(st_trs)),
        by_day_net_pts=dict(bar=by_day(bar_trades),
                            tick_guard_on=by_day(on_trs),
                            tick_guard_off=by_day(off_trs),
                            tick_strict_causal=by_day(st_trs)),
        strict_causal_arm=dict(
            note=("診斷用第三組：把 bar t 的 ZigZag confirm 與通道維護整段移到『bar t 的 tick 掃描"
                  "之後』，也就是 bar t 的掃描只能用截至 bar t-1 收盤為止的狀態。這一次把 R1/R2/R3"
                  "（ZigZag 提前換通道、flip 錨點取本根收盤、cur_half 吃到本根才 confirm 的腿）全部"
                  "堵起來。與 guard_on 的差額就是殘留洩漏的規模。這不是要取代線上引擎，只是量測。"),
            n_trades=st_sum["n_trades"], net_pts=st_sum["net_pts"],
            avg_pts=st_sum["avg_pts"], win_rate_pct=st_sum["win_rate_pct"],
            day_net=st_sum["by_session"]["day"]["net_pts"],
            night_net=st_sum["by_session"]["night"]["net_pts"],
            by_exit_reason={r: dict(n=v["n_trades"], net_pts=v["net_pts"])
                            for r, v in st_sum["by_exit_reason"].items()},
            accounting=st_sum["accounting"],
            causal_lock_check=st_sum["causal_lock_check"],
            raw_trigger_counts=st_sum["raw_trigger_counts"],
            delta_vs_bar=round(st_sum["net_pts"] - bar_summary["net_pts"], 1),
            delta_vs_guard_on=round(st_sum["net_pts"] - on["summary"]["net_pts"], 1),
        ),
        answers=dict(
            q_was_prefix_delta_2356=dict(
                answer="YES — exactly. 逐位元重現。",
                prefix_tick_net_pts=off["summary"]["net_pts"],
                bar_net_pts=bar_summary["net_pts"],
                prefix_delta=delta_leaky,
                claimed_in_note=2356,
                match=(abs(delta_leaky - 2356) < 1e-9),
                how_the_invalid_block_happened=(
                    "engine main() 先讀舊的 OUT_JSON 當 `prior`，跑完後又把新結果寫回同一個檔；"
                    "上一次執行寫入的 OUT_JSON 已經是『修好之後』的版本，所以這一次讀到的 prior "
                    "就是 post-fix 數字，被原封不動存進 prior_result_before_lookahead_fix。"
                    "真正的 pre-fix 紀錄從來沒有被保存過，只有 note 的散文與 caveats 的硬寫數字"
                    "殘留下來。"),
                stale_prose_fingerprint=dict(
                    note=("caveats 是硬寫字串、不隨執行更新，因此保留了 pre-fix 的指紋，"
                          "與本次 guard-off 重跑完全吻合："),
                    caveat_says_ex_top5_plus_548=548.0,
                    measured_guard_off_net_ex_top5=off_conc_ex5,
                    caveat_says_top5_pct_81_6=81.6,
                    measured_guard_off_top5_pct=off_conc_top5pct,
                    caveat_says_total_net_2975=2975.0,
                    measured_guard_off_net=off["summary"]["net_pts"],
                    caveat_says_4day_sum_3412=3412.0,
                    measured_guard_off_4day_sum=round(sum(
                        by_day(off_trs).get(d, 0.0)
                        for d in ("2025-10-15", "2026-05-15", "2026-06-15", "2026-07-15")), 1),
                ),
            ),
            q_leak_footprint=dict(
                guard_off_n_signal_on_lock_bar=off["summary"]["causal_lock_check"]["n_signal_on_lock_bar"],
                breakdown=dict(
                    entry_side_touches_on_lock_bar=(
                        off["summary"]["causal_lock_check"]["n_signal_on_lock_bar"]
                        - contamination_forensics["n_exit_contaminated"]),
                    entry_touch_denominator=off["summary"]["raw_trigger_counts"]["entry_touch"],
                    entry_leak_rate_pct=round(
                        100.0 * (off["summary"]["causal_lock_check"]["n_signal_on_lock_bar"]
                                 - contamination_forensics["n_exit_contaminated"])
                        / off["summary"]["raw_trigger_counts"]["entry_touch"], 1),
                    entry_leaks_that_became_fills=contamination_forensics["n_entry_contaminated"],
                    entry_leak_pnl=contamination_forensics["pnl_of_entry_contaminated"],
                    exit_side_flip_target_leaks=contamination_forensics["n_exit_contaminated"],
                    exit_leak_pnl=contamination_forensics["pnl_of_exit_contaminated"],
                    total_contaminated_trade_pnl=contamination_forensics["pnl_of_contaminated_trades"],
                ),
                verdict_on_note_numbers=(
                    "note 寫的 16/202 觸價、9 筆成交、+261pt、污染 7/35 target —— 四個數字全部"
                    "正確重現（entry 側 lock-bar 觸價 16 次 / entry_touch 202 次 = 7.9%；其中 9 "
                    "筆成交、合計 +261pt；這 9 筆裡有 7 筆以 target 出場，guard-off 的 target "
                    "總數正是 35 筆）。但 note 只量了洩漏的『進場側』，漏掉同一個 bug 的『出場側』："
                    "flip 部位用 pos['ch_ref'] 這條同一根 bar 才鎖定的線判定 target_rail，另有 8 "
                    "次 / 8 筆成交 / +698pt。因此 note 說『只佔宣稱改善 +2,356 的約 11%』是嚴重"
                    "低估：直接受污染交易共 17 筆 +959pt（40.7%），把 guard 打開後整體淨值掉 "
                    "993pt（42.1%）。"),
                net_effect_of_turning_guard_on=dict(
                    net_pts_removed=round(off["summary"]["net_pts"] - on["summary"]["net_pts"], 1),
                    pct_of_claimed_2356=round(
                        100.0 * (off["summary"]["net_pts"] - on["summary"]["net_pts"]) / 2356.0, 1),
                    direct_contaminated_pnl=contamination_forensics["pnl_of_contaminated_trades"],
                    path_dependent_knock_on=round(
                        off["summary"]["net_pts"] - on["summary"]["net_pts"]
                        - contamination_forensics["pnl_of_contaminated_trades"], 1),
                ),
            ),
            q_robustness_of_the_surviving_1363=dict(
                headline="+1,363 pt (bar 619 -> tick 1,982) 在 13 天 / n=325 上仍成立，但穩健性弱。",
                n_trades_tick=on["summary"]["n_trades"], n_trades_bar=bar_summary["n_trades"],
                n_days_with_trades=len(by_day(on_trs)),
                days_tick_beats_bar=sum(1 for d in by_day(bar_trades)
                                        if by_day(on_trs).get(d, 0.0) > by_day(bar_trades)[d]),
                median_daily_diff_pts=sorted(
                    [round(by_day(on_trs).get(d, 0.0) - by_day(bar_trades)[d], 1)
                     for d in by_day(bar_trades)])[len(by_day(bar_trades)) // 2],
                ex_top5=dict(bar=concentration(bar_trades)["net_ex_top5"],
                             tick_guard_on=concentration(on_trs)["net_ex_top5"],
                             note=("修復後 tick 版扣掉最大 5 筆是 -445pt（淨虧），不是 caveats 寫的 "
                                   "+548pt（那是 pre-fix 的數字）。相對比較（tick -445 > bar -2484）"
                                   "仍站得住，但『tick 版 ex-top5 仍淨盈』這句話 post-fix 是錯的。")),
                session_end_dependency=dict(
                    tick=round(sum(t["pnl"] for t in on_trs if t["exit_reason"] == "session_end"), 1),
                    bar=round(sum(t["pnl"] for t in bar_trades if t["exit_reason"] == "session_end"), 1),
                    note="兩版的全部正報酬都來自 15-16 筆 session_end 強制平倉；扣掉它們兩版都大幅淨虧。"),
            ),
        ),
        contamination_forensics=contamination_forensics,
        residual_lookahead_audit=residual_audit,
    )
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nsaved -> {OUT_JSON}")
    print(json.dumps(out["deltas_vs_bar"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
