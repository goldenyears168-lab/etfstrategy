#!/usr/bin/env python3
"""981M / 9661 / 8888 在同一時窗（2025-01-01 起）用同一套子群切法對照。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

D = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
START = "2025-01-01"


def load_px() -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, high, low, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(START,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    return (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
              .drop(columns=["rk", "source"]))


def build(tid: str, px: pd.DataFrame) -> pd.DataFrame:
    d = pd.read_pickle(D / f"branch_{tid}_pricelevels.pkl")
    d = d[d.trade_date >= START].drop_duplicates(["stock_id", "trade_date"])
    d["buy_vwap"] = d.buy_amt / d.buy_vol.replace(0, np.nan)
    d["sell_vwap"] = d.sell_amt / d.sell_vol.replace(0, np.nan)
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner", validate="one_to_one")
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    m["dt_sh"] = m[["buy_vol", "sell_vol"]].min(axis=1)
    m["dt_lot"] = m.dt_sh / 1000.0
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_sh
    m["dt_noti"] = m.dt_sh * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rt"] = m.dt_sh / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["part"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.vol.replace(0, np.nan)
    return m.dropna(subset=["buy_vwap", "sell_vwap"]).query("dt_sh>0")


def row(lab: str, d: pd.DataFrame) -> str:
    if len(d) < 50:
        return f"{lab:<26}{len(d):>7}  少"
    pdy = d.groupby("trade_date").size()
    v = d.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    return (f"{lab:<26}{len(d):>8}{d.stock_id.nunique():>6}{pdy.median():>7.0f}"
            f"{pdy.std()/pdy.mean():>6.2f}{d.dt_noti.sum()/1e8:>9.1f}"
            f"{d.dt_pnl.sum()/d.dt_noti.sum()*100:>+10.4f}{(d.spread>0).mean()*100:>7.1f}%"
            f"{d.part.median()*100:>8.3f}{v.buy_pos.median():>8.3f}{v.sell_pos.median():>8.3f}"
            f"{d.dt_lot.median():>8.2f}{(d.buy_vol % 1000 == 0).mean():>8.2f}")


def main() -> int:
    px = load_px()
    hdr = ("子群".ljust(24) + "n".rjust(8) + "檔".rjust(6) + "日均".rjust(7) + "CV".rjust(6)
           + "名目億".rjust(9) + "毛%".rjust(10) + "勝率".rjust(8) + "參與%".rjust(8)
           + "買位".rjust(8) + "賣位".rjust(8) + "中位張".rjust(8) + "整張率".rjust(8))
    for t in ("981M", "9661", "8888"):
        m = build(t, px)
        att = m.groupby("stock_id").trade_date.nunique()
        core = set(att[att >= 300].index)
        print(f"\n########## {t} （{START} 起 {m.trade_date.nunique()} 日）##########")
        print(hdr)
        print(row("全分點", m))
        print(row("rt>0.95 純當沖", m[m.rt > 0.95]))
        print(row("rt<0.3 純方向", m[m.rt < 0.3]))
        print(row(f"核心宇宙(>=300日 {len(core)}檔)", m[m.stock_id.isin(core)]))
        print(row("名目P90+", m[m.dt_noti >= m.dt_noti.quantile(0.9)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
