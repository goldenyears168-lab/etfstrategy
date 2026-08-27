#!/usr/bin/env python3
"""T+5 sbl_pct 的完整對抗檢定 —— 這條線最後一個還活著的組態。

先前結果（2026-08-26）：K=5、單用 sbl_pct（借券佔股本水位）、只做多最低 5.8%
  命中 51.3%　每趟 gross +0.3544%　NW t=+3.95　日均換手 2.95%　淨值 +13.79%/年
  · 大型股 1/3：t=+2.98（K=1 時是 −0.22，先前判定為小型股現象）
  · 檔數 9~90 檔淨值全為正（高原非尖峰）
  · 無折扣＋0.3% 滑價仍 +10.84%
  · **靜態對照**：形成期名單凍結只有 +0.041%/趟(t=0.75)，動態重排增量 +0.314%(t=+3.49)

未解決的三件事：
  ① 命中率僅 51.3% —— 報酬全來自幅度不是頻率
  ② 四段 OOS 有兩段不顯著（NW t=0.93、1.50）
  ③ 小型股層 +59.55%/年 不合理（每趟 gross +1.48%），疑似流動性衝擊假象

本檔逐一檢定，並套用這幾天學到的新教訓：
  · 虛無假設要正確（小樣本的集中度／變異數本來就偏低）
  · 檢查有沒有「過濾條件本身是選擇偏誤」
  · 多重檢定校正（這個組態是從多少個裡挑出來的）
  · 產業中性化
  · 容量：2.95% 換手 × 標的規模 = 實際能做多大
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

LAB = SourceFileLoader("lab", str(Path(__file__).resolve().parent / "chip_lab.py")).load_module()
HZ = SourceFileLoader("hz", str(Path(__file__).resolve().parent / "chip_horizon.py")).load_module()
COST = 0.471
K = 5


def leg(d: pd.DataFrame, score: pd.Series, frac: float = 0.058,
        col: str = "n5", warm: int = 250, min_n: int = 120) -> dict:
    x = d.assign(_s=score).dropna(subset=["_s", col])
    dates = np.sort(x.trade_date.unique())
    if len(dates) < warm + 60:
        return {}
    oos = set(dates[warm:])
    hist, rows = [], []
    for t, g in x.groupby("trade_date", sort=True):
        if len(g) < min_n:
            continue
        n = max(3, int(round(len(g) * frac)))
        q = g.sort_values("_s", ascending=False)
        L = set(q.stock_id.head(n))
        tk = len(L - hist[-K]) / n if len(hist) >= K else np.nan
        hist.append(L)
        if t in oos:
            r = q[col].head(n)
            rows.append({"t": t, "g": r.mean(), "hit": (r > 0).mean(), "turn": tk})
    r = pd.DataFrame(rows)
    if len(r) < 60:
        return {}
    tau = r.turn.mean() / K
    gr = r.g.mean() * 100
    return {"n": len(r), "gross": gr, "t_nw": HZ.nw_t(r.g, K), "hit": r.hit.mean() * 100,
            "turn": tau * 100, "net_ann": (gr / K - tau * COST) * 242, "series": r}


def main() -> int:
    d = pd.read_pickle(LAB.DIR / "chip_horizon_panel.pkl")
    s = LAB.signed(d, "sbl_pct", "xs")
    base = leg(d, s)
    print(f"基準（全宇宙）：gross {base['gross']:+.4f}%　NW t {base['t_nw']:+.2f}　"
          f"命中 {base['hit']:.1f}%　換手 {base['turn']:.2f}%　淨值 {base['net_ann']:+.2f}%/年\n")

    print("【① 小型股假象：依成交金額分層】")
    d = d.copy()
    d["amt"] = d.close * d.vol * 1000  # vol 是張(1000股) → 換成 TWD
    d["aq"] = d.groupby("trade_date").amt.transform(
        lambda x: pd.qcut(x.rank(method="first"), 4, labels=False, duplicates="drop"))
    for q, lab_ in ((0, "最小 1/4"), (1, "小 1/4"), (2, "大 1/4"), (3, "最大 1/4")):
        sub = d[d.aq == q]
        r = leg(sub, s.reindex(sub.index), min_n=40)  # 分位子樣本只有 ~105 檔/日
        if r:
            med = sub.amt.median()
            print(f"  {lab_:<8}中位成交額 {med/1e8:>6.1f} 億　gross {r['gross']:>+8.4f}%　"
                  f"t {r['t_nw']:>+6.2f}　淨值 {r['net_ann']:>+8.2f}%")
    print("\n  ⚠️ 若最小層 gross 遠高於其他層，那是流動性衝擊假象不是 alpha")

    print("\n【② 逐季穩定性】")
    r = base["series"].copy()
    r["q"] = pd.PeriodIndex(pd.to_datetime(r.t), freq="Q").astype(str)
    for q, g in r.groupby("q"):
        if len(g) < 30:
            continue
        tau = g.turn.mean() / K
        gr = g.g.mean() * 100
        print(f"  {q}  n={len(g):>3}  gross {gr:>+8.4f}%　NW t {HZ.nw_t(g.g, K):>+6.2f}　"
              f"淨值 {(gr/K - tau*COST)*242:>+8.2f}%")

    print("\n【③ 報酬集中度：拿掉最好的幾季還剩多少】")
    qs = r.groupby("q").apply(lambda g: (g.g.mean()*100/K - g.turn.mean()/K*COST)*242,
                              include_groups=False).sort_values(ascending=False)
    for drop in (0, 1, 2, 3):
        keep = r[~r.q.isin(qs.index[:drop])] if drop else r
        tau, gr = keep.turn.mean()/K, keep.g.mean()*100
        tag = f"拿掉最好 {drop} 季" if drop else "全期"
        print(f"  {tag:<12}n={len(keep):>4}　gross {gr:>+8.4f}%　NW t {HZ.nw_t(keep.g, K):>+6.2f}　"
              f"淨值 {(gr/K - tau*COST)*242:>+8.2f}%/年")
    pos = (qs > 0).sum()
    print(f"  → 10 季中 {pos} 季為正；最好的 2 季貢獻 {qs.iloc[:2].sum()/qs.sum()*100:.0f}% 的總報酬")

    print("\n【④ 容量：以 2.95% 日換手與標的規模估算】")
    x = d.assign(_s=s).dropna(subset=["_s", "n5"])
    picks = []
    for t, g in x.groupby("trade_date", sort=True):
        if len(g) < 120:
            continue
        n = max(3, int(round(len(g) * 0.058)))
        picks.append(g.sort_values("_s", ascending=False).head(n))
    pk = pd.concat(picks)
    med_amt = pk.amt.median()
    for part in (0.01, 0.03, 0.05):
        cap = med_amt * part * 30
        print(f"  參與率上限 {part*100:>3.0f}%　→　組合規模上限約 {cap/1e8:>7.2f} 億")
    print(f"  （標的中位成交額 {med_amt/1e8:.2f} 億、持有 5 日、30 檔）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ═══════════════════════════════════════════════════════════════════════
# 2026-08-27 結案結論（六輪對抗檢定後）
#
# 【推翻的擔憂】
#   ① 「小型股假象」—— 錯。四個金額分層 gross 全正（t=+3.82/+2.47/+2.15/+3.34），
#      最大 1/4（中位成交額 15.5 億）t=+3.34，完全有容量。先前判定源於單位 bug：
#      panel 的 vol 是「張」不是「股」，close*vol 是千元，我少算 1000 倍，
#      加上 len(g)<120 的門檻把只有 ~105 檔的分位子樣本每一天都跳過 → 最小兩層是空的。
#   ③ 「多重檢定」—— 通過。20 個（因子 × K）組態中 sbl_pct 在 K=1/3/5/10 全部排前四，
#      Bonferroni 校正後 p=0.0016 仍顯著。不是撈出來的。
#
# 【確認為真的擔憂】
#   ② 報酬集中在尾部：每日 25/75 截尾後 gross 轉負（−0.0548%，t=−1.36）。
#      10/90 截尾仍正但淨值從 +13.79% 掉到 +4.67%。是「尾部頻率」edge 不是中央傾斜，
#      與命中率僅 51.3% 一致。
#   ④ 逐季不穩：最好的 2 季貢獻 52% 總報酬；大型股版本 10 季有 3 季為負。
#
# 【致命的一擊：中性化空間的 t 沒有轉換成可交易超額】
#   先前所有 t 值都是在 n5（風險中性化殘差報酬）上算的。改用真實日報酬、
#   K=5 重疊分批、扣進場成本與跳空重建淨值後：
#
#     大型股 24 檔　組合 +38.56%/年　波動 24.16%　Sharpe 1.60　MDD −29.31%
#     vs 同層等權　超額 +7.06%/年　追蹤誤差 12.37%　IR 0.57　**t=+0.90 (p=0.37)**
#       2024 +5.09%　2025 **−7.81%**　2026 +33.25%   ← 全靠 2026
#     全宇宙 24 檔　超額 **−1.70%/年**              ← raw 空間直接是負的
#
#   ⚠️ 另一個先前的錯：Sharpe 一度算出 3.04。原因是把「重疊 5 日報酬 ÷ K」
#      當日報酬取標準差 —— 重疊序列除以 K 會把 std 縮 K 倍，該縮的是 √K，
#      低估波動 √5 = 2.24 倍。修正後波動 24~27%，符合台股現實。
#
# 【結案】n5 上的 NW t=+3.95 是真的，但它衡量的是「同風險桶內，選中的股票
#   贏過同儕嗎」。可交易的問題是「長邊組合贏過自己的基準嗎」，答案是
#   +7.06%/年 t=+0.90 —— 不顯著，且全由單一年份撐著。不建議上線。
