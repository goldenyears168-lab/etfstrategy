"""限價出場的成交率與逆選擇 —— 省下 1.50 點滑價，代價是什麼？

背景（2026-08-25 實測）：市價出場滑價 = 半個價差，微台/小台皆 1.50 點、大台 2.00 點，
三者都精確等於理論下界（最佳檔深度中位 1~2 口，1 口單不會吃穿）。加上這一項後：
    微台 ×2.0  毛 2.86 − 成本 6.35 = −3.49/筆
    小台 ×2.0  毛 2.86 − 成本 3.95 = −1.09/筆   ← 最接近，但仍負
成本三項裡期交稅 1.85 是尺度不變的硬地板、手續費已壓到 0.60（小台），
**市價滑價 1.50 是唯一還有結構性空間的一項**（78% 的出場走市價）。
若改限價出場省下 1.50，小台成本 → 2.45，每筆變 +0.41。

但限價出場不是免費的。兩個代價必須一起量，不能只看成交率：
  A. 排不到隊 → 到期限被迫市價平掉，滑價照付，還多賠了等待期間的價格移動
  B. **逆選擇**：對多單的限價賣出，價格漲才會成交（有利），價格跌就卡住（不利）
     —— 賺錢的單容易出、賠錢的單出不掉。這可能把省下的 1.50 全部吃掉甚至更多。

模型：在取樣時刻掛在最佳檔（多單出場＝掛賣一），排在該檔現有量之後；
之後累積「打進這個價位的對手方成交量」超過排隊量才算成交。未成交則在期限
以市價平掉（付半個價差 + 期間價格移動）。
"""
from __future__ import annotations

import bisect
import json
import statistics
from pathlib import Path

CACHE = Path.home() / "goldenstocks-data/cache"
ROOTS = {"tmf": ("微台 TMF", 10), "mxf": ("小台 MXF", 50)}
HORIZONS = [10, 30, 60, 300]
SAMPLE_EVERY = 30          # 每 30 秒取一個出場時點


def load(root: str):
    trades = []
    for f in sorted((CACHE / f"{root}_trades").glob("*.jsonl")):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("stale"):
                continue
            b, a, p = d.get("bid"), d.get("ask"), d.get("price")
            if not b or not a or not p or a <= b:
                continue
            t = d["ts"]
            sec = (int(t[11:13]) * 3600 + int(t[14:16]) * 60 + int(t[17:19]))
            trades.append((t[:10], sec, float(p), int(d.get("size") or 0), float(b), float(a)))
    trades.sort(key=lambda x: (x[0], x[1]))
    books = {}
    for f in sorted((CACHE / f"{root}_books").glob("*.jsonl")):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event") != "data" or d.get("stale"):
                continue
            bb = d.get("bids") or []; aa = d.get("asks") or []
            if not bb or not aa:
                continue
            t = d["ts"]
            sec = int(t[11:13]) * 3600 + int(t[14:16]) * 60 + int(t[17:19])
            books.setdefault(t[:10], []).append((sec, bb[0]["size"], aa[0]["size"]))
    for k in books:
        books[k].sort()
    return trades, books


def main() -> None:
    for root, (label, ptv) in ROOTS.items():
        trades, books = load(root)
        if len(trades) < 5000:
            print(f"{label}: 樣本不足\n"); continue
        byday = {}
        for r in trades:
            byday.setdefault(r[0], []).append(r)
        print(f"=== {label} · {len(byday)} 天 · {len(trades):,} 筆成交 ===")
        res = {h: {"fill": 0, "n": 0, "unfilled_drift": [], "filled_drift": []} for h in HORIZONS}
        for day, rows in byday.items():
            secs = [r[1] for r in rows]
            bk = books.get(day) or []
            bsec = [x[0] for x in bk]
            if not bk:
                continue
            t0, t1 = secs[0], secs[-1]
            for t in range(t0, t1 - max(HORIZONS), SAMPLE_EVERY):
                i = bisect.bisect_left(secs, t)
                if i >= len(rows):
                    continue
                _, _, p0, _, b0, a0 = rows[i]
                mid0 = (a0 + b0) / 2
                j = bisect.bisect_left(bsec, t) - 1
                if j < 0:
                    continue
                q_ask = bk[j][2]          # 賣一排隊量（多單出場掛賣一，排在它後面）
                for h in HORIZONS:
                    e = res[h]; e["n"] += 1
                    cum = 0; filled = False
                    k = i
                    while k < len(rows) and rows[k][1] <= t + h:
                        _, _, pk, sk, _, ak = rows[k]
                        if pk >= a0:      # 有人以 >= 我的掛單價買進 → 消耗我前面的隊列
                            cum += sk
                            if cum > q_ask:
                                filled = True
                                break
                        k += 1
                    kk = bisect.bisect_left(secs, t + h)
                    kk = min(kk, len(rows) - 1)
                    mid_end = (rows[kk][4] + rows[kk][5]) / 2
                    drift = mid_end - mid0        # 中價漂移（對多單而言，正=有利）
                    if filled:
                        e["fill"] += 1; e["filled_drift"].append(drift)
                    else:
                        e["unfilled_drift"].append(drift)
        print(f"{'期限':>6s}{'成交率':>9s}{'成交單的中價漂移':>18s}{'未成交單的中價漂移':>20s}{'樣本':>9s}")
        print("-" * 66)
        for h in HORIZONS:
            e = res[h]
            if not e["n"]:
                continue
            fd = statistics.mean(e["filled_drift"]) if e["filled_drift"] else float("nan")
            ud = statistics.mean(e["unfilled_drift"]) if e["unfilled_drift"] else float("nan")
            print(f"{str(h) + 's':>6s}{e['fill'] / e['n'] * 100:>8.1f}%{fd:>+18.2f}{ud:>+20.2f}{e['n']:>9d}")
        # 成本比較（以多單出場為例）
        h = 60
        e = res[h]
        if e["n"]:
            p = e["fill"] / e["n"]
            ud = statistics.mean(e["unfilled_drift"]) if e["unfilled_drift"] else 0.0
            half = 1.5 if root != "txf" else 2.0
            cost_mkt = half
            cost_lim = (1 - p) * (half - ud)     # 未成交者：付半價差，且承受漂移
            print(f"\n  以 {h} 秒期限、多單出場為例：")
            print(f"    直接市價出場       成本 {cost_mkt:.2f} 點")
            print(f"    限價出場(未成交轉市價) 成本 {cost_lim:.2f} 點"
                  f"   (成交率 {p*100:.0f}%、未成交者平均漂移 {ud:+.2f})")
            print(f"    → 省下 {cost_mkt - cost_lim:+.2f} 點/筆")
        print()


if __name__ == "__main__":
    main()
