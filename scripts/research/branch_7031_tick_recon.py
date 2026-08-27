#!/usr/bin/env python3
"""7031 逐筆重建 —— **依名目分層 + 層內隨機抽樣**（禁止依價差分層）。

分層變數 = dt_noti（與損益無關）。價差分層會讓 Spearman(指標, 價差) 變成
循環論證（9661 的 pooled ρ=+0.432 在層內全部歸零）。
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from finmind_client import fetch_finmind, fetch_taiwan_stock_trading_daily_report

sys.path.insert(0, str(Path(__file__).resolve().parent))
from branch_tick_reconstruct import analyse  # noqa: E402

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = sys.argv[1] if len(sys.argv) > 1 else "7031"
N_PER = int(sys.argv[2]) if len(sys.argv) > 2 else 45
RNG = np.random.default_rng(20260827)


def main() -> int:
    m = pd.read_pickle(DIR / f"branch_{TID}_joined.pkl")
    d = m[(m.rt > 0.95) & (m.dt_vol > 0)].dropna(subset=["spread"]).copy()
    d = d[d.trade_date >= "2025-06-01"]
    d["stratum"] = pd.qcut(d.dt_noti, 3, labels=["小名目", "中名目", "大名目"])
    picks = []
    for k, g in d.groupby("stratum", observed=True):
        idx = RNG.choice(len(g), size=min(N_PER, len(g)), replace=False)
        picks.append(g.iloc[idx])
    sel = pd.concat(picks)
    print(f"{TID} 純當沖母體 {len(d):,} → 依名目三分層各隨機抽 {N_PER}，共 {len(sel)}")
    for k, g in sel.groupby("stratum", observed=True):
        print(f"  {k}: n={len(g)} 名目中位 {g.dt_noti.median()/1e4:,.0f} 萬 "
              f"價差中位 {g.spread.median():+.3f}%")
    rows, t0 = [], time.time()
    for i, r in enumerate(sel.itertuples()):
        try:
            lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(
                trade_date=r.trade_date, data_id=r.stock_id))
            lv = lv[lv.securities_trader_id == TID]
            for c in ("price", "buy", "sell"):
                lv[c] = pd.to_numeric(lv[c], errors="coerce")
            tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", r.stock_id,
                                            date.fromisoformat(r.trade_date),
                                            date.fromisoformat(r.trade_date)))
            a = analyse(lv, tk)
            if a:
                # 逐筆單量分布（張）—— 程式應集中在少數固定張數
                tv = pd.to_numeric(tk.volume, errors="coerce").dropna()
                a["mkt_ticks"] = len(tv)
                rows.append({"stratum": str(r.stratum), "stock_id": r.stock_id,
                             "trade_date": r.trade_date, "spread": r.spread,
                             "noti": r.dt_noti, "n_lvl_lv": len(lv), **a})
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {r.stock_id} {r.trade_date} {type(exc).__name__}", flush=True)
        time.sleep(0.45)
        if i % 20 == 19:
            print(f"  {i+1}/{len(sel)}　{(time.time()-t0)/60:.1f} 分", flush=True)
    out = pd.DataFrame(rows)
    out.to_pickle(DIR / f"branch_{TID}_tick_recon.pkl")
    print(f"\n成功重建 {len(out)}/{len(sel)}\n")
    print(f"{'層':<8}{'n':>4}{'價差%':>9}{'買時點':>9}{'賣時點':>9}{'時點不確定':>11}"
          f"{'買在內盤%':>11}{'賣在內盤%':>11}{'市場內盤%':>11}{'佔量%':>8}{'價位檔':>7}")
    for k in ("小名目", "中名目", "大名目"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<6}{len(g):>4}{g.spread.median():>+8.3f}%{g.buy_t.median():>9.3f}"
              f"{g.sell_t.median():>9.3f}{g.buy_t_unc.median():>11.3f}"
              f"{g.buy_inner.median()*100:>10.1f}%{g.sell_inner.median()*100:>10.1f}%"
              f"{g.mkt_inner.median()*100:>10.1f}%{g.buy_share.median()*100:>7.3f}%"
              f"{g.n_lvl.median():>7.0f}")
    a = out
    print(f"\n全樣本 n={len(a)}　買時點 {a.buy_t.median():.3f} 賣時點 {a.sell_t.median():.3f}"
          f"　先買後賣比例 {(a.buy_t < a.sell_t).mean():.1%}")
    print(f"  買在內盤 {a.buy_inner.median()*100:.1f}% vs 市場 {a.mkt_inner.median()*100:.1f}%"
          f"　→ 超額 {(a.buy_inner - a.mkt_inner).median()*100:+.2f}pp"
          f"（>0 = 在賣壓價位承接 = 被動接刀）")
    print(f"  賣在內盤 {a.sell_inner.median()*100:.1f}% → 超額 "
          f"{(a.sell_inner - a.mkt_inner).median()*100:+.2f}pp（<0 = 賣在買盤價位）")
    print(f"  佔市場量中位 {a.buy_share.median()*100:.3f}%　時點不確定度中位 "
          f"{a.buy_t_unc.median():.3f}（>0.25 表示價位整天反覆出現、時點推論不可信）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
