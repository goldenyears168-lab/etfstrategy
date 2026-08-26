#!/usr/bin/env python3
"""用逐筆重建分點當沖程式的進出時序 —— 日頻還原不出來的部分只能在這裡看。

已知（來自分價明細）：他們在每個價位買了多少、賣了多少。
已知（來自逐筆）：每個價位在什麼時間、以多少量、用內盤還是外盤成交。
兩者交叉可以推出：

  1. **時點**：以他們在各價位的量為權重，回推平均買進/賣出時刻
  2. **主被動**：他們買進的價位上，市場是內盤（賣壓）還是外盤（買盤）主導？
     若集中在內盤主導的價位 → 他們是承接賣壓的**被動接刀**
  3. **超額佔比**：他們在某價位的量佔比 ÷ 市場在該價位的量佔比

⚠️ 這是推論不是觀測 —— 分價資料沒有時間戳。若某價位整天反覆出現，
時點推論的不確定性很大，本檔以「價位的時間離散度」標示可信度。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from finmind_client import fetch_finmind

OUT = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def analyse(lv: pd.DataFrame, tk: pd.DataFrame) -> dict | None:
    """lv: 該分點在各價位的 buy/sell 量；tk: 該股當日逐筆。"""
    tk = tk.copy()
    tk["p"] = pd.to_numeric(tk.deal_price, errors="coerce")
    tk["v"] = pd.to_numeric(tk.volume, errors="coerce")
    tk["t"] = tk.Time.map(secs)
    tk = tk.dropna(subset=["p", "v", "t"])
    if tk.empty or tk.v.sum() == 0:
        return None
    open_t, close_t = tk.t.min(), tk.t.max()
    span = max(close_t - open_t, 1)
    # 每個價位：市場總量、內盤量、成交時間的量加權平均與離散度
    g = tk.groupby("p")
    mk = pd.DataFrame({
        "mv": g.v.sum(),
        "inner": tk[tk.TickType.astype(str) == "2"].groupby("p").v.sum(),
        "t_mean": g.apply(lambda x: (x.t * x.v).sum() / x.v.sum(), include_groups=False),
        "t_sd": g.apply(lambda x: np.sqrt(max((((x.t - (x.t*x.v).sum()/x.v.sum())**2)*x.v).sum()
                                              / x.v.sum(), 0)), include_groups=False),
    }).fillna({"inner": 0.0})
    mk["inner_ratio"] = mk.inner / mk.mv
    j = lv.set_index("price").join(mk, how="inner")
    if j.empty or j.buy.sum() == 0 or j.sell.sum() == 0:
        return None
    def wm(col, w):
        return (j[col] * j[w]).sum() / j[w].sum()
    tot_mv = j.mv.sum()
    return {
        "buy_t": (wm("t_mean", "buy") - open_t) / span,      # 0=開盤 1=收盤
        "sell_t": (wm("t_mean", "sell") - open_t) / span,
        "buy_t_unc": wm("t_sd", "buy") / span,               # 時點推論的不確定性
        "buy_inner": wm("inner_ratio", "buy"),               # 買在內盤主導的價位嗎
        "sell_inner": wm("inner_ratio", "sell"),
        "mkt_inner": (j.inner.sum() / tot_mv) if tot_mv else np.nan,
        "buy_share": j.buy.sum() / tot_mv,
        "n_lvl": len(j),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trader", default="9661")
    ap.add_argument("--n", type=int, default=100, help="每層取幾個案例")
    ap.add_argument("--year", default="2026")
    args = ap.parse_args()

    from finmind_client import fetch_taiwan_stock_trading_daily_report
    m = pd.read_pickle(OUT / "br9661_pricelevel_joined.pkl")
    m["rt"] = m[["buy_vol", "sell_vol"]].min(axis=1) / m[["buy_vol", "sell_vol"]].max(axis=1)
    d = m[(m.trade_date.str[:4] == args.year) & (m.rt > 0.95)
          & (m.dt_notional > 5e6)].dropna(subset=["spread_pct"]).copy()
    d = d.sort_values("spread_pct")
    n = args.n
    mid = len(d) // 2
    strata = {"最賠": d.head(n), "中位": d.iloc[mid - n // 2: mid + n // 2], "最賺": d.tail(n)}
    print(f"母體 {len(d):,} 個 stock-day（{args.year} 純當沖、名目 >500 萬）")
    for k, v in strata.items():
        print(f"  {k}: {len(v)} 個　價差 {v.spread_pct.min():+.3f}% ~ {v.spread_pct.max():+.3f}%")
    rows = []
    t0 = time.time()
    todo = [(k, r) for k, v in strata.items() for r in v.itertuples()]
    for i, (lab, r) in enumerate(todo):
        try:
            lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(
                trade_date=r.trade_date, data_id=r.stock_id))
            lv = lv[lv.securities_trader_id == args.trader]
            for c in ("price", "buy", "sell"):
                lv[c] = pd.to_numeric(lv[c], errors="coerce")
            tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", r.stock_id,
                                            date.fromisoformat(r.trade_date),
                                            date.fromisoformat(r.trade_date)))
            a = analyse(lv, tk)
            if a:
                rows.append({"stratum": lab, "stock_id": r.stock_id,
                             "trade_date": r.trade_date, "spread_pct": r.spread_pct,
                             "notional": r.dt_notional, **a})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
        if i % 30 == 29:
            print(f"  {i+1}/{len(todo)}　{(time.time()-t0)/60:.1f} 分", flush=True)
    out = pd.DataFrame(rows)
    out.to_pickle(OUT / f"branch_{args.trader}_tick_recon.pkl")
    print(f"\n成功重建 {len(out)} 個案例\n")
    print(f"{'層':<6}{'n':>4}{'價差%':>9}{'買進時點':>10}{'賣出時點':>10}"
          f"{'買在內盤%':>11}{'賣在內盤%':>11}{'市場內盤%':>11}{'佔量%':>8}")
    for k in ("最賠", "中位", "最賺"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<4}{len(g):>4}{g.spread_pct.median():>+8.3f}%{g.buy_t.median():>10.3f}"
              f"{g.sell_t.median():>10.3f}{g.buy_inner.median()*100:>10.1f}%"
              f"{g.sell_inner.median()*100:>10.1f}%{g.mkt_inner.median()*100:>10.1f}%"
              f"{g.buy_share.median()*100:>7.2f}%")
    print("\n時點 0=開盤、1=收盤；『買在內盤%』>市場內盤% 代表他們在賣壓價位承接（被動接刀）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
