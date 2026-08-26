#!/usr/bin/env python3
"""抓分點日報的**分價明細** —— 這是唯一能算出分點真實損益與進出價位的資料。

DB 裡的 stock_broker_branch_daily 只有每日買賣總量，看不到成交在哪個價位。
FinMind 的 ``TaiwanStockTradingDailyReport`` 用 ``securities_trader_id`` 查，
單日一次呼叫可拿整個分點所有標的的**分價**買賣量（實測 1,120 檔 / 6,374 列 / 1.4s）。

有了分價就能算：
  · 買進 VWAP 與賣出 VWAP → 當沖價差 → **真實損益**
  · 兩個 VWAP 在當日 OHLC 裡的相對位置 → **追價還是接刀、早盤還是尾盤**

⚠️ 順帶發現：DB 的分點資料對 9661 少收 13.7% 的標的（API 1,120 vs DB 966，
且是單向缺漏、兩邊都有的 966 檔買量 100% 一致）。先前的 9661 分析都建立在
有缺口的子集上 —— 方向性結論不受影響，但絕對數字會偏低。

輸出：每個 stock-day 一列的聚合（買賣量、買賣 VWAP、價位區間、價位檔數）。
原始分價明細太大（5.6M 列）不全存，只留聚合。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from finmind_client import fetch_taiwan_stock_trading_daily_report
from stock_db import connect_ro

OUT = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def trading_days(start: str, end: str) -> list[str]:
    c = connect_ro()
    return [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", (start, end))]


def one_day(trader: str, day: str, retries: int = 4) -> pd.DataFrame | None:
    for i in range(retries):
        try:
            r = fetch_taiwan_stock_trading_daily_report(
                trade_date=day, securities_trader_id=trader)
            if not r:
                return None
            d = pd.DataFrame(r)
            for c in ("price", "buy", "sell"):
                d[c] = pd.to_numeric(d[c], errors="coerce")
            d = d.dropna(subset=["price"])
            g = d.groupby("stock_id")
            out = pd.DataFrame({
                "buy_vol": g.buy.sum(), "sell_vol": g.sell.sum(),
                "buy_amt": g.apply(lambda x: (x.price * x.buy).sum(), include_groups=False),
                "sell_amt": g.apply(lambda x: (x.price * x.sell).sum(), include_groups=False),
                "p_lo": g.price.min(), "p_hi": g.price.max(), "n_lvl": g.price.nunique(),
            }).reset_index()
            out["trade_date"] = day
            return out
        except Exception as exc:  # noqa: BLE001
            if i == retries - 1:
                print(f"  ✗ {day}: {type(exc).__name__} {str(exc)[:70]}", flush=True)
                return None
            time.sleep(3 * (i + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trader", default="9661")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    out = OUT / f"branch_{args.trader}_pricelevels.pkl"
    days = trading_days(args.start, args.end)
    done: set[str] = set()
    parts: list[pd.DataFrame] = []
    if out.exists():
        prev = pd.read_pickle(out)
        parts.append(prev)
        done = set(prev.trade_date.unique())
        print(f"已有 {len(done)} 日，續抓")
    todo = [d for d in days if d not in done]
    print(f"分點 {args.trader}　待抓 {len(todo)} 日（{args.start}~{args.end}）", flush=True)
    t0 = time.time()
    for i, day in enumerate(todo):
        r = one_day(args.trader, day)
        if r is not None:
            parts.append(r)
        time.sleep(args.sleep)
        if i % 50 == 49 or i == len(todo) - 1:
            el = time.time() - t0
            print(f"  {i+1}/{len(todo)}　{el/60:.1f} 分　"
                  f"預估剩餘 {el/(i+1)*(len(todo)-i-1)/60:.1f} 分", flush=True)
            pd.concat(parts, ignore_index=True).to_pickle(out)
    d = pd.concat(parts, ignore_index=True).drop_duplicates(["stock_id", "trade_date"])
    d["buy_vwap"] = d.buy_amt / d.buy_vol.replace(0, np.nan)
    d["sell_vwap"] = d.sell_amt / d.sell_vol.replace(0, np.nan)
    d.to_pickle(out)
    print(f"\n完成：{len(d):,} 個 stock-day · {d.trade_date.nunique()} 日 · "
          f"{d.stock_id.nunique()} 檔 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
