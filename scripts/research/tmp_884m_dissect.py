#!/usr/bin/env python3
"""884M 玉山數位：全分點損益 + 行為子群切分（9661 解剖流程步驟 3–4）。

坑：
  · 分價 buy/sell 是「股」，逐筆 volume 是「張」→ part 要 /1000
  · merge 前後都斷言無重複（否則任何 shift 都會變未來函數）
  · 成本相加不取平均：當沖 1.8 折 0.201%、現股 6 折 0.471%
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
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    assert not px.duplicated(["stock_id", "trade_date"]).any(), "px 有重複"
    return px


def build(tid: str) -> pd.DataFrame:
    d = pd.read_pickle(DIR / f"branch_{tid}_pricelevels.pkl")
    d = d.drop_duplicates(["stock_id", "trade_date"])
    assert not d.duplicated(["stock_id", "trade_date"]).any(), "分價有重複"
    d["buy_vwap"] = d.buy_amt / d.buy_vol.replace(0, np.nan)
    d["sell_vwap"] = d.sell_amt / d.sell_vol.replace(0, np.nan)
    px = load_px(d.trade_date.min())
    n0 = len(d)
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner")
    assert not m.duplicated(["stock_id", "trade_date"]).any(), "merge 後有重複"
    print(f"分價 {n0:,} → 有價格 {len(m):,}（{len(m)/n0:.1%}）")
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    m["dt_vol"] = m[["buy_vol", "sell_vol"]].min(axis=1)          # 股
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_vol
    m["dt_noti"] = m.dt_vol * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rt"] = m.dt_vol / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["part"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.vol.replace(0, np.nan)
    m["lots"] = (m.buy_vol + m.sell_vol) / 1000.0                 # 張
    return m


def stats(dt: pd.DataFrame, label: str) -> dict:
    dt = dt.dropna(subset=["buy_vwap", "sell_vwap"])
    dt = dt[dt.dt_vol > 0]
    if len(dt) < 30:
        return {"label": label, "n": len(dt)}
    g, n = dt.dt_pnl.sum(), dt.dt_noti.sum()
    v = dt.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    pd_ = dt.groupby("trade_date").size()
    return {
        "label": label, "n": len(dt), "days": dt.trade_date.nunique(),
        "stocks": dt.stock_id.nunique(),
        "per_day": pd_.median(), "per_day_cv": pd_.std() / pd_.mean(),
        "noti_yi": n / 1e8, "gross_pct": g / n * 100,
        "win": (dt.spread > 0).mean() * 100,
        "part": dt.part.median() * 100, "rt": dt.rt.median(),
        "buy_pos": v.buy_pos.median(), "sell_pos": v.sell_pos.median(),
        "lots_med": dt.lots.median(),
        "net_18_yi": (g - n * 0.00201) / 1e8,
        "net_60_yi": (g - n * 0.00471) / 1e8,
    }


def show(rows: list[dict]) -> None:
    hdr = (f"{'子群':<26}{'n':>7}{'日均':>6}{'CV':>6}{'當沖度':>7}{'參與%':>7}"
           f"{'名目億':>8}{'毛邊際%':>9}{'勝率':>7}{'買位':>6}{'賣位':>6}{'張/日檔':>8}"
           f"{'淨1.8折':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if r.get("n", 0) < 30:
            print(f"{r['label']:<26}{r.get('n',0):>7}  （樣本不足）")
            continue
        print(f"{r['label']:<26}{r['n']:>7,}{r['per_day']:>6.0f}{r['per_day_cv']:>6.2f}"
              f"{r['rt']:>7.3f}{r['part']:>7.2f}{r['noti_yi']:>8,.0f}{r['gross_pct']:>+9.4f}"
              f"{r['win']:>6.1f}%{r['buy_pos']:>6.3f}{r['sell_pos']:>6.3f}"
              f"{r['lots_med']:>8.1f}{r['net_18_yi']:>+9.2f}")


def main() -> int:
    tid = sys.argv[1] if len(sys.argv) > 1 else "884M"
    m = build(tid)
    m.to_pickle(DIR / f"branch_{tid}_joined.pkl")
    print(f"\n{tid}　{len(m):,} 個 stock-day · {m.trade_date.nunique()} 日 · "
          f"{m.stock_id.nunique()} 檔\n")

    rows = [stats(m, "全分點")]
    for lo, hi, lab in [(0.95, 1.01, "(a) rt>0.95 純當沖"),
                        (0.9, 1.01, "    rt>0.90"),
                        (0.7, 0.9, "    rt 0.7~0.9"),
                        (0.3, 0.7, "    rt 0.3~0.7"),
                        (-0.01, 0.3, "(b) rt<0.3 純方向")]:
        rows.append(stats(m[m.rt.between(lo, hi)], lab))
    show(rows)

    # (c) 依部位大小分層（名目）
    print("\n【(c) 依單日名目分層】")
    d = m[m.dt_vol > 0].dropna(subset=["dt_noti"]).copy()
    q = pd.qcut(d.dt_noti.rank(method="first"), 5, labels=False)
    show([stats(d[q == k], f"名目 Q{k+1} (中位 {d[q==k].dt_noti.median()/1e4:,.0f} 萬)")
          for k in range(5)])

    # (c2) 純當沖 × 名目分層
    print("\n【(c2) rt>0.95 內再依名目分層】")
    d2 = m[(m.rt > 0.95) & (m.dt_vol > 0)].dropna(subset=["dt_noti"]).copy()
    if len(d2) > 500:
        q2 = pd.qcut(d2.dt_noti.rank(method="first"), 4, labels=False)
        show([stats(d2[q2 == k], f"DT 名目 Q{k+1} (中位 {d2[q2==k].dt_noti.median()/1e4:,.0f} 萬)")
              for k in range(4)])

    # 單筆規格化：張數分布
    print("\n【張數（買+賣）分布 — 規格化檢查】")
    for lab, sub in [("全分點", m), ("rt>0.95", m[m.rt > 0.95])]:
        L = sub.lots.dropna()
        print(f"  {lab}: n={len(L):,} 中位 {L.median():.1f} 張　"
              f"p10 {L.quantile(.1):.1f} p90 {L.quantile(.9):.1f}　"
              f"≤2張佔 {(L <= 2).mean():.1%}　≤10張佔 {(L <= 10).mean():.1%}")
        # 價位檔數 = 一天內在該股成交過幾個不同價位
        print(f"       價位檔數中位 {sub.n_lvl.median():.0f}　"
              f"=1 檔佔 {(sub.n_lvl == 1).mean():.1%}")

    # 每日規模穩定性（rt>0.95 子群）
    print("\n【rt>0.95 子群逐月規模】")
    s = m[(m.rt > 0.95) & (m.dt_vol > 0)].copy()
    s["ym"] = s.trade_date.str[:7]
    gm = s.groupby("ym").agg(n=("stock_id", "size"), days=("trade_date", "nunique"),
                             noti=("dt_noti", "sum"), pnl=("dt_pnl", "sum"))
    gm["per_day"] = gm.n / gm.days
    gm["gross_pct"] = gm.pnl / gm.noti * 100
    print(gm[["n", "days", "per_day", "gross_pct"]].to_string(
        float_format=lambda x: f"{x:,.3f}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
