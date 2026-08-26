#!/usr/bin/env python3
"""從分價明細算單一分點的真實損益與手法側寫（階段 2 的標準報表）。

用法：PYTHONPATH=src .venv/bin/python scripts/research/branch_pnl_report.py 960T [...]

⚠️ 成本口徑：當沖證交稅減半 0.15%（至 2027 年底），現股 0.30%。
   手續費 0.1425% × 折數 × 2。自營商通常近乎零手續費（自家席位）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def load_px(start: str) -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, high, low, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    return (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
              .drop(columns=["rk", "source"]))


def report(tid: str, px: pd.DataFrame) -> dict | None:
    f = DIR / f"branch_{tid}_pricelevels.pkl"
    if not f.exists():
        return None
    d = pd.read_pickle(f)
    d["buy_vwap"] = d.buy_amt / d.buy_vol.replace(0, np.nan)
    d["sell_vwap"] = d.sell_amt / d.sell_vol.replace(0, np.nan)
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner")
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    m["dt_vol"] = m[["buy_vol", "sell_vol"]].min(axis=1)
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_vol
    m["dt_noti"] = m.dt_vol * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rt"] = m.dt_vol / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["part"] = (m.buy_vol + m.sell_vol) / 1000 / m.vol.replace(0, np.nan)
    dt = m.dropna(subset=["buy_vwap", "sell_vwap"])
    dt = dt[dt.dt_vol > 0]
    if len(dt) < 200:
        return None
    g, n = dt.dt_pnl.sum(), dt.dt_noti.sum()
    v = dt.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    return {
        "tid": tid, "n": len(dt), "days": dt.trade_date.nunique(),
        "stocks": dt.stock_id.nunique(),
        "per_day": dt.groupby("trade_date").size().median(),
        "noti": n, "gross": g, "gross_pct": g / n * 100,
        "win": (dt.spread > 0).mean() * 100,
        "part": dt.part.median() * 100, "rt": dt.rt.median(),
        "buy_pos": v.buy_pos.median(), "sell_pos": v.sell_pos.median(),
        "net_0": (g - n * 0.0015) / 1e8,          # 只付證交稅（自營近零手續費）
        "net_18": (g - n * 0.00201) / 1e8,        # 1.8 折當沖
        "net_60": (g - n * 0.00321) / 1e8,        # 6 折當沖
    }


def main() -> int:
    tids = sys.argv[1:] or ["960T", "980T", "930T", "9A0T", "700T", "779T", "691T", "648T"]
    px = load_px("2024-01-01")
    c = connect_ro()
    nm = {}
    for d in ("2026-08-25", "2025-06-10"):
        for t, n in c.execute("SELECT DISTINCT securities_trader_id, securities_trader "
                              "FROM stock_broker_branch_daily WHERE trade_date=?", (d,)):
            nm.setdefault(t, n)
    rows = [r for r in (report(t, px) for t in tids) if r]
    if not rows:
        print("尚無可用資料")
        return 0
    print(f"{'分點':<6}{'名稱':<13}{'日均檔':>7}{'當沖度':>7}{'參與率':>8}{'名目(億)':>10}"
          f"{'毛邊際%':>9}{'勝率':>7}{'買位':>6}{'賣位':>6}{'淨@稅':>9}{'淨@1.8折':>10}")
    for r in rows:
        print(f"{r['tid']:<6}{str(nm.get(r['tid'],'?'))[:12]:<13}{r['per_day']:>7.0f}"
              f"{r['rt']:>7.3f}{r['part']:>7.2f}%{r['noti']/1e8:>10,.0f}{r['gross_pct']:>+8.4f}"
              f"{r['win']:>6.1f}%{r['buy_pos']:>6.3f}{r['sell_pos']:>6.3f}"
              f"{r['net_0']:>+9.2f}{r['net_18']:>+10.2f}")
    pd.DataFrame(rows).to_pickle(DIR / "branch_pnl_report.pkl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
