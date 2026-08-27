#!/usr/bin/env python3
"""7031：同 stock-day 上「全分點」對照 —— 毛邊際是不是純粹的規模假象？

疑問：7031 的 +0.64% 毛邊際看起來完勝 9661(+0.10%)／8888(−0.01%)，
但那三個判準（毛邊際、整張率、過離散度）可能全都只是**分點規模的代理**：
大分點把上千個客戶淨掉 → 買賣 VWAP 都逼近全日 VWAP → 量到的價差機械趨近 0。

檢定：抽 7031 交易過的 stock-day，把該股當日**所有分點**的分價抓下來，
對每個分點算同一條當沖價差，再看「價差 vs 該分點當日規模」的關係，
最後把 7031 放到那條曲線上。若 7031 落在同規模同儕的中位附近 → 無 edge。

抽樣依名目分層 + 層內隨機（**禁止依價差分層** —— 循環論證）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

from finmind_client import fetch_taiwan_stock_trading_daily_report

D = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = "7031"
RNG = np.random.default_rng(1234)
N_PER = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def main() -> int:
    m = pd.read_pickle(D / f"branch_{TID}_joined.pkl")
    d = m[m.dt_vol > 0].dropna(subset=["spread"]).copy()
    d["st"] = pd.qcut(d.dt_noti, 3, labels=["小", "中", "大"])
    sel = pd.concat([g.iloc[RNG.choice(len(g), min(N_PER, len(g)), replace=False)]
                     for _, g in d.groupby("st", observed=True)])
    print(f"母體 {len(d):,} → 名目三分層各抽 {N_PER}，共 {len(sel)} 個 stock-day")
    rows, t0 = [], time.time()
    for i, r in enumerate(sel.itertuples()):
        try:
            lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(
                trade_date=r.trade_date, data_id=r.stock_id))
            for c in ("price", "buy", "sell"):
                lv[c] = pd.to_numeric(lv[c], errors="coerce")
            lv = lv.dropna(subset=["price"])
            g = lv.groupby("securities_trader_id")
            a = pd.DataFrame({
                "bv": g.buy.sum(), "sv": g.sell.sum(),
                "ba": g.apply(lambda x: (x.price * x.buy).sum(), include_groups=False),
                "sa": g.apply(lambda x: (x.price * x.sell).sum(), include_groups=False),
                "nlvl": g.price.nunique(),
            }).reset_index()
            a["stock_id"] = r.stock_id
            a["trade_date"] = r.trade_date
            rows.append(a)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {r.stock_id} {r.trade_date} {type(exc).__name__}", flush=True)
        time.sleep(0.45)
        if i % 30 == 29:
            print(f"  {i+1}/{len(sel)}　{(time.time()-t0)/60:.1f} 分", flush=True)
    a = pd.concat(rows, ignore_index=True)
    a.to_pickle(D / f"branch_{TID}_allbranch_control.pkl")
    a["bvw"] = a.ba / a.bv.replace(0, np.nan)
    a["svw"] = a.sa / a.sv.replace(0, np.nan)
    a["dt"] = a[["bv", "sv"]].min(axis=1)
    a = a[(a.dt > 0)].dropna(subset=["bvw", "svw"]).copy()
    a["spread"] = (a.svw / a.bvw - 1) * 100
    a["noti"] = a.dt * (a.bvw + a.svw) / 2
    a["pnl"] = (a.svw - a.bvw) * a.dt
    a["rt"] = a.dt / a[["bv", "sv"]].max(axis=1)
    # 分點當日規模 = 該分點在這批 stock-day 上的平均雙邊股數
    sz = a.groupby("securities_trader_id").agg(
        n=("spread", "size"), med_vol=("bv", "median"),
        gross=("pnl", "sum"), noti=("noti", "sum"),
        sp=("spread", "median"), win=("spread", lambda s: (s > 0).mean() * 100),
        whole=("bv", lambda s: (s % 1000 == 0).mean() * 100))
    sz["gross_pct"] = sz.gross / sz.noti * 100
    big = sz[sz.n >= 20]
    print(f"\n對照母體：{a.securities_trader_id.nunique()} 個分點 · {len(a):,} 個"
          f"（分點×stock-day）；出現 ≥20 次的 {len(big)} 個分點\n")

    print("=== 分點規模 vs 量到的當沖價差（規模混淆檢定）===")
    big = big.copy()
    big["q"] = pd.qcut(big.n, 5, labels=["最小", "小", "中", "大", "最大"])
    print(f"{'規模五分位':<10}{'分點數':>7}{'出現次數中位':>13}{'毛邊際%':>10}{'中位價差%':>11}"
          f"{'勝率%':>8}{'買整張%':>9}")
    for q, g in big.groupby("q", observed=True):
        gp = g.gross.sum() / g.noti.sum() * 100
        print(f"  {str(q):<10}{len(g):>7}{g.n.median():>13.0f}{gp:>+10.4f}"
              f"{g.sp.median():>+11.4f}{g.win.median():>8.1f}{g.whole.median():>9.1f}")
    rho = sps.spearmanr(big.n, big.gross_pct)
    rho2 = sps.spearmanr(big.n, big.whole)
    print(f"  Spearman(出現次數, 毛邊際) = {rho.statistic:+.3f} p={rho.pvalue:.2e}")
    print(f"  Spearman(出現次數, 買整張%) = {rho2.statistic:+.3f} p={rho2.pvalue:.2e}")

    if TID in big.index:
        r = big.loc[TID]
        pool = big[(big.n >= r.n * 0.5) & (big.n <= r.n * 2)]
        pct = (pool.gross_pct < r.gross_pct).mean() * 100
        pct_all = (big.gross_pct < r.gross_pct).mean() * 100
        print(f"\n=== 7031 在對照中的位置 ===")
        print(f"  7031：出現 {r.n:.0f} 次　毛邊際 {r.gross_pct:+.4f}%　中位價差 {r.sp:+.4f}%"
              f"　勝率 {r.win:.1f}%　買整張 {r.whole:.1f}%")
        print(f"  同規模同儕（出現次數 0.5~2×，n={len(pool)} 個分點）："
              f"毛邊際中位 {pool.gross_pct.median():+.4f}%　"
              f"中位價差中位 {pool.sp.median():+.4f}%　勝率中位 {pool.win.median():.1f}%")
        print(f"  → 7031 在同規模同儕中的百分位 = {pct:.0f}%（全體 {pct_all:.0f}%）")
        print(f"  同規模同儕毛邊際分布：10/25/50/75/90 = "
              + " / ".join(f"{pool.gross_pct.quantile(q):+.3f}" for q in (.1, .25, .5, .75, .9)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
