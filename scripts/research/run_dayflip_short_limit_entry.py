#!/usr/bin/env python3
"""開盤掛限價空單 15 分鐘 vs 直接用開盤價：成交率與報酬（research only）.

規則：T+1 09:00 掛限價賣（放空）於 T0收盤 x (1+X)。
  · 開盤價已 >= 觸發價 → 成交於開盤價（優於限價）
  · 否則 09:00~09:15 內最高價觸及 → 成交於觸發價
  · 都沒有 → 不成交
回補：T+1 收盤。指數腿：1.5 x IX0001(open->close)。

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_short_limit_entry.py
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
EVENTS = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/events.json"
OUT = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short"
COST = 0.003
WIN_1M = ("09:00:00", "09:14:00")
WIN_5M = ("09:00:00", "09:10:00")


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    ev = json.loads(EVENTS.read_text())
    # 用 T0 收盤重建：events 裡存的是 gap，o1 = c0*(1+gap)
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)

    sids = sorted({e["sid"] for e in ev})
    d1s = sorted({e["d1"] for e in ev})
    lo, hi = d1s[0], d1s[-1]

    log("載入日線 ...")
    day: dict[tuple[str, str], tuple] = {}
    for i in range(0, len(sids), 400):
        ch = sids[i : i + 400]
        ph = ",".join("?" * len(ch))
        for sid, d, o, h, l, c in con.execute(
            f"SELECT stock_id,trade_date,open,high,low,close FROM stock_daily_bars "
            f"WHERE source='finmind' AND stock_id IN ({ph}) "
            f"AND trade_date BETWEEN ? AND ? AND close>0",
            (*ch, lo, hi),
        ):
            day[(str(sid), str(d))] = (float(o), float(h), float(l), float(c))

    log("載入開盤 15 分鐘高點（1m 優先，5m 備援）...")
    first15: dict[tuple[str, str], tuple[float, float]] = {}  # (open, high15)
    for tbl, w in (("stock_kbar_5m", WIN_5M), ("stock_kbar_1m", WIN_1M)):
        agg: dict[tuple[str, str], list] = defaultdict(list)
        for i in range(0, len(sids), 300):
            ch = sids[i : i + 300]
            ph = ",".join("?" * len(ch))
            for sid, d, mnt, o, h in con.execute(
                f"SELECT stock_id,trade_date,minute,open,high FROM {tbl} "
                f"WHERE stock_id IN ({ph}) AND trade_date BETWEEN ? AND ? "
                f"AND minute BETWEEN ? AND ? AND high>0",
                (*ch, lo, hi, w[0], w[1]),
            ):
                agg[(str(sid), str(d))].append((str(mnt), float(o), float(h)))
        for k, rows in agg.items():
            rows.sort()
            first15[k] = (rows[0][1], max(r[2] for r in rows))
        log(f"  {tbl}: 覆蓋 {len(agg):,} 檔日")
    con.close()

    ix_leg = {e["d1"]: e["rets"]["1d"][1] for e in ev}

    def sim(trig_fn, tag: str, pool: list[dict]) -> dict:
        fills = []
        eligible = 0
        for e in pool:
            k = (e["sid"], e["d1"])
            if k not in first15 or k not in day:
                continue
            eligible += 1
            c0 = day.get((e["sid"], e["date"]), (0, 0, 0, 0))[3]
            if c0 <= 0:
                continue
            trig = c0 * (1 + trig_fn(e))
            o15, h15 = first15[k]
            # 資料品質硬檢查：部分分鐘 K 在同一檔日內混用兩種價格尺度
            # （如 6669 的 15 分高點 8025 vs 當日最高 2675）。不合格直接丟棄，不做校正。
            d_open, d_high = day[k][0], day[k][1]
            if o15 <= 0 or d_open <= 0:
                continue
            if abs(o15 / d_open - 1) > 0.002 or h15 > d_high * 1.001 or h15 < o15:
                continue
            if o15 >= trig:
                fill = o15
            elif h15 >= trig:
                fill = trig
            else:
                continue
            c1 = day[k][3]
            stock_leg = -(c1 / fill - 1)
            tot = stock_leg + 1.5 * ix_leg[e["d1"]] - COST
            fills.append(dict(e=e, fill=fill, c1=c1, stock=stock_leg, tot=tot))
        if not fills:
            return dict(tag=tag, eligible=eligible, fills=0)
        byd = defaultdict(list)
        for f in fills:
            byd[f["e"]["d1"]].append(f["tot"])
        dm = [mean(v) for v in byd.values()]
        sd = pstdev(dm) or 1e-9
        return dict(
            tag=tag, eligible=eligible, fills=len(fills),
            fill_rate=round(100 * len(fills) / eligible, 1) if eligible else 0,
            days=len(dm),
            stock_med=round(100 * median([f["stock"] for f in fills]), 2),
            stock_mean=round(100 * mean([f["stock"] for f in fills]), 2),
            stock_win=round(100 * mean([f["stock"] > 0 for f in fills]), 1),
            day_mean=round(100 * mean(dm), 3),
            day_med=round(100 * median(dm), 3),
            day_win=round(100 * mean([x > 0 for x in dm]), 1),
            t=round(mean(dm) / (sd / len(dm) ** 0.5), 2),
        )

    allpool = ev
    res = {"triggers": [], "adaptive": [], "by_amtshare": []}

    log("固定觸發價掃描 ...")
    for x in (0.03, 0.05, 0.06, 0.07, 0.08, 0.09):
        res["triggers"].append(sim(lambda e, x=x: x, f"掛單 +{x:.0%} (15分)", allpool))
    # 對照：只用開盤價（不掛單，開盤 >= X 才做）
    for x in (0.05, 0.07):
        pool = [e for e in allpool if e["gap"] >= x]
        r = sim(lambda e: -0.99, f"對照·開盤>= {x:.0%} 開盤價成交", pool)
        res["triggers"].append(r)

    log("依分點買量調整觸發價 ...")
    # X = base + k * amt_share（分點 T0 買進金額 / ADV20）
    for base, k in ((0.07, 0.0), (0.05, 0.2), (0.05, 0.4), (0.06, 0.2), (0.07, 0.2), (0.07, -0.2)):
        res["adaptive"].append(
            sim(lambda e, b=base, k=k: max(0.02, min(0.095, b + k * min(e["amt_share"], 0.3))),
                f"X = {base:.0%} + {k} × 佔ADV", allpool)
        )

    log("固定 +7% 掛單下，依分點買量分層 ...")
    for lo_, hi_, nm in ((0, 0.02, "<2%ADV"), (0.02, 0.05, "2~5%"),
                         (0.05, 0.12, "5~12%"), (0.12, 9, ">=12%")):
        pool = [e for e in allpool if lo_ <= e["amt_share"] < hi_]
        res["by_amtshare"].append(sim(lambda e: 0.07, f"+7% · {nm}", pool))
    for lo_, hi_, nm in ((0, 0.5, "<0.5億"), (0.5, 1.0, "0.5~1億"),
                         (1.0, 2.0, "1~2億"), (2.0, 99, ">=2億")):
        pool = [e for e in allpool if lo_ <= e["amt_buy_yi"] < hi_]
        res["by_amtshare"].append(sim(lambda e: 0.07, f"+7% · 買進{nm}", pool))

    (OUT / "limit_entry.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for sec, rows in res.items():
        print(f"\n=== {sec} ===")
        for r in rows:
            if not r.get("fills"):
                print(f"  {r['tag']:<28s} 無成交 (eligible={r['eligible']})")
                continue
            print(
                f"  {r['tag']:<28s} 成交{r['fills']:5d}/{r['eligible']:5d}={r['fill_rate']:5.1f}% "
                f"日數{r['days']:4d} 個股腿中位{r['stock_med']:6.2f} 平均{r['stock_mean']:6.2f} "
                f"勝率{r['stock_win']:5.1f} | 日均{r['day_mean']:6.3f} 中位{r['day_med']:6.3f} "
                f"日勝率{r['day_win']:5.1f} t={r['t']:5.2f}"
            )
    log(f"→ {OUT/'limit_entry.json'}")


if __name__ == "__main__":
    main()
