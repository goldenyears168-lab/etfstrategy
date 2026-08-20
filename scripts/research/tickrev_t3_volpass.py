#!/usr/bin/env python3
"""tickrev_t3 step 3 — per-day volatility profile, so the sampling plan can be
stratified by real volatility regime instead of "one day per month".

For each date's DOMINANT outright contract (taken from tickrev_t3_coverage_days.json,
i.e. the exact series the engine will trade), computes per session:
  range_pt      = session high - session low
  rv_pt         = sum |1-minute close-to-close change|  (realized path length)
  n_min         = minutes with at least one print
Night session is stitched from D (>=15:00) + D+1 (<=05:00), matching build_sessions.

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_volpass.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
from multiprocessing import Pool
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "reports/research/channel_lab"
TICK_DIR = LAB / "finmind_tx_tick_by_day"
COV = LAB / "tickrev_t3_coverage_days.json"
OUT = LAB / "tickrev_t3_vol_raw.json"


def scan(job):
    date, ref = job
    p = TICK_DIR / f"{date}.json"
    try:
        rows = json.loads(p.read_text())
    except Exception:
        return date, None
    # bucket per minute for the three windows
    out = {}
    for tag in ("day", "night_head", "night_tail"):
        out[tag] = {}
    for r in rows:
        if r["contract_date"] != ref:
            continue
        ts = r["date"]
        hh = ts[11:]
        px = float(r["price"])
        minute = ts[11:16]
        if "08:45:00" <= hh <= "13:45:00":
            out["day"][minute] = px
        if hh >= "15:00:00":
            out["night_head"][minute] = px
        if hh <= "05:00:00":
            out["night_tail"][minute] = px
        # also track hi/lo
        for tag, cond in (("day", "08:45:00" <= hh <= "13:45:00"),
                          ("night_head", hh >= "15:00:00"),
                          ("night_tail", hh <= "05:00:00")):
            if cond:
                b = out[tag]
                k = b.get("__hi")
                if k is None or px > k:
                    b["__hi"] = px
                k = b.get("__lo")
                if k is None or px < k:
                    b["__lo"] = px
    res = {}
    for tag, b in out.items():
        hi = b.pop("__hi", None)
        lo = b.pop("__lo", None)
        closes = [b[m] for m in sorted(b)]
        rv = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        res[tag] = dict(n_min=len(closes), hi=hi, lo=lo,
                        rng=(round(hi - lo, 1) if hi is not None else None),
                        rv=round(rv, 1),
                        first=(closes[0] if closes else None),
                        last=(closes[-1] if closes else None))
    return date, res


def main() -> None:
    cov = json.loads(COV.read_text())
    jobs = [(d, v["dominant_contract"]) for d, v in sorted(cov.items())
            if v.get("status") == "ok"]
    print(f"vol pass over {len(jobs)} files")
    t0 = time.time()
    out = {}
    with Pool(min(8, os.cpu_count() or 4)) as pool:
        for i, (date, res) in enumerate(pool.imap_unordered(scan, jobs, chunksize=4), 1):
            out[date] = res
            if i % 200 == 0:
                print(f"  {i}/{len(jobs)} {time.time()-t0:.0f}s", flush=True)
    OUT.write_text(json.dumps({d: out[d] for d in sorted(out)}, indent=1))
    print(f"done {time.time()-t0:.0f}s -> {OUT}")


if __name__ == "__main__":
    main()
