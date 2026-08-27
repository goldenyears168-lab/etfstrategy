#!/usr/bin/env python3
"""981M 核心 33 檔子群 vs 同一批 stock-day 上的 9661 / 8888 / 全市場所有分點。

問題：核心宇宙的高毛邊際是「這個分點會做」還是「這些股票好做」？
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

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
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_sh
    m["dt_noti"] = m.dt_sh * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rt"] = m.dt_sh / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["part"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.vol.replace(0, np.nan)
    return m.dropna(subset=["buy_vwap", "sell_vwap"]).query("dt_sh>0")


def line(lab: str, d: pd.DataFrame) -> str:
    v = d.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    return (f"{lab:<16}{len(d):>8}{d.dt_pnl.sum()/d.dt_noti.sum()*100:>+10.4f}"
            f"{d.spread.median():>+11.4f}{(d.spread>0).mean()*100:>8.1f}%"
            f"{v.buy_pos.median():>8.3f}{v.sell_pos.median():>8.3f}"
            f"{d.part.median()*100:>9.3f}{d.rt.median():>7.2f}")


def main() -> int:
    px = load_px()
    a = pd.read_pickle(D / "branch_981M_joined.pkl")
    att = a.groupby("stock_id").trade_date.nunique()
    core = set(att[att >= 300].index)
    print(f"核心 33 檔：{sorted(core)}")
    hdr = (f"{'':<16}{'n':>8}{'毛%':>10}{'中位價差%':>11}{'勝率':>9}"
           f"{'買位':>8}{'賣位':>8}{'參與%':>9}{'rt':>7}")
    print("\n" + hdr)
    print(line("981M 核心33", a[a.stock_id.isin(core)]))
    print(line("981M 其餘", a[~a.stock_id.isin(core)]))
    for t in ("9661", "8888"):
        b = build(t, px)
        key = ["stock_id", "trade_date"]
        idx = a[a.stock_id.isin(core)].set_index(key).index
        bb = b[b.set_index(key).index.isin(idx)]
        print(line(f"{t} 同批", bb))
    # 全市場：同一批 stock-day 上所有分點的整體
    s = pd.read_pickle(D / "branch_liq_scan.pkl")
    s = s[s.stock_id.isin(core)].dropna(subset=["spread_pct"])
    s = s[(s.buy_vol > 0) & (s.sell_vol > 0)]
    print(f"\n[逐筆掃描抽樣] 核心33檔上共 {len(s):,} 個 分點×stock-day，"
          f"{s.securities_trader_id.nunique()} 個分點，"
          f"中位價差 {s.spread_pct.median():+.4f}%　勝率 {(s.spread_pct>0).mean()*100:.1f}%")
    g981 = s[s.securities_trader_id == "981M"]
    if len(g981) > 10:
        print(f"  其中 981M n={len(g981)} 中位價差 {g981.spread_pct.median():+.4f}% "
              f"liq {g981.liq.median():+.2f}")
    # 每檔：981M vs 該檔全市場分點中位
    print("\n[每檔 981M 中位價差 減 該檔同日全市場分點中位價差]")
    mk = s.groupby(["stock_id", "trade_date"]).spread_pct.median().rename("mkt_med").reset_index()
    j = g981.merge(mk, on=["stock_id", "trade_date"], validate="one_to_one")
    dd = (j.spread_pct - j.mkt_med)
    print(f"  n={len(dd)}　中位 {dd.median():+.4f}%　勝過市場中位比例 {(dd>0).mean()*100:.1f}%"
          + (f"　Wilcoxon p={sps.wilcoxon(dd).pvalue:.3f}" if len(dd) > 20 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
