#!/usr/bin/env python3
"""vp_g1a companion — POSITIVE CONTROL / causal-boundary injection test.

A null result is only credible if the same machinery can detect a signal
that is known to be there.  This script re-runs the vp_g1a sampler on a
CONTIGUOUS block of recent days in three modes:

  clean    : the shipped causal contract (profile uses trades <= t)
  inject60 : profile is built from trades <= t+60  (deliberate look-ahead)
  inject300: profile is built from trades <= t+300 (bigger look-ahead)

If the node features carry ANY mechanical relationship to the forward
path, the injected runs must show a large IC.  If clean ~ 0 and injected
is large, the clean zero is a real absence of ex-ante information, not a
broken feature pipeline.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/research/vp_g1a_causal_injection.py \
      --days 120
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "scripts/research/vp_g1a_node_ic.py"
OUT = ROOT / "reports/research/channel_lab/vp_g1a_causal_injection.json"

spec = importlib.util.spec_from_file_location("vp_g1a", SRC)
G = importlib.util.module_from_spec(spec)
sys.modules["vp_g1a"] = G
spec.loader.exec_module(G)


def run_day(date, prev_date, shift):
    """Same sampler as vp_g1a.process_day but the profile window is moved
    forward by `shift` seconds (shift=0 -> the shipped causal contract)."""
    import random
    from collections import Counter

    rows = G._load(date)
    if not rows:
        return []
    cal_day, _ = G.calendar_fronts(date)
    seq = []
    for r in rows:
        if r["contract_date"] != cal_day:
            continue
        s = r["date"]
        sec = int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])
        if 8 * 3600 + 45 * 60 <= sec < 13 * 3600 + 45 * 60:
            seq.append((sec, int(float(r["price"])), int(r["volume"])))
    seq.sort()
    if len(seq) < 500:
        return []
    ts = [t[0] for t in seq]
    px = [t[1] for t in seq]
    vol = [t[2] for t in seq]
    out = []
    rng = random.Random(hash((date, "inj", shift)) & 0xFFFFFFFF)
    t = ((ts[0] + G.LOOKBACK) // G.CADENCE + 1) * G.CADENCE
    while t + G.MAX_H <= ts[-1]:
        t_now = t
        t += G.CADENCE
        cut_hi = t_now + shift                    # <-- injected boundary
        hi = bisect.bisect_right(ts, cut_hi)
        lo = bisect.bisect_right(ts, cut_hi - G.LOOKBACK)
        if hi - lo < G.MIN_TRADES:
            continue
        j_cur = bisect.bisect_right(ts, t_now) - 1
        if j_cur < 0:
            continue
        cur = px[j_cur]                            # current price is ALWAYS at t
        prof = Counter()
        v_tot = 0
        px_v = 0.0
        for i in range(lo, hi):
            prof[px[i]] += vol[i]
            v_tot += vol[i]
            px_v += px[i] * vol[i]
        f = G._features(prof, v_tot, px_v, cur, rng)
        if f is None:
            continue
        ok = True
        for hz in G.HORIZONS:
            k = bisect.bisect_right(ts, t_now + hz) - 1
            if k < 0:
                ok = False
                break
            f[f"r{hz}"] = float(px[k] - cur)
        if not ok:
            continue
        f["cluster_day"] = date
        out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120,
                    help="most recent N trading days, CONTIGUOUS")
    args = ap.parse_args()

    dates = sorted(p.stem for p in G.TICK_DIR.glob("*.json"))
    pairs = [(d, dates[i - 1] if i else None) for i, d in enumerate(dates)]
    pairs = pairs[-args.days:]                     # contiguous tail, never strided

    hz_keys = [f"r{h}" for h in G.HORIZONS]
    feats = ["near_node_dist", "topdec_near_dist", "poc_dist",
             "plac_matched_dist", "all_asym"]
    res = {
        "schema": "vp_g1a_causal_injection/v1",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "purpose": ("positive control: if a deliberate look-ahead makes the node "
                    "features predictive, the clean run's zero is an absence of "
                    "ex-ante information, not a dead pipeline"),
        "window": [pairs[0][0], pairs[-1][0]], "n_days_requested": args.days,
        "modes": {},
    }
    for name, shift in (("clean", 0), ("inject60", 60), ("inject300", 300)):
        rows = []
        for d, prev in pairs:
            rows.extend(run_day(d, prev, shift))
        ic, sp, counts = G.per_day_stats(rows, feats, hz_keys)
        res["modes"][name] = {
            "shift_s": shift, "n_samples": len(rows), "n_days": len(counts),
            "stats": G.summarise(ic, sp, feats, hz_keys),
        }
        print(f"[inject] {name}: {len(rows)} samples / {len(counts)} days",
              file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print(f"[inject] wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
