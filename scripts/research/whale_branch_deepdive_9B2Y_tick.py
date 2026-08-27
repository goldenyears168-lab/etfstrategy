#!/usr/bin/env python3
"""9B2Y：對行為子群做逐筆重建，算流動性指標與 Spearman(指標, 當日價差)。

複用 branch_liquidity_scan 的 branch_metrics（一次呼叫拿該 stock-day 全部分點，
再取 9B2Y 那一列），成本 = 每個 stock-day 兩次 API。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from branch_liquidity_scan import branch_metrics, tick_profile  # noqa: E402

from finmind_client import fetch_finmind, fetch_taiwan_stock_trading_daily_report  # noqa: E402

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = "9B2Y"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=70, help="每層取幾個案例")
    ap.add_argument("--rt", type=float, default=0.95)
    ap.add_argument("--minnoti", type=float, default=2e6)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    m = pd.read_pickle(DIR / f"branch_{TID}_joined.pkl")
    d = m[(m.rt > args.rt) & (m.dt_noti > args.minnoti)].dropna(subset=["spread"]).copy()
    d = d.sort_values("spread").reset_index(drop=True)
    n, mid = args.n, len(d) // 2
    strata = {"最賠": d.head(n), "中位": d.iloc[mid - n // 2: mid + n // 2], "最賺": d.tail(n)}
    print(f"母體 {len(d):,} 個 stock-day（rt>{args.rt}、當沖名目>{args.minnoti/1e4:.0f}萬）")
    for k, v in strata.items():
        print(f"  {k}: {len(v)} 個　價差 {v.spread.min():+.3f}% ~ {v.spread.max():+.3f}%")

    rows, t0, bad = [], time.time(), 0
    todo = [(k, r) for k, v in strata.items() for r in v.itertuples()]
    for i, (lab, r) in enumerate(todo):
        try:
            lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(
                trade_date=r.trade_date, data_id=r.stock_id))
            if lv.empty:
                bad += 1
                continue
            for c_ in ("price", "buy", "sell"):
                lv[c_] = pd.to_numeric(lv[c_], errors="coerce")
            lv = lv.dropna(subset=["price"])
            tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", r.stock_id,
                                            date.fromisoformat(r.trade_date),
                                            date.fromisoformat(r.trade_date)))
            prof = tick_profile(tk) if not tk.empty else None
            if prof is None:
                bad += 1
                continue
            mm = branch_metrics(lv, prof)
            mm = mm[mm.securities_trader_id == TID]
            if mm.empty:
                bad += 1
                continue
            rows.append({"stratum": lab, "stock_id": r.stock_id, "trade_date": r.trade_date,
                         "spread_daily": r.spread, "dt_noti": r.dt_noti,
                         **mm.iloc[0].drop("securities_trader_id").to_dict()})
        except Exception as exc:  # noqa: BLE001
            bad += 1
            if bad <= 3:
                print(f"   err {r.stock_id} {r.trade_date}: {type(exc).__name__} {str(exc)[:60]}")
        time.sleep(0.45)
        if i % 30 == 29:
            print(f"  {i+1}/{len(todo)}　{(time.time()-t0)/60:.1f} 分　失敗 {bad}", flush=True)

    out = pd.DataFrame(rows)
    tag = args.out or f"rt{args.rt}"
    out.to_pickle(DIR / f"branch_{TID}_tick_recon_{tag}.pkl")
    print(f"\n成功重建 {len(out)} 個案例（失敗 {bad}）\n")
    if out.empty:
        return 0
    print(f"{'層':<6}{'n':>4}{'價差%':>9}{'買時點':>8}{'賣時點':>8}{'買內盤%':>9}{'賣內盤%':>9}"
          f"{'市場內盤%':>10}{'流動性指標':>11}{'佔量%':>8}")
    for k in ("最賠", "中位", "最賺"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<4}{len(g):>4}{g.spread_daily.median():>+8.3f}%{g.buy_t.median():>8.3f}"
              f"{g.sell_t.median():>8.3f}{g.buy_inner.median()*100:>8.1f}%"
              f"{g.sell_inner.median()*100:>8.1f}%{g.mkt_inner.median()*100:>9.1f}%"
              f"{g.liq.median():>11.2f}{g.part.median()*100:>7.2f}%")

    o = out.dropna(subset=["liq", "spread_daily"])
    rho, p = sps.spearmanr(o.liq, o.spread_daily)
    print(f"\nSpearman(流動性指標, 當日價差) = {rho:+.3f}  p={p:.3g}  n={len(o)}")
    rho2, p2 = sps.spearmanr(o.liq, o.spread_pct)
    print(f"Spearman(流動性指標, 逐筆重算價差) = {rho2:+.3f}  p={p2:.3g}")
    print(f"流動性指標 中位 {o.liq.median():+.2f} · 平均 {o.liq.mean():+.2f} · "
          f"sd {o.liq.std():.2f} · >0 比例 {(o.liq > 0).mean():.1%}")
    tt = sps.ttest_1samp(o.liq.dropna(), 0)
    print(f"H0: 指標=0 → t={tt.statistic:+.2f} p={tt.pvalue:.3g}")
    print(f"\n基準：9661 程式 Spearman +0.432 / 全市場 +0.423")
    print(f"日內價差一致性檢查：corr(日頻 spread, 逐筆 spread_pct) = "
          f"{np.corrcoef(o.spread_daily, o.spread_pct)[0,1]:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
