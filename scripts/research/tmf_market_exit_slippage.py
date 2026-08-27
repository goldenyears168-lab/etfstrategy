"""市價出場滑價實測 —— 微台/小台/大台，用 websocket 逐筆自帶的 bid/ask.

為什麼做這個：commit a1d50ce 找到唯一的正期望值路徑是「掛單距離×2 ＋ 搬到小台」，
    微台 ×2.0   毛 2.86 / 成本 4.05 = −1.19/筆
    小台 ×2.0   毛 2.86 / 成本 1.65 = +1.21/筆  → +NT$2,929/日
但那個 1.65 **沿用微台量到的限價滑價**，而 78% 的出場是市價單、市價滑價從未量過。
該 commit 自己標註：「若市價出場付掉半個價差，小台成本 → 2.85、每筆 → +0.01，
剛好損益兩平。整個結論押在那一個還沒量到的數字上。」本腳本就是去量那個數字。

資料：cache/{root}_trades/*.jsonl 每筆帶成交當下的 bid/ask（真實可成交價，
不是 VWAP 那種拿不到的統計量——見 memory fade-edge-refuted-by-delay-test）。
搭配 cache/{root}_books/ 檢查最佳檔深度夠不夠吃下 1 口。

量三件事：
  1. 價差分佈（點）——市價單至少付半個價差
  2. 成交價相對中價的偏離 |price-mid|——實際付出的滑價
  3. 最佳檔深度 vs 1 口——會不會吃穿第一檔（穿了就付更多）
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

CACHE = Path.home() / "goldenstocks-data/cache"
ROOTS = {"tmf": ("微台 TMF", 10), "mxf": ("小台 MXF", 50), "txf": ("大台 TXF", 200)}
TAX_PTS_PER_SIDE = 0.924      # 期交稅（點數上尺度不變，見 a1d50ce）
FEE_NTD_PER_SIDE = 15.0       # 每邊手續費（NT$）


def pct(v, p):
    s = sorted(v)
    return s[int(len(s) * p)] if s else float("nan")


def main() -> None:
    for root, (label, pt_value) in ROOTS.items():
        tf = sorted((CACHE / f"{root}_trades").glob("*.jsonl"))
        bf = sorted((CACHE / f"{root}_books").glob("*.jsonl"))
        if not tf:
            print(f"{label}: 無資料\n"); continue
        spreads, slips, sizes = [], [], []
        aggr = defaultdict(int)
        days = set()
        for f in tf:
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
                days.add(d["ts"][:10])
                sp = a - b
                mid = (a + b) / 2
                spreads.append(sp)
                slips.append(abs(p - mid))
                sizes.append(int(d.get("size") or 0))
                aggr["買方主動" if p >= a else ("賣方主動" if p <= b else "價差內")] += 1
        if len(spreads) < 200:
            print(f"{label}: 樣本不足（{len(spreads)}）\n"); continue
        # 最佳檔深度
        depth = []
        for f in bf:
            for line in open(f):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("event") != "data" or d.get("stale"):
                    continue
                bb = d.get("bids") or []; aa = d.get("asks") or []
                if bb and aa:
                    depth.append(min(bb[0]["size"], aa[0]["size"]))
        n = len(spreads)
        med_sp = statistics.median(spreads)
        med_sl = statistics.median(slips)
        fee_pts = FEE_NTD_PER_SIDE / pt_value
        print(f"=== {label}（NT${pt_value}/點）· {len(days)} 天 · {n:,} 筆成交 ===")
        print(f"  價差(點)     中位 {med_sp:.2f}   p25 {pct(spreads,.25):.2f}   p75 {pct(spreads,.75):.2f}   平均 {statistics.mean(spreads):.2f}")
        print(f"  |成交-中價|  中位 {med_sl:.2f}   p75 {pct(slips,.75):.2f}   平均 {statistics.mean(slips):.2f}   ← 市價單實付滑價")
        print(f"  半個價差     {med_sp/2:.2f} 點（理論下界）→ 實測 {med_sl:.2f} 點"
              f"{'（一致）' if abs(med_sl-med_sp/2)<0.3 else '（偏離，可能吃穿第一檔）'}")
        if depth:
            print(f"  最佳檔深度   中位 {statistics.median(depth):.0f} 口   "
                  f"< 1 口的比例 {sum(1 for x in depth if x < 1)/len(depth)*100:.1f}%")
        print(f"  主動方向     " + "  ".join(f"{k}:{v/n*100:.0f}%" for k, v in aggr.items()))
        # 成本試算：來回 = 2×手續費 + 2×稅 + 出場市價滑價（進場限價假設 0）
        cost_limit_exit = 2 * fee_pts + 2 * TAX_PTS_PER_SIDE
        cost_mkt_exit = cost_limit_exit + med_sl
        print(f"\n  來回成本試算（點）：手續費 {2*fee_pts:.2f} + 期交稅 {2*TAX_PTS_PER_SIDE:.2f} = {cost_limit_exit:.2f}")
        print(f"    ＋市價出場滑價 {med_sl:.2f}  →  **{cost_mkt_exit:.2f} 點**")
        for label2, gross in (("掛單距離×1.0", 1.44), ("掛單距離×2.0", 2.86)):
            net = gross - cost_mkt_exit
            print(f"    {label2}: 毛 {gross:.2f} − 成本 {cost_mkt_exit:.2f} = {net:+.2f} 點/筆"
                  f"  ({net*pt_value*48.3:+,.0f} NT$/日 @48.3筆)")
        print()


if __name__ == "__main__":
    main()
