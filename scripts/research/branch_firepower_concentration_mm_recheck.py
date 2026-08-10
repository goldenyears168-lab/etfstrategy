#!/usr/bin/env python3
"""用「火力集中度」重檢造市分點排除名單（research only）.

背景：`market_maker_branch_exclusion_v1.json` 的 17 家排除，靠的是三個判準——
最活躍股票近乎全勤、買賣金額比 ≈50/50、觸及股票數近乎全市場。H12 已證明這三個
判準套在已驗證好分點 9217（凱基-松山）自己身上同樣成立（4/7 核心持股買賣比在
50±3pp、全勤 61–99%、觸及 2,239 檔，比 9800 的 2,019 還多），所以名單目前被標記為
「暫時可信但論證基礎不夠強」。

問題出在三個判準測的都是**名目廣度**（出現在幾檔、出現幾天）。造市帳戶與方向性
大戶在名目廣度上本來就可能一樣——差別在**金額怎麼分配**：

  * 造市／綜合流量：錢平均攤在幾千檔上 → 高分散
  * 知情重押：即使名目上碰很多檔，錢會集中在少數幾檔 → 高集中

本支用金額口徑的集中度取代名目廣度：
  hhi        = Σ(該分點當日各股成交金額²) / (當日總成交金額)²   ← 金額赫芬達指數
  top1_share = 當日最大單一持股成交金額 / 當日總成交金額
  net_ratio  = |Σ淨買金額| / Σ|淨買金額|                        ← 全書方向性
並且必須通過「不是規模代理」的檢驗：對 log(規模) 迴歸後取殘差再比較。

  PYTHONPATH=src .venv/bin/python scripts/research/branch_firepower_concentration_mm_recheck.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import stock_db  # noqa: E402

EXCL = ROOT / "reports/research/branch-footprint-screen/market_maker_branch_exclusion_v1.json"
GOOD = "9217"          # 凱基-松山，唯一通過決定性驗證的分點
MIN_DAYS = 60
FOCUS = Path("/tmp/branch_focus.pkl")


def main() -> int:
    d = pd.read_pickle(FOCUS)
    meta = json.loads(EXCL.read_text())
    excl = {s["trader_id"]: s["name"] for s in meta["symbols"]}
    print(f"分點-日 {len(d):,} 列　{d.tid.nunique()} 家　{d.trade_date.min()} ~ {d.trade_date.max()}")

    d["hhi"] = d.g2 / d.g.replace(0, np.nan) ** 2
    d["top1_share"] = d.gmax / d.g.replace(0, np.nan)
    d["net_ratio"] = d.nsum.abs() / d.nabs.replace(0, np.nan)
    d["net_top1"] = d.nmax / d.nabs.replace(0, np.nan)

    g = d.groupby("tid").agg(days=("trade_date", "size"), size_g=("g", "median"),
                             ns=("ns", "median"), hhi=("hhi", "median"),
                             top1=("top1_share", "median"), net_ratio=("net_ratio", "median"),
                             net_top1=("net_top1", "median"))
    g = g[g.days >= MIN_DAYS].copy()
    g["logsize"] = np.log10(g.size_g.clip(lower=1))

    # 是不是規模的代理？對 log(規模) 迴歸取殘差
    for c in ["hhi", "top1", "net_ratio", "net_top1"]:
        m = g[c].notna() & g.logsize.notna()
        b = np.polyfit(g.logsize[m], g[c][m], 1)
        g[f"{c}_res"] = g[c] - np.polyval(b, g.logsize)
        r = np.corrcoef(g.logsize[m], g[c][m])[0, 1]
        print(f"  corr(log規模, {c}) = {r:+.3f}"
              + ("　← 高度是規模代理" if abs(r) > 0.6 else ""))

    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    nm = pd.read_sql("select distinct securities_trader_id tid, securities_trader nm "
                     "from stock_broker_branch_daily where trade_date>='2026-07-01'", conn)
    conn.close()
    g["name"] = g.index.map(nm.drop_duplicates("tid").set_index("tid").nm)

    g["grp"] = np.where(g.index == GOOD, "★9217",
                        np.where(g.index.isin(excl), "排除名單", "其他"))
    pd.set_option("display.width", 220)
    print(f"\n=== 分組中位數（{len(g)} 家，≥{MIN_DAYS} 個交易日）===")
    summ = g.groupby("grp").agg(n=("hhi", "size"), 規模=("size_g", "median"), 觸及檔數=("ns", "median"),
                                金額HHI=("hhi", "median"), 最大持股佔比=("top1", "median"),
                                全書方向性=("net_ratio", "median"),
                                HHI規模調整=("hhi_res", "median"),
                                方向性規模調整=("net_ratio_res", "median"))
    summ["規模"] = (summ.規模 / 1e8).round(2)
    print(summ.round(4).to_string())

    print(f"\n=== 排除名單 17 家 vs ★9217 逐家對照 ===")
    show = g[g.grp != "其他"].copy().sort_values("hhi_res", ascending=False)
    show["規模億"] = (show.size_g / 1e8).round(2)
    cols = ["name", "grp", "days", "規模億", "ns", "hhi", "top1", "net_ratio", "hhi_res", "net_ratio_res"]
    print(show[cols].round(4).to_string())

    # 9217 在全體分佈中的百分位
    print("\n=== ★9217 在全體分點分布中的百分位 ===")
    for c in ["hhi", "top1", "net_ratio", "hhi_res", "net_ratio_res", "ns"]:
        if GOOD in g.index and pd.notna(g.loc[GOOD, c]):
            pct = (g[c] < g.loc[GOOD, c]).mean() * 100
            epct = (g[c] < g.loc[list(excl)].reindex(g.index).dropna()[c].median()).mean() * 100 \
                if len(g.index.intersection(excl)) else np.nan
            print(f"  {c:16s} 9217 = {g.loc[GOOD, c]:.4f}（第 {pct:.0f} 百分位）"
                  f"　排除名單中位數在第 {epct:.0f} 百分位")

    out = ROOT / "reports/research/branch-footprint-screen/branch_firepower_concentration_v1.csv"
    g.to_csv(out)
    print(f"\n寫入 {out}")

    if "--criterion-stats" in sys.argv:
        criterion_stats(g, excl)
    return 0


def criterion_stats(g: pd.DataFrame, excl: dict) -> None:
    """重現 criterion_v2 區塊的所有 headline 數字（門檻 / recall / 誤殺 / 舊判準對照）。

    ⚠️ 門檻＝9217 自身 hhi_res 的**完整浮點值**，比較子為**嚴格大於**。
    用四捨五入後的 0.0445 會把 9217 自己 flag 成造市——criterion_v2.threshold_precision_note
    記錄了這個陷阱，這裡是它的可執行證明。
    """
    thr = float(g.loc[GOOD, "hhi_res"])
    inlist = g.index.isin(excl)
    print("\n" + "=" * 72)
    print(f"criterion_v2 統計重現　門檻 = {thr!r}（9217 自身 hhi_res，嚴格大於）")
    print("=" * 72)
    for lab, sel in [("完整精度 hhi_res > threshold", g.hhi_res > thr),
                     ("四捨五入 hhi_res > 0.0445", g.hhi_res > round(thr, 4)),
                     ("四捨五入 hhi_res >= 0.0445", g.hhi_res >= round(thr, 4))]:
        hit, fp = int((sel & inlist).sum()), int((sel & ~inlist).sum())
        print(f"  {lab:32s} flag {int(sel.sum()):3d}　recall {hit}/{len(excl)}　"
              f"誤殺 {fp} 家（{fp/len(g)*100:.2f}%）　9217 {'被誤殺 ❌' if sel.get(GOOD, False) else '保留 ✓'}")
    missed = sorted(g.index[inlist & ~(g.hhi_res > thr)])
    print(f"  漏網：{missed}")

    ns_thr = float(g.loc[GOOD, "ns"])
    sel = g.ns > ns_thr
    hit, fp = int((sel & inlist).sum()), int((sel & ~inlist).sum())
    print(f"\n  舊判準對照（每日觸及檔數 ns > {ns_thr:.0f}）: flag {int(sel.sum())}　"
          f"recall {hit}/{len(excl)}　誤殺 {fp} 家（{fp/len(g)*100:.2f}%）")
    e = g[inlist]
    print(f"\n  名單 hhi_res 中位 {e.hhi_res.median():.4f}　高於 9217 者 {(e.hhi_res > thr).sum()}/{len(e)}")
    m = g.hhi.notna() & g.logsize.notna()
    print(f"  corr(log10 規模, hhi) = {np.corrcoef(g.logsize[m], g.hhi[m])[0, 1]:.4f}")


if __name__ == "__main__":
    raise SystemExit(main())
