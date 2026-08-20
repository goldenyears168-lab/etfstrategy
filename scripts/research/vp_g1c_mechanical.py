#!/usr/bin/env python3
"""vp_g1c_mechanical — Volume-profile node (HVN) mechanical decomposition.

Question (G1c): after controlling for the four boring/collinear explanations,
does "this price level is a high-volume node" retain ANY independent information
about (a) whether a resting limit order there gets filled, and (b) the gross
points-per-fill of the live TMF intraday reversion channel?

Controls (the four mandated mechanical explanations):
  C1 price-has-been-there  : visit indicator, trade-count-at-price node strength
                             (c1/c3), recency of last visit (minutes)
  C2 activity / volatility : window volume, tick count, path length, range
  C3 distance from spot    : d (12..66 pts), and |d|<=3 handled by construction
                             (the live band starts at 12 so no |D|<=3 rows exist;
                              we additionally report the node-distance
                              distribution separately)
  C4 round numbers/anchors : P mod 10/50/100, prev-day H/L/C, day open,
                             running day high/low

Design rules honoured (from the research brief):
  * ex-ante contract roll (third-Wednesday calendar), never a volume argmax
  * sharding is BY WHOLE DAY (contiguous date blocks); every rolling window is
    strictly inside one day, so shard assignment cannot move any statistic
  * cache dir != input dir, output json != any input
  * clustering unit = trading day; per-year and per-day-sign consistency reported
  * every "effect" claim is paired with a control/placebo arm
  * NOT a filter: node info may only re-price WITHIN the allowed hang band,
    trade count is held fixed by construction (every snapshot quotes both sides)

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/vp_g1c_mechanical.py cache
  PYTHONPATH=src .venv/bin/python scripts/research/vp_g1c_mechanical.py analyze
  PYTHONPATH=src .venv/bin/python scripts/research/vp_g1c_mechanical.py all
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np

TICK_DIR = Path.home() / "goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day"
CACHE_DIR = Path.home() / "goldenstocks-data/cache/research/vp_g1c_daycache"
OUT_DIR = Path("/Users/jackm4/goldenstocks/reports/research/channel_lab")
OUT_JSON = OUT_DIR / "vp_g1c_mechanical.json"

# --- session / snapshot geometry -------------------------------------------
SESS_OPEN = 8 * 3600 + 45 * 60          # 08:45:00
SESS_CLOSE = 13 * 3600 + 45 * 60        # 13:45:00
LOOKBACK = 600                          # 10 minutes volume profile
TOUCH_H = 1800                          # 30 min window for the resting order
POST_K = 900                            # 15 min held after fill
SNAP_STEP = 180                         # snapshot every 3 min
SNAP_T0 = LOOKBACK                      # first snapshot 08:55
SNAP_T1 = (SESS_CLOSE - SESS_OPEN) - TOUCH_H - POST_K   # last snapshot 13:00

# candidate hang distances: live band 12-33 at 1pt, the x2 proposal 36-66 at 3pt
DISTS = np.array(list(range(12, 34)) + list(range(36, 67, 3)), dtype=np.int32)
N_SNAP = len(range(SNAP_T0, SNAP_T1 + 1, SNAP_STEP))
ROWS_PER_DAY = N_SNAP * 2 * len(DISTS)

COST_LINE_TMF = 4.79   # points round trip, cost line v2 (trigger anchor)
GROSS_PER_FILL = 2.86  # current live gross per fill

COLS = [
    "day_idx", "snap_idx", "tod", "side", "d", "P", "mid",
    "vol1", "vol3", "ntk1", "ntk3", "z1", "z3", "c1", "c3",
    "rec", "vis", "avgsz",
    "volwin", "ntkwin", "pathwin", "rangewin",
    "r10", "r50", "r100",
    "a_pdh", "a_pdl", "a_pdc", "a_open", "a_dh", "a_dl",
    "touched", "pnl", "tt", "mfe", "mae",
    "near_share", "argmax_dist", "hvn_dist",
]
CIX = {c: i for i, c in enumerate(COLS)}


# --------------------------------------------------------------------------
# ex-ante contract roll: third Wednesday settlement, no look-ahead whatsoever
# --------------------------------------------------------------------------
def third_wednesday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    # weekday(): Mon=0 .. Wed=2
    offset = (2 - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 14)


def front_contract(day: dt.date) -> str:
    """Day-session front month. D <= third-Wednesday(M) -> M, else next month."""
    w3 = third_wednesday(day.year, day.month)
    if day <= w3:
        return f"{day.year:04d}{day.month:02d}"
    y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return f"{y:04d}{m:02d}"


# --------------------------------------------------------------------------
# stage 1: parse one raw day -> compact day-session arrays of the front month
# --------------------------------------------------------------------------
def build_day_cache(date_str: str) -> dict | None:
    src = TICK_DIR / f"{date_str}.json"
    try:
        raw = json.loads(src.read_text())
    except Exception:
        return None
    if not raw:
        return None
    day = dt.date.fromisoformat(date_str)
    want = front_contract(day)
    ts, px, vol = [], [], []
    for r in raw:
        if r.get("futures_id") != "TX" or r.get("contract_date") != want:
            continue
        hhmmss = r["date"][11:]
        s = int(hhmmss[0:2]) * 3600 + int(hhmmss[3:5]) * 60 + int(hhmmss[6:8])
        if s < SESS_OPEN or s > SESS_CLOSE:
            continue
        ts.append(s - SESS_OPEN)
        px.append(int(round(r["price"])))
        vol.append(int(r["volume"]))
    if len(ts) < 2000:
        return None
    ts = np.asarray(ts, dtype=np.int32)
    px = np.asarray(px, dtype=np.int32)
    vol = np.asarray(vol, dtype=np.int32)
    order = np.argsort(ts, kind="stable")
    ts, px, vol = ts[order], px[order], vol[order]
    np.savez_compressed(CACHE_DIR / f"{date_str}.npz", ts=ts, px=px, vol=vol)
    return {
        "date": date_str, "contract": want, "n": int(len(ts)),
        "open": int(px[0]), "high": int(px.max()),
        "low": int(px.min()), "close": int(px[-1]),
    }


def _cache_block(dates):
    return [m for m in (build_day_cache(d) for d in dates) if m is not None]


def run_cache(workers: int = 8) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dates = sorted(p.stem for p in TICK_DIR.glob("*.json"))
    # contiguous date blocks, never round-robin
    step = (len(dates) + workers - 1) // workers      # contiguous date blocks
    blocks = [dates[i:i + step] for i in range(0, len(dates), step)]
    import multiprocessing as mp
    with mp.Pool(workers) as pool:
        res = pool.map(_cache_block, blocks)
    meta = sorted((m for sub in res for m in sub), key=lambda m: m["date"])
    (CACHE_DIR / "_index.json").write_text(json.dumps(meta))
    print(f"[cache] {len(meta)} usable days -> {CACHE_DIR}", flush=True)


# --------------------------------------------------------------------------
# stage 2: per-day snapshot -> candidate-level feature/target rows
# --------------------------------------------------------------------------
def build_day_rows(date_str: str, day_idx: int, prev: dict | None) -> np.ndarray | None:
    z = np.load(CACHE_DIR / f"{date_str}.npz")
    ts, px, vol = z["ts"], z["px"], z["vol"]
    n = len(ts)
    if n < 2000:
        return None
    cummax = np.maximum.accumulate(px)
    cummin = np.minimum.accumulate(px)
    sp_mx, sp_mn = build_sparse(px)
    day_open = int(px[0])
    pdh = prev["high"] if prev else np.nan
    pdl = prev["low"] if prev else np.nan
    pdc = prev["close"] if prev else np.nan

    snaps = list(range(SNAP_T0, SNAP_T1 + 1, SNAP_STEP))
    out = np.full((len(snaps) * 2 * len(DISTS), len(COLS)), np.nan, dtype=np.float32)
    w = 0
    for si, t in enumerate(snaps):
        i_now = np.searchsorted(ts, t, side="right")        # ticks strictly <= t
        if i_now < 30:
            w += 2 * len(DISTS)
            continue
        i_lo = np.searchsorted(ts, t - LOOKBACK, side="right")
        wpx = px[i_lo:i_now]
        wvol = vol[i_lo:i_now]
        wts = ts[i_lo:i_now]
        if len(wpx) < 20:
            w += 2 * len(DISTS)
            continue
        mid = int(px[i_now - 1])
        pmin, pmax = int(wpx.min()), int(wpx.max())
        span = pmax - pmin + 1
        idx = wpx - pmin
        volprof = np.bincount(idx, weights=wvol.astype(np.float64), minlength=span)
        ntkprof = np.bincount(idx, minlength=span).astype(np.float64)
        # last-visit time per level (minutes ago), 10.0 == never visited
        lastseen = np.full(span, -1e9, dtype=np.float64)
        np.maximum.at(lastseen, idx, wts.astype(np.float64))
        levels_traded = int((volprof > 0).sum())
        volwin = float(volprof.sum())
        ntkwin = float(len(wpx))
        mean_v = volwin / max(levels_traded, 1)
        mean_c = ntkwin / max(levels_traded, 1)
        near_share = float(volprof[max(0, mid - pmin - 3):mid - pmin + 4].sum() / max(volwin, 1.0))
        argmax_dist = float(abs(int(np.argmax(volprof)) + pmin - mid))
        hv = np.convolve(volprof, np.ones(3), mode="same") / 3.0
        strong = np.nonzero(hv >= 3.0 * mean_v)[0]
        hvn_dist = float(np.min(np.abs(strong + pmin - mid))) if len(strong) else np.nan
        pathwin = float(np.abs(np.diff(wpx.astype(np.float64))).sum())
        rangewin = float(pmax - pmin)
        dh = float(cummax[i_now - 1])
        dl = float(cummin[i_now - 1])
        tod = t / 60.0

        # forward slices for touch + post-fill mark
        f0 = i_now
        f1 = np.searchsorted(ts, t + TOUCH_H, side="right")
        if f1 - f0 < 5:
            w += 2 * len(DISTS)
            continue
        # running extremes must restart at the slice, not carry the whole day
        fmin_run = np.minimum.accumulate(px[f0:f1])
        fmax_run = np.maximum.accumulate(px[f0:f1])
        neg_fmin_run = -fmin_run
        fts = ts[f0:f1]

        for sgn in (1, -1):          # +1 = buy below mid, -1 = sell above mid
            P = mid - sgn * DISTS    # buy side: mid-d ; sell side: mid+d
            # --- profile features at P (band 1 and band 3) -------------------
            k = P - pmin
            ok = (k >= 0) & (k < span)
            kc = np.clip(k, 0, span - 1)
            v1 = np.where(ok, volprof[kc], 0.0)
            c1n = np.where(ok, ntkprof[kc], 0.0)
            v3 = v1.copy()
            c3n = c1n.copy()
            for off in (-1, 1):
                kk = k + off
                ok2 = (kk >= 0) & (kk < span)
                kk2 = np.clip(kk, 0, span - 1)
                v3 += np.where(ok2, volprof[kk2], 0.0)
                c3n += np.where(ok2, ntkprof[kk2], 0.0)
            ls = np.where(ok, lastseen[kc], -1e9)
            for off in (-1, 1):
                kk = k + off
                ok2 = (kk >= 0) & (kk < span)
                kk2 = np.clip(kk, 0, span - 1)
                ls = np.maximum(ls, np.where(ok2, lastseen[kk2], -1e9))
            rec = np.where(ls > -1e8, (t - ls) / 60.0, float(LOOKBACK) / 60.0)
            vis = (c3n > 0).astype(np.float32)

            # --- touch + pnl -------------------------------------------------
            if sgn == 1:
                pos = np.searchsorted(neg_fmin_run, -P.astype(np.int64), side="left")
            else:
                pos = np.searchsorted(fmax_run, P.astype(np.int64), side="left")
            touched = pos < len(fts)
            posc = np.clip(pos, 0, len(fts) - 1)
            tau = np.where(touched, fts[posc], np.nan)
            mark_t = tau + POST_K
            mi = np.searchsorted(ts, mark_t, side="right") - 1
            mi = np.clip(mi, 0, n - 1)
            mark_px = px[mi].astype(np.float64)
            pnl = np.where(touched, sgn * (mark_px - P), np.nan)
            i_tau = np.clip(f0 + posc, 0, n - 1)
            i_end = np.clip(mi + 1, i_tau + 1, n)
            rmax = _rmq(sp_mx, i_tau, i_end, True).astype(np.float64)
            rmin = _rmq(sp_mn, i_tau, i_end, False).astype(np.float64)
            if sgn == 1:
                mfe = rmax - P
                mae = P - rmin
            else:
                mfe = P - rmin
                mae = rmax - P
            mfe = np.where(touched, mfe, np.nan)
            mae = np.where(touched, mae, np.nan)

            m = len(DISTS)
            blk = out[w:w + m]
            blk[:, CIX["day_idx"]] = day_idx
            blk[:, CIX["snap_idx"]] = si
            blk[:, CIX["tod"]] = tod
            blk[:, CIX["side"]] = sgn
            blk[:, CIX["d"]] = DISTS
            blk[:, CIX["P"]] = P
            blk[:, CIX["mid"]] = mid
            blk[:, CIX["vol1"]] = v1
            blk[:, CIX["vol3"]] = v3
            blk[:, CIX["ntk1"]] = c1n
            blk[:, CIX["ntk3"]] = c3n
            blk[:, CIX["z1"]] = v1 / mean_v
            blk[:, CIX["z3"]] = (v3 / 3.0) / mean_v
            blk[:, CIX["c1"]] = c1n / mean_c
            blk[:, CIX["c3"]] = (c3n / 3.0) / mean_c
            blk[:, CIX["rec"]] = rec
            blk[:, CIX["vis"]] = vis
            blk[:, CIX["avgsz"]] = v1 / np.maximum(c1n, 1.0)
            blk[:, CIX["volwin"]] = volwin
            blk[:, CIX["ntkwin"]] = ntkwin
            blk[:, CIX["pathwin"]] = pathwin
            blk[:, CIX["rangewin"]] = rangewin
            blk[:, CIX["r10"]] = (P % 10 == 0)
            blk[:, CIX["r50"]] = (P % 50 == 0)
            blk[:, CIX["r100"]] = (P % 100 == 0)
            blk[:, CIX["a_pdh"]] = (np.abs(P - pdh) <= 2) if prev else 0.0
            blk[:, CIX["a_pdl"]] = (np.abs(P - pdl) <= 2) if prev else 0.0
            blk[:, CIX["a_pdc"]] = (np.abs(P - pdc) <= 2) if prev else 0.0
            blk[:, CIX["a_open"]] = (np.abs(P - day_open) <= 2)
            blk[:, CIX["a_dh"]] = (np.abs(P - dh) <= 2)
            blk[:, CIX["a_dl"]] = (np.abs(P - dl) <= 2)
            blk[:, CIX["touched"]] = touched.astype(np.float32)
            blk[:, CIX["pnl"]] = pnl
            blk[:, CIX["tt"]] = np.where(touched, tau - t, np.nan)
            blk[:, CIX["mfe"]] = mfe
            blk[:, CIX["mae"]] = mae
            blk[:, CIX["near_share"]] = near_share
            blk[:, CIX["argmax_dist"]] = argmax_dist
            blk[:, CIX["hvn_dist"]] = hvn_dist
            w += m
    return out[:w]


def build_sparse(a: np.ndarray):
    """Sparse tables for O(1) range max/min over half-open [lo, hi)."""
    n = len(a)
    K = max(1, int(np.log2(n)) + 1)
    mx = np.empty((K, n), dtype=np.int32)
    mn = np.empty((K, n), dtype=np.int32)
    mx[0] = a
    mn[0] = a
    for j in range(1, K):
        w = 1 << j
        h = 1 << (j - 1)
        m = n - w + 1
        if m <= 0:
            mx[j] = mx[j - 1]
            mn[j] = mn[j - 1]
            continue
        mx[j, :m] = np.maximum(mx[j - 1, :m], mx[j - 1, h:h + m])
        mn[j, :m] = np.minimum(mn[j - 1, :m], mn[j - 1, h:h + m])
        mx[j, m:] = mx[j - 1, m:]
        mn[j, m:] = mn[j - 1, m:]
    return mx, mn


def _rmq(tbl, lo, hi, is_max):
    L = np.maximum(hi - lo, 1)
    j = np.floor(np.log2(L)).astype(np.int64)
    j = np.clip(j, 0, tbl.shape[0] - 1)
    a = tbl[j, lo]
    b = tbl[j, np.maximum(hi - (1 << j), lo)]
    return np.maximum(a, b) if is_max else np.minimum(a, b)


def _rows_block(args):
    items, outdir = args
    got = 0
    for date_str, day_idx, prev in items:
        r = build_day_rows(date_str, day_idx, prev)
        if r is None or len(r) == 0:
            continue
        np.save(Path(outdir) / f"rows_{day_idx:05d}.npy", r)
        got += 1
    return got


def run_rows(workers: int = 8) -> None:
    meta = json.loads((CACHE_DIR / "_index.json").read_text())
    rowdir = CACHE_DIR / "rows"
    rowdir.mkdir(exist_ok=True)
    for f in rowdir.glob("*.npy"):
        f.unlink()
    items = []
    for i, m in enumerate(meta):
        prev = meta[i - 1] if i > 0 else None
        items.append((m["date"], i, prev))
    step = (len(items) + workers - 1) // workers      # contiguous date blocks
    blocks = [(items[i:i + step], str(rowdir)) for i in range(0, len(items), step)]
    import multiprocessing as mp
    with mp.Pool(workers) as pool:
        res = pool.map(_rows_block, blocks)
    print(f"[rows] {sum(res)} days -> {rowdir}", flush=True)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("cache", "all"):
        run_cache()
    if cmd in ("rows", "all"):
        run_rows()
    if cmd in ("analyze", "all"):
        R, X, C, day_idx, years, touched, node, F = run_analyze()
        R = run_matching(R, X, C, day_idx, years, touched, node, F)
        R = run_matched_ladder(R, X, C, day_idx, years)
        R = run_policy(R, X, C, day_idx)
        R = summarise(R)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        assert OUT_JSON.resolve() != (CACHE_DIR / "_index.json").resolve()
        OUT_JSON.write_text(json.dumps(R, indent=2, default=float))
        print(json.dumps(R["headline"], indent=2, default=float), flush=True)
        print(f"[out] {OUT_JSON}", flush=True)


# ==========================================================================
# stage 3: mechanical decomposition
# ==========================================================================
def _load_all():
    rd = CACHE_DIR / "rows"
    fs = sorted(rd.glob("rows_*.npy"))
    X = np.concatenate([np.load(f) for f in fs])
    meta = json.loads((CACHE_DIR / "_index.json").read_text())
    return X, meta


def ols_cluster(X, y, gstart, chunk=250_000):
    """OLS with day-clustered covariance. `gstart` = row index where each day
    block starts (rows are stored in day order, so groups are contiguous)."""
    n, p = X.shape
    XtX = np.zeros((p, p))
    Xty = np.zeros(p)
    for s in range(0, n, chunk):
        Xc = X[s:s + chunk].astype(np.float64)
        XtX += Xc.T @ Xc
        Xty += Xc.T @ y[s:s + chunk]
    XtXi = np.linalg.pinv(XtX)
    beta = XtXi @ Xty
    ssr = 0.0
    meat = np.zeros((p, p))
    for a, b in zip(gstart[:-1], gstart[1:]):
        if b <= a:
            continue
        Xc = X[a:b].astype(np.float64)
        e = y[a:b] - Xc @ beta
        ssr += float(e @ e)
        sg = Xc.T @ e
        meat += np.outer(sg, sg)
    ybar = float(y.mean())
    sst = float(((y - ybar) ** 2).sum())
    V = XtXi @ meat @ XtXi
    se = np.sqrt(np.clip(np.diag(V), 0, None))
    return {"beta": beta, "se": se, "r2": 1.0 - ssr / sst, "n": int(n), "ssr": ssr, "sst": sst}


def _blocks_for(mask, day_idx):
    """Return row-block start offsets (into the masked array) per day."""
    d = day_idx[mask]
    starts = [0] + list(np.nonzero(np.diff(d) != 0)[0] + 1) + [len(d)]
    return np.array(sorted(set(starts)))


def _z(a):
    s = a.std()
    return (a - a.mean()) / s if s > 0 else a * 0.0


def build_features(X, C):
    """Feature blocks C1..C4 + node, all standardised."""
    d = X[:, C["d"]].astype(np.float64)
    tod = X[:, C["tod"]].astype(np.float64)
    side = X[:, C["side"]].astype(np.float64)
    rng = X[:, C["rangewin"]].astype(np.float64)
    vol = X[:, C["volwin"]].astype(np.float64)
    ntk = X[:, C["ntkwin"]].astype(np.float64)
    path = X[:, C["pathwin"]].astype(np.float64)
    z3 = X[:, C["z3"]].astype(np.float64)
    c3 = X[:, C["c3"]].astype(np.float64)
    rec = X[:, C["rec"]].astype(np.float64)
    vis = X[:, C["vis"]].astype(np.float64)

    dn = d / np.maximum(rng, 1.0)                    # distance in window-range units
    F = {}
    F["C3_dist"] = [("d", _z(d)), ("d2", _z(d ** 2)), ("logd", _z(np.log(d))),
                    ("side", side), ("tod", _z(tod)), ("tod2", _z(tod ** 2)),
                    ("d_over_range", _z(np.clip(dn, 0, 5)))]
    F["C2_act"] = [("lvol", _z(np.log1p(vol))), ("lntk", _z(np.log1p(ntk))),
                   ("lpath", _z(np.log1p(path))), ("lrng", _z(np.log1p(rng))),
                   ("lrng_x_d", _z(np.log1p(rng) * d))]
    F["C4_anchor"] = [(k, X[:, C[k]].astype(np.float64)) for k in
                      ("r10", "r50", "r100", "a_pdh", "a_pdl", "a_pdc", "a_open", "a_dh", "a_dl")]
    F["C1_been"] = [("vis", vis), ("rec", _z(rec)), ("lc3", _z(np.log1p(c3))),
                    ("lc3_2", _z(np.log1p(c3) ** 2))]
    F["NODE"] = [("lz3", _z(np.log1p(z3)))]
    return F


def _design(F, blocks, mask):
    names, cols = ["const"], [np.ones(int(mask.sum()))]
    for b in blocks:
        for nm, v in F[b]:
            names.append(nm)
            cols.append(v[mask])
    return np.column_stack(cols).astype(np.float32), names


def run_analyze():
    X, meta = _load_all()
    C = CIX
    day_idx = X[:, C["day_idx"]].astype(np.int64)
    dates = [m["date"] for m in meta]
    years = np.array([int(dates[i][:4]) for i in day_idx])
    res = {"meta": {}}
    R = res

    # ---------------- 0. sample description -----------------------------
    touched = X[:, C["touched"]] == 1
    z3 = X[:, C["z3"]].astype(np.float64)
    c3 = X[:, C["c3"]].astype(np.float64)
    node = (z3 >= 3.0)
    R["meta"] = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "source_tick_dir": str(TICK_DIR),
        "cache_dir": str(CACHE_DIR),
        "n_days": len(meta), "date_start": dates[0], "date_end": dates[-1],
        "n_rows": int(len(X)), "n_snapshots": int(len(X) // (2 * len(DISTS))),
        "rows_per_snapshot": int(2 * len(DISTS)),
        "geometry": {"lookback_s": LOOKBACK, "snap_step_s": SNAP_STEP,
                     "touch_horizon_s": TOUCH_H, "hold_after_fill_s": POST_K,
                     "dists": DISTS.tolist(),
                     "session": "08:45-13:45 day session only"},
        "contract_rule": "ex-ante third-Wednesday calendar roll (no volume argmax)",
        "cost_line_tmf_pts": COST_LINE_TMF, "live_gross_per_fill_pts": GROSS_PER_FILL,
    }

    # ---------------- 1. node geometry (control #3 groundwork) ----------
    snap_first = np.arange(0, len(X), 2 * len(DISTS))
    R["node_geometry"] = {
        "near_share_mid_pm3_mean": float(np.nanmean(X[snap_first, C["near_share"]])),
        "near_share_mid_pm3_p50": float(np.nanmedian(X[snap_first, C["near_share"]])),
        "argmax_level_dist_from_mid_p10_50_90": [float(v) for v in np.nanpercentile(X[snap_first, C["argmax_dist"]], [10, 50, 90])],
        "argmax_within_3pts_of_mid_frac": float(np.nanmean(X[snap_first, C["argmax_dist"]] <= 3)),
        "nearest_band3_3x_node_dist_p10_50_90": [float(v) for v in np.nanpercentile(X[snap_first, C["hvn_dist"]], [10, 50, 90])],
        "snapshots_with_any_band3_3x_node_frac": float(np.mean(~np.isnan(X[snap_first, C["hvn_dist"]]))),
        "node_rate_in_hang_band_z3ge3": float(node.mean()),
        "node_rate_by_d": {int(dd): float(node[X[:, C["d"]] == dd].mean()) for dd in (12, 18, 24, 33, 48, 66)},
    }

    # ---------------- 2. collinearity of node with the boring stuff -----
    def corr(a, b):
        return float(np.corrcoef(a, b)[0, 1])
    lz3, lc3 = np.log1p(z3), np.log1p(c3)
    rec = X[:, C["rec"]].astype(np.float64)
    R["collinearity"] = {
        "corr_lz3_lc3_tradecount_node": corr(lz3, lc3),
        "corr_lz3_recency_min": corr(lz3, rec),
        "corr_lz3_visited": corr(lz3, X[:, C["vis"]].astype(np.float64)),
        "corr_lz3_d": corr(lz3, X[:, C["d"]].astype(np.float64)),
        "corr_lz3_log_windowvol": corr(lz3, np.log1p(X[:, C["volwin"]].astype(np.float64))),
        "corr_lz3_log_range": corr(lz3, np.log1p(X[:, C["rangewin"]].astype(np.float64))),
        "corr_lz3_r10_roundnumber": corr(lz3, X[:, C["r10"]].astype(np.float64)),
        "r2_of_lz3_on_all_controls": None,   # filled below
    }

    # ---------------- 3. unconditional / naive headline -----------------
    def arm(m):
        mt = m & touched
        return {"n_rows": int(m.sum()), "n_fills": int(mt.sum()),
                "touch_rate": float(X[m, C["touched"]].mean()),
                "pnl_per_fill": float(np.nanmean(X[mt, C["pnl"]])),
                "mfe_per_fill": float(np.nanmean(X[mt, C["mfe"]])),
                "mae_per_fill": float(np.nanmean(X[mt, C["mae"]]))}
    R["naive"] = {"node_z3ge3": arm(node), "non_node": arm(~node),
                  "diff_node_minus_non": {}}
    for k in ("touch_rate", "pnl_per_fill", "mfe_per_fill", "mae_per_fill"):
        R["naive"]["diff_node_minus_non"][k] = R["naive"]["node_z3ge3"][k] - R["naive"]["non_node"][k]
    R["unconditional_by_d"] = {int(dd): arm(X[:, C["d"]] == dd) for dd in DISTS.tolist()}

    # ---------------- 4. nested regression ladder -----------------------
    F = build_features(X, C)
    ladder = [("M0_const", []),
              ("M1_+dist", ["C3_dist"]),
              ("M2_+activity", ["C3_dist", "C2_act"]),
              ("M3_+anchors", ["C3_dist", "C2_act", "C4_anchor"]),
              ("M4_+beenthere", ["C3_dist", "C2_act", "C4_anchor", "C1_been"]),
              ("M5_+NODE", ["C3_dist", "C2_act", "C4_anchor", "C1_been", "NODE"])]
    # node-first ordering, to show how much of NODE's raw power is really C1
    ladder_alt = [("A3_+anchors", ["C3_dist", "C2_act", "C4_anchor"]),
                  ("A4_+NODE", ["C3_dist", "C2_act", "C4_anchor", "NODE"]),
                  ("A5_+beenthere", ["C3_dist", "C2_act", "C4_anchor", "C1_been", "NODE"])]

    targets = {
        "T1_touched": (np.ones(len(X), bool), X[:, C["touched"]].astype(np.float64)),
        "T2_pnl_per_fill": (touched, np.nan_to_num(X[:, C["pnl"]].astype(np.float64))),
        "T3_mfe_per_fill": (touched, np.nan_to_num(X[:, C["mfe"]].astype(np.float64))),
    }
    R["regression_ladder"] = {}
    for tname, (mask, yfull) in targets.items():
        y = yfull[mask]
        gs = _blocks_for(mask, day_idx)
        out = {}
        prev_r2 = 0.0
        for mname, blocks in ladder:
            if not blocks:
                out[mname] = {"r2": 0.0, "incr_r2": 0.0}
                prev_r2 = 0.0
                continue
            Xd, names = _design(F, blocks, mask)
            r = ols_cluster(Xd, y, gs)
            i = names.index("lz3") if "lz3" in names else None
            out[mname] = {"r2": r["r2"], "incr_r2": r["r2"] - prev_r2, "n": r["n"]}
            if i is not None:
                out[mname].update({"beta_lz3": float(r["beta"][i]),
                                   "se_lz3_dayclustered": float(r["se"][i]),
                                   "t_lz3": float(r["beta"][i] / r["se"][i])})
            prev_r2 = r["r2"]
            del Xd
        # alt ordering
        alt = {}
        pr = None
        for mname, blocks in ladder_alt:
            Xd, names = _design(F, blocks, mask)
            r = ols_cluster(Xd, y, gs)
            e = {"r2": r["r2"]}
            if pr is not None:
                e["incr_r2"] = r["r2"] - pr
            if "lz3" in names:
                i = names.index("lz3")
                e.update({"beta_lz3": float(r["beta"][i]),
                          "se_lz3_dayclustered": float(r["se"][i]),
                          "t_lz3": float(r["beta"][i] / r["se"][i])})
            pr = r["r2"]
            alt[mname] = e
            del Xd
        out["_alt_node_before_beenthere"] = alt
        R["regression_ladder"][tname] = out

    # how predictable is the node itself from the boring stuff?
    mask_all = np.ones(len(X), bool)
    Xd, names = _design(F, ["C3_dist", "C2_act", "C4_anchor", "C1_been"], mask_all)
    r = ols_cluster(Xd, _z(np.log1p(z3)), _blocks_for(mask_all, day_idx))
    R["collinearity"]["r2_of_lz3_on_all_controls"] = r["r2"]
    del Xd

    np.save(CACHE_DIR / "_tmp_none.npy", np.zeros(1))
    (CACHE_DIR / "_tmp_none.npy").unlink()
    return R, X, C, day_idx, years, touched, node, F


def _resid(Xd, y, chunk=250_000):
    n, p = Xd.shape
    XtX = np.zeros((p, p)); Xty = np.zeros(p)
    for s in range(0, n, chunk):
        Xc = Xd[s:s + chunk].astype(np.float64)
        XtX += Xc.T @ Xc; Xty += Xc.T @ y[s:s + chunk]
    beta = np.linalg.pinv(XtX) @ Xty
    e = np.empty(n)
    for s in range(0, n, chunk):
        e[s:s + chunk] = y[s:s + chunk] - Xd[s:s + chunk].astype(np.float64) @ beta
    return e


def _qbin(v, q):
    edges = np.nanquantile(v, np.linspace(0, 1, q + 1)[1:-1])
    return np.searchsorted(edges, v, side="right")


def fe_effect(y, treat, binid, day, min_bin=20):
    """Within-bin (fixed-effect) effect of `treat` on `y`, day-clustered.
    Equivalent to OLS of y on treat with bin dummies (FWL demeaning)."""
    nb = int(binid.max()) + 1
    cnt = np.bincount(binid, minlength=nb).astype(np.float64)
    sy = np.bincount(binid, weights=y, minlength=nb)
    st = np.bincount(binid, weights=treat, minlength=nb)
    keep = cnt >= min_bin
    ok = keep[binid]
    my = (sy / np.maximum(cnt, 1))[binid]
    mt = (st / np.maximum(cnt, 1))[binid]
    yt = np.where(ok, y - my, 0.0)
    tt = np.where(ok, treat - mt, 0.0)
    num_all = yt * tt
    den_all = tt * tt
    D = float(den_all.sum())
    if D <= 0:
        return None
    beta = float(num_all.sum() / D)
    # rows are stored in day order, so day blocks are already contiguous
    ud = np.unique(day)
    bnds = np.searchsorted(day, ud, side="left").tolist()
    num_g = np.add.reduceat(num_all, bnds)
    den_g = np.add.reduceat(den_all, bnds)
    g = num_g - beta * den_g
    se = float(np.sqrt((g ** 2).sum()) / D)
    day_eff = np.where(den_g > 0, num_g / np.maximum(den_g, 1e-12), np.nan)
    return {"beta": beta, "se_dayclustered": se, "t": beta / se if se > 0 else np.nan,
            "n_used": int(ok.sum()), "n_bins_used": int(keep.sum()),
            "day_eff": day_eff, "days": ud, "den_g": den_g}


def run_matching(R, X, C, day_idx, years, touched, node, F):
    z3 = X[:, C["z3"]].astype(np.float64)
    c3 = X[:, C["c3"]].astype(np.float64)
    rec = X[:, C["rec"]].astype(np.float64)
    rng = X[:, C["rangewin"]].astype(np.float64)
    dcol = X[:, C["d"]].astype(np.int64)
    side = X[:, C["side"]].astype(np.int64)

    dmap = {int(v): i for i, v in enumerate(DISTS.tolist())}
    di = np.array([dmap[int(v)] for v in dcol])
    rq = _qbin(rec, 4)
    cq = _qbin(c3, 5)
    gq = _qbin(rng, 5)
    binid = ((di * 4 + rq) * 5 + cq) * 5 + gq
    binid = binid * 2 + (side > 0).astype(np.int64)

    # ---- placebo node values -------------------------------------------
    # mirror: same snapshot & |d|, opposite side of mid (wrong level, same regime)
    m = len(DISTS)
    idx = np.arange(len(X))
    blk = (idx // m) % 2
    mirror = np.where(blk == 0, idx + m, idx - m)
    z3_mirror = z3[mirror]
    # cross-day: same within-day position, a different day of identical length
    starts = np.searchsorted(day_idx, np.unique(day_idx), side="left")
    ends = list(starts[1:]) + [len(X)]
    lens = np.array(ends) - np.array(starts)
    z3_xday = z3.copy()
    for L in np.unique(lens):
        g = np.nonzero(lens == L)[0]
        if len(g) < 2:
            continue
        src = np.roll(g, 1)
        for a, b in zip(g, src):
            z3_xday[starts[a]:starts[a] + L] = z3[starts[b]:starts[b] + L]

    out = {}
    arms = {"node_real": (z3 >= 3.0).astype(np.float64),
            "placebo_mirror_level": (z3_mirror >= 3.0).astype(np.float64),
            "placebo_cross_day": (z3_xday >= 3.0).astype(np.float64),
            "tradecount_node_c3ge3": (c3 >= 3.0).astype(np.float64)}
    targets = {"T1_touched": (np.ones(len(X), bool), X[:, C["touched"]].astype(np.float64)),
               "T2_pnl_per_fill": (touched, np.nan_to_num(X[:, C["pnl"]].astype(np.float64))),
               "T3_mfe_per_fill": (touched, np.nan_to_num(X[:, C["mfe"]].astype(np.float64)))}

    ud_all = np.unique(day_idx)
    fst = np.searchsorted(day_idx, ud_all, side="left")
    day_year_map = {int(k): int(years[i0]) for k, i0 in zip(ud_all.tolist(), fst.tolist())}
    for tname, (mask, yfull) in targets.items():
        y = yfull[mask]
        b = binid[mask]
        dd = day_idx[mask]
        sub = {}
        for aname, tr in arms.items():
            # for the count-node arm the bin set must NOT already control for c3
            bb = b
            if aname == "tradecount_node_c3ge3":
                bb = (((di * 4 + rq) * 5 + gq) * 2 + (side > 0).astype(np.int64))[mask]
            r = fe_effect(y, tr[mask], bb, dd)
            if r is None:
                continue
            e = {"effect_pts" if tname != "T1_touched" else "effect_prob": r["beta"],
                 "se_dayclustered": r["se_dayclustered"], "t_dayclustered": r["t"],
                 "n_used": r["n_used"], "n_bins": r["n_bins_used"]}
            de, dg = r["day_eff"], r["den_g"]
            valid = np.isfinite(de) & (dg > 0)
            e["days_with_effect"] = int(valid.sum())
            e["frac_days_same_sign_as_pooled"] = float(np.mean(np.sign(de[valid]) == np.sign(r["beta"])))
            # per-year (weighted by within-day denominator)
            byyear = {}
            dy = np.array([day_year_map.get(int(k), -1) for k in r["days"]])
            for Y in sorted(set(dy.tolist())):
                sel = (dy == Y) & valid
                if sel.sum() < 5:
                    continue
                byyear[str(Y)] = {"n_days": int(sel.sum()),
                                  "effect": float(np.sum(de[sel] * dg[sel]) / np.sum(dg[sel]))}
            e["by_year"] = byyear
            sub[aname] = e
        out[tname] = sub

    # ---- partial IC after the full control set --------------------------
    mask_all = np.ones(len(X), bool)
    Xd, names = _design(F, ["C3_dist", "C2_act", "C4_anchor", "C1_been"], mask_all)
    ez = _resid(Xd, _z(np.log1p(z3)))
    pic = {}
    for tname, (mask, yfull) in targets.items():
        Xs, _ = _design(F, ["C3_dist", "C2_act", "C4_anchor", "C1_been"], mask)
        ey = _resid(Xs, yfull[mask])
        pic[tname] = {"partial_ic_vs_lz3": float(np.corrcoef(ey, ez[mask])[0, 1]),
                      "raw_ic_vs_lz3": float(np.corrcoef(yfull[mask], np.log1p(z3)[mask])[0, 1])}
        del Xs
    del Xd
    R["matched_within_bin"] = out
    R["partial_ic"] = pic
    R["matching_spec"] = {
        "bins": "distance(33) x recency_quartile(4) x tradecount_node_quintile(5) x window_range_quintile(5) x side(2)",
        "n_bins_nominal": int(33 * 4 * 5 * 5 * 2),
        "min_rows_per_bin": 20,
        "treatment": "band-3 volume node z3 >= 3x window mean volume per level",
        "estimator": "within-bin OLS (FWL demeaning), day-clustered SE",
    }
    return R


def run_matched_ladder(R, X, C, day_idx, years):
    """v2: dense, distance-matched treatment + progressive control ladder.

    Treatment is the level's volume-node RANK within the live 12-33 hang band of
    the same snapshot-side (0 = weakest of 22, 1 = strongest). This is dense by
    construction and holds the quote count fixed, so it is exactly the decision
    the live channel actually gets to make.
    """
    m = len(DISTS)
    nlive = DIST_LIST.index(33) + 1
    nblk = len(X) // m

    def band_rank(vals):
        V = vals.reshape(nblk, m)[:, :nlive]
        good = np.isfinite(V)
        Vf = np.where(good, V, -np.inf)
        o = np.argsort(Vf, axis=1, kind="stable")
        rk = np.empty_like(o)
        np.put_along_axis(rk, o, np.arange(nlive)[None, :].repeat(nblk, 0), axis=1)
        r = rk.astype(np.float64) / (nlive - 1)
        return np.where(good, r, np.nan), (rk == nlive - 1) & good

    z3 = X[:, C["z3"]].astype(np.float64)
    idx = np.arange(len(X))
    blk = (idx // m) % 2
    mirror = np.where(blk == 0, idx + m, idx - m)
    starts = np.searchsorted(day_idx, np.unique(day_idx), side="left")
    ends = list(starts[1:]) + [len(X)]
    lens = np.array(ends) - np.array(starts)
    z3_xday = z3.copy()
    for L in np.unique(lens):
        g = np.nonzero(lens == L)[0]
        if len(g) < 2:
            continue
        for a, b in zip(g, np.roll(g, 1)):
            z3_xday[starts[a]:starts[a] + L] = z3[starts[b]:starts[b] + L]

    rank_real, arg_real = band_rank(z3)
    rank_mirr, arg_mirr = band_rank(z3[mirror])
    rank_xday, arg_xday = band_rank(z3_xday)
    rank_cnt, arg_cnt = band_rank(X[:, C["c3"]].astype(np.float64))

    # flatten back onto live-band rows only
    live = np.zeros(len(X), bool)
    live.reshape(nblk, m)[:, :nlive] = True
    def flat(a):
        out = np.full(len(X), np.nan)
        out.reshape(nblk, m)[:, :nlive] = a
        return out
    T = {"volume_node_rank": flat(rank_real),
         "volume_node_is_band_argmax": flat(arg_real.astype(float)),
         "tradecount_node_rank": flat(rank_cnt),
         "placebo_mirror_rank": flat(rank_mirr),
         "placebo_crossday_rank": flat(rank_xday)}

    d = X[:, C["d"]].astype(np.int64)
    side = (X[:, C["side"]] > 0).astype(np.int64)
    dmap = {int(v): i for i, v in enumerate(DIST_LIST)}
    di = np.array([dmap[int(v)] for v in d])
    tq = _qbin(X[:, C["tod"]].astype(np.float64), 4)
    rq = _qbin(X[:, C["rec"]].astype(np.float64), 4)
    cq = _qbin(X[:, C["c3"]].astype(np.float64), 5)
    gq = _qbin(X[:, C["rangewin"]].astype(np.float64), 5)
    vq = _qbin(X[:, C["volwin"]].astype(np.float64), 5)
    rn = X[:, C["r10"]].astype(np.int64) + 2 * X[:, C["r50"]].astype(np.int64)
    an = (X[:, C["a_pdh"]] + X[:, C["a_pdl"]] + X[:, C["a_pdc"]]
          + X[:, C["a_open"]] + X[:, C["a_dh"]] + X[:, C["a_dl"]] > 0).astype(np.int64)

    BINS = [
        ("B0_distance_side", (di * 2 + side)),
        ("B1_+timeofday", ((di * 2 + side) * 4 + tq)),
        ("B2_+activity_vol", (((di * 2 + side) * 4 + tq) * 5 + gq) * 5 + vq),
        ("B3_+roundnum_anchor", ((((di * 2 + side) * 4 + tq) * 5 + gq) * 5 + vq) * 4 + rn * 2 + an),
        ("B4_+beenthere_recency", (((((di * 2 + side) * 4 + tq) * 5 + gq) * 5 + vq) * 4 + rn * 2 + an) * 4 + rq),
        ("B5_+tradecount_node", ((((((di * 2 + side) * 4 + tq) * 5 + gq) * 5 + vq) * 4 + rn * 2 + an) * 4 + rq) * 5 + cq),
    ]

    touched = X[:, C["touched"]] == 1
    pnl = X[:, C["pnl"]].astype(np.float64)
    pq = np.where(touched, np.nan_to_num(pnl), 0.0)
    TG = {
        "T1_touched": (live, X[:, C["touched"]].astype(np.float64)),
        "T2_pnl_per_fill": (live & touched, np.nan_to_num(pnl)),
        "T2w_pnl_per_fill_win40": (live & touched, np.clip(np.nan_to_num(pnl), -40, 40)),
        "T3_mfe_per_fill": (live & touched, np.nan_to_num(X[:, C["mfe"]].astype(np.float64))),
        "T4_absmove_per_fill": (live & touched, np.abs(np.nan_to_num(pnl))),
        "T6_mae_per_fill": (live & touched, np.nan_to_num(X[:, C["mae"]].astype(np.float64))),
        "T5_pnl_per_quote": (live, pq),
        "T5w_pnl_per_quote_win40": (live, np.clip(pq, -40, 40)),
    }
    day_year = {}
    ud_all = np.unique(day_idx)
    fst = np.searchsorted(day_idx, ud_all, side="left")
    for k, i0 in zip(ud_all.tolist(), fst.tolist()):
        day_year[int(k)] = int(years[i0])

    res = {}
    for tname, (mask, y) in TG.items():
        ys = y[mask]
        dd = day_idx[mask]
        binf = [(bn, np.unique(b[mask], return_inverse=True)[1]) for bn, b in BINS]
        sub = {}
        for aname, tv in T.items():
            tvals = tv[mask]
            arm = {}
            for bname, bb in binf:
                r = fe_effect(ys, tvals, bb, dd)
                if r is None:
                    continue
                de, dg = r["day_eff"], r["den_g"]
                v = np.isfinite(de) & (dg > 0)
                dy = np.array([day_year.get(int(k), -1) for k in r["days"]])
                by = {}
                for Y in sorted(set(dy.tolist())):
                    sel = (dy == Y) & v
                    if sel.sum() >= 5:
                        by[str(Y)] = {"n_days": int(sel.sum()),
                                      "effect": float(np.sum(de[sel] * dg[sel]) / np.sum(dg[sel]))}
                arm[bname] = {"effect": r["beta"], "se_dayclustered": r["se_dayclustered"],
                              "t_dayclustered": r["t"], "n_bins": r["n_bins_used"],
                              "n_used": r["n_used"],
                              "frac_days_same_sign": float(np.mean(np.sign(de[v]) == np.sign(r["beta"]))),
                              "by_year": by}
            sub[aname] = arm
        res[tname] = sub
    R["matched_ladder_v2"] = res
    R["matched_ladder_v2_spec"] = {
        "treatment": "rank of the level's band-3 volume-node strength among the 22 live-band "
                     "levels (12-33 pts) of the same snapshot-side; 0=weakest, 1=strongest. "
                     "Effect units = target change from weakest to strongest level.",
        "bins": [b[0] for b in BINS],
        "why_dense": "binary z3>=3 fires on only %.3f%% of hang-band rows, far too rare for a "
                     "matched test; the within-band rank is dense and distance-matched by "
                     "construction" % (100.0 * float((z3 >= 3).mean())),
        "estimator": "within-bin OLS (FWL), day-clustered SE, cluster unit = trading day",
    }
    # been-there vs distance table (control #1 made explicit)
    bt = {}
    for dd_ in (12, 18, 24, 33, 48, 66):
        mm = d == dd_
        bt[int(dd_)] = {"mean_recency_min": float(np.nanmean(X[mm, C["rec"]])),
                        "frac_visited_in_lookback": float(np.nanmean(X[mm, C["vis"]])),
                        "mean_z3": float(np.nanmean(X[mm, C["z3"]])),
                        "mean_c3": float(np.nanmean(X[mm, C["c3"]]))}
    R["been_there_vs_distance"] = bt
    return R


def _day_boot_ratio(num_by_day, den_by_day, n=2000, seed=7):
    """Day-block bootstrap CI for a ratio estimator sum(num)/sum(den)."""
    rng = np.random.default_rng(seed)
    G = len(num_by_day)
    pt = float(num_by_day.sum() / max(den_by_day.sum(), 1e-12))
    draws = np.empty(n)
    for i in range(n):
        s = rng.integers(0, G, G)
        d = den_by_day[s].sum()
        draws[i] = num_by_day[s].sum() / d if d > 0 else np.nan
    lo, hi = np.nanpercentile(draws, [2.5, 97.5])
    return {"point": pt, "ci95": [float(lo), float(hi)], "boot_se": float(np.nanstd(draws))}


def run_policy(R, X, C, day_idx):
    m = len(DISTS)
    nblk = len(X) // m
    Z = X[:, C["z3"]].reshape(nblk, m)
    PN = X[:, C["pnl"]].reshape(nblk, m).astype(np.float64)
    TC = X[:, C["touched"]].reshape(nblk, m).astype(np.float64)
    DY = day_idx.reshape(nblk, m)[:, 0]
    n_live = int(np.searchsorted(DIST_LIST, 33, side="right"))   # live band 12-33
    Zl = np.nan_to_num(Z[:, :n_live], nan=-1e9)
    valid = np.isfinite(Z[:, 0])
    base_i = DIST_LIST.index(22)
    picks = {
        "baseline_fixed_d22": np.full(nblk, base_i),
        "node_max_z3": np.argmax(Zl, axis=1),
        "anti_node_min_z3": np.argmin(np.where(Zl <= -1e8, 1e9, Zl), axis=1),
        "pseudo_random_d": (np.arange(nblk) * 7919 % n_live),
    }
    ud = np.unique(DY)
    dpos = np.searchsorted(ud, DY)
    out = {}
    for pname, pi in picks.items():
        r = np.arange(nblk)
        tc = np.where(valid, TC[r, pi], np.nan)
        pn = np.where(valid, PN[r, pi], np.nan)
        dd = np.array(DIST_LIST)[pi].astype(float)
        fill = np.nan_to_num(tc)
        pnl0 = np.nan_to_num(pn)
        num = np.bincount(dpos, weights=pnl0, minlength=len(ud))
        den = np.bincount(dpos, weights=fill, minlength=len(ud))
        nblk_d = np.bincount(dpos, weights=np.isfinite(tc).astype(float), minlength=len(ud))
        b = _day_boot_ratio(num, den)
        out[pname] = {
            "mean_chosen_distance_pts": float(np.nanmean(np.where(valid, dd, np.nan))),
            "touch_rate": float(den.sum() / max(nblk_d.sum(), 1)),
            "pnl_per_fill_pts": b["point"], "pnl_per_fill_ci95": b["ci95"],
            "pnl_per_quote_pts": float(num.sum() / max(nblk_d.sum(), 1)),
            "n_quotes": int(nblk_d.sum()), "n_fills": int(den.sum()),
        }
    # paired day-level difference node - baseline (same days, same snapshots)
    r = np.arange(nblk)
    for pname in ("node_max_z3", "anti_node_min_z3", "pseudo_random_d"):
        pi = picks[pname]
        dnum = np.bincount(dpos, weights=np.nan_to_num(np.where(valid, PN[r, pi], np.nan)) -
                           np.nan_to_num(np.where(valid, PN[r, picks["baseline_fixed_d22"]], np.nan)),
                           minlength=len(ud))
        dden = np.bincount(dpos, weights=np.isfinite(np.where(valid, TC[r, pi], np.nan)).astype(float),
                           minlength=len(ud))
        b = _day_boot_ratio(dnum, dden)
        out[pname]["minus_baseline_pts_per_quote"] = b["point"]
        out[pname]["minus_baseline_ci95"] = b["ci95"]
        out[pname]["minus_baseline_frac_days_positive"] = float(np.mean(dnum > 0))
    R["policy_sim"] = out
    R["policy_sim_note"] = ("re-prices WITHIN the live 12-33 band only; quote count is held "
                            "fixed at one per snapshot-side, so this is an execution/cost-family "
                            "experiment, not a filter")
    return R


DIST_LIST = DISTS.tolist()


def summarise(R):
    """All summary text is recomputed from R; nothing is hard-coded."""
    lad = R["regression_ladder"]
    s = {}
    for t, d in lad.items():
        s[t] = {
            "r2_controls_only_M4": round(d["M4_+beenthere"]["r2"], 6),
            "r2_with_node_M5": round(d["M5_+NODE"]["r2"], 6),
            "incremental_r2_of_node": round(d["M5_+NODE"]["incr_r2"], 8),
            "node_t_dayclustered": round(d["M5_+NODE"].get("t_lz3", float("nan")), 3),
            "node_raw_incr_r2_before_beenthere": round(
                d["_alt_node_before_beenthere"]["A4_+NODE"].get("incr_r2", float("nan")), 8),
            "share_of_raw_node_power_absorbed_by_beenthere": None,
        }
        raw = d["_alt_node_before_beenthere"]["A4_+NODE"].get("incr_r2")
        fin = d["M5_+NODE"]["incr_r2"]
        if raw and raw > 0:
            s[t]["share_of_raw_node_power_absorbed_by_beenthere"] = round(1.0 - fin / raw, 4)
    R["summary_by_target"] = s

    L = R["matched_ladder_v2"]
    hl = {}
    for tname in ("T5w_pnl_per_quote_win40", "T2w_pnl_per_fill_win40", "T1_touched",
                  "T3_mfe_per_fill", "T4_absmove_per_fill", "T6_mae_per_fill"):
        a = L[tname]["volume_node_rank"]
        raw = a["B0_distance_side"]["effect"]
        fin = a["B5_+tradecount_node"]["effect"]
        hl[tname] = {
            "effect_raw_distance_matched_B0": round(raw, 5),
            "effect_full_controls_B5": round(fin, 5),
            "shrink_vs_B0_pct": round(100.0 * (1.0 - abs(fin) / abs(raw)), 2) if raw else None,
            "t_dayclustered_B5": round(a["B5_+tradecount_node"]["t_dayclustered"], 3),
            "frac_days_same_sign_B5": round(a["B5_+tradecount_node"]["frac_days_same_sign"], 4),
            "placebo_mirror_B5": round(L[tname]["placebo_mirror_rank"]["B5_+tradecount_node"]["effect"], 5),
            "placebo_crossday_B5": round(L[tname]["placebo_crossday_rank"]["B5_+tradecount_node"]["effect"], 5),
        }
    main_t = "T5w_pnl_per_quote_win40"
    eff = hl[main_t]["effect_full_controls_B5"]
    a = L[main_t]["volume_node_rank"]["B5_+tradecount_node"]
    R["headline"] = {
        "primary_target": main_t,
        "unit": "points per quote, weakest->strongest volume node inside the 12-33 hang band",
        "effect_full_controls_pts": eff,
        "se_dayclustered": round(a["se_dayclustered"], 5),
        "t_dayclustered": round(a["t_dayclustered"], 3),
        "frac_days_same_sign": round(a["frac_days_same_sign"], 4),
        "effect_before_mechanical_controls_pts": hl[main_t]["effect_raw_distance_matched_B0"],
        "placebo_mirror_pts": hl[main_t]["placebo_mirror_B5"],
        "placebo_crossday_pts": hl[main_t]["placebo_crossday_B5"],
        "incremental_r2_of_node_touch": R["summary_by_target"]["T1_touched"]["incremental_r2_of_node"],
        "incremental_r2_of_node_pnl": R["summary_by_target"]["T2_pnl_per_fill"]["incremental_r2_of_node"],
        "r2_of_node_strength_on_controls": round(R["collinearity"]["r2_of_lz3_on_all_controls"], 5),
        "partial_ic_touch_after_controls": round(R["partial_ic"]["T1_touched"]["partial_ic_vs_lz3"], 5),
        "raw_ic_touch": round(R["partial_ic"]["T1_touched"]["raw_ic_vs_lz3"], 5),
        "cost_line_tmf_pts": COST_LINE_TMF,
        "live_gross_per_fill_pts": GROSS_PER_FILL,
        "effect_as_pct_of_cost_line": round(100.0 * abs(eff) / COST_LINE_TMF, 3),
        "verdict_rule": ("actionable requires |effect| > 0.5 pt AND |t_day| > 3 AND day-sign "
                         "consistency > 0.55 AND |effect| > 3x both placebos"),
    }
    R["headline_by_target"] = hl
    # is the survivor a DIRECTION signal or just a restated VOLATILITY signal?
    def b5(t, arm="volume_node_rank"):
        return L[t][arm]["B5_+tradecount_node"]
    sg, mg = b5("T2w_pnl_per_fill_win40"), b5("T4_absmove_per_fill")
    R["directional_vs_magnitude"] = {
        "signed_pnl_per_fill_effect_pts": round(sg["effect"], 4),
        "signed_pnl_t_dayclustered": round(sg["t_dayclustered"], 3),
        "signed_pnl_frac_days_same_sign": round(sg["frac_days_same_sign"], 4),
        "signed_pnl_years_agreeing": sum(1 for v in sg["by_year"].values()
                                         if np.sign(v["effect"]) == np.sign(sg["effect"])),
        "abs_move_effect_pts": round(mg["effect"], 4),
        "abs_move_t_dayclustered": round(mg["t_dayclustered"], 3),
        "abs_move_frac_days_same_sign": round(mg["frac_days_same_sign"], 4),
        "abs_move_years_agreeing": sum(1 for v in mg["by_year"].values()
                                       if np.sign(v["effect"]) == np.sign(mg["effect"])),
        "n_years": len(mg["by_year"]),
        "reading": ("if |move| is strongly and consistently negative while signed pnl is weak "
                    "and year-unstable, the node is a volatility/stalling proxy, not a "
                    "directional support-resistance signal"),
    }
    # volume node vs pure trade-count node: does the VOLUME dimension add anything?
    R["volume_vs_tradecount"] = {
        t: {"volume_node_rank_B5": round(b5(t)["effect"], 5),
            "tradecount_node_rank_B5": round(b5(t, "tradecount_node_rank")["effect"], 5),
            "ratio": round(b5(t)["effect"] / b5(t, "tradecount_node_rank")["effect"], 4)
            if b5(t, "tradecount_node_rank")["effect"] else None}
        for t in ("T1_touched", "T2w_pnl_per_fill_win40", "T3_mfe_per_fill",
                  "T4_absmove_per_fill", "T5w_pnl_per_quote_win40")}
    R["volume_vs_tradecount"]["corr_log_volume_node_vs_log_tradecount_node"] = round(
        R["collinearity"]["corr_lz3_lc3_tradecount_node"], 5)
    # ---- data-derived verdict (no hard-coded numbers anywhere) ----------
    dm = R["directional_vs_magnitude"]
    cd = abs(hl["T2w_pnl_per_fill_win40"]["placebo_crossday_B5"])
    mv_cd = abs(L["T4_absmove_per_fill"]["placebo_crossday_rank"]["B5_+tradecount_node"]["effect"])
    dir_ok = (abs(dm["signed_pnl_t_dayclustered"]) > 3
              and dm["signed_pnl_frac_days_same_sign"] > 0.55
              and dm["signed_pnl_years_agreeing"] == dm["n_years"]
              and abs(dm["signed_pnl_per_fill_effect_pts"]) > 3 * cd)
    mag_ok = (abs(dm["abs_move_t_dayclustered"]) > 3
              and dm["abs_move_frac_days_same_sign"] > 0.55
              and dm["abs_move_years_agreeing"] == dm["n_years"]
              and abs(dm["abs_move_effect_pts"]) > 3 * mv_cd)
    vr = R["volume_vs_tradecount"]
    ratios = [v["ratio"] for k, v in vr.items() if isinstance(v, dict) and v.get("ratio")]
    R["verdict"] = {
        "node_has_independent_DIRECTIONAL_information": bool(dir_ok),
        "node_retains_MAGNITUDE_stalling_information": bool(mag_ok),
        "volume_dimension_adds_nothing_over_trade_count": bool(max(abs(1 - r) for r in ratios) < 0.10),
        "max_deviation_of_volume_from_tradecount_effect": round(max(abs(1 - r) for r in ratios), 4),
        "node_strength_r2_explained_by_mechanical_controls": round(
            R["collinearity"]["r2_of_lz3_on_all_controls"], 5),
        "touch_ic_destroyed_pct": round(100 * (1 - abs(R["partial_ic"]["T1_touched"]["partial_ic_vs_lz3"])
                                               / abs(R["partial_ic"]["T1_touched"]["raw_ic_vs_lz3"])), 3),
        "mfe_ic_destroyed_pct": round(100 * (1 - abs(R["partial_ic"]["T3_mfe_per_fill"]["partial_ic_vs_lz3"])
                                             / abs(R["partial_ic"]["T3_mfe_per_fill"]["raw_ic_vs_lz3"])), 3),
        "hvn_reading_a_support_or_b_stuck": ("b_stuck" if b5("T3_mfe_per_fill")["effect"] < 0
                                             else "a_support"),
        "mfe_minus_mae_effect_pts": round(b5("T3_mfe_per_fill")["effect"] - b5("T6_mae_per_fill")["effect"], 4),
    }
    e = R["headline"]
    passes = (abs(eff) > 0.5 and abs(a["t_dayclustered"]) > 3
              and a["frac_days_same_sign"] > 0.55
              and abs(eff) > 3 * max(abs(hl[main_t]["placebo_mirror_B5"]),
                                     abs(hl[main_t]["placebo_crossday_B5"])))
    e["actionable"] = bool(passes)
    return R


if __name__ == "__main__":
    main()
