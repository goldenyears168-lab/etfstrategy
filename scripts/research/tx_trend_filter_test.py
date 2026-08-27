#!/usr/bin/env python3
"""趨勢過濾能不能救 2023？——預先登記的檢定，全部變體都回報。

背景
----
2026-08-25/26：嚴格事前規則（只看 ≤ 當根 K，進出場用下一根開盤）在 TX 834 天
4,209 趟上的結果是：**市價進場淨值恰好 0.00 點/趟**（毛 2.42 ＝ 成本 2.42），
把進場改成掛限價等，淨值單調升到 −30 點的 +5.32。掛單距離是真效果、跨年 5/6。

但逐年拆解揭穿一件事：**2023 整欄為負，而且掛越遠賠越多**
（市價 +0.21 / −10點 −1.42 / −20點 −5.52 / −30點 −2.59 / −50點 −46.12）。
其他五年全正。

機制解釋（這是**先驗**，不是事後編的）：這套規則賺的不是方向預測（市價版
edge = 0），而是**流動性提供**——掛遠等別人來打你。流動性提供的天敵是單邊
趨勢：趨勢日裡價格不會回來碰你的遠端掛單，只有在**趨勢反轉那幾天**才會成交，
於是變成純負選擇。2023 正是台股單邊上漲年。

所以假設是：**用一個事前的趨勢強度指標把趨勢時段濾掉，應該能救 2023 而不
破壞其他五年。** 若 2023 沒救起來、或其他年份被拖垮，就是這個解釋錯了。

【紀律】
  · 固定進場距離（不再掃 offset），避免把「找濾網」和「找距離」複合成雙重挖掘
  · 三個濾網變體 × 三個門檻，**全部回報**，不只報最好的
  · 判準：2023 轉正 **且** 其他五年不變差（不是只看總和）
"""
from __future__ import annotations

import argparse
import json
import pickle
import statistics as st
from pathlib import Path

import numpy as np

COST = 2.42
DEFAULT_OFFSET = 30.0
WAIT = 10


def cache_path() -> Path:
    return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "tx_1m_bars_daysession.pkl"


def signals(B: dict) -> tuple[list[bool], list[bool]]:
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


def eff_ratio(C: np.ndarray, i: int, w: int = 60) -> float:
    """Kaufman 效率比：|淨移動| / Σ|逐根移動|。1＝完美趨勢、0＝純震盪。只用 ≤ i。"""
    j = max(0, i - w)
    seg = C[j:i + 1]
    if len(seg) < 10:
        return 0.0
    noise = float(np.abs(np.diff(seg)).sum())
    return abs(float(seg[-1] - seg[0])) / noise if noise > 0 else 0.0


def vwap_slope_bps(B: dict, i: int, w: int = 60) -> float:
    VW = B["VWAP"]
    j = max(0, i - w)
    return abs(float(VW[i] - VW[j])) / max(float(VW[j]), 1e-9) * 1e4


def run(B: dict, x: float, gate) -> tuple[list[float], int, int]:
    """gate(i) → True 表示**允許**進場。回傳 (每趟損益, 掛了沒成交, 被濾掉的訊號數)"""
    O, L, n = B["O"], B["L"], B["n"]
    bs, ss = signals(B)
    legs: list[float] = []
    miss = blocked = 0
    pos = None
    pend = None
    for i in range(n - 1):
        if pos is None and pend is None and bs[i]:
            if not gate(i):
                blocked += 1
            elif x == 0:
                pos = O[i + 1]
            else:
                pend = (O[i + 1] - x, i + 1 + WAIT)
        elif pend is not None:
            lim, dead = pend
            if ss[i]:
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
    return legs, miss, blocked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offset", type=float, default=DEFAULT_OFFSET)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    days = pickle.load(cache_path().open("rb"))
    years = sorted({d[:4] for d, _ in days})
    print(f"TX {len(days)} 天 · {days[0][0]} → {days[-1][0]} · "
          f"進場固定 −{args.offset:.0f} 點 · 成本 {COST}\n")

    # 預先登記的濾網：全部回報
    variants: list[tuple[str, object]] = [("無濾網（基準）", lambda B: (lambda i: True))]
    for th in (0.30, 0.40, 0.50):
        variants.append((f"效率比 < {th:.2f}（濾掉趨勢）",
                         (lambda th_: (lambda B: (lambda i: eff_ratio(B["C"], i) < th_)))(th)))
    for th in (8.0, 12.0, 20.0):
        variants.append((f"VWAP 斜率 < {th:.0f} bps",
                         (lambda th_: (lambda B: (lambda i: vwap_slope_bps(B, i) < th_)))(th)))
    for th in (0.30, 0.40):
        variants.append((f"效率比 < {th:.2f} 且 VWAP 斜率 < 12",
                         (lambda th_: (lambda B: (lambda i: eff_ratio(B["C"], i) < th_
                                                  and vwap_slope_bps(B, i) < 12.0)))(th)))

    out: dict = {"offset": args.offset, "cost": COST, "n_days": len(days), "variants": {}}
    hdr = f"{'濾網':<26}{'趟數':>6}{'毛/趟':>8}{'淨/趟':>8}" + "".join(f"{y:>8}" for y in years) + f"{'正年':>6}"
    print(hdr)
    for name, mk in variants:
        per_year: dict[str, list[float]] = {y: [] for y in years}
        allg: list[float] = []
        for d, B in days:
            g, _, _ = run(B, args.offset, mk(B))
            allg += g
            per_year[d[:4]] += g
        if not allg:
            continue
        gm = st.mean(allg)
        ys = {y: (st.mean(v) if v else float("nan")) for y, v in per_year.items()}
        pos = sum(1 for y in years if ys[y] == ys[y] and ys[y] > COST)
        print(f"{name:<26}{len(allg):>6}{gm:>+8.2f}{gm - COST:>+8.2f}"
              + "".join(f"{ys[y]:>+8.2f}" if ys[y] == ys[y] else f"{'—':>8}" for y in years)
              + f"{pos:>4}/{len(years)}")
        out["variants"][name] = {
            "n": len(allg), "gross": round(gm, 3), "net": round(gm - COST, 3),
            "se": round(st.stdev(allg) / len(allg) ** 0.5, 3) if len(allg) > 1 else None,
            "by_year": {y: (round(ys[y], 3) if ys[y] == ys[y] else None) for y in years},
            "n_by_year": {y: len(v) for y, v in per_year.items()},
        }
    print(f"\n判準：2023 要轉正（> {COST}）**且**其他五年不變差。只有總和變好不算。")
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
