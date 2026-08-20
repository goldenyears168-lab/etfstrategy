#!/usr/bin/env python3
"""vp_g1a — Volume-profile high-volume-node (HVN) directional information
coefficient, with matched and random placebos.

QUESTION
--------
At time t, build a volume profile from the trades in (t-600s, t].  A
"node" is a price (or a band of `band` adjacent prices) whose volume is
>= `thr` x the window's mean volume per occupied price level.  Does the
SIGNED distance from the current price to such a node predict the
forward return at 30 / 60 / 300 / 900 seconds?

Two literature readings are possible and we do NOT assume either:
  (a) attractor / value area  -> node above  => price drifts UP   (IC > 0)
  (b) congestion / obstacle   -> node above  => price stalls/rejects (IC < 0)

CAUSALITY CONTRACT (hard boundaries, asserted in code)
------------------------------------------------------
* every feature at sample time t uses ONLY trades with ts <= t
  (`_profile_at` receives a half-open slice [lo, hi) and asserts
  ts[hi-1] <= t and ts[lo] > t-600).
* every target uses ONLY trades with ts > t, inside the same session
  block (no cross-session, no cross-midnight carry).
* front-month contract is chosen by the EX-ANTE TAIFEX calendar rule
  (3rd Wednesday settlement; night session rolls one day early).  The
  whole-day argmax rule (`_dominant_outright_contract`) is a confirmed
  look-ahead and is deliberately NOT used.
* day sharding is by CONTIGUOUS date blocks, never round-robin; every
  rolling window is intra-block so no window ever spans a shard seam.

Usage
-----
PYTHONPATH=src .venv/bin/python scripts/research/vp_g1a_node_ic.py
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TICK_DIR = DATA / "cache/tmf_channel/finmind_tx_tick_by_day"
OUT = ROOT / "reports/research/channel_lab/vp_g1a_node_ic.json"

LOOKBACK = 600            # seconds of trades in the volume profile
CADENCE = 60              # sample every 60 s
HORIZONS = (30, 60, 300, 900)
MAX_H = max(HORIZONS)
MIN_TRADES = 30           # window must have this many trades
MIN_LEVELS = 5            # ... and this many distinct occupied prices
BAND = 1                  # adjacent prices merged into one node candidate
THR = 3.0                 # node iff band volume >= THR x band mean volume
COST_LINE = 4.79          # TMF round-trip cost, points (cost line v2)
GROSS_PER_FILL = 2.86     # live gross per fill, points

IS_END = "2024-12-31"     # in-sample  : 2021-12-01 .. 2024-12-31
OOS_START = "2025-01-01"  # out-of-sample: 2025-01-01 .. 2026-08-14

# session blocks inside one calendar-day tick file.
#   name, start, end(exclusive), which calendar contract, cluster owner
BLOCKS = (
    ("early", 0, 5 * 3600, "night", "prev"),          # 00:00-05:00 = tail of prev night
    ("day", 8 * 3600 + 45 * 60, 13 * 3600 + 45 * 60, "day", "self"),
    ("night", 15 * 3600, 24 * 3600, "night", "self"),
)

TOPQ = 0.10               # scale-free node = top decile of occupied-level volume
BOTQ = 0.50               # its placebo pool = bottom half of occupied-level volume

FEATURES = (
    "poc_dist",           # signed distance to point of control (always defined)
    "near_node_dist",     # signed distance to NEAREST >=THRx node cluster  <- primary
    "strong_node_dist",   # signed distance to STRONGEST node cluster
    "signed_near_strength",  # sign(dist) * strength of the nearest node
    "node_asym",          # (node vol above - below) / window vol
    "plac_matched_dist",  # PLACEBO: distance-matched non-node level
    "plac_random_dist",   # PLACEBO: uniformly random non-node level
    "topdec_near_dist",   # scale-free node: nearest top-decile-volume level
    "plac_topdec_dist",   # PLACEBO for it: distance-matched bottom-half level
    "all_asym",           # control: (vol above - below) / window vol
    "vwap_dist",          # control: vwap - cur
    "mid_dist",           # control: range midpoint - cur
)
PRIMARY = ("near_node_dist", "r900", "day")
PRIMARY_SF = ("topdec_near_dist", "r900", "day")   # scale-free twin


# --------------------------------------------------------------------------
# ex-ante front-month calendar (TAIFEX: 3rd Wednesday is last trading day)
# --------------------------------------------------------------------------
def _w3(y: int, m: int) -> dt.date:
    d = dt.date(y, m, 1)
    while d.weekday() != 2:
        d += dt.timedelta(days=1)
    return d + dt.timedelta(days=14)


def _nxt(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def calendar_fronts(date: str) -> tuple[str, str]:
    """(day-session front, night-session front) for calendar date `date`.

    Knowable before either session opens -> no look-ahead.
    """
    d = dt.date.fromisoformat(date)
    w = _w3(d.year, d.month)
    ym_day = (d.year, d.month) if d <= w else _nxt(d.year, d.month)
    ym_night = (d.year, d.month) if d < w else _nxt(d.year, d.month)
    return "%04d%02d" % ym_day, "%04d%02d" % ym_night


# --------------------------------------------------------------------------
# volume profile
# --------------------------------------------------------------------------
def _profile_at(ts, px, vol, lo, hi, t_now):
    """Volume profile over the half-open trade slice [lo, hi).

    CAUSAL BOUNDARY -- asserted, not assumed.
    """
    assert hi > lo
    assert ts[hi - 1] <= t_now, "look-ahead: profile contains a trade after t"
    assert ts[lo] > t_now - LOOKBACK, "stale: profile reaches before t-600"
    prof: Counter = Counter()
    v_tot = 0
    px_v = 0.0
    for i in range(lo, hi):
        p = px[i]
        v = vol[i]
        prof[p] += v
        v_tot += v
        px_v += p * v
    return prof, v_tot, px_v


def _features(prof, v_tot, px_v, cur, rng):
    """All predictors for one sample.  Returns dict or None if unusable."""
    if v_tot <= 0 or len(prof) < MIN_LEVELS:
        return None
    p_lo = min(prof)
    p_hi = max(prof)
    n_grid = p_hi - p_lo + 1
    grid = [0] * n_grid
    for p, v in prof.items():
        grid[p - p_lo] = v
    k_occ = len(prof)
    mu = v_tot / k_occ

    # prefix sums for band aggregation
    cum = [0] * (n_grid + 1)
    s = 0
    for i, v in enumerate(grid):
        s += v
        cum[i + 1] = s
    h = (BAND - 1) // 2
    denom = BAND * mu

    node_runs = []      # contiguous runs of node centres, as (i0, i1) inclusive
    cur_run = None
    strengths = [0.0] * n_grid
    for i in range(n_grid):
        a = max(0, i - h)
        b = min(n_grid - 1, i + h)
        strengths[i] = (cum[b + 1] - cum[a]) / denom
        is_node = grid[i] > 0 and strengths[i] >= THR
        if is_node:
            if cur_run is None:
                cur_run = [i, i]
            else:
                cur_run[1] = i
        elif cur_run is not None:
            node_runs.append(tuple(cur_run))
            cur_run = None
    if cur_run is not None:
        node_runs.append(tuple(cur_run))

    # node clusters -> (volume-weighted price, strength, volume)
    clusters = []
    node_vol_total = 0
    for i0, i1 in node_runs:
        a = max(0, i0 - h)
        b = min(n_grid - 1, i1 + h)
        wsum = 0
        psum = 0.0
        for j in range(a, b + 1):
            wsum += grid[j]
            psum += (p_lo + j) * grid[j]
        if wsum <= 0:
            continue
        clusters.append((psum / wsum, max(strengths[i0:i1 + 1]), wsum))
        node_vol_total += wsum

    out = {}
    # --- controls (always defined) ---
    poc_p = max(prof.items(), key=lambda kv: (kv[1], -abs(kv[0] - cur)))[0]
    out["poc_dist"] = float(poc_p - cur)
    v_up = sum(v for p, v in prof.items() if p > cur)
    v_dn = sum(v for p, v in prof.items() if p < cur)
    out["all_asym"] = (v_up - v_dn) / v_tot
    out["vwap_dist"] = px_v / v_tot - cur
    out["mid_dist"] = (p_lo + p_hi) / 2.0 - cur
    out["range"] = float(p_hi - p_lo)
    out["n_nodes"] = len(clusters)
    out["node_share"] = node_vol_total / v_tot if v_tot else 0.0
    out["cur_price"] = float(cur)

    # ---- scale-free node: top-decile-volume occupied level (always defined) --
    occ = sorted(prof.items(), key=lambda kv: kv[1])          # ascending volume
    n_top = max(1, int(round(TOPQ * len(occ))))
    n_bot = max(1, int(round(BOTQ * len(occ))))
    top_lvls = [p for p, _v in occ[-n_top:] if p != cur]
    bot_lvls = [p for p, _v in occ[:n_bot] if p != cur]
    if top_lvls:
        tp = min(top_lvls, key=lambda p: (abs(p - cur), -prof[p]))
        out["topdec_near_dist"] = float(tp - cur)
        want_sf = tp - cur
        sgn_sf = 1 if want_sf > 0 else -1
        same_sf = [p for p in bot_lvls if (p - cur) * sgn_sf > 0]
        if same_sf:
            bp = min(same_sf, key=lambda p: abs(abs(p - cur) - abs(want_sf)))
            out["plac_topdec_dist"] = float(bp - cur)

    if not clusters:
        return out    # node features absent -> sample kept only for controls

    nvu = sum(w for p, _s, w in clusters if p > cur)
    nvd = sum(w for p, _s, w in clusters if p < cur)
    out["node_asym"] = (nvu - nvd) / v_tot
    near = min(clusters, key=lambda c: (abs(c[0] - cur), -c[1]))
    strong = max(clusters, key=lambda c: (c[1], -abs(c[0] - cur)))
    out["near_node_dist"] = near[0] - cur
    out["strong_node_dist"] = strong[0] - cur
    out["near_node_strength"] = near[1]
    out["signed_near_strength"] = (1.0 if near[0] > cur else -1.0) * near[1]

    # ---------------- placebos ----------------
    # candidate universe = occupied, traded, NON-node price levels
    node_idx = set()
    for i0, i1 in node_runs:
        for j in range(max(0, i0 - h), min(n_grid - 1, i1 + h) + 1):
            node_idx.add(j)
    cands = [p_lo + j for j in range(n_grid)
             if grid[j] > 0 and j not in node_idx and (p_lo + j) != cur]
    if cands:
        # (1) distance-matched, SAME SIGN as the real nearest node
        want = out["near_node_dist"]
        sgn = 1 if want > 0 else -1
        same = [p for p in cands if (p - cur) * sgn > 0]
        if same:
            pm = min(same, key=lambda p: abs(abs(p - cur) - abs(want)))
            out["plac_matched_dist"] = float(pm - cur)
            out["plac_matched_strength"] = strengths[pm - p_lo]
        # (2) uniformly random non-node level
        pr = cands[rng.randrange(len(cands))]
        out["plac_random_dist"] = float(pr - cur)
    return out


# --------------------------------------------------------------------------
# one calendar-day tick file -> list of sample rows
# --------------------------------------------------------------------------
def _load(date: str):
    f = TICK_DIR / f"{date}.json"
    if not f.exists():
        return None
    with f.open() as fh:
        return json.load(fh)


def process_day(date: str, prev_date: str | None):
    rows = _load(date)
    if not rows:
        return [], {"date": date, "skip": "no_file"}
    cal_day, cal_night = calendar_fronts(date)
    prev_night = calendar_fronts(prev_date)[1] if prev_date else None

    by_contract = defaultdict(list)
    for r in rows:
        cd = r["contract_date"]
        if "/" in cd:                     # calendar spread, not outright
            continue
        s = r["date"]
        sec = int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])
        by_contract[cd].append((sec, int(float(r["price"])), int(r["volume"])))

    out = []
    meta = {"date": date, "cal_day": cal_day, "cal_night": cal_night, "blocks": {}}
    for name, b0, b1, which, owner in BLOCKS:
        if which == "day":
            con = cal_day
        else:
            con = prev_night if owner == "prev" else cal_night
        if con is None:
            continue
        cluster = prev_date if owner == "prev" else date
        if cluster is None:
            continue
        seq = sorted(t for t in by_contract.get(con, []) if b0 <= t[0] < b1)
        meta["blocks"][name] = {"contract": con, "n_ticks": len(seq)}
        if len(seq) < 500:
            continue
        ts = [t[0] for t in seq]
        px = [t[1] for t in seq]
        vol = [t[2] for t in seq]
        t_first, t_last = ts[0], ts[-1]

        rng = random.Random(hash((date, name)) & 0xFFFFFFFF)
        lo = 0
        hi = 0
        t = ((t_first + LOOKBACK) // CADENCE + 1) * CADENCE
        while t + MAX_H <= min(t_last, b1 - 1):
            while hi < len(ts) and ts[hi] <= t:
                hi += 1
            while lo < hi and ts[lo] <= t - LOOKBACK:
                lo += 1
            t += CADENCE
            if hi - lo < MIN_TRADES:
                continue
            cur = px[hi - 1]
            t_now = t - CADENCE
            prof, v_tot, px_v = _profile_at(ts, px, vol, lo, hi, t_now)
            f = _features(prof, v_tot, px_v, cur, rng)
            if f is None:
                continue
            # ---- targets: strictly ts > t_now, inside this block only ----
            tg = {}
            ok = True
            for hz in HORIZONS:
                j = bisect.bisect_right(ts, t_now + hz) - 1
                assert j >= hi - 1
                if j < 0 or ts[j] > b1:
                    ok = False
                    break
                tg[f"r{hz}"] = float(px[j] - cur)
            if not ok:
                continue
            f.update(tg)
            f["cluster_day"] = cluster
            f["block"] = name
            f["t"] = t_now
            out.append(f)
    return out, meta


def _chunk_worker(args):
    """Runs a CONTIGUOUS block of dates (hard rule: never round-robin)."""
    pairs = args
    rows_all = []
    metas = []
    for date, prev in pairs:
        r, m = process_day(date, prev)
        rows_all.extend(r)
        metas.append(m)
    return rows_all, metas


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------
def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            rk[order[k]] = avg
        i = j + 1
    return rk


def _pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((x - mb) ** 2 for x in b)
    if sa <= 0 or sb <= 0:
        return None
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / math.sqrt(sa * sb)


def spearman(a, b):
    return _pearson(_rank(a), _rank(b))


def agg(vals):
    """mean / sd / t / n / share positive for a list of per-day statistics."""
    v = [x for x in vals if x is not None and not math.isnan(x)]
    n = len(v)
    if n < 2:
        return {"n_days": n, "mean": None, "sd": None, "t": None, "pos_share": None}
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    t = m / (sd / math.sqrt(n)) if sd > 0 else None
    return {"n_days": n, "mean": m, "sd": sd, "t": t,
            "pos_share": sum(1 for x in v if x > 0) / n}


def tercile_spread(feat, tgt):
    """per-day mean(target | top tercile of feat) - mean(target | bottom)."""
    n = len(feat)
    if n < 30:
        return None
    order = sorted(range(n), key=lambda i: feat[i])
    k = n // 3
    top = [tgt[i] for i in order[-k:]]
    bot = [tgt[i] for i in order[:k]]
    return sum(top) / len(top) - sum(bot) / len(bot)


_PDS_CACHE: dict = {}


def per_day_stats_cached(key, rows, feats, horizons):
    if key not in _PDS_CACHE:
        _PDS_CACHE[key] = per_day_stats(rows, feats, horizons)
    return _PDS_CACHE[key]


def per_day_stats(rows, feats, horizons):
    """rows -> {(feature, target): {day: ic}}, and tercile spreads."""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["cluster_day"]].append(r)
    ic = defaultdict(dict)
    sp = defaultdict(dict)
    counts = {}
    for day, rs in by_day.items():
        counts[day] = len(rs)
        for f in feats:
            xs = [r[f] for r in rs if f in r]
            if len(xs) < 30:
                continue
            idx = [i for i, r in enumerate(rs) if f in r]
            for hz in horizons:
                ys = [rs[i][hz] for i in idx]
                c = spearman(xs, ys)
                if c is not None:
                    ic[(f, hz)][day] = c
                s = tercile_spread(xs, ys)
                if s is not None:
                    sp[(f, hz)][day] = s
    return ic, sp, counts


def summarise(ic, sp, feats, horizons, days_subset=None, label=""):
    out = {}
    for f in feats:
        for hz in horizons:
            d = ic.get((f, hz), {})
            s = sp.get((f, hz), {})
            if days_subset is not None:
                d = {k: v for k, v in d.items() if k in days_subset}
                s = {k: v for k, v in s.items() if k in days_subset}
            if not d:
                continue
            out[f"{f}|{hz}"] = {
                "ic": agg(list(d.values())),
                "tercile_spread_pts": agg(list(s.values())),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="debug: first N days")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out_path = Path(args.out)
    assert out_path.resolve() != TICK_DIR.resolve(), "output must not sit in the input dir"

    dates = sorted(p.stem for p in TICK_DIR.glob("*.json"))
    if args.limit:
        dates = dates[: args.limit]
    pairs = [(d, dates[i - 1] if i else None) for i, d in enumerate(dates)]

    # CONTIGUOUS shards (hard rule 3): shard s owns pairs[s*w:(s+1)*w].
    w = math.ceil(len(pairs) / args.workers)
    shards = [pairs[i:i + w] for i in range(0, len(pairs), w)]
    rows = []
    metas = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for r, m in ex.map(_chunk_worker, shards):
            rows.extend(r)
            metas.extend(m)
    print(f"[vp_g1a] days={len(dates)} samples={len(rows)}", file=sys.stderr)

    hz_keys = [f"r{h}" for h in HORIZONS]
    all_days = sorted({r["cluster_day"] for r in rows})
    is_days = {d for d in all_days if d <= IS_END}
    oos_days = {d for d in all_days if d >= OOS_START}

    result = {
        "schema": "vp_g1a_node_ic/v1",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "config": {
            "lookback_s": LOOKBACK, "cadence_s": CADENCE, "band": BAND,
            "thr_x_mean": THR, "horizons_s": list(HORIZONS),
            "min_trades": MIN_TRADES, "min_levels": MIN_LEVELS,
            "corpus": str(TICK_DIR), "instrument": "TX outright (price proxy for TMF)",
            "contract_rule": "ex-ante TAIFEX 3rd-Wednesday calendar; night rolls 1 day early",
            "is_window": ["2021-12-01", IS_END], "oos_window": [OOS_START, "2026-08-14"],
            "cost_line_pts": COST_LINE, "gross_per_fill_pts": GROSS_PER_FILL,
            "primary_test": {"feature": PRIMARY[0], "target": PRIMARY[1],
                             "block": PRIMARY[2], "sample": "OOS",
                             "decision_rule": "node-minus-placebo |t|>=2 on day-clustered "
                                              "per-day IC AND same sign in IS"},
        },
        "coverage": {
            "n_date_files": len(dates), "first": dates[0], "last": dates[-1],
            "n_samples": len(rows), "n_cluster_days": len(all_days),
            "n_is_days": len(is_days), "n_oos_days": len(oos_days),
        },
    }

    feats = [f for f in FEATURES]
    sections = {}
    for blk in ("day", "night", "early", "ALL"):
        sub = rows if blk == "ALL" else [r for r in rows if r["block"] == blk]
        if len(sub) < 500:
            continue
        ic, sp, counts = per_day_stats_cached(blk, sub, feats, hz_keys)
        sec = {
            "n_samples": len(sub),
            "n_days": len(counts),
            "full": summarise(ic, sp, feats, hz_keys),
            "IS": summarise(ic, sp, feats, hz_keys, is_days),
            "OOS": summarise(ic, sp, feats, hz_keys, oos_days),
        }
        # per-year breakdown for the primary features only
        years = sorted({d[:4] for d in counts})
        sec["by_year_primary"] = {}
        yr_cov: dict = defaultdict(lambda: [0, 0, 0.0])
        for r in sub:
            c = yr_cov[r["cluster_day"][:4]]
            c[0] += 1
            c[1] += 1 if "near_node_dist" in r else 0
            c[2] += r["range"]
        for y in years:
            ys = {d for d in counts if d.startswith(y)}
            sec["by_year_primary"][y] = {
                "stats": summarise(ic, sp,
                                   [PRIMARY[0], "plac_matched_dist", "plac_random_dist",
                                    PRIMARY_SF[0], "plac_topdec_dist", "all_asym"],
                                   [PRIMARY[1]], ys),
                "n_days": len(ys),
                "n_samples": yr_cov[y][0],
                "node_present_share": yr_cov[y][1] / max(1, yr_cov[y][0]),
                "mean_window_range_pts": yr_cov[y][2] / max(1, yr_cov[y][0]),
            }
        sections[blk] = sec
    result["sections"] = sections

    # ---- node MINUS placebo, paired per day (the number that counts) ----
    PAIRS = (
        ("near_node_dist", ("plac_matched_dist", "plac_random_dist",
                            "all_asym", "vwap_dist")),
        ("topdec_near_dist", ("plac_topdec_dist", "all_asym", "vwap_dist")),
        ("signed_near_strength", ("plac_matched_dist",)),
    )
    deltas = {}
    for blk in ("day", "night", "ALL"):
        sub = rows if blk == "ALL" else [r for r in rows if r["block"] == blk]
        if len(sub) < 500:
            continue
        ic, sp, counts = per_day_stats_cached(blk, sub, feats, hz_keys)
        blk_d = {}
        for node_f, placebos in PAIRS:
            for hz in hz_keys:
                node = ic.get((node_f, hz), {})
                for pl in placebos:
                    pd_ = ic.get((pl, hz), {})
                    common = sorted(set(node) & set(pd_))
                    if len(common) < 30:
                        continue
                    dis = [d for d in common if d <= IS_END]
                    dos = [d for d in common if d >= OOS_START]
                    blk_d[f"{hz}|{node_f}-minus-{pl}"] = {
                        "full": agg([node[d] - pd_[d] for d in common]),
                        "IS": agg([node[d] - pd_[d] for d in dis]),
                        "OOS": agg([node[d] - pd_[d] for d in dos]),
                    }
                # tercile-spread delta (points): the economic version
                nsp = sp.get((node_f, hz), {})
                psp = sp.get((placebos[0], hz), {})
                common = sorted(set(nsp) & set(psp))
                if len(common) >= 30:
                    blk_d[f"{hz}|spread_pts|{node_f}-minus-{placebos[0]}"] = {
                        "full": agg([nsp[d] - psp[d] for d in common]),
                        "IS": agg([nsp[d] - psp[d] for d in common if d <= IS_END]),
                        "OOS": agg([nsp[d] - psp[d] for d in common if d >= OOS_START]),
                    }
        deltas[blk] = blk_d
    result["node_minus_placebo"] = deltas

    # ---- descriptive: node availability / geometry ----
    day_rows = [r for r in rows if r["block"] == "day"]
    if day_rows:
        have = [r for r in day_rows if "near_node_dist" in r]
        ds = sorted(abs(r["near_node_dist"]) for r in have)
        rr = sorted(r["range"] for r in day_rows)
        def q(v, p):
            return v[min(len(v) - 1, int(p * len(v)))] if v else None
        result["descriptives_day"] = {
            "node_present_share": len(have) / len(day_rows),
            "abs_near_node_dist_p10_p50_p90": [q(ds, .1), q(ds, .5), q(ds, .9)],
            "window_range_p10_p50_p90": [q(rr, .1), q(rr, .5), q(rr, .9)],
            "mean_n_nodes": sum(r["n_nodes"] for r in day_rows) / len(day_rows),
            "mean_node_share": sum(r["node_share"] for r in day_rows) / len(day_rows),
            "matched_placebo_available_share":
                sum(1 for r in have if "plac_matched_dist" in r) / max(1, len(have)),
            "abs_target_pts_p50": {
                hz: q(sorted(abs(r[hz]) for r in day_rows), .5) for hz in hz_keys},
        }

    # ---- economics, recomputed from the data (never hard-coded) ----
    econ = {}
    dsec = deltas.get("day", {})
    for k in [x for x in dsec if x.startswith(tuple(f"{h}|spread_pts|" for h in hz_keys))]:
        if True:
            for smp in ("IS", "OOS", "full"):
                m = dsec[k][smp]["mean"]
                if m is None:
                    continue
                econ[f"{k}|{smp}"] = {
                    "tercile_spread_delta_pts": m,
                    "pts_per_fill_one_sided": m / 2.0,
                    "pct_of_cost_line": 100.0 * abs(m / 2.0) / COST_LINE,
                    "pct_of_gross_per_fill": 100.0 * abs(m / 2.0) / GROSS_PER_FILL,
                    "t": dsec[k][smp]["t"], "n_days": dsec[k][smp]["n_days"],
                }
    result["economics_day"] = econ

    # ---- conditional on node_share (the "node volume / window volume" feature) --
    cond = {}
    for blk in ("day",):
        sub = [r for r in rows if r["block"] == blk and "near_node_dist" in r]
        by_day = defaultdict(list)
        for r in sub:
            by_day[r["cluster_day"]].append(r)
        for half in ("hi_share", "lo_share"):
            ics = {}
            for day, rs in by_day.items():
                if len(rs) < 40:
                    continue
                med = sorted(r["node_share"] for r in rs)[len(rs) // 2]
                pick = [r for r in rs
                        if (r["node_share"] >= med if half == "hi_share"
                            else r["node_share"] < med)]
                if len(pick) < 20:
                    continue
                c = spearman([r["near_node_dist"] for r in pick],
                             [r[PRIMARY[1]] for r in pick])
                if c is not None:
                    ics[day] = c
            cond[f"{blk}|{half}|near_node_dist|{PRIMARY[1]}"] = {
                "full": agg(list(ics.values())),
                "IS": agg([v for d, v in ics.items() if d <= IS_END]),
                "OOS": agg([v for d, v in ics.items() if d >= OOS_START]),
            }
    result["conditional_on_node_share"] = cond

    # ---- verdict, recomputed from the numbers above (never hard-coded) ----
    def _pick(dct, key, smp):
        v = dct.get(key, {}).get(smp, {})
        return v.get("mean"), v.get("t"), v.get("n_days"), v.get("pos_share")

    dd = deltas.get("day", {})
    verdict = {}
    for label, key in (("thr3x", f"{PRIMARY[1]}|near_node_dist-minus-plac_matched_dist"),
                       ("scale_free", f"{PRIMARY_SF[1]}|topdec_near_dist-minus-plac_topdec_dist")):
        m_o, t_o, n_o, ps_o = _pick(dd, key, "OOS")
        m_i, t_i, n_i, _ = _pick(dd, key, "IS")
        directional = (t_o is not None and abs(t_o) >= 2.0
                       and m_i is not None and m_o is not None
                       and (m_i > 0) == (m_o > 0))
        verdict[label] = {
            "delta_ic_OOS_mean": m_o, "delta_ic_OOS_t": t_o, "delta_ic_OOS_n_days": n_o,
            "delta_ic_OOS_pos_share": ps_o,
            "delta_ic_IS_mean": m_i, "delta_ic_IS_t": t_i, "delta_ic_IS_n_days": n_i,
            "sign_agrees_IS_OOS": (m_i is not None and m_o is not None
                                   and (m_i > 0) == (m_o > 0)),
            "has_directional_info": "YES" if directional else "NO",
        }
    verdict["overall_has_directional_info"] = (
        "YES" if any(v.get("has_directional_info") == "YES"
                     for v in verdict.values() if isinstance(v, dict)) else "NO")
    result["verdict"] = verdict

    # ---- contract audit ----
    fb = [m for m in metas if m.get("skip")]
    result["contract_audit"] = {
        "n_files_skipped": len(fb),
        "example_fronts": {m["date"]: [m["cal_day"], m["cal_night"]]
                           for m in metas[-3:] if "cal_day" in m},
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[vp_g1a] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
