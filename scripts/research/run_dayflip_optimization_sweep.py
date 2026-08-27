#!/usr/bin/env python3
"""dayflip-futures-short v1 的進階優化探索（research only · 不改凍結規格）.

用既有 tick 路徑快取與期貨日線，測試六個方向：
  A 分段目標（先出一半再出剩下）
  B 提早強制平倉（13:00 / 12:00 vs 13:45）
  C headroom 部位配置（口數 ∝ 1/(10−fgap)）
  D 鎖漲停日的實際可回補性（tick 級）
  E 結算日／到期週效應
  F fgap 分層 × 目標價交互（高跳空用小目標？）

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_optimization_sweep.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

import stock_db

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
COST = 0.0005
MARKS = list(range(0, 305, 5))
ADV_MIN, FGAP_MIN = 800, 0.06


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def daystat(pairs: list[tuple[str, float]]) -> dict:
    byd = defaultdict(list)
    for d, p in pairs:
        byd[d].append(p)
    dm = [mean(v) for v in byd.values()]
    sd = pstdev(dm) or 1e-9
    return dict(n=len(pairs), days=len(dm),
                mean=100 * mean(dm), med=100 * median(dm),
                win=100 * mean([x > 0 for x in dm]),
                t=mean(dm) / (sd / len(dm) ** 0.5),
                worst=100 * min(dm))


def fmt(s: dict) -> str:
    return (f"n={s['n']:4d} 日{s['days']:3d} 日均{s['mean']:6.3f} 中位{s['med']:6.3f} "
            f"勝{s['win']:5.1f} t{s['t']:5.2f} 最差日{s['worst']:6.2f}")


def main() -> None:
    ev = json.loads((OUT / "events.json").read_text())
    cf = {k: v for k, v in
          json.loads((OUT / "futures_daily_cache.json").read_text()).items() if v}
    tick = {k: v for k, v in
            json.loads((OUT / "tick_path_cache.json").read_text()).items() if v}
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])
    spec = json.loads((OUT / "FROZEN_SPEC_V1.json").read_text())
    FLIP = spec["seat_flip_table_frozen"]["values"]
    MANUAL = {tuple(x) for x in spec["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}

    roll = {}
    for sid, m in cf.items():
        ds = sorted(m)
        for i, d in enumerate(ds):
            if i >= 20:
                roll[(sid, d)] = mean([m[x][4] for x in ds[i - 20:i]])

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    px = {}
    for sid, d, c in con.execute(
        "SELECT stock_id,trade_date,close FROM stock_daily_bars WHERE source='finmind' "
        "AND trade_date BETWEEN '2024-04-01' AND '2026-08-07' AND close>0"
    ):
        px[(str(sid), str(d))] = float(c)
    br = defaultdict(lambda: defaultdict(dict))
    for tid in FLIP:
        for d, sid, b, s in con.execute(
            "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, "2024-04-01", "2026-08-07")):
            br[tid][str(sid)][str(d)] = (float(b or 0), float(s or 0))
    con.close()
    cal = sorted({d for (_, d) in px})
    ci = {d: i for i, d in enumerate(cal)}

    def net_ratio(tid, sid, d0):
        i = ci.get(d0)
        if i is None or i < 60:
            return None
        tb = ts = 0.0
        for d in cal[i - 60:i]:
            b, s = (br[tid].get(sid) or {}).get(d, (0.0, 0.0))
            p = px.get((sid, d))
            if p is None:
                continue
            tb += b * p
            ts += s * p
        return None if tb < 1e8 else (tb - ts) / tb

    grp = defaultdict(list)
    for e in ev:
        if e["sid"] in mega or e["sid"] not in cf:
            continue
        grp[(e["sid"], e["date"])].append(e)

    trades = []
    for (sid, d0), es in grp.items():
        m = cf[sid]
        d1 = es[0]["d1"]
        if d0 not in m or d1 not in m:
            continue
        adv = roll.get((sid, d0))
        if adv is None or adv < ADV_MIN:
            continue
        fo, pf = m[d1][0], m[d0][1]
        if fo <= 0 or pf <= 0:
            continue
        fgap = fo / pf - 1
        if fgap < FGAP_MIN:
            continue
        t = tick.get(f"{sid}|{d1}")
        if not t:
            continue
        keep = []
        for e in es:
            if (e["tid"], sid) in MANUAL:
                continue
            nr = net_ratio(e["tid"], sid, d0)
            if nr is not None and nr >= 0.30:
                continue
            keep.append(e)
        if not keep or not any(FLIP.get(e["tid"], 0) >= 0.40 for e in keep):
            continue
        ds = sorted(m)
        i = ds.index(d1)
        trades.append(dict(
            sid=sid, d0=d0, d1=d1, fgap=fgap, entry=t["entry"],
            low=t["low"], close=t["close"], path=t["path"], runlow=t["runlow"],
            next_open=(m[ds[i + 1]][0] if i + 1 < len(ds) else None),
            amt=sum(x["amt_buy_yi"] for x in es),
            advshare=sum(x["amt_share"] for x in es)))
    log(f"凍結規格訊號 {len(trades)} 筆 / {len({t['d1'] for t in trades})} 天")

    def first_hit(t, target):
        for mk in MARKS:
            if t["runlow"][str(mk)] <= t["entry"] * (1 - target):
                return mk
        return None

    res = {}

    # ---- A 分段目標 ----
    print("\n=== A 分段目標（第一段出 half、剩下等第二段或收盤）===")
    rowsA = []
    for t1, t2, half in ((0.02, 0.02, 0.0), (0.01, 0.02, 0.5), (0.01, 0.03, 0.5),
                         (0.015, 0.03, 0.5), (0.01, 0.02, 0.3), (0.01, 0.04, 0.5),
                         (0.005, 0.02, 0.5)):
        out = []
        for t in trades:
            h1 = first_hit(t, t1) if half > 0 else None
            h2 = first_hit(t, t2)
            close_r = -(t["close"] / t["entry"] - 1)
            if half == 0:
                p = (t2 if h2 is not None else close_r) - COST
            else:
                leg1 = t1 if h1 is not None else close_r
                leg2 = t2 if h2 is not None else close_r
                p = half * leg1 + (1 - half) * leg2 - COST
            out.append((t["d1"], p))
        s = daystat(out)
        tag = ("固定 2%（現行）" if half == 0
               else f"{t1:.1%}出{half:.0%} + {t2:.1%}出剩下")
        print(f"  {tag:<26} {fmt(s)}")
        rowsA.append(dict(rule=tag, **{k: round(v, 3) for k, v in s.items()}))
    res["A_partial_target"] = rowsA

    # ---- B 提早強制平倉 ----
    print("\n=== B 提早強制平倉（目標 2% 不變）===")
    rowsB = []
    for mk, nm in ((150, "11:15"), (195, "12:00"), (255, "13:00"),
                   (285, "13:30"), (300, "13:45(現行)")):
        out = []
        for t in trades:
            h = first_hit(t, 0.02)
            if h is not None and h <= mk:
                p = 0.02 - COST
            else:
                p = -(t["path"][str(mk)] / t["entry"] - 1) - COST
            out.append((t["d1"], p))
        s = daystat(out)
        print(f"  {nm:<26} {fmt(s)}")
        rowsB.append(dict(rule=nm, **{k: round(v, 3) for k, v in s.items()}))
    res["B_early_exit"] = rowsB

    # ---- C headroom 部位配置 ----
    print("\n=== C 部位配置（日內按權重加權，總曝險固定）===")
    rowsC = []
    for nm, wf in (("等權（現行）", lambda t: 1.0),
                   ("∝ 1/(10−fgap)", lambda t: 1.0 / max(0.5, 10 - 100 * t["fgap"])),
                   ("∝ 1/(10−fgap)^2", lambda t: 1.0 / max(0.5, 10 - 100 * t["fgap"]) ** 2),
                   ("∝ fgap", lambda t: 100 * t["fgap"]),
                   ("只做 fgap>=8", lambda t: 1.0 if t["fgap"] >= 0.08 else 0.0)):
        byd = defaultdict(list)
        for t in trades:
            h = first_hit(t, 0.02)
            p = (0.02 - COST) if h is not None else (-(t["close"] / t["entry"] - 1) - COST)
            byd[t["d1"]].append((wf(t), p))
        dm = []
        for d, items in byd.items():
            tw = sum(w for w, _ in items)
            if tw <= 0:
                continue
            dm.append(sum(w * p for w, p in items) / tw)
        sd = pstdev(dm) or 1e-9
        s = dict(n=len(trades), days=len(dm), mean=100 * mean(dm), med=100 * median(dm),
                 win=100 * mean([x > 0 for x in dm]),
                 t=mean(dm) / (sd / len(dm) ** 0.5), worst=100 * min(dm))
        print(f"  {nm:<26} {fmt(s)}")
        rowsC.append(dict(rule=nm, **{k: round(v, 3) for k, v in s.items()}))
    res["C_sizing"] = rowsC

    # ---- D 鎖漲停可回補性 ----
    print("\n=== D 鎖漲停日的實際可回補性（tick 級）===")
    locked = [t for t in trades if t["low"] >= t["entry"] - 1e-9]
    near = [t for t in trades if 0 < (t["entry"] - t["low"]) / t["entry"] < 0.005]
    print(f"  完全沒有低於進場價的成交（真鎖死）: {len(locked)} / {len(trades)} "
          f"= {100*len(locked)/len(trades):.1f}%")
    print(f"  最多只低於進場價 0.5% 以內: {len(near)} 筆")
    if locked:
        nxt = [(-(t['next_open'] / t['entry'] - 1)) for t in locked if t["next_open"]]
        print(f"  真鎖死那批若被迫留倉到 T+2 開盤: 平均 {100*mean(nxt):+.2f}% "
              f"中位 {100*median(nxt):+.2f}% 最差 {100*min(nxt):+.2f}%")
        print(f"  → 假設無法回補的期望值衝擊: 這 {len(locked)} 筆從 −0.05% 變成 "
              f"{100*mean(nxt):+.2f}%，全樣本日均影響約 "
              f"{(len(locked)/len(trades))*(mean(nxt)+0.0005)*100:+.3f}pp")
    res["D_locked"] = dict(locked=len(locked), total=len(trades),
                           pct=round(100 * len(locked) / len(trades), 1))

    # ---- E 結算日效應 ----
    print("\n=== E 到期週效應（台指期結算日＝每月第三個週三）===")
    def settle_week(d: str) -> bool:
        y, m, _ = (int(x) for x in d.split("-"))
        import calendar
        weds = [dd for dd in range(1, 32)
                if dd <= calendar.monthrange(y, m)[1]
                and datetime(y, m, dd).weekday() == 2]
        third = weds[2] if len(weds) >= 3 else weds[-1]
        return abs(int(d.split("-")[2]) - third) <= 2
    rowsE = []
    for nm, f in (("到期週(±2日)", settle_week),
                  ("非到期週", lambda d: not settle_week(d))):
        out = []
        for t in trades:
            if not f(t["d1"]):
                continue
            h = first_hit(t, 0.02)
            out.append((t["d1"], (0.02 - COST) if h is not None
                        else (-(t["close"] / t["entry"] - 1) - COST)))
        if len(out) < 10:
            print(f"  {nm:<26} n={len(out)} 太少")
            continue
        s = daystat(out)
        print(f"  {nm:<26} {fmt(s)}")
        rowsE.append(dict(rule=nm, **{k: round(v, 3) for k, v in s.items()}))
    res["E_settlement"] = rowsE

    # ---- F fgap × 目標價交互 ----
    print("\n=== F fgap 分層 × 目標價 ===")
    rowsF = []
    for lo, hi, nm in ((0.06, 0.075, "fgap 6~7.5%"), (0.075, 0.09, "fgap 7.5~9%"),
                       (0.09, 9, "fgap >=9%")):
        sub = [t for t in trades if lo <= t["fgap"] < hi]
        if len(sub) < 15:
            continue
        line = f"  {nm:<14} n={len(sub):3d} |"
        best = None
        for tg in (0.01, 0.015, 0.02, 0.03):
            out = []
            for t in sub:
                h = first_hit(t, tg)
                out.append((t["d1"], (tg - COST) if h is not None
                            else (-(t["close"] / t["entry"] - 1) - COST)))
            s = daystat(out)
            line += f" {tg:.1%}:{s['mean']:6.3f}"
            if best is None or s["mean"] > best[1]:
                best = (tg, s["mean"])
        print(line + f"  ← 最佳 {best[0]:.1%}")
        rowsF.append(dict(bucket=nm, n=len(sub), best_target=best[0],
                          best_day_mean=round(best[1], 3)))
    res["F_fgap_target"] = rowsF

    (OUT / "optimization_sweep.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"→ {OUT/'optimization_sweep.json'}")


if __name__ == "__main__":
    main()
