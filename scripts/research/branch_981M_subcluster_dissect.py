#!/usr/bin/env python3
"""981M 元大苗栗：分點內部行為子群解剖（複製 9661 流程）。

用法：PYTHONPATH=src .venv/bin/python scripts/research/branch_981M_subcluster_dissect.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = sys.argv[1] if len(sys.argv) > 1 else "981M"


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
    d["buy_vwap"] = d.buy_amt / d.buy_vol.replace(0, np.nan)
    d["sell_vwap"] = d.sell_amt / d.sell_vol.replace(0, np.nan)
    px = load_px("2024-12-01")
    n0 = len(d)
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner", validate="one_to_one")
    print(f"merge: 分價 {n0:,} → 對到價格 {len(m):,}（{len(m)/n0*100:.1f}%）")
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    # 分價 buy/sell 是「股」；vol 是「張」
    m["dt_sh"] = m[["buy_vol", "sell_vol"]].min(axis=1)          # 股
    m["dt_lot"] = m.dt_sh / 1000.0                                # 張
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_sh            # 元
    m["dt_noti"] = m.dt_sh * (m.buy_vwap + m.sell_vwap) / 2       # 元
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rt"] = m.dt_sh / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["part"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.vol.replace(0, np.nan)
    m["tot_lot"] = (m.buy_vol + m.sell_vol) / 1000.0
    m = m.dropna(subset=["buy_vwap", "sell_vwap"])
    m = m[m.dt_sh > 0]
    return m


def stats(d: pd.DataFrame, label: str) -> dict:
    if len(d) < 30:
        return {"label": label, "n": len(d)}
    g, n = d.dt_pnl.sum(), d.dt_noti.sum()
    v = d.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    per_day = d.groupby("trade_date").size()
    return {
        "label": label, "n": len(d), "days": d.trade_date.nunique(),
        "stocks": d.stock_id.nunique(),
        "per_day": per_day.median(), "per_day_cv": per_day.std() / per_day.mean(),
        "noti_yi": n / 1e8, "gross_pct": g / n * 100 if n else np.nan,
        "gross_yi": g / 1e8,
        "win": (d.spread > 0).mean() * 100,
        "part": d.part.median() * 100, "rt": d.rt.median(),
        "buy_pos": v.buy_pos.median(), "sell_pos": v.sell_pos.median(),
        "dt_lot_med": d.dt_lot.median(), "dt_lot_cv": d.dt_lot.std() / d.dt_lot.mean(),
        "noti_med_wan": d.dt_noti.median() / 1e4,
        "net18": (g - n * 0.00201) / 1e8, "net60": (g - n * 0.00321) / 1e8,
    }


def show(rows: list[dict]) -> None:
    hdr = (f"{'子群':<22}{'n':>7}{'日':>5}{'檔':>6}{'日均':>7}{'CV':>6}"
           f"{'名目億':>8}{'毛%':>9}{'勝率':>7}{'參與%':>7}{'rt':>6}"
           f"{'買位':>7}{'賣位':>7}{'中位張':>8}{'淨1.8折億':>10}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if r.get("n", 0) < 30:
            print(f"{r['label']:<22}{r.get('n',0):>7}  （樣本不足）")
            continue
        print(f"{r['label']:<22}{r['n']:>7}{r['days']:>5}{r['stocks']:>6}"
              f"{r['per_day']:>7.0f}{r['per_day_cv']:>6.2f}{r['noti_yi']:>8.1f}"
              f"{r['gross_pct']:>+9.4f}{r['win']:>6.1f}%{r['part']:>7.2f}{r['rt']:>6.2f}"
              f"{r['buy_pos']:>7.3f}{r['sell_pos']:>7.3f}{r['dt_lot_med']:>8.1f}"
              f"{r['net18']:>+10.2f}")


def main() -> int:
    m = build()
    print(f"\n全樣本 {len(m):,} stock-day · {m.trade_date.nunique()} 日 · "
          f"{m.stock_id.nunique()} 檔\n")
    rows = [stats(m, "全分點")]
    rows.append(stats(m[m.rt > 0.95], "(a) rt>0.95 純當沖"))
    rows.append(stats(m[m.rt > 0.90], "    rt>0.90"))
    rows.append(stats(m[m.rt.between(0.3, 0.9)], "    0.3<=rt<=0.9"))
    rows.append(stats(m[m.rt < 0.3], "(b) rt<0.3 純方向"))
    # (c) 部位大小分層（當沖名目）
    q = m.dt_noti.quantile([0.25, 0.5, 0.75, 0.9]).to_dict()
    rows.append(stats(m[m.dt_noti < q[0.25]], "(c) 名目 Q1(最小)"))
    rows.append(stats(m[m.dt_noti.between(q[0.25], q[0.5])], "(c) 名目 Q2"))
    rows.append(stats(m[m.dt_noti.between(q[0.5], q[0.75])], "(c) 名目 Q3"))
    rows.append(stats(m[m.dt_noti >= q[0.75]], "(c) 名目 Q4(最大)"))
    rows.append(stats(m[m.dt_noti >= q[0.9]], "(c) 名目 P90+"))
    # (d) 單筆張數規格化檢查
    rows.append(stats(m[m.dt_lot <= 2], "(d) 當沖<=2張"))
    rows.append(stats(m[m.dt_lot > 20], "(d) 當沖>20張"))
    # (e) 交叉：純當沖 × 名目
    pd_ = m[m.rt > 0.95]
    if len(pd_) > 200:
        qq = pd_.dt_noti.quantile([0.5, 0.9]).to_dict()
        rows.append(stats(pd_[pd_.dt_noti >= qq[0.5]], "(e) rt>.95 & 名目上半"))
        rows.append(stats(pd_[pd_.dt_noti >= qq[0.9]], "(e) rt>.95 & 名目P90+"))
        rows.append(stats(pd_[pd_.dt_noti < qq[0.5]], "(e) rt>.95 & 名目下半"))
    show(rows)

    print("\n[rt 分布]")
    print(m.rt.describe(percentiles=[.1, .25, .5, .75, .9, .95, .99]).round(3).to_string())
    print("\n[當沖張數分布]")
    print(m.dt_lot.describe(percentiles=[.25, .5, .75, .9, .99]).round(2).to_string())
    print("\n[每日檔數 by month]")
    mm = m.copy(); mm["ym"] = mm.trade_date.str[:7]
    t = mm.groupby(["ym", "trade_date"]).size().groupby("ym").agg(["median", "mean", "std", "count"])
    print(t.round(1).to_string())
    print("\n[純當沖 rt>0.95 每月檔數]")
    p = mm[mm.rt > 0.95]
    t2 = p.groupby(["ym", "trade_date"]).size().groupby("ym").agg(["median", "mean", "std", "count"])
    print(t2.round(1).to_string())
    m.to_pickle(DIR / f"branch_{TID}_joined.pkl")
    print(f"\n→ {DIR / f'branch_{TID}_joined.pkl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
