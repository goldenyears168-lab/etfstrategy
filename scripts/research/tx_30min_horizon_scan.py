"""30 分鐘持有期：哪些進場條件能讓期望值跨過成本線？

背景（2026-08-25/26 實測）：
  · 成本地板 3.95 點（小台×2.0：手續費 0.60 + 期交稅 1.85 + 市價出場滑價 1.50）
  · 成本佔比隨持有期衰減：1分 44% → 10分 14% → **30分 8%** → 1小時 6% → 全日 1%
  · 現行 PV16 持倉中位 4 分鐘，成本佔比 25~30%，所以毛額 2.86 追不上成本 3.95
  · 均值回歸的 OU 半衰期日盤 29 分／夜盤 96 分——30 分鐘正好對上這個現象的自然尺度
→ 目標持有期定在 30 分鐘。

方法上刻意跟過去八輪相反：**不先設計訊號再測**（那八次全部樣本外失敗），
而是先算好未來 30 分鐘報酬，再廣掃「當下有什麼特徵能預測它」。

紀律（全部來自 2026-08-13~25 那幾輪的教訓）：
  1. 一律用**每分鐘 VWAP**——順序無關，繞開 archive 秒內按價格排序的問題
  2. **C 檢定內建**：進場延後 1 分鐘（訊號在 t 分算出，t+1 分才進場）
  3. 特徵全部 PIT：只用 ≤t 的資料
  4. 依**時間**切 train/holdout（前半訓練、後半驗證），不是隨機切
  5. 判準不是 t>2，是**淨額要跨過 3.95 點成本線**
  6. 明列掃了幾個特徵（多重檢定意識），並報單筆主導
"""
from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

TX = Path.home() / "goldenstocks-data/cache/momentum_rotation/taifex_tick_daily/TX_TX.csv"
HOLD_MIN = 30
ENTRY_DELAY_MIN = 1
COST_PTS = 3.95
FEATS = ["range_pos", "rvol30", "dev_ma60", "mom5", "mom30", "vol_ratio", "tod_min", "spread_proxy"]


def minute_bars():
    """{date: (minutes, vwap, vol)}——每分鐘 VWAP，只取當日主力契約。"""
    per_day_contract = defaultdict(lambda: defaultdict(int))
    raw = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in csv.DictReader(open(TX)):
        cd = r.get("contract_date") or ""
        if "/" in cd:
            continue
        d = r["date"][:10]
        per_day_contract[d][cd] += 1
    main = {d: max(c, key=c.get) for d, c in per_day_contract.items()}
    for r in csv.DictReader(open(TX)):
        cd = r.get("contract_date") or ""
        if "/" in cd:
            continue
        d = r["date"][:10]
        if cd != main.get(d):
            continue
        try:
            p = float(r["price"]); v = float(r["volume"])
        except (KeyError, ValueError, TypeError):
            continue
        if p <= 0:
            continue
        m = int(r["date"][11:13]) * 60 + int(r["date"][14:16])
        cell = raw[d][m]
        cell[0] += p * v; cell[1] += v
    out = {}
    for d, mm in raw.items():
        ms = sorted(mm)
        vw = np.array([mm[m][0] / mm[m][1] if mm[m][1] else np.nan for m in ms])
        vol = np.array([mm[m][1] for m in ms])
        out[d] = (np.array(ms), vw, vol)
    return out


def main() -> None:
    print("建每分鐘 VWAP（只取當日主力契約）...", flush=True)
    bars = minute_bars()
    days = sorted(bars)
    print(f"  {len(days)} 天 · {sum(len(bars[d][0]) for d in days):,} 根分鐘 K\n")
    rows = []
    for d in days:
        ms, vw, vol = bars[d]
        idx = {m: i for i, m in enumerate(ms)}
        med_vol = float(np.median(vol)) if len(vol) else 1.0
        for i, m in enumerate(ms):
            j_ent = idx.get(m + ENTRY_DELAY_MIN)
            j_exit = idx.get(m + ENTRY_DELAY_MIN + HOLD_MIN)
            i60 = idx.get(m - 60); i30 = idx.get(m - 30); i5 = idx.get(m - 5)
            if None in (j_ent, j_exit, i60, i30, i5):
                continue
            win60 = vw[i60:i + 1]; win30 = vw[i30:i + 1]
            rng = float(win60.max() - win60.min())
            if rng <= 0 or not np.isfinite(vw[i]):
                continue
            rr = np.diff(win30) / win30[:-1]
            rows.append({
                "date": d, "m": int(m),
                "range_pos": float((vw[i] - win60.min()) / rng),
                "rvol30": float(np.std(rr) * 1e4) if len(rr) > 3 else np.nan,
                "dev_ma60": float((vw[i] - win60.mean()) / vw[i] * 1e4),
                "mom5": float((vw[i] - vw[i5]) / vw[i5] * 1e4),
                "mom30": float((vw[i] - vw[i30]) / vw[i30] * 1e4),
                "vol_ratio": float(vol[i] / med_vol) if med_vol else np.nan,
                "tod_min": float(m),
                "spread_proxy": float(rng / vw[i] * 1e4),
                "y": float(vw[j_exit] - vw[j_ent]),          # 未來 30 分鐘的**點數**變動
            })
    rows = [r for r in rows if all(np.isfinite(r[f]) for f in FEATS) and np.isfinite(r["y"])]
    print(f"樣本 {len(rows):,} 個進場時點（已延後 1 分鐘進場）")
    cut = days[len(days) // 2]
    tr = [r for r in rows if r["date"] < cut]
    ho = [r for r in rows if r["date"] >= cut]
    print(f"train {len(tr):,}（{days[0]}~）   holdout {len(ho):,}（{cut}~{days[-1]}）")
    print(f"未來 30 分鐘變動：|中位| {statistics.median([abs(r['y']) for r in rows]):.1f} 點"
          f"   成本線 {COST_PTS} 點\n")

    def ic(rs, f):
        a = [r[f] for r in rs]; b = [r["y"] for r in rs]
        ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
        return float(np.corrcoef(ra, rb)[0, 1])

    def quint(rs, f, sign):
        s = sorted(rs, key=lambda r: r[f] * sign)
        q = len(s) // 5
        lo = [r["y"] for r in s[:q]]; hi = [r["y"] for r in s[-q:]]
        # 多做最高分位、空最低分位；各自付一次來回成本
        gross = statistics.mean(hi) - statistics.mean(lo)
        se = math.sqrt(statistics.variance(hi) / len(hi) + statistics.variance(lo) / len(lo))
        return gross, gross - 2 * COST_PTS, (gross / se if se else float("nan")), len(hi)

    print("=" * 92)
    print(f"=== train 上各特徵的個別預測力（掃了 {len(FEATS)} 個特徵）===")
    print(f"{'特徵':>14s}{'IC':>9s}{'五分位毛額':>12s}{'扣成本後':>11s}{'t值':>8s}")
    print("-" * 92)
    passed = []
    for f in FEATS:
        v = ic(tr, f)
        sign = 1 if v >= 0 else -1
        g, n, t, _ = quint(tr, f, sign)
        mark = ""
        if n > 0 and abs(t) >= 2:
            passed.append((f, sign)); mark = "   ← 進 holdout"
        print(f"{f:>14s}{v:>+9.3f}{g:>+12.2f}{n:>+11.2f}{t:>+8.2f}{mark}")
    if not passed:
        print("\n→ train 上沒有任何特徵的五分位淨額為正且 t>=2。**不進 holdout，不做組合。**")
        print("   （在 train 就跨不過成本線的特徵，拿去 holdout 只是多一次擲骰子）")
        return
    print(f"\n=== holdout 驗證（{len(passed)} 個特徵通過 train）===")
    print(f"{'特徵':>14s}{'五分位毛額':>12s}{'扣成本後':>11s}{'t值':>8s}{'n':>8s}")
    for f, sign in passed:
        g, n, t, cnt = quint(ho, f, sign)
        print(f"{f:>14s}{g:>+12.2f}{n:>+11.2f}{t:>+8.2f}{cnt:>8d}")


if __name__ == "__main__":
    main()
