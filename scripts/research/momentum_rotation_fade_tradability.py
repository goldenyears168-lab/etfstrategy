"""反向(fade) edge 能不能付得起自己的成本 —— 三項切片分析.

背景（2026-08-20/21）：micro VCP 那套「3 秒 coil + 3 分鐘趨勢 + 2.5× 爆量」訊號，
在時刻配對對照組下每個持有期都是**顯著負 edge**（1s −7.74bps t=−5.75、5s −7.08
t=−4.43、8s −6.18 t=−3.59，15s 之後掉到邊緣）。也就是它是反轉前兆而非延續前兆。

但全體平均的反向毛額 +7.7bps **連全市場最便宜的標的（小型緯穎 8.4bps）都養不起**。
所以真正的問題不是「反向有沒有 edge」，而是「**有沒有哪個子集的 edge 大過它自己的
成本**」。三項：
  1. 逐檔 fade_gross / tick_cost 比值——全體平均會掩蓋「少數標的很強、多數是 0」
  2. edge 會不會隨爆量幅度放大——若成立就能只做極端事件，用更少交易換更大單筆
  3. 可執行持有期（1 秒送不出單，看 5/8/15 秒）還剩多少

紀律：依股票切 train/holdout（種子 42，與先前同一切分）、檢查單筆主導、
成本一律用**結構性**的跳動單位/價格（交易所級距表決定，零 look-ahead），
不用 Roll（同一段資料估出來、且實測低估 3.3 倍）。
"""
from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

REC = Path("reports/research/momentum_rotation_signal_records.json")
SEED = 42
HZ = ["1", "2", "3", "5", "8", "15", "30"]


def tw_tick(p: float) -> float:
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0


def tstat(a: list[float], b: list[float]) -> float:
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return (statistics.mean(a) - statistics.mean(b)) / se if se > 0 else float("nan")


def main() -> None:
    d = json.load(open(REC))
    sig, ctl = d["signals"], d["controls"]
    # 成本：用該檔所有訊號當下價格的中位數推跳動單位
    px_by_code = defaultdict(list)
    for r in sig:
        px_by_code[r["code"]].append(r["px"])
    cost = {c: tw_tick(statistics.median(v)) / statistics.median(v) * 1e4
            for c, v in px_by_code.items()}

    codes = sorted({r["code"] for r in sig})
    rng = random.Random(SEED)
    sh = codes[:]; rng.shuffle(sh)
    train, hold = set(sh[:len(sh) // 2]), set(sh[len(sh) // 2:])
    print(f"訊號 {len(sig)} · 對照 {len(ctl)} · {len(codes)} 檔 "
          f"（train {len(train)} / holdout {len(hold)}，種子 {SEED}）\n")

    def fade(rows, hz):   # 反做的毛報酬 = 訊號方向報酬取負號
        return [-r[hz] for r in rows if r.get(hz) is not None]

    print("=" * 92)
    print("=== 1. 逐檔：反向毛額 vs 該檔自己的成本（只列訊號 ≥8 筆者）===")
    print(f"{'代碼':6s}{'n':>4s}{'成本bps':>9s}" + "".join(f"{h+'s毛額':>10s}" for h in ["1", "5", "8"])
          + f"{'8s淨額':>9s}{'比值':>7s}")
    print("-" * 92)
    rows_out = []
    for c in codes:
        rs = [r for r in sig if r["code"] == c]
        if len(rs) < 8:
            continue
        g = {h: statistics.mean(fade(rs, h)) if fade(rs, h) else float("nan") for h in ["1", "5", "8"]}
        net8 = g["8"] - cost[c]
        rows_out.append((c, len(rs), cost[c], g, net8, g["8"] / cost[c] if cost[c] else 0,
                         "train" if c in train else "holdout"))
    rows_out.sort(key=lambda x: -x[5])
    for c, n, cs, g, net8, ratio, grp in rows_out[:20]:
        print(f"{c:6s}{n:>4d}{cs:>9.1f}{g['1']:>10.1f}{g['5']:>10.1f}{g['8']:>10.1f}"
              f"{net8:>9.1f}{ratio:>7.2f}  {grp}")
    pos = [r for r in rows_out if r[5] > 1.0]
    print(f"\n  比值 >1（8 秒反向毛額 > 自己成本）的：{len(pos)}/{len(rows_out)} 檔"
          f"  其中 holdout 組 {sum(1 for r in pos if r[6] == 'holdout')} 檔")

    print("\n" + "=" * 92)
    print("=== 2. edge 會不會隨爆量幅度放大 ===")
    mv = sorted(r["move_bps"] for r in sig)
    cuts = [mv[len(mv) * k // 4] for k in (1, 2, 3)]
    print(f"    move_bps 四分位切點: {cuts[0]:.0f} / {cuts[1]:.0f} / {cuts[2]:.0f} bps")
    print(f"{'分組':>22s}{'n':>6s}" + "".join(f"{h+'s':>12s}" for h in ["1", "5", "8", "15"]))
    bins = [(0, cuts[0]), (cuts[0], cuts[1]), (cuts[1], cuts[2]), (cuts[2], 1e9)]
    for lo, hi in bins:
        rs = [r for r in sig if lo <= r["move_bps"] < hi]
        cells = []
        for h in ["1", "5", "8", "15"]:
            f_, cf = fade(rs, h), fade(ctl, h)
            cells.append(f"{statistics.mean(f_):>+7.1f}(t{tstat(f_, cf):>+4.1f})" if len(f_) >= 20 else "        n/a")
        print(f"{f'{lo:.0f}~{hi:.0f}bps':>22s}{len(rs):>6d}" + "".join(f"{x:>12s}" for x in cells))

    print("\n" + "=" * 92)
    print("=== 3. holdout 組的可執行持有期 + 單筆主導檢查 ===")
    hs = [r for r in sig if r["code"] in hold]
    hc = [r for r in ctl if r["code"] in hold]
    print(f"{'持有':>6s}{'n':>6s}{'反向毛額':>12s}{'t值':>8s}{'最大單筆佔比':>14s}{'拿掉最大一筆':>14s}")
    for h in HZ:
        f_, cf = fade(hs, h), fade(hc, h)
        if len(f_) < 20:
            continue
        tot = sum(f_)
        mx = max(f_)
        drop = (tot - mx) / (len(f_) - 1)
        print(f"{h+'s':>6s}{len(f_):>6d}{statistics.mean(f_):>+12.2f}{tstat(f_, cf):>8.2f}"
              f"{(mx / tot * 100 if tot else float('nan')):>13.0f}%{drop:>+14.2f}")

    print("\n  參考成本線：小型緯穎 8.4 / 白名單其餘 9.7~10.9 / 8-13 實彈那 12 檔 13~48.7 bps")


if __name__ == "__main__":
    main()
