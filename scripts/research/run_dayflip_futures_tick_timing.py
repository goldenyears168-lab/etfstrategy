#!/usr/bin/env python3
"""個股期貨空單：MFE 何時發生 / −X% 目標何時成交（tick 級 · research only）.

對每個訊號 (股票, T+1)：抓該日個股期貨 tick，取近月契約，
  entry = 08:45 第一筆成交價
  逐筆掃描，記錄 −1/−2/−3/−4/−5% 首次觸價時間、當日最低點時間、最大不利時間

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_dayflip_futures_tick_timing.py
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

from run_xd_50m_stock_futures_timing import fetch_futures_ticks

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
CACHE = OUT / "tick_timing_cache.json"
ADV_MIN, FGAP_MIN = 2000, 0.06
TARGETS = (0.01, 0.02, 0.03, 0.04, 0.05)


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    ev = json.loads((OUT / "events.json").read_text())
    cache_fut = {k: v for k, v in
                 json.loads((OUT / "futures_daily_cache.json").read_text()).items() if v}
    futmap = json.loads((OUT / "stock_futures_universe.json").read_text())["map"]
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])

    roll = {}
    for sid, m in cache_fut.items():
        ds = sorted(m)
        for i, d in enumerate(ds):
            if i >= 20:
                roll[(sid, d)] = mean([m[x][4] for x in ds[i - 20:i]])

    grp = defaultdict(list)
    for e in ev:
        if e["sid"] in mega or e["sid"] not in cache_fut:
            continue
        grp[(e["sid"], e["date"])].append(e)

    sigs = []
    for (sid, d0), es in grp.items():
        d1 = es[0]["d1"]
        m = cache_fut[sid]
        if d0 not in m or d1 not in m:
            continue
        fo, pf = m[d1][0], m[d0][1]
        adv = roll.get((sid, d0))
        if fo <= 0 or pf <= 0 or adv is None or adv < ADV_MIN:
            continue
        if fo / pf - 1 < FGAP_MIN:
            continue
        sigs.append(dict(sid=sid, d0=d0, d1=d1,
                         fid=futmap[sid] + "F" if len(futmap[sid]) == 2 else futmap[sid],
                         amt=sum(x["amt_buy_yi"] for x in es)))
    log(f"訊號 {len(sigs)} 筆待抓 tick")

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for i, s in enumerate(sigs, 1):
        key = f"{s['sid']}|{s['d1']}"
        if key in cache:
            continue
        try:
            ticks = fetch_futures_ticks(s["fid"], s["d1"])
        except Exception as ex:  # noqa: BLE001
            log(f"  {key} ERR {str(ex)[:60]}")
            cache[key] = None
            continue
        if not ticks:
            cache[key] = None
            continue
        vol = Counter()
        for t in ticks:
            vol[t.contract] += max(t.volume, 1)
        near = vol.most_common(1)[0][0]
        ts = [t for t in ticks if t.contract == near]
        if not ts:
            cache[key] = None
            continue
        entry = ts[0].price
        entry_ts = ts[0].ts.strftime("%H:%M:%S")
        hits = {}
        low, low_ts, high, high_ts = entry, entry_ts, entry, entry_ts
        for t in ts:
            if t.price < low:
                low, low_ts = t.price, t.ts.strftime("%H:%M:%S")
            if t.price > high:
                high, high_ts = t.price, t.ts.strftime("%H:%M:%S")
            for x in TARGETS:
                k = f"{x:.2f}"
                if k not in hits and t.price <= entry * (1 - x):
                    hits[k] = t.ts.strftime("%H:%M:%S")
        cache[key] = dict(entry=entry, entry_ts=entry_ts, n=len(ts),
                          low=low, low_ts=low_ts, high=high, high_ts=high_ts,
                          close=ts[-1].price, hits=hits, amt=s["amt"])
        if i % 20 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            log(f"  {i}/{len(sigs)}")
        time.sleep(0.2)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    rows = [v for v in cache.values() if v]
    log(f"成功 {len(rows)} / {len(cache)}")

    def mins(hhmmss: str) -> float:
        h, m, s = (int(x) for x in hhmmss.split(":"))
        return (h * 60 + m + s / 60) - (8 * 60 + 45)

    res = {}
    res["target_timing"] = []
    for x in TARGETS:
        k = f"{x:.2f}"
        hit = [r for r in rows if k in r["hits"]]
        if not hit:
            continue
        ms = sorted(mins(r["hits"][k]) for r in hit)
        res["target_timing"].append(dict(
            target=f"{x:.0%}", hit_rate=round(100 * len(hit) / len(rows), 1),
            med_min=round(median(ms), 1),
            p25_min=round(ms[len(ms) // 4], 1), p75_min=round(ms[3 * len(ms) // 4], 1),
            within_15m=round(100 * mean([m <= 15 for m in ms]), 1),
            within_30m=round(100 * mean([m <= 30 for m in ms]), 1),
            within_60m=round(100 * mean([m <= 60 for m in ms]), 1),
            before_0900=round(100 * mean([m <= 15 for m in ms]), 1),
        ))
    lows = sorted(mins(r["low_ts"]) for r in rows)
    highs = sorted(mins(r["high_ts"]) for r in rows)
    res["extremes"] = dict(
        low_med_min=round(median(lows), 1),
        low_within_15m=round(100 * mean([m <= 15 for m in lows]), 1),
        low_within_60m=round(100 * mean([m <= 60 for m in lows]), 1),
        low_after_240m=round(100 * mean([m >= 240 for m in lows]), 1),
        high_med_min=round(median(highs), 1),
        high_within_15m=round(100 * mean([m <= 15 for m in highs]), 1),
        mfe_med=round(100 * median([(r["entry"] - r["low"]) / r["entry"] for r in rows]), 2),
        mae_med=round(100 * median([(r["high"] - r["entry"]) / r["entry"] for r in rows]), 2),
    )
    # 分時段 MFE 貢獻
    buckets = [(0, 15, "08:45-09:00"), (15, 75, "09:00-10:00"),
               (75, 165, "10:00-11:30"), (165, 300, "11:30-13:45")]
    res["low_bucket"] = []
    for lo, hi, nm in buckets:
        n = sum(1 for m in lows if lo <= m < hi)
        res["low_bucket"].append(dict(bucket=nm, pct=round(100 * n / len(lows), 1), n=n))
    (OUT / "tick_timing.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    log(f"→ {OUT/'tick_timing.json'}")


if __name__ == "__main__":
    main()
