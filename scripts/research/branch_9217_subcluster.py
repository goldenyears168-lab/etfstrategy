#!/usr/bin/env python3
"""9217 凱基松山：全分點損益 + 行為子群切分（沿用 9661 解剖流程）。

坑防護：
  · 分價 buy/sell 是「股」，逐筆 volume 是「張」→ part 要除 1000
  · merge 前後斷言無重複（避免 shift 抓到同一天那類污染）
  · 成本相加不取平均：當沖 0.201%（1.8 折）／0.321%（6 折）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = sys.argv[1] if len(sys.argv) > 1 else "9217"


def load_px(start: str) -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, high, low, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    assert not px.duplicated(["stock_id", "trade_date"]).any(), "px 有重複"
    return px


def build() -> pd.DataFrame:
    d = pd.read_pickle(DIR / f"branch_{TID}_pricelevels.pkl")
    d = d.drop_duplicates(["stock_id", "trade_date"])
    assert not d.duplicated(["stock_id", "trade_date"]).any(), "分價有重複"
    px = load_px("2025-01-01")
    n0 = len(d)
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner")
    assert not m.duplicated(["stock_id", "trade_date"]).any(), "merge 後重複"
    print(f"分價 {n0:,} → merge 價格後 {len(m):,}")
    m["buy_vwap"] = m.buy_amt / m.buy_vol.replace(0, np.nan)
    m["sell_vwap"] = m.sell_amt / m.sell_vol.replace(0, np.nan)
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    m["dt_vol"] = m[["buy_vol", "sell_vol"]].min(axis=1)          # 股
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_vol
    m["dt_noti"] = m.dt_vol * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rt"] = m.dt_vol / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["part"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.vol.replace(0, np.nan)   # 股→張
    m["lots_per_lvl"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.n_lvl.replace(0, np.nan)
    return m


def summarize(dt: pd.DataFrame, label: str) -> dict:
    if len(dt) < 30:
        return {"label": label, "n": len(dt), "err": "樣本不足"}
    g, n = dt.dt_pnl.sum(), dt.dt_noti.sum()
    v = dt.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    per_day = dt.groupby("trade_date").size()
    sp = dt.spread.dropna()
    t = sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp))) if len(sp) > 2 else np.nan
    return {
        "label": label, "n": len(dt), "days": dt.trade_date.nunique(),
        "stocks": dt.stock_id.nunique(),
        "per_day": per_day.median(), "per_day_cv": per_day.std() / per_day.mean(),
        "noti_yi": n / 1e8, "gross_pct": g / n * 100,
        "win": (dt.spread > 0).mean() * 100, "t_spread": t,
        "rt": dt.rt.median(), "part": dt.part.median() * 100,
        "buy_pos": v.buy_pos.median(), "sell_pos": v.sell_pos.median(),
        "lots_med": dt.lots_per_lvl.median(),
        "net_18_yi": (g - n * 0.00201) / 1e8,
        "net_60_yi": (g - n * 0.00321) / 1e8,
    }


HDR = (f"{'子群':<26}{'n':>7}{'日':>5}{'檔':>6}{'日均':>6}{'CV':>6}{'名目億':>8}"
       f"{'毛邊際%':>9}{'t':>6}{'勝率':>7}{'當沖度':>7}{'參與%':>7}{'買位':>6}{'賣位':>6}"
       f"{'張/價位':>8}{'淨1.8折億':>10}")


def line(r: dict) -> str:
    if "err" in r:
        return f"{r['label']:<26}{r['n']:>7}  {r['err']}"
    return (f"{r['label']:<26}{r['n']:>7}{r['days']:>5}{r['stocks']:>6}{r['per_day']:>6.0f}"
            f"{r['per_day_cv']:>6.2f}{r['noti_yi']:>8,.0f}{r['gross_pct']:>+9.4f}"
            f"{r['t_spread']:>+6.1f}{r['win']:>6.1f}%{r['rt']:>7.3f}{r['part']:>7.3f}"
            f"{r['buy_pos']:>6.3f}{r['sell_pos']:>6.3f}{r['lots_med']:>8.1f}"
            f"{r['net_18_yi']:>+10.1f}")


def main() -> int:
    m = build()
    m.to_pickle(DIR / f"branch_{TID}_joined.pkl")
    dt = m.dropna(subset=["buy_vwap", "sell_vwap"])
    dt = dt[dt.dt_vol > 0].copy()
    print(f"\n{TID}　{len(m):,} stock-day · {m.trade_date.nunique()} 日 · "
          f"{m.stock_id.nunique()} 檔；其中雙邊有成交 {len(dt):,}\n")
    rows = [summarize(dt, "全分點(雙邊)")]
    # (a) 純當沖
    for lo in (0.90, 0.95, 0.99):
        rows.append(summarize(dt[dt.rt > lo], f"(a) rt>{lo}"))
    # (b) 純方向
    rows.append(summarize(dt[dt.rt < 0.30], "(b) rt<0.30"))
    rows.append(summarize(dt[dt.rt < 0.10], "(b) rt<0.10"))
    # (c) 部位大小分層（當沖名目）
    q = dt.dt_noti
    for lab, lo, hi in (("(c) 名目<50萬", 0, 5e5), ("(c) 50萬~500萬", 5e5, 5e6),
                        ("(c) 500萬~5000萬", 5e6, 5e7), ("(c) >5000萬", 5e7, np.inf)):
        rows.append(summarize(dt[(q >= lo) & (q < hi)], lab))
    # (d) 交叉：純當沖 × 名目
    pure = dt[dt.rt > 0.95]
    for lab, lo, hi in (("(d) rt>.95 & <50萬", 0, 5e5), ("(d) rt>.95 & 50~500萬", 5e5, 5e6),
                        ("(d) rt>.95 & >500萬", 5e6, np.inf)):
        rows.append(summarize(pure[(pure.dt_noti >= lo) & (pure.dt_noti < hi)], lab))
    # (e) 參與率高（單一標的主導者）
    rows.append(summarize(dt[dt.part > 0.05], "(e) 參與率>5%"))
    rows.append(summarize(dt[(dt.rt > 0.95) & (dt.part > 0.05)], "(e) rt>.95 & 參與>5%"))
    print(HDR)
    for r in rows:
        print(line(r))
    pd.DataFrame(rows).to_pickle(DIR / f"branch_{TID}_subclusters.pkl")

    # 規模穩定性：純當沖子群每日檔數
    p = dt[dt.rt > 0.95].groupby("trade_date").size()
    print(f"\n純當沖(rt>0.95) 每日檔數：中位 {p.median():.0f}　"
          f"10/90 分位 {p.quantile(.1):.0f}/{p.quantile(.9):.0f}　CV {p.std()/p.mean():.2f}　"
          f"出席日 {len(p)}/{dt.trade_date.nunique()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
