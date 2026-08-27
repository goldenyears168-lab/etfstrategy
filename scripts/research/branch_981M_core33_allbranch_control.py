#!/usr/bin/env python3
"""981M 核心33檔 stock-day 上，其他已快取分點的同批表現（容量對照）。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

D = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
START = "2025-01-01"


def main() -> int:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(START,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))

    def build(t: str) -> pd.DataFrame:
        d = pd.read_pickle(D / f"branch_{t}_pricelevels.pkl")
        d = d[d.trade_date >= START].drop_duplicates(["stock_id", "trade_date"])
        d["bv"] = d.buy_amt / d.buy_vol.replace(0, np.nan)
        d["sv"] = d.sell_amt / d.sell_vol.replace(0, np.nan)
        m = d.merge(px, on=["stock_id", "trade_date"], how="inner", validate="one_to_one")
        m["dt"] = m[["buy_vol", "sell_vol"]].min(axis=1)
        m["pnl"] = (m.sv - m.bv) * m.dt
        m["noti"] = m.dt * (m.bv + m.sv) / 2
        m["spread"] = (m.sv / m.bv - 1) * 100
        m["part"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.vol.replace(0, np.nan)
        return m.dropna(subset=["bv", "sv"]).query("dt>0")

    a = build("981M")
    att = a.groupby("stock_id").trade_date.nunique()
    core = set(att[att >= 300].index)
    key = ["stock_id", "trade_date"]
    aa = a[a.stock_id.isin(core)]
    idx = aa.set_index(key).index
    print("分點".ljust(8) + "n".rjust(8) + "毛%".rjust(10) + "中位價差%".rjust(12)
          + "勝率".rjust(9) + "參與%".rjust(10))

    def show(t: str, d: pd.DataFrame) -> None:
        print(f"{t:<8}{len(d):>8}{d.pnl.sum()/d.noti.sum()*100:>+10.4f}"
              f"{d.spread.median():>+12.4f}{(d.spread>0).mean()*100:>8.1f}%"
              f"{d.part.median()*100:>10.3f}")

    show("981M", aa)
    for t in ("9661", "8888", "1480", "1650", "9268", "9800"):
        f = D / f"branch_{t}_pricelevels.pkl"
        if not f.exists():
            continue
        b = build(t)
        bb = b[b.set_index(key).index.isin(idx)]
        if len(bb) < 200:
            print(f"{t:<8}{len(bb):>8}  （配對太少）")
            continue
        show(t, bb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
