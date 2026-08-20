#!/usr/bin/env python3
"""tickrev_t3 step 1 — raw inventory of the FinMind TX tick corpus.

Pass 1 (parallel, one worker per day-file): for every contract_date present in
the file, count rows and distinct 1-minute buckets inside three time windows
    day     : 08:45:00 <= ts <= 13:45:00   (same calendar date)
    night_a : ts >= 15:00:00               (same calendar date, night head)
    night_b : ts <= 05:00:00               (this file acting as the NEXT day's
                                            night tail)
matching slow_cell_tick_latency_lab.build_sessions() window definitions exactly.

Writes an intermediate cache; pass 2 (tickrev_t3_coverage.py) stitches
D + (D+1) and applies _dominant_outright_contract().

Run:
    PYTHONPATH=src .venv/bin/python scripts/research/tickrev_t3_inventory.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TICK_DIR = ROOT / "reports/research/channel_lab/finmind_tx_tick_by_day"
OUT = ROOT / "reports/research/channel_lab/tickrev_t3_inventory_raw.json"


def scan(path_str: str) -> tuple[str, dict]:
    p = Path(path_str)
    date = p.stem
    size = p.stat().st_size
    try:
        rows = json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        return date, {"error": repr(exc), "bytes": size}
    if not rows:
        return date, {"bytes": size, "n_rows": 0, "contracts": {}, "futures_ids": {}}

    per = defaultdict(lambda: {"n": 0, "day_n": 0, "day_min": set(),
                               "na_n": 0, "na_min": set(),
                               "nb_n": 0, "nb_min": set(),
                               "first": None, "last": None})
    fids = defaultdict(int)
    bad_ts = 0
    for r in rows:
        cd = r["contract_date"]
        fids[r["futures_id"]] += 1
        ts = r["date"]              # "YYYY-MM-DD HH:MM:SS"
        if len(ts) != 19:
            bad_ts += 1
            continue
        hhmmss = ts[11:]
        minute = ts[11:16]
        e = per[cd]
        e["n"] += 1
        if e["first"] is None or ts < e["first"]:
            e["first"] = ts
        if e["last"] is None or ts > e["last"]:
            e["last"] = ts
        if "08:45:00" <= hhmmss <= "13:45:00":
            e["day_n"] += 1
            e["day_min"].add(minute)
        if hhmmss >= "15:00:00":
            e["na_n"] += 1
            e["na_min"].add(minute)
        if hhmmss <= "05:00:00":
            e["nb_n"] += 1
            e["nb_min"].add(minute)

    contracts = {}
    for cd, e in per.items():
        contracts[cd] = {
            "n": e["n"], "first": e["first"], "last": e["last"],
            "day_n": e["day_n"], "day_min": len(e["day_min"]),
            "night_head_n": e["na_n"], "night_head_min": len(e["na_min"]),
            "night_tail_n": e["nb_n"], "night_tail_min": len(e["nb_min"]),
        }
    return date, {"bytes": size, "n_rows": len(rows), "contracts": contracts,
                  "futures_ids": dict(fids), "bad_timestamps": bad_ts}


def main() -> None:
    files = sorted(str(p) for p in TICK_DIR.glob("*.json"))
    print(f"scanning {len(files)} files ({sum(os.path.getsize(f) for f in files)/1e9:.2f} GB)")
    t0 = time.time()
    out: dict[str, dict] = {}
    nproc = min(8, os.cpu_count() or 4)
    with Pool(nproc) as pool:
        for i, (date, rec) in enumerate(pool.imap_unordered(scan, files, chunksize=4), 1):
            out[date] = rec
            if i % 100 == 0:
                print(f"  {i}/{len(files)}  {time.time()-t0:.0f}s", flush=True)
    OUT.write_text(json.dumps({d: out[d] for d in sorted(out)}, indent=1))
    print(f"done in {time.time()-t0:.0f}s -> {OUT}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
