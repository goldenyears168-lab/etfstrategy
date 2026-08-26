#!/usr/bin/env python3
"""用流動性指標反向找程式 —— 不先篩分點，直接掃 stock-day。

## 為什麼改這條路

分點層級的聚合統計（廣度／規律／出席／分散／當沖度）全部被「客戶數」污染，
三次嘗試都只找得到大型散戶分點與機構通道（見 skill branch-algo-hunt）。

但 9661 的解剖證明：**流動性指標**（買進內盤偏離 − 賣出內盤偏離）與損益
單調相關（Spearman +0.432, p=4.9e-15），而且它是**逐筆層級**的量，
不會被客戶數稀釋 —— 一個分點若有一群散戶亂做，指標會被平均掉趨近 0；
只有真的系統性提供／消耗流動性的參與者才會持續偏離。

因此改成：**掃 stock-day，對該日該股的全部 815 個分點同時算指標**，
再看誰跨多個 stock-day 持續偏離。

## 成本
每個 stock-day 兩次呼叫（分價 by data_id 拿全部分點 + 逐筆）。
500 個 stock-day ≈ 1,000 呼叫 ≈ 20 分鐘。

## 單位陷阱
分價的 buy/sell 是**股**，逐筆的 volume 是**張**，差 1000 倍。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from finmind_client import fetch_finmind, fetch_taiwan_stock_trading_daily_report
from stock_db import connect_ro

OUT = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def tick_profile(tk: pd.DataFrame) -> pd.DataFrame | None:
    """每個價位：市場總量（張）、內盤量、量加權平均時刻。"""
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
        "mv": g.v.sum(),
        "inner": inner,
        "t_mean": g.apply(lambda x: (x.t * x.v).sum() / x.v.sum(), include_groups=False),
    }).fillna({"inner": 0.0})
    prof["inner_ratio"] = prof.inner / prof.mv
    prof.attrs["t0"] = tk.t.min()
    prof.attrs["span"] = max(tk.t.max() - tk.t.min(), 1)
    prof.attrs["mkt_inner"] = tk[tk.TickType.astype(str) == "2"].v.sum() / tk.v.sum()
    prof.attrs["mkt_vol"] = tk.v.sum()
    return prof


def branch_metrics(lv: pd.DataFrame, prof: pd.DataFrame) -> pd.DataFrame:
    """對該 stock-day 的**每一個分點**算流動性指標。"""
    j = lv.join(prof, on="price", how="inner")
    if j.empty:
        return pd.DataFrame()
    mi = prof.attrs["mkt_inner"]
    t0, span = prof.attrs["t0"], prof.attrs["span"]
    mv = prof.attrs["mkt_vol"]
    g = j.groupby("securities_trader_id")

    def w(col, wt):
        num = (j[col] * j[wt]).groupby(j.securities_trader_id).sum()
        den = j[wt].groupby(j.securities_trader_id).sum()
        return num / den.replace(0, np.nan)

    out = pd.DataFrame({
        "buy_vol": g.buy.sum(), "sell_vol": g.sell.sum(),
        "buy_inner": w("inner_ratio", "buy"), "sell_inner": w("inner_ratio", "sell"),
        "buy_t": (w("t_mean", "buy") - t0) / span,
        "sell_t": (w("t_mean", "sell") - t0) / span,
        "buy_vwap": w("price", "buy"), "sell_vwap": w("price", "sell"),
    })
    out["b_rel"] = (out.buy_inner - mi) * 100
    out["s_rel"] = (out.sell_inner - mi) * 100
    out["liq"] = out.b_rel - out.s_rel
    out["rt"] = (out[["buy_vol", "sell_vol"]].min(axis=1)
                 / out[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan))
    # ⚠️ 分價是「股」、逐筆是「張」，除 1000 對齊
    out["part"] = (out.buy_vol + out.sell_vol) / 1000.0 / mv
    out["spread_pct"] = (out.sell_vwap / out.buy_vwap - 1) * 100
    out["mkt_inner"] = mi
    return out.reset_index()


def pick_cases(n_stock: int, n_date: int, start: str, end: str) -> list[tuple[str, str]]:
    """分層抽樣：流動性夠的股票 × 均勻分布的日期。"""
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date BETWEEN ? AND ? AND close>=10""",
        c, params=(start, end))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
    liq = px.groupby("stock_id").vol.median()
    liq = liq[liq >= 1000]
    rng = np.random.default_rng(20260827)
    # 依成交量三分層各抽 1/3，避免只抽到大型股
    q = pd.qcut(liq.rank(method="first"), 3, labels=False)
    sids = []
    for k in range(3):
        pool = liq[q == k].index.to_numpy()
        sids += list(rng.choice(pool, min(n_stock // 3, len(pool)), replace=False))
    dates = np.sort(px.trade_date.unique())
    picks = list(dates[np.linspace(0, len(dates) - 1, n_date).astype(int)])
    return [(s, d) for s in sids for d in picks]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stocks", type=int, default=48)
    ap.add_argument("--dates", type=int, default=10)
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-08-26")
    args = ap.parse_args()
    cases = pick_cases(args.stocks, args.dates, args.start, args.end)
    print(f"抽樣 {len(cases)} 個 stock-day（{args.stocks} 檔 × {args.dates} 日）", flush=True)
    rows, t0, bad = [], time.time(), 0
    for i, (sid, day) in enumerate(cases):
        try:
            lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(
                trade_date=day, data_id=sid))
            if lv.empty:
                continue
            for c_ in ("price", "buy", "sell"):
                lv[c_] = pd.to_numeric(lv[c_], errors="coerce")
            lv = lv.dropna(subset=["price"]).set_index("price")
            tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", sid,
                                            date.fromisoformat(day), date.fromisoformat(day)))
            prof = tick_profile(tk) if not tk.empty else None
            if prof is None:
                continue
            m = branch_metrics(lv.reset_index(), prof)
            if not m.empty:
                m["stock_id"] = sid
                m["trade_date"] = day
                rows.append(m)
        except Exception:  # noqa: BLE001
            bad += 1
        time.sleep(0.25)
        if i % 50 == 49:
            print(f"  {i+1}/{len(cases)}　{(time.time()-t0)/60:.1f} 分　失敗 {bad}", flush=True)
    d = pd.concat(rows, ignore_index=True)
    d.to_pickle(OUT / "branch_liq_scan.pkl")
    print(f"\n{len(d):,} 筆 (分點 × stock-day)　"
          f"{d.securities_trader_id.nunique()} 個分點　{d.stock_id.nunique()} 檔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
