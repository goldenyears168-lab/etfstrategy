#!/usr/bin/env python3
"""個股期貨空單：時間停損掃描（tick 級 · research only）.

重抓 tick 並存下 5 分鐘一點的價格路徑，測試組合規則：
  · 掛 −X% 限價回補；若在 T 分鐘內觸價 → 成交於 −X%
  · 否則於第 T 分鐘以市價平倉（用該時點最後成交價）
  · T = None 代表抱到 13:45 收盤

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_dayflip_futures_time_stop.py
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

from run_xd_50m_stock_futures_timing import fetch_futures_ticks

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
CACHE = OUT / "tick_path_cache.json"
ADV_MIN, FGAP_MIN = 2000, 0.06
COST = 0.0005
MARKS = list(range(0, 305, 5))   # 08:45 起算的分鐘刻度


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def build_signals() -> list[dict]:
    ev = json.loads((OUT / "events.json").read_text())
    cf = {k: v for k, v in
          json.loads((OUT / "futures_daily_cache.json").read_text()).items() if v}
    futmap = json.loads((OUT / "stock_futures_universe.json").read_text())["map"]
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])
    roll = {}
    for sid, m in cf.items():
        ds = sorted(m)
        for i, d in enumerate(ds):
            if i >= 20:
                roll[(sid, d)] = mean([m[x][4] for x in ds[i - 20:i]])
    grp = defaultdict(list)
    for e in ev:
        if e["sid"] in mega or e["sid"] not in cf:
            continue
        grp[(e["sid"], e["date"])].append(e)
    out = []
    for (sid, d0), es in grp.items():
        d1 = es[0]["d1"]
        m = cf[sid]
        if d0 not in m or d1 not in m:
            continue
        fo, pf = m[d1][0], m[d0][1]
        adv = roll.get((sid, d0))
        if fo <= 0 or pf <= 0 or adv is None or adv < ADV_MIN or fo / pf - 1 < FGAP_MIN:
            continue
        code = futmap[sid]
        out.append(dict(sid=sid, d0=d0, d1=d1,
                        fid=code + "F" if len(code) == 2 else code,
                        amt=sum(x["amt_buy_yi"] for x in es)))
    return out


def main() -> None:
    sigs = build_signals()
    log(f"訊號 {len(sigs)} 筆")
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
        base = ts[0].ts.replace(hour=8, minute=45, second=0, microsecond=0)
        entry = ts[0].price
        path: dict[str, float] = {}
        runlow: dict[str, float] = {}
        low = entry
        mi = 0
        for t in ts:
            m = (t.ts - base).total_seconds() / 60.0
            low = min(low, t.price)
            while mi < len(MARKS) and m >= MARKS[mi]:
                path[str(MARKS[mi])] = t.price
                runlow[str(MARKS[mi])] = low
                mi += 1
        last = ts[-1].price
        while mi < len(MARKS):
            path[str(MARKS[mi])] = last
            runlow[str(MARKS[mi])] = low
            mi += 1
        cache[key] = dict(entry=entry, close=last, low=low, amt=s["amt"],
                          date=s["d0"], d1=s["d1"], path=path, runlow=runlow)
        if i % 25 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            log(f"  {i}/{len(sigs)}")
        time.sleep(0.2)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    rows = [v for v in cache.values() if v]
    log(f"成功 {len(rows)}/{len(cache)}")

    def sim(target: float, tstop: int | None) -> dict:
        out = []
        for r in rows:
            e = r["entry"]
            if tstop is None:
                hit_low = r["low"]
                exit_px = r["close"]
            else:
                hit_low = r["runlow"][str(tstop)]
                exit_px = r["path"][str(tstop)]
            if target is not None and hit_low <= e * (1 - target):
                pnl = target - COST
            else:
                pnl = -(exit_px / e - 1) - COST
            out.append((r["d1"], pnl))
        byd = defaultdict(list)
        for d, p in out:
            byd[d].append(p)
        dm = [mean(v) for v in byd.values()]
        sd = pstdev(dm) or 1e-9
        return dict(
            target=("無" if target is None else f"{target:.0%}"),
            tstop=("收盤" if tstop is None else f"{tstop}分"),
            n=len(out), days=len(dm),
            ev_med=round(100 * median([p for _, p in out]), 2),
            ev_mean=round(100 * mean([p for _, p in out]), 2),
            day_mean=round(100 * mean(dm), 3),
            day_med=round(100 * median(dm), 3),
            day_win=round(100 * mean([x > 0 for x in dm]), 1),
            t=round(mean(dm) / (sd / len(dm) ** 0.5), 2),
        )

    res = {"grid": [], "is_oos": []}
    for target in (0.02, 0.03, 0.04, None):
        for tstop in (15, 30, 45, 60, 90, 120, None):
            res["grid"].append(sim(target, tstop))

    def sim_sub(target, tstop, sub):
        keep = rows
        try:
            globals()["rows"] = sub
            return sim(target, tstop)
        finally:
            globals()["rows"] = keep

    is_rows = [r for r in rows if r["date"] < "2026-01-01"]
    oos_rows = [r for r in rows if r["date"] >= "2026-01-01"]
    for target, tstop in ((0.02, 30), (0.02, 60), (0.02, None), (0.03, 60), (None, 30)):
        res["is_oos"].append(dict(
            rule=f"目標{'無' if target is None else f'{target:.0%}'} · 停{('收盤' if tstop is None else str(tstop)+'分')}",
            IS=sim_sub(target, tstop, is_rows),
            OOS=sim_sub(target, tstop, oos_rows),
        ))

    (OUT / "time_stop.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{'目標':>5}{'時間停損':>8}{'事件中位':>9}{'事件平均':>9}{'日均':>8}{'日中位':>8}{'日勝率':>8}{'t':>7}")
    for r in res["grid"]:
        print(f"{r['target']:>5}{r['tstop']:>8}{r['ev_med']:>9.2f}{r['ev_mean']:>9.2f}"
              f"{r['day_mean']:>8.3f}{r['day_med']:>8.3f}{r['day_win']:>8.1f}{r['t']:>7.2f}")
    print(f"\n{'規則':<20}{'IS 日數/日均/中位/勝率':<32}{'OOS 日數/日均/中位/勝率'}")
    for r in res["is_oos"]:
        a, b = r["IS"], r["OOS"]
        print(f"{r['rule']:<20}{a['days']:>4}{a['day_mean']:>8.3f}{a['day_med']:>8.3f}{a['day_win']:>7.1f}   "
              f"{b['days']:>4}{b['day_mean']:>8.3f}{b['day_med']:>8.3f}{b['day_win']:>7.1f}")
    log(f"→ {OUT/'time_stop.json'}")


if __name__ == "__main__":
    main()
