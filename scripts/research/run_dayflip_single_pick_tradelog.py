#!/usr/bin/env python3
"""每日單押一檔：排序法則比較 + 逐筆交易紀錄（research only）.

規則：08:45 空個股期貨 → 掛 −2% 限價回補 → 未觸價則 13:45 收盤平倉
每天若有多檔訊號，依排序法則只取第一名。

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_single_pick_tradelog.py \
      [--adv-min 800]
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
COST = 0.0005
TARGET = 0.02
MARKS = list(range(0, 305, 5))


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def hhmm(minutes: float) -> str:
    total = 8 * 60 + 45 + minutes
    return f"{int(total // 60):02d}:{int(total % 60):02d}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adv-min", type=float, default=800)
    ap.add_argument("--fgap-min", type=float, default=0.06)
    args = ap.parse_args()

    ev = json.loads((OUT / "events.json").read_text())
    cf = {k: v for k, v in
          json.loads((OUT / "futures_daily_cache.json").read_text()).items() if v}
    tick = {k: v for k, v in
            json.loads((OUT / "tick_path_cache.json").read_text()).items() if v}
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

    trades = []
    for (sid, d0), es in grp.items():
        d1 = es[0]["d1"]
        m = cf[sid]
        if d0 not in m or d1 not in m:
            continue
        fo, pf = m[d1][0], m[d0][1]
        adv = roll.get((sid, d0))
        if fo <= 0 or pf <= 0 or adv is None or adv < args.adv_min:
            continue
        fgap = fo / pf - 1
        if fgap < args.fgap_min:
            continue
        tk = tick.get(f"{sid}|{d1}")
        if not tk:
            continue
        entry = tk["entry"]
        # 出場：首次觸及 −2% 的 5 分鐘刻度；未觸價則收盤
        exit_min, exit_px, how = None, None, "收盤平倉"
        for mk in MARKS:
            if tk["runlow"][str(mk)] <= entry * (1 - TARGET):
                exit_min, exit_px, how = mk, entry * (1 - TARGET), "觸價回補"
                break
        if exit_min is None:
            exit_px = tk["close"]
        pnl = ((TARGET - COST) if how == "觸價回補"
               else -(exit_px / entry - 1) - COST)
        e0 = es[0]
        trades.append(dict(
            signal_date=d0, trade_date=d1, stock=sid,
            entry_price=entry, exit_price=round(exit_px, 2),
            exit_time=("13:45" if exit_min is None else hhmm(exit_min)),
            exit_min=(None if exit_min is None else exit_min),
            how=how, pnl_pct=round(100 * pnl, 2),
            fgap=round(100 * fgap, 2), fut_adv=round(adv),
            amt_yi=round(sum(x["amt_buy_yi"] for x in es), 2),
            advshare=round(100 * sum(x["amt_share"] for x in es), 2),
            share_vol=round(100 * sum(x["buy_share"] for x in es), 2),
            n_seats=len(es), seats=",".join(sorted({x["tid"] for x in es})),
            ret0=round(100 * e0["ret0"], 2), ret5=round(100 * e0["ret5"], 2),
            rvol=round(e0["rvol"], 2), adv_yi=round(e0["adv_yi"], 1),
            mfe=round(100 * (entry - tk["low"]) / entry, 2),
        ))
    log(f"交易候選 {len(trades)} 筆 / {len({t['trade_date'] for t in trades})} 天 / "
        f"{len({t['stock'] for t in trades})} 檔")
    with (OUT / "all_trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader()
        w.writerows(sorted(trades, key=lambda x: (x["trade_date"], x["stock"])))

    byd = defaultdict(list)
    for t in trades:
        byd[t["trade_date"]].append(t)

    RANKS = {
        "跳空最大": lambda t: -t["fgap"],
        "跳空最小": lambda t: t["fgap"],
        "分點買進金額最大": lambda t: -t["amt_yi"],
        "佔ADV20最大": lambda t: -t["advshare"],
        "佔當日量最大": lambda t: -t["share_vol"],
        "席數最多": lambda t: -t["n_seats"],
        "期貨流動性最高": lambda t: -t["fut_adv"],
        "個股ADV最大": lambda t: -t["adv_yi"],
        "T0漲幅最大": lambda t: -t["ret0"],
        "量能倍數最大": lambda t: -t["rvol"],
        "股號最小(對照)": lambda t: t["stock"],
    }

    def evaluate(pick) -> dict:
        pnl = []
        for d in sorted(byd):
            best = sorted(byd[d], key=pick)[0]
            pnl.append((d, best["pnl_pct"] / 100))
        xs = [p for _, p in pnl]
        sd = pstdev(xs) or 1e-9
        eq, peak, mdd = 1.0, 1.0, 0.0
        for _, p in pnl:
            eq *= 1 + p
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        return dict(days=len(xs), mean=round(100 * mean(xs), 3),
                    med=round(100 * median(xs), 3),
                    win=round(100 * mean([x > 0 for x in xs]), 1),
                    t=round(mean(xs) / (sd / len(xs) ** 0.5), 2),
                    cum=round(100 * (eq - 1), 1), mdd=round(100 * mdd, 1),
                    worst=round(100 * min(xs), 2))

    res = {"adv_min": args.adv_min, "ranks": []}
    for nm, fn in RANKS.items():
        res["ranks"].append(dict(rule=nm, **evaluate(fn)))
    # 等權全押（對照）
    xs = [mean([t["pnl_pct"] / 100 for t in byd[d]]) for d in sorted(byd)]
    sd = pstdev(xs) or 1e-9
    eq, peak, mdd = 1.0, 1.0, 0.0
    for p in xs:
        eq *= 1 + p
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    res["ranks"].append(dict(rule="等權全押(對照)", days=len(xs),
                             mean=round(100 * mean(xs), 3),
                             med=round(100 * median(xs), 3),
                             win=round(100 * mean([x > 0 for x in xs]), 1),
                             t=round(mean(xs) / (sd / len(xs) ** 0.5), 2),
                             cum=round(100 * (eq - 1), 1), mdd=round(100 * mdd, 1),
                             worst=round(100 * min(xs), 2)))
    res["ranks"].sort(key=lambda r: -r["t"])

    best_rule = res["ranks"][0]["rule"]
    pick = RANKS.get(best_rule)
    if pick:
        picks = [sorted(byd[d], key=pick)[0] for d in sorted(byd)]
        with (OUT / "single_pick_tradelog.csv").open("w", newline="",
                                                     encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(picks[0].keys()))
            w.writeheader()
            w.writerows(picks)
        res["best_rule"] = best_rule
        res["n_2026"] = len([p for p in picks if p["trade_date"] >= "2026-01-01"])

    (OUT / "single_pick.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{'排序法則':<18}{'天數':>5}{'日均%':>8}{'中位%':>8}{'勝率%':>8}{'t':>7}"
          f"{'累積%':>9}{'MaxDD%':>9}{'最差日%':>9}")
    for r in res["ranks"]:
        print(f"{r['rule']:<18}{r['days']:>5}{r['mean']:>8.3f}{r['med']:>8.3f}"
              f"{r['win']:>8.1f}{r['t']:>7.2f}{r['cum']:>9.1f}{r['mdd']:>9.1f}"
              f"{r['worst']:>9.2f}")
    log(f"→ {OUT/'all_trades.csv'} · {OUT/'single_pick_tradelog.csv'}")


if __name__ == "__main__":
    main()
