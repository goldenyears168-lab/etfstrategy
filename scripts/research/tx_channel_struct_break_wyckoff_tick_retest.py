"""Tick-level retest of the Wyckoff "effort vs result" (volume / |net price
displacement|) feature against struct_break trade outcomes, per assigned item
#14. The 1-min-bar version (tx_channel_struct_break_wyckoff_divergence.py,
run earlier this session) found fragile, window-size-sensitive day-level
correlations. This script rebuilds the SAME concept using TRUE TICK DATA
(load_front_month_ticks) instead of bar OHLCV approximations, re-tests
against struct_break outcomes on the CURRENT live PAPER_RECIPE
(RECIPE_VERSION=final_v1_4_0), day-clustered, plus a split-half honesty
check (first half vs second half of trading days, chronologically) — the
same check that caught the bar-level version as fragile/overfit.

Methodology notes (see session rules):
- Trades come from re-simulating PAPER_RECIPE on the w83 cache
  (tx_1m_fullnight_cache_full.json, 83 days, 2026-04-01..07-31), matching
  the "current baseline" instruction.
- Feature is computed CAUSALLY: for a struct_break trade entering at
  (day=D, et=HH:MM), pull raw ticks strictly BEFORE the entry timestamp
  from the SAME calendar-day tick file (finmind_tx_tick_by_day/D.json —
  confirmed this file spans the full 00:00-23:59 calendar day, i.e. the
  SAME calendar-day bucketing convention as the bar cache's 'day' field, so
  no cross-file stitching is needed except for entries in the first W
  minutes after 00:00, which are dropped as NaN — a known small-sample edge
  case, disclosed below, NOT patched with previous-day stitching this round
  given time budget).
- effort/result ratio(W minutes) = sum(tick volume in (et-W, et)) /
  max(|price at last tick before et - price at first tick in window|, 1.0pt
  floor). This differs from the bar-level version in two ways: (1) true
  tick prices/timestamps instead of 1-min OHLC bars, (2) window CUTS OFF
  strictly before et (does not include the entry bar's own close), which is
  slightly more causal than the original bar version (which used bar `eb`'s
  own close inclusive).
- Day-level test: one obs/day (mean feature, mean pnl across that day's
  struct_break trades), Spearman IC across days — the established
  established pattern (see divergence script). Pooled (trade-level) IC also
  reported for reference only, NOT the significance claim.
- Split-half honesty check: sort days chronologically, split into first
  half / second half, redo the day-level IC independently in each half. A
  feature that reverses sign or collapses to ~0 in one half is flagged as
  not robust (mirrors what caught the bar-level version).

Not wired into any pipeline; scratch research script. Read-only backtest
simulation only, no live/order-layer code touched.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sstats

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from tmf_channel.cache_store import bar_timestamps, list_days, load_day  # noqa: E402
from tmf_channel.engine import simulate, load_vixtwn_delta  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402
from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

CACHE = "tx_1m_fullnight_cache_full.json"
WINDOWS_MIN = (10, 20, 30)
MIN_DISPLACEMENT_FLOOR = 1.0  # pts

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "channel_lab"


def _hhmm(ts: str) -> str:
    """Extract bare HH:MM from either a full ISO 'DAYTHH:MM:...' engine
    timestamp or a bare 'HH:MM' string (defensive for both formats)."""
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]


def is_night(et: str) -> bool:
    if et is None:
        return False
    hm_str = _hhmm(et)
    hh, mm = hm_str.split(":")
    hm = int(hh) * 60 + int(mm)
    return hm >= 15 * 60 or hm < 5 * 60


def _ticks_spanning_session(day: str):
    """Front-month ticks for BOTH calendar dates a session touches -- the
    00:00-04:59 tail of a session-convention cache lives in day+1's tick file
    (fixed 2026-08-11)."""
    import pandas as pd
    from datetime import date, timedelta

    frames = []
    for cal in (day, (date.fromisoformat(day) + timedelta(days=1)).isoformat()):
        f = load_front_month_ticks(cal)
        if f is not None and not f.empty:
            frames.append(f)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True).sort_values("dt").reset_index(drop=True)


def tick_effort_result(ticks_day: pd.DataFrame, entry_dt: pd.Timestamp, window_min: int) -> float:
    """Causal tick-level effort/result ratio using ticks strictly before
    entry_dt within the SAME calendar-day tick frame `ticks_day`
    (dt, price, volume). NaN if the trailing window is not fully covered
    within this day's file (e.g. entry in first `window_min` minutes after
    00:00 — would need previous day's tail, not stitched this round)."""
    win_start = entry_dt - pd.Timedelta(minutes=window_min)
    day_floor = entry_dt.normalize()
    if win_start < day_floor:
        return float("nan")
    win = ticks_day[(ticks_day["dt"] >= win_start) & (ticks_day["dt"] < entry_dt)]
    if win.empty or len(win) < 3:
        return float("nan")
    vol_sum = float(win["volume"].sum())
    disp = abs(float(win["price"].iloc[-1]) - float(win["price"].iloc[0]))
    return vol_sum / max(disp, MIN_DISPLACEMENT_FLOOR)


def day_level_test(recs, label):
    """recs: list of (day, pnl, feat)."""
    recs = [(d, p, f) for d, p, f in recs if not (isinstance(f, float) and np.isnan(f))]
    n = len(recs)
    print(f"\n--- {label}: n_trades={n} ---")
    if n < 10:
        print("  too few trades for a meaningful test")
        return None
    pnls = np.array([r[1] for r in recs])
    feats = np.array([r[2] for r in recs])
    ic_pool, p_pool = sstats.spearmanr(feats, pnls)
    print(f"  pooled Spearman IC={ic_pool:.3f} p={p_pool:.4g} (n={n}, NOT day-clustered, reference only)")

    by_day = defaultdict(list)
    for d, p, f in recs:
        by_day[d].append((p, f))
    counts = sorted(len(v) for v in by_day.values())
    print(f"  n distinct days={len(by_day)}, per-day trade counts (sorted)={counts}")
    day_pnl = {d: np.mean([r[0] for r in v]) for d, v in by_day.items()}
    day_feat = {d: np.mean([r[1] for r in v]) for d, v in by_day.items()}
    if len(day_pnl) >= 8:
        days_sorted = sorted(day_pnl.keys())
        pnl_arr = np.array([day_pnl[d] for d in days_sorted])
        feat_arr = np.array([day_feat[d] for d in days_sorted])
        ic_day, p_day = sstats.spearmanr(feat_arr, pnl_arr)
        print(f"  DAY-LEVEL (1 obs/day) Spearman IC={ic_day:.3f} p={p_day:.4g} (n_days={len(days_sorted)})  <-- significance claim")

        # split-half honesty check (chronological)
        mid = len(days_sorted) // 2
        first_days, second_days = days_sorted[:mid], days_sorted[mid:]
        res = {}
        for half_name, half_days in (("first-half", first_days), ("second-half", second_days)):
            if len(half_days) < 6:
                print(f"  {half_name}: too few days ({len(half_days)}) for split-half test")
                continue
            f_arr = np.array([day_feat[d] for d in half_days])
            p_arr = np.array([day_pnl[d] for d in half_days])
            ic_h, p_h = sstats.spearmanr(f_arr, p_arr)
            print(f"  SPLIT-HALF {half_name} (n_days={len(half_days)}, {half_days[0]}..{half_days[-1]}): IC={ic_h:.3f} p={p_h:.4g}")
            res[half_name] = (ic_h, p_h)
        return dict(ic_day=ic_day, p_day=p_day, n_days=len(days_sorted), halves=res)
    else:
        print("  too few distinct days for day-level test")
        return None


def main():
    days = list_days(source=CACHE)
    vix = load_vixtwn_delta() or {}
    import copy
    recipe = copy.deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    struct_all = []       # (day, pnl, {w: ratio})
    struct_day_sess = []
    struct_night_sess = []

    tick_cache: dict[str, pd.DataFrame | None] = {}
    n_days_ok = 0
    n_days_no_ticks = 0

    for day in days:
        rows = load_day(day, source=CACHE)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]
        H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]
        C = [float(r["c"]) for r in rows]
        V = [float(r["v"]) for r in rows]
        T = bar_timestamps(day, rows, source=CACHE)

        try:
            trades, events, ws, wl, rvol, regime, open_pos = simulate(
                O, H, L, C, V, T, recipe, vix_delta=vix
            )
        except Exception as e:
            print(f"{day}: simulate failed: {e}")
            continue
        n_days_ok += 1

        sb_trades = [tr for tr in trades if str(tr.get("why", "")).split("|")[0] == "struct_break"]
        if not sb_trades:
            continue

        if day not in tick_cache:
            try:
                tick_cache[day] = _ticks_spanning_session(day)
            except Exception as e:
                print(f"{day}: tick load failed: {e}")
                tick_cache[day] = None
        ticks_day = tick_cache[day]
        if ticks_day is None or ticks_day.empty:
            n_days_no_ticks += 1
            continue

        for tr in sb_trades:
            et = tr.get("et")
            if not et:
                continue
            try:
                # Real calendar date from et (post-midnight bars are day+1
                # for a session-convention cache); ticks are re-keyed to match.
                entry_dt = pd.Timestamp(f"{str(et)[:10]} {_hhmm(et)}:00")
            except Exception:
                continue
            pnl = tr.get("pnl", 0.0)
            ratios = {w: tick_effort_result(ticks_day, entry_dt, w) for w in WINDOWS_MIN}
            rec = (day, pnl, ratios)
            struct_all.append(rec)
            if is_night(et):
                struct_night_sess.append(rec)
            else:
                struct_day_sess.append(rec)

    print(f"days simulated ok: {n_days_ok}/{len(days)}  (days w/ struct_break trades but no tick file: {n_days_no_ticks})")
    print(f"total struct_break trades w/ tick coverage attempt: {len(struct_all)}  (day-session: {len(struct_day_sess)}, night: {len(struct_night_sess)})")

    all_results = {}
    for scope_name, scope in (("ALL struct_break", struct_all), ("DAY-session struct_break", struct_day_sess), ("NIGHT-session struct_break", struct_night_sess)):
        print(f"\n{'='*72}\n{scope_name} (TICK-LEVEL)\n{'='*72}")
        scope_results = {}
        for w in WINDOWS_MIN:
            recs = [(r[0], r[1], r[2][w]) for r in scope]
            res = day_level_test(recs, f"{scope_name}: tick effort/result ratio (W={w}min)")
            scope_results[f"W{w}"] = res
        all_results[scope_name] = scope_results

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "struct_break_wyckoff_tick_retest_result.json"

    def _clean(o):
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, tuple):
            return list(o)
        if isinstance(o, float) and np.isnan(o):
            return None
        return o

    out_path.write_text(json.dumps(dict(
        n_days_ok=n_days_ok,
        n_struct_break_trades=len(struct_all),
        n_day_session=len(struct_day_sess),
        n_night_session=len(struct_night_sess),
        windows_min=list(WINDOWS_MIN),
        results=_clean(all_results),
    ), indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
