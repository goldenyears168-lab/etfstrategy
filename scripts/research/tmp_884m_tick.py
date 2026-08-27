#!/usr/bin/env python3
"""884M 子群逐筆重建：流動性指標 = 買進內盤偏離 − 賣出內盤偏離。

用法：PYTHONPATH=src .venv/bin/python scripts/research/tmp_884m_tick.py --n 70 --rt 0.95
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

from finmind_client import fetch_finmind, fetch_taiwan_stock_trading_daily_report

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def analyse(lv: pd.DataFrame, tk: pd.DataFrame) -> dict | None:
    tk = tk.copy()
    tk["p"] = pd.to_numeric(tk.deal_price, errors="coerce")
    tk["v"] = pd.to_numeric(tk.volume, errors="coerce")
    tk["t"] = tk.Time.map(secs)
    tk = tk.dropna(subset=["p", "v", "t"])
    if tk.empty or tk.v.sum() == 0:
        return None
    t0, span = tk.t.min(), max(tk.t.max() - tk.t.min(), 1)
    g = tk.groupby("p")
    inner = tk[tk.TickType.astype(str) == "2"].groupby("p").v.sum()
    mk = pd.DataFrame({
        "mv": g.v.sum(), "inner": inner,
        "t_mean": g.apply(lambda x: (x.t * x.v).sum() / x.v.sum(), include_groups=False),
    }).fillna({"inner": 0.0})
    mk["inner_ratio"] = mk.inner / mk.mv
    j = lv.set_index("price").join(mk, how="inner")
    if j.empty or j.buy.sum() == 0 or j.sell.sum() == 0:
        return None
    mkt_inner = tk[tk.TickType.astype(str) == "2"].v.sum() / tk.v.sum()

    def wm(col, w):
        return (j[col] * j[w]).sum() / j[w].sum()

    b_rel = (wm("inner_ratio", "buy") - mkt_inner) * 100
    s_rel = (wm("inner_ratio", "sell") - mkt_inner) * 100
    # 演算法足跡：每個價位的量是否被切得一樣大（TWAP/iceberg → CV 低）
    bq = lv.loc[lv.buy > 0, "buy"]
    sq = lv.loc[lv.sell > 0, "sell"]
    def cv(x):
        return float(x.std() / x.mean()) if len(x) >= 4 and x.mean() > 0 else np.nan
    return {
        "lvl_cv_buy": cv(bq), "lvl_cv_sell": cv(sq),
        "n_lvl_buy": len(bq), "n_lvl_sell": len(sq),
        "lot_int_buy": float((bq % 1000 == 0).mean()) if len(bq) else np.nan,
        "buy_t": (wm("t_mean", "buy") - t0) / span,
        "sell_t": (wm("t_mean", "sell") - t0) / span,
        "b_rel": b_rel, "s_rel": s_rel, "liq": b_rel - s_rel,
        "mkt_inner": mkt_inner * 100,
        "buy_share": j.buy.sum() / 1000.0 / tk.v.sum() * 100,   # 股→張
        "n_lvl_hit": len(j),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader", default="884M")
    ap.add_argument("--n", type=int, default=70)
    ap.add_argument("--rt", type=float, default=0.95)
    ap.add_argument("--min-noti", type=float, default=1e6)
    ap.add_argument("--tag", default="dt")
    args = ap.parse_args()

    m = pd.read_pickle(DIR / f"branch_{args.trader}_joined.pkl")
    d = m[(m.rt > args.rt) & (m.dt_noti > args.min_noti)].dropna(subset=["spread"]).copy()
    d = d.sort_values("spread").reset_index(drop=True)
    n, mid = args.n, len(d) // 2
    strata = {"最賠": d.head(n), "中位": d.iloc[mid - n // 2: mid + n // 2], "最賺": d.tail(n)}
    print(f"母體 {len(d):,} 個 stock-day（rt>{args.rt}、名目>{args.min_noti/1e4:,.0f} 萬）")
    for k, v in strata.items():
        print(f"  {k}: {len(v)}　價差 {v.spread.min():+.3f}% ~ {v.spread.max():+.3f}%")

    rows, t0 = [], time.time()
    todo = [(k, r) for k, v in strata.items() for r in v.itertuples()]
    for i, (lab, r) in enumerate(todo):
        try:
            lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(
                trade_date=r.trade_date, data_id=r.stock_id))
            lv = lv[lv.securities_trader_id == args.trader]
            for c in ("price", "buy", "sell"):
                lv[c] = pd.to_numeric(lv[c], errors="coerce")
            lv = lv.dropna(subset=["price"])
            tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", r.stock_id,
                                            date.fromisoformat(r.trade_date),
                                            date.fromisoformat(r.trade_date)))
            a = analyse(lv, tk)
            if a:
                rows.append({"stratum": lab, "stock_id": r.stock_id,
                             "trade_date": r.trade_date, "spread": r.spread,
                             "dt_noti": r.dt_noti, "lots": r.lots, **a})
        except Exception as exc:  # noqa: BLE001
            if i < 5:
                print("  err", type(exc).__name__, str(exc)[:60])
        time.sleep(0.45)
        if i % 30 == 29:
            print(f"  {i+1}/{len(todo)}　{(time.time()-t0)/60:.1f} 分　成功 {len(rows)}",
                  flush=True)

    out = pd.DataFrame(rows)
    out.to_pickle(DIR / f"branch_{args.trader}_{args.tag}_tick.pkl")
    print(f"\n成功重建 {len(out)} 個案例\n")
    if out.empty:
        return 0
    print(f"{'層':<6}{'n':>4}{'價差%':>9}{'買時點':>8}{'賣時點':>8}"
          f"{'b_rel':>8}{'s_rel':>8}{'流動性':>9}{'市場內盤%':>10}{'佔量%':>8}")
    for k in ("最賠", "中位", "最賺"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<4}{len(g):>4}{g.spread.median():>+8.3f}%{g.buy_t.median():>8.3f}"
              f"{g.sell_t.median():>8.3f}{g.b_rel.median():>+8.2f}{g.s_rel.median():>+8.2f}"
              f"{g.liq.median():>+9.2f}{g.mkt_inner.median():>10.1f}{g.buy_share.median():>8.2f}")

    v = out.dropna(subset=["liq", "spread"])
    rho, p = sps.spearmanr(v.liq, v.spread)
    print(f"\nSpearman(流動性指標, 當日價差) = {rho:+.3f}　p={p:.2e}　n={len(v)}")
    print(f"流動性指標 中位 {v.liq.median():+.2f}　平均 {v.liq.mean():+.2f}　"
          f"t={v.liq.mean()/(v.liq.std()/np.sqrt(len(v))):+.2f}")
    # 分層內部相關（排除分層本身造成的機械相關）
    for k in ("最賠", "中位", "最賺"):
        g = v[v.stratum == k]
        if len(g) > 20:
            r2, p2 = sps.spearmanr(g.liq, g.spread)
            print(f"  層內 {k}: rho={r2:+.3f} p={p2:.3f} n={len(g)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
