#!/usr/bin/env python3
"""981M 子群的逐筆流動性指標重建（複製 branch_liquidity_scan 的指標定義）。

liq = (買進價位的市場內盤比 − 市場內盤比) − (賣出價位的市場內盤比 − 市場內盤比)
    = 買進內盤偏離 − 賣出內盤偏離   （>0 提供流動性 / <0 消耗流動性）

用法：PYTHONPATH=src .venv/bin/python scripts/research/branch_981M_tick_liquidity.py \
        [TID] [--n 60] [--rt 0.95] [--minnoti 2e6]
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


def tick_profile(tk: pd.DataFrame):
    tk = tk.copy()
    tk["p"] = pd.to_numeric(tk.deal_price, errors="coerce")
    tk["v"] = pd.to_numeric(tk.volume, errors="coerce")
    tk["t"] = tk.Time.map(secs)
    tk = tk.dropna(subset=["p", "v", "t"])
    if tk.empty or tk.v.sum() == 0:
        return None
    g = tk.groupby("p")
    inner = tk[tk.TickType.astype(str) == "2"].groupby("p").v.sum()
    prof = pd.DataFrame({
        "mv": g.v.sum(), "inner": inner,
        "t_mean": g.apply(lambda x: (x.t * x.v).sum() / x.v.sum(), include_groups=False),
    }).fillna({"inner": 0.0})
    prof["inner_ratio"] = prof.inner / prof.mv
    prof.attrs["t0"] = tk.t.min()
    prof.attrs["span"] = max(tk.t.max() - tk.t.min(), 1)
    prof.attrs["mkt_inner"] = tk[tk.TickType.astype(str) == "2"].v.sum() / tk.v.sum()
    prof.attrs["mkt_vol"] = tk.v.sum()
    prof.attrs["ntick"] = len(tk)
    return prof


def one(tid: str, sid: str, day: str) -> dict | None:
    lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(trade_date=day, data_id=sid))
    if lv.empty:
        return None
    lv = lv[lv.securities_trader_id == tid].copy()
    if lv.empty:
        return None
    for c in ("price", "buy", "sell"):
        lv[c] = pd.to_numeric(lv[c], errors="coerce")
    lv = lv.dropna(subset=["price"]).groupby("price")[["buy", "sell"]].sum()
    tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", sid,
                                    date.fromisoformat(day), date.fromisoformat(day)))
    if tk.empty:
        return None
    prof = tick_profile(tk)
    if prof is None:
        return None
    j = lv.join(prof, how="inner")
    if j.empty or j.buy.sum() == 0 or j.sell.sum() == 0:
        return None
    mi = prof.attrs["mkt_inner"]
    t0, span, mv = prof.attrs["t0"], prof.attrs["span"], prof.attrs["mkt_vol"]

    def w(col, wt):
        return (j[col] * j[wt]).sum() / j[wt].sum()

    b_in, s_in = w("inner_ratio", "buy"), w("inner_ratio", "sell")
    bvwap = (j.index.to_series() * j.buy).sum() / j.buy.sum()
    svwap = (j.index.to_series() * j.sell).sum() / j.sell.sum()
    return {
        "stock_id": sid, "trade_date": day,
        "b_rel": (b_in - mi) * 100, "s_rel": (s_in - mi) * 100,
        "liq": (b_in - s_in) * 100, "mkt_inner": mi * 100,
        "buy_t": (w("t_mean", "buy") - t0) / span,
        "sell_t": (w("t_mean", "sell") - t0) / span,
        "buy_vwap_lv": bvwap, "sell_vwap_lv": svwap,
        "spread_lv": (svwap / bvwap - 1) * 100,
        "share": (j.buy.sum() + j.sell.sum()) / 1000.0 / mv * 100,
        "n_lvl": len(j), "mkt_vol": mv, "ntick": prof.attrs["ntick"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tid", nargs="?", default="981M")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--rt", type=float, default=0.95)
    ap.add_argument("--minnoti", type=float, default=2e6)
    ap.add_argument("--core", type=int, default=0, help=">0 則只取出席>=該天數的核心宇宙")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    m = pd.read_pickle(DIR / f"branch_{args.tid}_joined.pkl")
    if args.core:
        att = m.groupby("stock_id").trade_date.nunique()
        m = m[m.stock_id.isin(att[att >= args.core].index)]
        print(f"核心宇宙 {m.stock_id.nunique()} 檔")
    d = m[(m.rt > args.rt) & (m.dt_noti > args.minnoti)].dropna(subset=["spread"]).copy()
    d = d.sort_values("spread")
    n, mid = args.n, len(d) // 2
    strata = {"最賠": d.head(n), "中位": d.iloc[mid - n // 2: mid + n // 2], "最賺": d.tail(n)}
    print(f"母體 {len(d):,} 個 stock-day（rt>{args.rt}、名目>{args.minnoti:,.0f}）")
    for k, v in strata.items():
        print(f"  {k}: {len(v)} 個　價差 {v.spread.min():+.3f}% ~ {v.spread.max():+.3f}%")
    rows, t0 = [], time.time()
    todo = [(k, r) for k, v in strata.items() for r in v.itertuples()]
    for i, (lab, r) in enumerate(todo):
        try:
            a = one(args.tid, r.stock_id, r.trade_date)
            if a:
                rows.append({"stratum": lab, "spread_agg": r.spread,
                             "noti": r.dt_noti, "rt": r.rt, **a})
        except Exception as exc:  # noqa: BLE001
            if i < 5:
                print(f"  ! {r.stock_id} {r.trade_date}: {type(exc).__name__} {str(exc)[:60]}")
        time.sleep(0.45)
        if i % 30 == 29:
            print(f"  {i+1}/{len(todo)}　{(time.time()-t0)/60:.1f} 分　ok={len(rows)}", flush=True)
    out = pd.DataFrame(rows)
    out.to_pickle(DIR / f"branch_{args.tid}{args.tag}_tick_liq.pkl")
    print(f"\n成功重建 {len(out)} 個案例\n")
    if out.empty:
        return 0
    print(f"{'層':<6}{'n':>4}{'價差%':>9}{'買偏離':>9}{'賣偏離':>9}{'流動性':>9}"
          f"{'市場內盤%':>11}{'買時點':>8}{'賣時點':>8}{'佔量%':>8}")
    for k in ("最賠", "中位", "最賺"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<4}{len(g):>4}{g.spread_lv.median():>+8.3f}%{g.b_rel.median():>+9.2f}"
              f"{g.s_rel.median():>+9.2f}{g.liq.median():>+9.2f}{g.mkt_inner.median():>10.1f}%"
              f"{g.buy_t.median():>8.3f}{g.sell_t.median():>8.3f}{g.share.median():>7.2f}%")
    v = out.dropna(subset=["liq", "spread_lv"])
    rho, p = sps.spearmanr(v.liq, v.spread_lv)
    print(f"\nSpearman(流動性指標, 逐筆重算價差) = {rho:+.3f}　p={p:.2e}　n={len(v)}")
    rho2, p2 = sps.spearmanr(v.liq, v.spread_agg)
    print(f"Spearman(流動性指標, 分價聚合價差) = {rho2:+.3f}　p={p2:.2e}")
    print(f"\n流動性指標中位 {v.liq.median():+.2f}　平均 {v.liq.mean():+.2f}　"
          f">0 比例 {(v.liq>0).mean()*100:.1f}%")
    print(f"買偏離中位 {v.b_rel.median():+.2f}　賣偏離中位 {v.s_rel.median():+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
