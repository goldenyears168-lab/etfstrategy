#!/usr/bin/env python3
"""Item F — fetch per-stock-futures open_interest history for a liquid sample.

Transplants the champion `fut_foreign_oi_z60` methodology (see
scripts/research/chip_macro/build_panel.py / eval_signals.py) down to the
INDIVIDUAL stock-futures level. Caveat discovered during this run: FinMind's
`TaiwanFuturesInstitutionalInvestors` dataset (foreign/trust/dealer net OI
breakdown) only has coverage for index-level products (TX/TE/MTX/...) —
querying data_id='CDF' (2330's futures) returns zero rows. TAIFEX does not
publish an institutional-category OI breakdown for individual stock futures.
So this uses TOTAL open_interest (all participants combined) per stock, which
is a genuine methodology difference from the champion signal (net-foreign-OI)
and is called out explicitly in FINDINGS.md.

Sample: top ~45 names by recent ADV (lots) among the 251-stock cleaned
universe map (reports/research/branch-footprint-screen/dayflip_gapup_short/
stock_futures_universe.json), restricted to the 83 stocks that already had a
liquidity-screen cache entry (so ADV is known without extra API calls).

Front-month selection: for each (stock, date), among trading_session=='position'
rows with a single (non-spread) contract_date, take the contract with the
highest volume that day (mirrors build_stock_futures_liquidity_universe.py).

READ-ONLY FinMind calls only. Output: futures_oi_cache.json under
reports/research/stock_futures_oi_signal/.

Run: PYTHONPATH=src .venv/bin/python scripts/research/stock_futures_oi_fetch.py
"""
from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from finmind_client import fetch_finmind

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_MAP = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
LIQ_CACHE = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
OUT_DIR = ROOT / "reports/research/stock_futures_oi_signal"
OUT_CACHE = OUT_DIR / "futures_oi_cache.json"
N_SAMPLE = 45
START = date(2020, 1, 1)
END = date.today()


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def pick_sample() -> list[tuple[str, str, float]]:
    umap = json.loads(UNIVERSE_MAP.read_text())["map"]
    fid_of = {sid: (c + "F" if len(c) == 2 else c) for sid, c in umap.items()}
    liq = json.loads(LIQ_CACHE.read_text())
    overlap = set(liq.keys()) & set(umap.keys())
    advs = []
    for sid in overlap:
        rows = liq[sid]
        vals = list(rows.values())
        if len(vals) < 30:
            continue
        vols = [v[4] for v in vals[-60:] if len(v) >= 5]
        if not vols:
            continue
        advs.append((sid, fid_of[sid], statistics.mean(vols)))
    advs.sort(key=lambda x: -x[2])
    return advs[:N_SAMPLE]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sample = pick_sample()
    log(f"sample n={len(sample)}: {[s for s, _, _ in sample]}")

    cache: dict[str, dict[str, list]] = json.loads(OUT_CACHE.read_text()) if OUT_CACHE.exists() else {}
    for sid, fid, adv in sample:
        if sid in cache and cache[sid]:
            log(f"  {sid} ({fid}) already cached, skip")
            continue
        try:
            rows = fetch_finmind("TaiwanFuturesDaily", fid, START, END, timeout=180)
        except Exception as ex:  # noqa: BLE001
            log(f"  {sid} {fid} ERR {str(ex)[:80]}")
            cache[sid] = {}
            OUT_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
            time.sleep(0.3)
            continue
        byd: dict[str, list] = defaultdict(list)
        for r in rows:
            cd = str(r.get("contract_date", ""))
            if "/" in cd or r.get("trading_session") != "position":
                continue
            if float(r.get("open") or 0) <= 0:
                continue
            byd[str(r["date"])].append(r)
        out = {}
        for d, rs in byd.items():
            near = max(rs, key=lambda x: float(x.get("volume") or 0))
            out[d] = [
                float(near.get("open_interest") or 0),
                float(near.get("volume") or 0),
                float(near.get("close") or 0),
                str(near.get("contract_date")),
            ]
        cache[sid] = out
        log(f"  {sid} ({fid}) adv={adv:.0f} -> {len(out)} days")
        OUT_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
        time.sleep(0.3)

    log(f"done -> {OUT_CACHE}  ({len(cache)} stocks)")


if __name__ == "__main__":
    main()
