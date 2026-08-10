"""Night-session equivalent of the day-session struct_break / PV8-regime /
pre-entry-amplitude analysis already done this session for the day session.

Uses w83 window (tx_1m_fullnight_cache_full.json, 83 days, 2026-04-01..2026-07-31),
the CURRENT live PAPER_RECIPE (already has day|normal + day|div_hh_weak_vol fully
blocked from cell-tune v3, and night|expand_dn / night|normal / night|div_hh_weak_vol
/ night|climax_up already blocked from an earlier v2 change).

Steps:
1. Run simulate() per day over the w83 window, tag each trade's session by entry
   time (et): night = >=15:00 or <05:00, day = 08:45-13:45.
2. Break down net pnl / exit-reason by (session, regime_e) cell, focusing on the
   still-open night regimes: climax_dn, contract, dry, expand_up.
3. Build a night-session weighted_amp30 (30-min recency-weighted amplitude) on
   CONTIGUOUS night bars per session (a session = one continuous 15:00->05:00
   block; do not roll a window across a session boundary/gap), evaluate it at
   each trade's entry bar, and test IC with struct_break pnl at night, day-
   clustered.

Not wired into any pipeline; scratch research script.
"""
from __future__ import annotations

import copy
import sys
from collections import defaultdict

import numpy as np
from scipy import stats as sstats

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from tmf_channel.cache_store import load_day, list_days  # noqa: E402
from tmf_channel.engine import simulate, load_vixtwn_delta  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402

CACHE = "tx_1m_fullnight_cache_full.json"
AMP_WINDOW = 30


def _hhmm(ts: str) -> str:
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]


def is_night(et: str) -> bool:
    if et is None:
        return False
    hh, mm = _hhmm(et).split(":")
    hm = int(hh) * 60 + int(mm)
    return hm >= 15 * 60 or hm < 5 * 60


def weighted_amp30(h: np.ndarray, l: np.ndarray) -> np.ndarray:
    """Linear-recency-weighted 30-bar rolling amplitude, matching
    tx_channel_amp_volume_interaction.weighted_amp30. NaN until warmed up."""
    ranges = h - l
    n = len(ranges)
    out = np.full(n, np.nan)
    w = np.arange(1, AMP_WINDOW + 1, dtype=float)
    wsum = w.sum()
    for i in range(AMP_WINDOW - 1, n):
        seg = ranges[i - AMP_WINDOW + 1 : i + 1]
        out[i] = float(np.dot(seg, w) / wsum)
    return out


def night_session_blocks(rows: list[dict]) -> list[tuple[int, int]]:
    """Return list of (start_idx, end_idx_inclusive) for contiguous night
    blocks within a day's full row list (indices into the day's O/H/L/C/T
    arrays used by simulate()). A night block is contiguous sess=='night' rows
    with no >2-min gap in HH:MM (handles the 15:00->24:00->05:00 wraparound by
    just checking sess flag + monotbasic gap check on the row order, since
    rows within one cached day file are already time-ordered for that block)."""
    blocks = []
    start = None
    prev_t = None

    def tmin(t):
        hh, mm = t.split(":")
        return int(hh) * 60 + int(mm)

    for i, r in enumerate(rows):
        if r["sess"] == "night":
            if start is None:
                start = i
                prev_t = tmin(r["t"])
            else:
                cur = tmin(r["t"])
                gap = (cur - prev_t) % 1440
                if gap > 5:
                    blocks.append((start, i - 1))
                    start = i
                prev_t = cur
        else:
            if start is not None:
                blocks.append((start, i - 1))
                start = None
    if start is not None:
        blocks.append((start, len(rows) - 1))
    return blocks


def main():
    days = list_days(source=CACHE)
    vix = load_vixtwn_delta() or {}
    recipe = copy.deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    cell_pnl = defaultdict(float)
    cell_n = defaultdict(int)
    cell_wins = defaultdict(int)
    cell_why = defaultdict(lambda: defaultdict(float))
    cell_why_n = defaultdict(lambda: defaultdict(int))

    # for amplitude/struct_break IC: per-trade records
    amp_records = []  # (day, pnl, amp_at_entry, why_cat)
    day_night_struct_pnl = defaultdict(list)  # day -> list of struct_break pnl (night trades)

    n_days_ok = 0
    for day in days:
        rows = load_day(day, source=CACHE)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]
        H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]
        C = [float(r["c"]) for r in rows]
        V = [float(r["v"]) for r in rows]
        T = [f"{day}T{r['t']}:00.000+08:00" for r in rows]

        try:
            trades, events, ws, wl, rvol, regime, open_pos = simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
        except Exception as e:
            print(f"{day}: simulate failed: {e}")
            continue
        n_days_ok += 1

        # precompute night-session amp arrays (per contiguous night block)
        h_arr = np.array(H)
        l_arr = np.array(L)
        blocks = night_session_blocks(rows)
        amp_full = np.full(len(rows), np.nan)
        for (s, e) in blocks:
            seg_h = h_arr[s : e + 1]
            seg_l = l_arr[s : e + 1]
            amp_full[s : e + 1] = weighted_amp30(seg_h, seg_l)

        for tr in trades:
            et = tr.get("et")
            sess = "night" if is_night(et) else "day"
            regime_e = tr.get("regime_e", "?")
            pnl = tr.get("pnl", 0.0)
            why = str(tr.get("why", "")).split("|")[0]
            key = (sess, regime_e)
            cell_pnl[key] += pnl
            cell_n[key] += 1
            if pnl > 0:
                cell_wins[key] += 1
            cell_why[key][why] += pnl
            cell_why_n[key][why] += 1

            if sess == "night":
                eb = tr.get("eb")
                amp_e = amp_full[eb] if eb is not None and 0 <= eb < len(amp_full) else np.nan
                amp_records.append((day, pnl, amp_e, why))
                if why == "struct_break":
                    day_night_struct_pnl[day].append(pnl)

    print(f"days simulated ok: {n_days_ok}/{len(days)}")
    print()
    print("=== (session, regime_e) cell breakdown, w83, current PAPER_RECIPE ===")
    for key in sorted(cell_pnl, key=lambda k: cell_pnl[k]):
        sess, reg = key
        n = cell_n[key]
        wr = cell_wins[key] / n * 100 if n else float("nan")
        print(f"{sess:5s} {reg:16s} n={n:4d} net={cell_pnl[key]:10.1f} win%={wr:5.1f}")
        # top why categories for this cell
        whys = sorted(cell_why[key].items(), key=lambda kv: kv[1])
        for w, wp in whys[:3]:
            wn = cell_why_n[key][w]
            print(f"      why={w:14s} n={wn:4d} pnl={wp:10.1f}")

    print()
    print("=== night struct_break only: pre-entry amp30 vs pnl (day-clustered) ===")
    struct_recs = [(d, p, a) for (d, p, a, w) in amp_records if w == "struct_break" and not np.isnan(a)]
    print(f"n night struct_break trades w/ valid pre-entry amp: {len(struct_recs)}")
    if len(struct_recs) >= 10:
        pnls = np.array([r[1] for r in struct_recs])
        amps = np.array([r[2] for r in struct_recs])
        ic, p_pool = sstats.spearmanr(amps, pnls)
        print(f"pooled Spearman IC (amp30 vs struct_break pnl): {ic:.3f} (p={p_pool:.4g}, n={len(struct_recs)})")
        median_amp = np.median(amps)
        lo_mask = amps <= median_amp
        hi_mask = amps > median_amp
        print(
            f"low-amp half:  n={lo_mask.sum():4d} mean_pnl={pnls[lo_mask].mean():8.1f} "
            f"win%={(pnls[lo_mask] > 0).mean() * 100:5.1f}"
        )
        print(
            f"high-amp half: n={hi_mask.sum():4d} mean_pnl={pnls[hi_mask].mean():8.1f} "
            f"win%={(pnls[hi_mask] > 0).mean() * 100:5.1f}"
        )

        # day-clustered: per-day correlation is not well-defined with few trades/day,
        # so instead do day-clustered t-test on mean daily pnl split by whether that
        # day's mean pre-entry amp was above/below the GLOBAL (all-days) median (a
        # simple day-level proxy, not causal-rolling — flagged as such below).
        by_day = defaultdict(list)
        for d, p, a in struct_recs:
            by_day[d].append((p, a))
        day_means_pnl = []
        day_means_amp = []
        for d, recs in by_day.items():
            ps = [r[0] for r in recs]
            as_ = [r[1] for r in recs]
            day_means_pnl.append(np.mean(ps))
            day_means_amp.append(np.mean(as_))
        day_means_pnl = np.array(day_means_pnl)
        day_means_amp = np.array(day_means_amp)
        n_days_with = len(day_means_pnl)
        print(f"n distinct days with night struct_break trades: {n_days_with}")
        if n_days_with >= 8:
            ic_day, p_day = sstats.spearmanr(day_means_amp, day_means_pnl)
            print(
                f"day-level (1 obs/day, mean pnl & mean amp) Spearman IC: {ic_day:.3f} "
                f"(p={p_day:.4g}, n={n_days_with})"
            )
    else:
        print("too few night struct_break trades with valid pre-entry amp for a meaningful test")

    print()
    print("=== ALL night trades (not just struct_break): amp30 vs pnl, day-clustered ===")
    all_night = [(d, p, a) for (d, p, a, w) in amp_records if not np.isnan(a)]
    print(f"n night trades w/ valid pre-entry amp: {len(all_night)}")
    if len(all_night) >= 10:
        pnls = np.array([r[1] for r in all_night])
        amps = np.array([r[2] for r in all_night])
        ic, p_pool = sstats.spearmanr(amps, pnls)
        print(f"pooled Spearman IC: {ic:.3f} (p={p_pool:.4g}, n={len(all_night)})")
        by_day = defaultdict(list)
        for d, p, a in all_night:
            by_day[d].append((p, a))
        day_means_pnl = np.array([np.mean([r[0] for r in recs]) for recs in by_day.values()])
        day_means_amp = np.array([np.mean([r[1] for r in recs]) for recs in by_day.values()])
        if len(day_means_pnl) >= 8:
            ic_day, p_day = sstats.spearmanr(day_means_amp, day_means_pnl)
            print(f"day-level Spearman IC: {ic_day:.3f} (p={p_day:.4g}, n={len(day_means_pnl)})")


if __name__ == "__main__":
    main()
