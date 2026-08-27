#!/usr/bin/env python3
"""高信心隔日沖席在 2026 年的「持續建倉標的」偵測 + 排除效果（research only）.

命題：若某席在某股是「持續建倉」（買進後留倉、淨部位持續累積），
      該席的買進就不是隔日沖倒貨壓力 → 不該計入做空訊號。

PIT 建倉分數（只用 T0 之前的資料）：
  對每個 (席位, 股票)，取 T0 前 N 個交易日：
    net_ratio = Σ(buy − sell) / Σbuy      ← 買進的錢有多少比例留在手上
    pair_flip = 該對事件的隔日賣回中位數
  持續建倉 = net_ratio ≥ 門檻 且 Σbuy 達最低量

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_accumulation_exclusion.py
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
LOOKBACK = 60          # 交易日
MIN_BUY_YI = 1.0       # 回看窗內合計買進至少 1 億才判定
ADV_MIN, FGAP_MIN, TARGET, COST = 800, 0.06, 0.02, 0.0005

NAME = {"920M": "凱基-宜蘭", "7008": "兆豐-三重", "989g": "元大-嘉義", "981j": "元大-士林",
        "5851": "統一-高雄", "9217": "凱基松山", "980h": "元大-台北", "918e": "群益-大安",
        "989X": "元大-民生三民", "779Z": "國票安和", "913R": "群益-北高雄", "9661": "富邦新店",
        "585Y": "統一土城", "9875": "元大-土城永寧", "918X": "群益-台北", "5383": "第一金-高雄",
        "1360": "港麥格理", "9A9R": "永豐金信義", "9325": "華南-忠孝", "9A81": "永豐金-匯立",
        "779n": "國票南京", "9227": "凱基城中", "9216": "凱基-信義", "920F": "凱基-站前"}


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def daystat(rs):
    b = defaultdict(list)
    for r in rs:
        b[r["d1"]].append(r["p"])
    dm = [mean(v) for v in b.values()]
    if len(dm) < 5:
        return None
    sd = pstdev(dm) or 1e-9
    return dict(n=len(rs), days=len(dm), mean=100 * mean(dm), med=100 * median(dm),
                win=100 * mean([x > 0 for x in dm]),
                t=mean(dm) / (sd / len(dm) ** 0.5))


def main() -> None:
    ev = json.loads((OUT / "events.json").read_text())
    cf = {k: v for k, v in
          json.loads((OUT / "futures_daily_cache.json").read_text()).items() if v}
    tick = {k: v for k, v in
            json.loads((OUT / "tick_path_cache.json").read_text()).items() if v}
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])

    # 席位隔日沖強度（全樣本，用於界定「高信心席」）
    byt = defaultdict(list)
    for e in ev:
        byt[e["tid"]].append(e)
    FLIP = {t: median([x["flip"] for x in es]) for t, es in byt.items()}
    HIGH = {t for t, f in FLIP.items() if f >= 0.4}
    log(f"高信心隔日沖席（中位 flip>=0.4）{len(HIGH)} 席：{sorted(HIGH)}")

    log("載入分點逐日買賣 ...")
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    rows: dict[str, dict[str, dict[str, tuple]]] = defaultdict(lambda: defaultdict(dict))
    for tid in NAME:
        for d, sid, b, s in con.execute(
            "SELECT trade_date, stock_id, buy, sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
            (tid, "2024-06-01", "2026-08-06"),
        ):
            rows[tid][str(sid)][str(d)] = (float(b or 0), float(s or 0))
    px: dict[tuple[str, str], float] = {}
    for sid, d, c in con.execute(
        "SELECT stock_id, trade_date, close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN '2024-06-01' AND '2026-08-06' "
        "AND close>0"
    ):
        px[(str(sid), str(d))] = float(c)
    cal = sorted({d for (_, d) in px})
    ci = {d: i for i, d in enumerate(cal)}
    con.close()
    log("載入完成")

    def accum(tid: str, sid: str, d0: str):
        """回看 LOOKBACK 交易日的建倉分數（不含 d0 當日，PIT）。"""
        i = ci.get(d0)
        if i is None or i < LOOKBACK:
            return None
        window = cal[i - LOOKBACK:i]
        m = rows[tid].get(sid) or {}
        tb = ts = 0.0
        for d in window:
            b, s = m.get(d, (0.0, 0.0))
            p = px.get((sid, d))
            if p is None:
                continue
            tb += b * p
            ts += s * p
        if tb < MIN_BUY_YI * 1e8:
            return None
        return dict(buy_yi=tb / 1e8, net_ratio=(tb - ts) / tb)

    # ---------- 1. 2026 年高信心席的持續建倉標的清單 ----------
    log("盤點 2026 年持續建倉 (席位×股票) ...")
    d2026 = [d for d in cal if "2026-01-01" <= d <= "2026-07-31"]
    probe = d2026[::10]  # 每 10 個交易日取樣一次
    holdings = defaultdict(list)
    for tid in sorted(HIGH):
        for sid in rows[tid]:
            if sid in mega or sid.startswith("00"):
                continue
            vals = []
            for d in probe:
                a = accum(tid, sid, d)
                if a:
                    vals.append(a)
            if len(vals) < 5:
                continue
            nr = median([v["net_ratio"] for v in vals])
            by = median([v["buy_yi"] for v in vals])
            pos = mean([v["net_ratio"] > 0.2 for v in vals])
            if nr >= 0.2 and pos >= 0.6:
                holdings[tid].append((sid, nr, by, pos, len(vals)))
    total = sum(len(v) for v in holdings.values())
    log(f"持續建倉組合 {total} 組 / {len(holdings)} 席")
    print("\n=== 2026 年高信心隔日沖席的『持續建倉』標的（排除權值股與 ETF）===")
    print(f"{'席位':<7}{'名稱':<12}{'股票':<7}{'淨留倉比中位':>13}{'窗內買進(億)':>13}{'建倉狀態佔比':>13}")
    lines = []
    for tid in sorted(holdings, key=lambda t: -len(holdings[t])):
        for sid, nr, by, pos, n in sorted(holdings[tid], key=lambda x: -x[1])[:8]:
            print(f"{tid:<7}{NAME[tid]:<12}{sid:<7}{nr:>13.3f}{by:>13.2f}{100*pos:>12.0f}%")
            lines.append(dict(tid=tid, name=NAME[tid], stock=sid,
                              net_ratio=round(nr, 3), buy_yi=round(by, 2),
                              pct_accum=round(100 * pos)))
    (OUT / "accumulation_pairs_2026.json").write_text(
        json.dumps({"lookback": LOOKBACK, "min_buy_yi": MIN_BUY_YI,
                    "n_pairs": total, "pairs": lines}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---------- 2. PIT 排除效果 ----------
    log("回測 PIT 建倉排除 ...")
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

    ACC_CACHE = {}

    def is_accum(tid, sid, d0, thr):
        k = (tid, sid, d0)
        if k not in ACC_CACHE:
            ACC_CACHE[k] = accum(tid, sid, d0)
        a = ACC_CACHE[k]
        return a is not None and a["net_ratio"] >= thr

    def build(thr):
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
            t = tick.get(f"{sid}|{d1}")
            if not t:
                continue
            keep = ([x for x in es if not is_accum(x["tid"], sid, d0, thr)]
                    if thr is not None else es)
            if not keep:
                continue
            entry = t["entry"]
            hit = any(t["runlow"][str(x)] <= entry * (1 - TARGET) for x in range(0, 305, 5))
            kept_flip = [FLIP.get(x["tid"], 0) for x in keep]
            out.append(dict(sid=sid, d0=d0, d1=d1,
                            p=(TARGET - COST) if hit else (-(t["close"] / entry - 1) - COST),
                            n=len(keep), nhigh=sum(1 for x in keep if x["tid"] in HIGH),
                            maxflip=max(kept_flip) if kept_flip else 0,
                            meanflip=mean(kept_flip) if kept_flip else 0,
                            dropped=len(es) - len(keep)))
        return out

    print(f"\n=== PIT 持續建倉排除的效果（ADV>=800 · fgap>=6% · 目標2%）===")
    print(f"{'排除門檻':<22}{'n':>5}{'日數':>5}{'日均%':>8}{'中位%':>8}{'勝率%':>7}{'t':>6}"
          f"{'剔除席次':>9}{'留下席均flip':>13}")
    res = []
    for thr, nm in ((None, "不排除"), (0.5, "net_ratio>=0.5"), (0.3, "net_ratio>=0.3"),
                    (0.2, "net_ratio>=0.2"), (0.1, "net_ratio>=0.1")):
        rs = build(thr)
        st = daystat(rs)
        drop = sum(r["dropped"] for r in rs)
        mf = mean([r["meanflip"] for r in rs])
        print(f"{nm:<22}{st['n']:>5}{st['days']:>5}{st['mean']:>8.3f}{st['med']:>8.3f}"
              f"{st['win']:>7.1f}{st['t']:>6.2f}{drop:>9}{mf:>13.3f}")
        res.append(dict(rule=nm, **{k: round(v, 3) if isinstance(v, float) else v
                                    for k, v in st.items()},
                        dropped_seats=drop, kept_mean_flip=round(mf, 3)))

    print(f"\n=== 排除後再疊加席位沖分過濾 ===")
    print(f"{'組合':<34}{'n':>5}{'日數':>5}{'日均%':>8}{'中位%':>8}{'勝率%':>7}{'t':>6}")
    combos = []
    for thr, nm in ((None, "不排除"), (0.3, "建倉排除(0.3)"), (0.2, "建倉排除(0.2)")):
        rs = build(thr)
        for f, fn in ((lambda r: True, "無"), (lambda r: r["nhigh"] >= 1, "高沖席>=1"),
                      (lambda r: r["n"] >= 3, "席數>=3"),
                      (lambda r: r["nhigh"] >= 1 and r["n"] >= 3, "高沖席>=1且席數>=3")):
            sub = [r for r in rs if f(r)]
            st = daystat(sub)
            if not st or st["n"] < 30:
                continue
            print(f"{nm + ' + ' + fn:<34}{st['n']:>5}{st['days']:>5}{st['mean']:>8.3f}"
                  f"{st['med']:>8.3f}{st['win']:>7.1f}{st['t']:>6.2f}")
            combos.append(dict(rule=f"{nm} + {fn}",
                               **{k: round(v, 3) if isinstance(v, float) else v
                                  for k, v in st.items()}))

    (OUT / "accumulation_exclusion.json").write_text(
        json.dumps({"thresholds": res, "combos": combos}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    log(f"→ {OUT/'accumulation_pairs_2026.json'} · {OUT/'accumulation_exclusion.json'}")


if __name__ == "__main__":
    main()
