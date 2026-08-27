#!/usr/bin/env python3
"""進場掛單位置掃描——同一組事前訊號，只改進場價位，跑歷史全樣本。

為什麼做這個
------------
2026-08-25：用嚴格事前規則（只看 ≤ 當根 K）在 7 個 MXF session 上測，
市價進場毛額 −0.57 點/趟；同一批訊號改成「掛在下一根開盤 −10 點等」變成
+3.35 點/趟。差 3.9 點，而 MXF 來回成本是 3.07 點——**進場位置這一個變因，
比訊號本身、比三個濾網、比整條牆研究加起來影響都大**。

但 7 個 session / 74 趟撐不起結論，而且掃描結果非單調
（−2 +3.07 / −5 +2.18 / −10 +3.35），形狀像雜訊不像結構。真訊號應該有
平滑的劑量反應，而且**峰的位置要穩定**。

這支就是去驗那件事：把同樣的掃描跑到幾百個 session，看倒 U 型的峰位置
是不是固定。峰穩定＝結構；每次跑落在不同格＝雜訊。

資料：``$GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/finmind_mtx_tick_by_day/``
（MTX ＝ 小型臺指期貨 ＝ MXF，71 個交易日）。合約用 **ex-ante** 近月規則
（第三個週三結算後轉倉），不用「當日成交量 argmax」——那是 2026-08-20 在
tick 研究裡抓到的整日 look-ahead。

用法
----
    PYTHONPATH=src .venv/bin/python scripts/research/mxf_entry_offset_sweep.py \\
        --product MTX --json-out reports/research/channel_lab/mxf_entry_offset_sweep.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

#: 來回成本（點）——成本線 v2 實測。TX 每點 NT$200，手續費換算成點數遠低於小台。
COST_BY_PRODUCT = {"MTX": 3.07, "TX": 2.42, "MXF": 3.07, "TXF": 2.42}
COST_PTS = 3.07
OFFSETS = (0, 2, 5, 8, 10, 15, 20, 30, 50)
WAIT_BARS = 10


def tick_dir(product: str) -> Path:
    base = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or Path.home() / "goldenstocks-data")
    return base / "cache" / "tmf_channel" / f"finmind_{product.lower()}_tick_by_day"


def _third_wednesday(y: int, m: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)
    return d + timedelta(days=14)


def front_contract(d: date) -> str:
    y, m = d.year, d.month
    if d > _third_wednesday(y, m):
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return f"{y}{m:02d}"


def load_day(p: Path) -> dict | None:
    """→ 1 分 K（只留 ex-ante 近月、只留日盤 08:45–13:45）。"""
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    d = date.fromisoformat(p.stem)
    want = front_contract(d)
    bars: dict[int, dict] = {}
    for r in rows:
        if str(r.get("contract_date") or "") != want:
            continue
        try:
            t = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S")
            px = float(r["price"])
            v = float(r["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (8 <= t.hour < 14):
            continue
        if t.hour == 8 and t.minute < 45:
            continue
        if t.hour == 13 and t.minute > 45:
            continue
        m = int(t.timestamp() // 60)
        b = bars.setdefault(m, {"o": px, "h": px, "l": px, "c": px, "v": 0.0, "pv": 0.0})
        b["h"] = max(b["h"], px)
        b["l"] = min(b["l"], px)
        b["c"] = px
        b["v"] += v
        b["pv"] += px * v
    if len(bars) < 120:
        return None
    ks = sorted(bars)
    arr = lambda k: np.array([bars[i][k] for i in ks])  # noqa: E731
    V = arr("v")
    return {"O": arr("o"), "H": arr("h"), "L": arr("l"), "C": arr("c"), "V": V,
            "VWAP": np.cumsum(arr("pv")) / np.maximum(np.cumsum(V), 1), "n": len(ks)}


def signals(B: dict) -> tuple[list[bool], list[bool]]:
    """嚴格因果：第 i 根只用 bars[0..i]。"""
    O, H, L, C, V, VW, n = B["O"], B["H"], B["L"], B["C"], B["V"], B["VWAP"], B["n"]
    MA = np.array([V[max(0, i - 20):i].mean() if i >= 5 else np.nan for i in range(n)])
    rv = np.where(np.isnan(MA), np.nan, V / np.maximum(MA, 1e-9))
    bs = [False] * n
    ss = [False] * n
    for i in range(n):
        if i < 25 or np.isnan(rv[i]):
            continue
        hh = H[i - 20:i].max()
        rng = max(H[i] - L[i], 1e-9)
        lo = (min(O[i], C[i]) - L[i]) / rng
        up = (H[i] - max(O[i], C[i])) / rng
        bs[i] = bool((rv[i] >= 2.5 and C[i] < C[i - 5] and lo >= 0.4)
                     or (C[i] > hh and rv[i] >= 1.5)
                     or (rv[i] <= 0.7 and C[i] > VW[i] and C[i - 1] <= VW[i - 1]))
        ss[i] = bool((rv[i] >= 2.5 and C[i] > C[i - 5] and up >= 0.4)
                     or (C[i] < VW[i] and C[i - 1] >= VW[i - 1] and rv[i] >= 1.3)
                     or (H[i] >= hh and rv[i] <= 0.9))
    return bs, ss


def run(B: dict, x: float, wait: int) -> tuple[list[float], int]:
    """x=0 市價進；x>0 掛「下一根開盤 − x」等最多 wait 根。出場一律市價。"""
    O, L, n = B["O"], B["L"], B["n"]
    bs, ss = signals(B)
    legs: list[float] = []
    miss = 0
    pos = None
    pend = None
    for i in range(n - 1):
        if pos is None and pend is None and bs[i]:
            if x == 0:
                pos = O[i + 1]
            else:
                pend = (O[i + 1] - x, i + 1 + wait)
        elif pend is not None:
            lim, dead = pend
            if ss[i]:                       # 還沒成交就出訊號 → 取消
                pend = None
                miss += 1
            elif L[i] <= lim:
                pos = lim
                pend = None
            elif i >= dead:
                pend = None
                miss += 1
        if pos is not None and ss[i]:
            legs.append(O[i + 1] - pos)
            pos = None
    return legs, miss


def summarize(legs: list[float], miss: int) -> dict:
    if not legs:
        return {"n": 0}
    g = st.mean(legs)
    return {"n": len(legs), "miss": miss,
            "fill_pct": round(100 * len(legs) / (len(legs) + miss), 1) if (len(legs) + miss) else None,
            "gross_pts": round(g, 3),
            "net_pts": round(g - COST_PTS, 3),
            "se_pts": round(st.stdev(legs) / len(legs) ** 0.5, 3) if len(legs) > 1 else None,
            "win_pct": round(100 * sum(1 for x in legs if x > 0) / len(legs), 1),
            "total_net": round((g - COST_PTS) * len(legs), 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--product", default="MTX")
    ap.add_argument("--wait", type=int, default=WAIT_BARS)
    ap.add_argument("--cost", type=float, default=None, help="來回成本（點），預設依商品")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    global COST_PTS
    COST_PTS = args.cost if args.cost is not None else COST_BY_PRODUCT.get(args.product.upper(), 3.07)

    files = sorted(tick_dir(args.product).glob("*.json"))
    days = []
    for p in files:
        B = load_day(p)
        if B:
            days.append((p.stem, B))
    print(f"{args.product}: {len(days)}/{len(files)} 天可用（ex-ante 近月 · 日盤 08:45–13:45）")
    if not days:
        return 1
    print(f"期間 {days[0][0]} → {days[-1][0]} · 成本 {COST_PTS} 點/趟 · 最多等 {args.wait} 根\n")

    half = len(days) // 2
    out: dict = {"product": args.product, "n_days": len(days),
                 "period": [days[0][0], days[-1][0]], "cost_pts": COST_PTS,
                 "wait_bars": args.wait, "offsets": {}}
    print(f"{'掛單距離':<10}{'趟數':>6}{'成交率':>8}{'毛/趟':>9}{'SE':>7}{'淨/趟':>9}"
          f"{'勝率':>7}{'前半毛':>9}{'後半毛':>9}{'同號':>6}")
    for x in OFFSETS:
        allg: list[float] = []
        miss = 0
        h1: list[float] = []
        h2: list[float] = []
        for k, (day, B) in enumerate(days):
            g, m = run(B, x, args.wait)
            allg += g
            miss += m
            (h1 if k < half else h2).append(sum(g) / len(g) if g else 0.0)
        s = summarize(allg, miss)
        if not s.get("n"):
            continue
        m1 = st.mean([v for v in h1 if v]) if any(h1) else float("nan")
        m2 = st.mean([v for v in h2 if v]) if any(h2) else float("nan")
        same = "✓" if (m1 - COST_PTS) * (m2 - COST_PTS) > 0 else "✗"
        out["offsets"][str(x)] = s | {"half1_gross": round(m1, 3), "half2_gross": round(m2, 3)}
        lbl = "市價" if x == 0 else f"−{x} 點"
        print(f"{lbl:<10}{s['n']:>6}{s['fill_pct']:>7.0f}%{s['gross_pts']:>+9.2f}"
              f"{s['se_pts']:>7.2f}{s['net_pts']:>+9.2f}{s['win_pct']:>6.1f}%"
              f"{m1:>+9.2f}{m2:>+9.2f}{same:>6}")

    # 逐年拆解——跨年穩定性是這次的主判準
    years = sorted({d[:4] for d, _ in days})
    if len(years) > 1:
        print(f"\n=== 逐年毛額/趟（成本 {COST_PTS}）===")
        print(f"{'掛單距離':<10}" + "".join(f"{y:>9}" for y in years) + f"{'為正年數':>10}")
        for x in OFFSETS:
            row = {}
            for y in years:
                g = []
                for d, B in days:
                    if d[:4] != y:
                        continue
                    gg, _ = run(B, x, args.wait)
                    g += gg
                row[y] = st.mean(g) if g else float("nan")
            pos = sum(1 for y in years if row[y] == row[y] and row[y] > COST_PTS)
            lbl = "市價" if x == 0 else f"−{x} 點"
            print(f"{lbl:<10}" + "".join(f"{row[y]:>+9.2f}" if row[y] == row[y] else f"{'—':>9}"
                                        for y in years) + f"{pos:>7}/{len(years)}")
            out["offsets"].setdefault(str(x), {})["by_year"] = {
                y: (round(row[y], 3) if row[y] == row[y] else None) for y in years}

    best = max(out["offsets"].items(), key=lambda kv: kv[1].get("gross_pts", -99))
    print(f"\n峰在 −{best[0]} 點（毛 {best[1]['gross_pts']:+.2f}）· "
          f"前半 {best[1]['half1_gross']:+.2f} / 後半 {best[1]['half2_gross']:+.2f}")
    print("判準：峰位置在前後半段要落在同一格、且形狀單調（劑量反應）才算結構。")
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
