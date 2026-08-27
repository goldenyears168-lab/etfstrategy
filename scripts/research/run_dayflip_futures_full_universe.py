#!/usr/bin/env python3
"""個股期貨 08:45 空單 · 全流動性宇宙 + PIT 量能過濾（research only）.

訊號（PIT）：T0 隔日沖席買進 ≥門檻 → T+1 08:45 期貨開盤相對前一日期貨收盤漲 ≥X% → 空期貨
過濾（PIT）：T0 為止的期貨 20 日均量 ≥ 門檻（不用未來資訊）
出場：T+1 期貨 13:45 收盤

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_futures_full_universe.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

import stock_db

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
COST = 0.0005
BETA = 1.5


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def daystats(rows: list[tuple[str, float]], min_n: int = 30) -> dict:
    if len(rows) < min_n:
        return dict(n=len(rows), thin=True)
    byd = defaultdict(list)
    for d, r in rows:
        byd[d].append(r)
    dm = [mean(v) for v in byd.values()]
    sd = pstdev(dm) or 1e-9
    return dict(n=len(rows), days=len(dm),
                day_mean=round(100 * mean(dm), 3),
                day_med=round(100 * median(dm), 3),
                day_win=round(100 * mean([x > 0 for x in dm]), 1),
                t=round(mean(dm) / (sd / len(dm) ** 0.5), 2))


def main() -> None:
    ev = json.loads((OUT / "events.json").read_text())
    cache = json.loads((OUT / "futures_daily_cache.json").read_text())
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])
    cache = {k: v for k, v in cache.items() if v}
    log(f"期貨快取 {len(cache)} 檔 · mega 排除 {len(mega)}")

    # PIT 滾動 20 日均量
    roll: dict[tuple[str, str], float] = {}
    for sid, m in cache.items():
        ds = sorted(m)
        for i, d in enumerate(ds):
            if i < 20:
                continue
            roll[(sid, d)] = mean([m[x][4] for x in ds[i - 20:i]])

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ix: dict[str, tuple[float, float]] = {}
    best: dict[str, int] = {}
    rank = {"yahoo": 0, "tej": 1, "finmind": 2}
    for d, o, c, src in con.execute(
        "SELECT date,open,close,source FROM daily_bars WHERE code='IX0001' "
        "AND date BETWEEN '2024-05-01' AND '2026-07-20' AND open>0 AND close>0"
    ):
        r = rank.get(str(src), 9)
        if str(d) not in best or r < best[str(d)]:
            best[str(d)] = r
            ix[str(d)] = (float(o), float(c))
    con.close()

    recs = []
    for e in ev:
        sid, d0, d1 = e["sid"], e["date"], e["d1"]
        if sid in mega or sid not in cache:
            continue
        m = cache[sid]
        if d0 not in m or d1 not in m or d1 not in ix:
            continue
        fo, fc = m[d1][0], m[d1][1]
        prev_fc = m[d0][1]
        if fo <= 0 or prev_fc <= 0:
            continue
        adv = roll.get((sid, d0))
        if adv is None:
            continue
        ixo, ixc = ix[d1]
        recs.append(dict(
            sid=sid, date=d0, d1=d1, adv=adv,
            fgap=fo / prev_fc - 1,
            short=-(fc / fo - 1),
            ix=ixc / ixo - 1,
            amt_share=e["amt_share"], amt=e["amt_buy_yi"],
        ))
    log(f"配對事件 {len(recs):,} / {len({r['sid'] for r in recs})} 檔")

    res: dict = {}

    # 1. 量能門檻 × 跳空門檻
    res["grid"] = []
    for adv_min in (2000, 5000, 10000, 20000):
        for x in (0.05, 0.06, 0.07, 0.09):
            sub = [r for r in recs if r["adv"] >= adv_min and r["fgap"] >= x]
            res["grid"].append(dict(
                adv_min=adv_min, x=f"{x:.0%}",
                stocks=len({r["sid"] for r in sub}),
                stock_leg=daystats([(r["d1"], r["short"] - COST) for r in sub]),
                hedged=daystats([(r["d1"], r["short"] + BETA * r["ix"] - COST)
                                 for r in sub]),
            ))

    # 2. 選定格 (adv>=10000, x=7%) 的 IS/OOS 與明細
    sel = [r for r in recs if r["adv"] >= 10000 and r["fgap"] >= 0.07]
    res["selected"] = dict(
        rule="PIT期貨20日均量>=1萬口 且 期貨08:45跳空>=7%",
        stocks=sorted({r["sid"] for r in sel}),
        top=[[s, n] for s, n in Counter(r["sid"] for r in sel).most_common(12)],
        all_stock_leg=daystats([(r["d1"], r["short"] - COST) for r in sel]),
        all_hedged=daystats([(r["d1"], r["short"] + BETA * r["ix"] - COST) for r in sel]),
        IS_stock=daystats([(r["d1"], r["short"] - COST)
                           for r in sel if r["date"] < "2026-01-01"], 15),
        OOS_stock=daystats([(r["d1"], r["short"] - COST)
                            for r in sel if r["date"] >= "2026-01-01"], 15),
        IS_hedged=daystats([(r["d1"], r["short"] + BETA * r["ix"] - COST)
                            for r in sel if r["date"] < "2026-01-01"], 15),
        OOS_hedged=daystats([(r["d1"], r["short"] + BETA * r["ix"] - COST)
                             for r in sel if r["date"] >= "2026-01-01"], 15),
    )

    # 3. 依分點買量微調
    res["adaptive"] = []
    for base, k in ((0.07, 0.0), (0.07, 0.2), (0.06, 0.2), (0.05, 0.2)):
        sub = [r for r in recs if r["adv"] >= 10000
               and r["fgap"] >= max(0.03, min(0.12, base + k * min(r["amt_share"], 0.3)))]
        res["adaptive"].append(dict(
            rule=f"fgap >= {base:.0%} + {k} × 佔ADV",
            stock_leg=daystats([(r["d1"], r["short"] - COST) for r in sub]),
            hedged=daystats([(r["d1"], r["short"] + BETA * r["ix"] - COST) for r in sub]),
        ))

    # 4. 每日事件數與部位分散
    byd = defaultdict(list)
    for r in sel:
        byd[r["d1"]].append(r)
    res["capacity"] = dict(
        signal_days=len(byd),
        events=len(sel),
        med_events_per_day=median([len(v) for v in byd.values()]) if byd else 0,
        max_events_per_day=max([len(v) for v in byd.values()]) if byd else 0,
        med_fut_adv_lots=round(median([r["adv"] for r in sel])) if sel else 0,
    )

    (OUT / "futures_full_universe.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    def fmt(s):
        if not s or s.get("thin"):
            return f"n={s.get('n', 0):4d} (薄)"
        return (f"n={s['n']:5d} 日{s['days']:3d} 日均{s['day_mean']:6.3f} "
                f"中位{s['day_med']:6.3f} 勝{s['day_win']:5.1f} t{s['t']:6.2f}")

    print("\n=== 量能 × 跳空 網格（PIT）===")
    for r in res["grid"]:
        print(f"  ADV>={r['adv_min']:6d}口 gap>={r['x']:>3s} 檔{r['stocks']:3d} | "
              f"個股腿 {fmt(r['stock_leg'])} | 含指數 {fmt(r['hedged'])}")
    s = res["selected"]
    print(f"\n=== {s['rule']} ===")
    print(f"  標的 {len(s['stocks'])} 檔: {s['top']}")
    print(f"  全期 個股腿 {fmt(s['all_stock_leg'])}")
    print(f"  全期 含指數 {fmt(s['all_hedged'])}")
    print(f"  IS  個股腿 {fmt(s['IS_stock'])} | 含指數 {fmt(s['IS_hedged'])}")
    print(f"  OOS 個股腿 {fmt(s['OOS_stock'])} | 含指數 {fmt(s['OOS_hedged'])}")
    print("\n=== 依分點買量微調 ===")
    for r in res["adaptive"]:
        print(f"  {r['rule']:<28s} 個股腿 {fmt(r['stock_leg'])} | 含指數 {fmt(r['hedged'])}")
    print(f"\n=== 容量 === {json.dumps(res['capacity'], ensure_ascii=False)}")
    log(f"→ {OUT/'futures_full_universe.json'}")


if __name__ == "__main__":
    main()
