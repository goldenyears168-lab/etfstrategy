"""Wyckoff "effort vs result" volume-price divergence, and volume-climax,
tested as predictors of struct_break trade outcomes specifically (the
established dominant loss driver: ~1064 trades, ~1.9% win rate, ~75% of total
losses — see prior thread findings; NOT re-derived here).

Prior VCP/ATR-compression operationalization already tested and found no
edge (tx_channel_vcp_contraction_breakout.py). This script tests the OTHER
untested concept from the same web-research grounding: effort-without-result
(high volume, little net price progress = absorption/distribution warning)
and volume-climax spikes, both evaluated causally AT the struct_break trade's
entry bar, using the current live PAPER_RECIPE re-simulated across the w83
window (tx_1m_fullnight_cache_full.json, 83 days). Also checks whether either
feature adds anything beyond the already-known pre-entry-amplitude lead via
residualization (matches the pattern in tx_channel_amp_persistence_drivers.py
Test 2: residualize feature-of-interest on the control var via linear OLS,
then correlate the residual with the outcome).

Day-clustered significance follows the established pattern for sparse
per-day trade counts (tx_channel_night_regime_amp_breakdown.py): compute ONE
number per day (mean feature value that day, mean pnl that day, mean
residual that day) and correlate ACROSS days (n = number of days with >=1
struct_break trade), rather than a naive within-day Spearman IC (struct_break
trades are too sparse per day — see printed per-day counts — for a
within-day correlation to be meaningful). Pooled (all-trades) Spearman is
also reported for reference but is NOT the significance claim (pooling
violates the day-independence assumption established earlier this session).

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
EFFORT_WINDOWS = (10, 20, 30)
CLIMAX_WINDOW = 20
MIN_DISPLACEMENT_FLOOR = 1.0  # pts; avoid ratio blowup near-zero net move


def tmin(t: str) -> int:
    hh, mm = t.split(":")
    return int(hh) * 60 + int(mm)


def _hhmm(ts: str) -> str:
    """Extract bare HH:MM from either a raw cache 't' ("HH:MM") or a full
    ISO timestamp ("YYYY-MM-DDTHH:MM:SS.mmm+08:00") like trade et/xt."""
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s[:5]


def is_night(et: str) -> bool:
    if et is None:
        return False
    hm = tmin(_hhmm(et))
    return hm >= 15 * 60 or hm < 5 * 60


def contiguous_blocks(rows: list[dict], want_sess: str) -> list[tuple[int, int]]:
    """(start,end_inclusive) index ranges of contiguous want_sess rows,
    matching tx_channel_night_regime_amp_breakdown.night_session_blocks but
    generalized to either session."""
    blocks = []
    start = None
    prev_t = None
    for i, r in enumerate(rows):
        if r["sess"] == want_sess:
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


def weighted_amp30(h: np.ndarray, l: np.ndarray) -> np.ndarray:
    ranges = h - l
    n = len(ranges)
    out = np.full(n, np.nan)
    w = np.arange(1, AMP_WINDOW + 1, dtype=float)
    wsum = w.sum()
    for i in range(AMP_WINDOW - 1, n):
        seg = ranges[i - AMP_WINDOW + 1 : i + 1]
        out[i] = float(np.dot(seg, w) / wsum)
    return out


def effort_result_ratio(c: np.ndarray, v: np.ndarray, window: int) -> np.ndarray:
    """volume "effort" over trailing `window` bars / net price "result"
    (|close-to-close displacement|) over the same window. High = lots of
    volume, little net progress ("effort without result" / Wyckoff
    absorption). Causal: uses bars [i-window+1, i] only."""
    n = len(c)
    out = np.full(n, np.nan)
    for i in range(window - 1, n):
        vol_sum = v[i - window + 1 : i + 1].sum()
        disp = abs(c[i] - c[i - window + 1])
        out[i] = vol_sum / max(disp, MIN_DISPLACEMENT_FLOOR)
    return out


def volume_climax(v: np.ndarray, window: int = CLIMAX_WINDOW) -> np.ndarray:
    """current-bar volume / trailing `window`-bar average volume (bars
    [i-window, i-1], excludes current bar to avoid tautology). >1 = above
    average; >=2/3 commonly used as a "climax" spike threshold."""
    n = len(v)
    out = np.full(n, np.nan)
    for i in range(window, n):
        avg = v[i - window : i].mean()
        if avg > 0:
            out[i] = v[i] / avg
    return out


def per_block_feature(rows, arr_builder, blocks):
    """Compute a feature only within each contiguous block (never rolls a
    window across a session-boundary gap), return a full-length array."""
    n = len(rows)
    out = np.full(n, np.nan)
    for s, e in blocks:
        out[s : e + 1] = arr_builder(s, e)
    return out


def day_level_test(recs, label):
    """recs: list of (day, pnl, feat). Day-level Spearman: 1 obs/day (mean
    pnl, mean feat) across days with >=1 struct_break trade. Also reports
    pooled Spearman for reference (NOT the significance claim)."""
    recs = [(d, p, f) for d, p, f in recs if not np.isnan(f)]
    n = len(recs)
    print(f"\n--- {label}: n_trades={n} ---")
    if n < 10:
        print("  too few trades for a meaningful test")
        return
    pnls = np.array([r[1] for r in recs])
    feats = np.array([r[2] for r in recs])
    ic_pool, p_pool = sstats.spearmanr(feats, pnls)
    print(f"  pooled Spearman IC={ic_pool:.3f} p={p_pool:.4g} (n={n}, NOT day-clustered, reference only)")

    by_day = defaultdict(list)
    for d, p, f in recs:
        by_day[d].append((p, f))
    counts = sorted(len(v) for v in by_day.values())
    print(f"  n distinct days={len(by_day)}, per-day trade counts (sorted)={counts}")
    day_pnl = np.array([np.mean([r[0] for r in v]) for v in by_day.values()])
    day_feat = np.array([np.mean([r[1] for r in v]) for v in by_day.values()])
    if len(day_pnl) >= 8:
        ic_day, p_day = sstats.spearmanr(day_feat, day_pnl)
        print(f"  DAY-LEVEL (1 obs/day) Spearman IC={ic_day:.3f} p={p_day:.4g} (n_days={len(day_pnl)})  <-- significance claim")
    else:
        print("  too few distinct days for day-level test")
    return dict(by_day=by_day, day_pnl=day_pnl, day_feat=day_feat)


def residualize_and_test(recs_feat, recs_ctrl, recs_pnl_by_key, label):
    """recs_feat/recs_ctrl/pnl: dict key->(day,val) aligned by same trade
    order. Pooled OLS feat~ctrl across ALL trades (all days), residual, then
    day-level test of residual vs pnl (same day-level pattern as above)."""
    days = [r[0] for r in recs_pnl_by_key]
    pnls = np.array([r[1] for r in recs_pnl_by_key])
    ctrl = np.array([r[1] for r in recs_ctrl])
    feat = np.array([r[1] for r in recs_feat])
    valid = ~(np.isnan(ctrl) | np.isnan(feat) | np.isnan(pnls))
    days = [d for d, ok in zip(days, valid) if ok]
    ctrl, feat, pnls = ctrl[valid], feat[valid], pnls[valid]
    n = len(pnls)
    print(f"\n--- residualize {label} on pre-entry amp30 (pooled OLS, all struct_break trades): n={n} ---")
    if n < 15:
        print("  too few trades")
        return
    b = np.polyfit(ctrl, feat, 1)
    resid = feat - np.polyval(b, ctrl)
    ic_pool, p_pool = sstats.spearmanr(resid, pnls)
    print(f"  pooled Spearman IC(residual, pnl)={ic_pool:.3f} p={p_pool:.4g} (reference only)")
    by_day = defaultdict(list)
    for d, p, r in zip(days, pnls, resid):
        by_day[d].append((p, r))
    if len(by_day) >= 8:
        day_pnl = np.array([np.mean([x[0] for x in v]) for v in by_day.values()])
        day_resid = np.array([np.mean([x[1] for x in v]) for v in by_day.values()])
        ic_day, p_day = sstats.spearmanr(day_resid, day_pnl)
        print(f"  DAY-LEVEL Spearman IC(residual,pnl)={ic_day:.3f} p={p_day:.4g} (n_days={len(day_pnl)})  <-- significance claim")
    else:
        print("  too few distinct days")


def main():
    days = list_days(source=CACHE)
    vix = load_vixtwn_delta() or {}
    recipe = copy.deepcopy(PAPER_RECIPE)
    recipe.setdefault("hang_anchor", "O")

    # trade-level records: day -> pnl, amp_e, effort_e[N], climax_e
    struct_all = []       # (day, pnl, amp, effort10, effort20, effort30, climax)
    struct_day_sess = []  # same, day-session only
    struct_night_sess = []

    n_days_ok = 0
    for day in days:
        rows = load_day(day, source=CACHE)
        if not rows:
            continue
        H = np.array([float(r["h"]) for r in rows])
        L = np.array([float(r["l"]) for r in rows])
        C = np.array([float(r["c"]) for r in rows])
        V = np.array([float(r["v"]) for r in rows])
        O = [float(r["o"]) for r in rows]
        Ol = list(O)
        Hl, Ll, Cl, Vl = list(H), list(L), list(C), list(V)
        T = [f"{day}T{r['t']}:00.000+08:00" for r in rows]

        try:
            trades, events, ws, wl, rvol, regime, open_pos = simulate(
                Ol, Hl, Ll, Cl, Vl, T, recipe, vix_delta=vix
            )
        except Exception as e:
            print(f"{day}: simulate failed: {e}")
            continue
        n_days_ok += 1

        day_blocks = contiguous_blocks(rows, "day")
        night_blocks = contiguous_blocks(rows, "night")
        all_blocks = sorted(day_blocks + night_blocks)

        amp_full = per_block_feature(rows, lambda s, e: weighted_amp30(H[s : e + 1], L[s : e + 1]), all_blocks)
        eff_full = {}
        for w in EFFORT_WINDOWS:
            eff_full[w] = per_block_feature(rows, lambda s, e, w=w: effort_result_ratio(C[s : e + 1], V[s : e + 1], w), all_blocks)
        climax_full = per_block_feature(rows, lambda s, e: volume_climax(V[s : e + 1]), all_blocks)

        for tr in trades:
            why = str(tr.get("why", "")).split("|")[0]
            if why != "struct_break":
                continue
            eb = tr.get("eb")
            if eb is None or not (0 <= eb < len(rows)):
                continue
            pnl = tr.get("pnl", 0.0)
            amp_e = amp_full[eb]
            effs = tuple(eff_full[w][eb] for w in EFFORT_WINDOWS)
            climax_e = climax_full[eb]
            rec = (day, pnl, amp_e, *effs, climax_e)
            struct_all.append(rec)
            if is_night(tr.get("et")):
                struct_night_sess.append(rec)
            else:
                struct_day_sess.append(rec)

    print(f"days simulated ok: {n_days_ok}/{len(days)}")
    print(f"total struct_break trades: {len(struct_all)}  (day-session: {len(struct_day_sess)}, night: {len(struct_night_sess)})")

    for scope_name, scope in (("ALL struct_break", struct_all), ("DAY-session struct_break", struct_day_sess), ("NIGHT-session struct_break", struct_night_sess)):
        print(f"\n{'='*72}\n{scope_name}\n{'='*72}")
        day_level_test([(r[0], r[1], r[2]) for r in scope], f"{scope_name}: pre-entry amp30 (control, sanity check)")
        for wi, w in enumerate(EFFORT_WINDOWS):
            day_level_test([(r[0], r[1], r[3 + wi]) for r in scope], f"{scope_name}: effort/result ratio (N={w})")
        day_level_test([(r[0], r[1], r[6]) for r in scope], f"{scope_name}: volume climax (V_i / trailing{CLIMAX_WINDOW}avg)")

        # residualize each effort/result window on amp30
        for wi, w in enumerate(EFFORT_WINDOWS):
            feat_recs = [(r[0], r[3 + wi]) for r in scope]
            ctrl_recs = [(r[0], r[2]) for r in scope]
            pnl_recs = [(r[0], r[1]) for r in scope]
            residualize_and_test(feat_recs, ctrl_recs, pnl_recs, f"{scope_name}: effort/result N={w}")
        # residualize climax on amp30 too
        residualize_and_test([(r[0], r[6]) for r in scope], [(r[0], r[2]) for r in scope], [(r[0], r[1]) for r in scope], f"{scope_name}: volume climax")


if __name__ == "__main__":
    main()
