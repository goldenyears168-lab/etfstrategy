#!/usr/bin/env python3
"""9B2Y 台新埔里：全分點損益 + 行為子群切分（9661 解剖流程步驟 3–4）。

坑防護：
  · 分價 buy/sell 是「股」，stock_daily_bars.volume 也是股 → part 直接相除
  · merge 前後皆斷言無 (stock_id, trade_date) 重複
  · 自營商無分價 → 已於探針確認 price>0 = 100%
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID = "9B2Y"

# 成本（相加，不取平均）
COST_DT_18 = 0.00201   # 當沖：證交稅減半 0.15% + 手續費 0.1425%×0.18×2
COST_DT_60 = 0.00321   # 當沖 6 折
COST_CASH_60 = 0.00471  # 現股 6 折


def load_px(start: str) -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, high, low, close, volume
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    assert not px.duplicated(["stock_id", "trade_date"]).any(), "px 有重複"
    return px


def build() -> pd.DataFrame:
    d = pd.read_pickle(DIR / f"branch_{TID}_pricelevels.pkl")
    print(f"分價原始：{len(d):,} 列 · {d.trade_date.nunique()} 日 · {d.stock_id.nunique()} 檔 "
          f"· {d.trade_date.min()}~{d.trade_date.max()}")
    assert not d.duplicated(["stock_id", "trade_date"]).any(), "分價有重複 ← 坑#3"
    px = load_px(d.trade_date.min())
    n0 = len(d)
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner", validate="one_to_one")
    print(f"merge 後 {len(m):,} 列（掉了 {n0-len(m):,}，多為 ETF/無日線）")
    assert not m.duplicated(["stock_id", "trade_date"]).any(), "merge 後重複 ← 坑#3"

    m["buy_vwap"] = m.buy_amt / m.buy_vol.replace(0, np.nan)
    m["sell_vwap"] = m.sell_amt / m.sell_vol.replace(0, np.nan)
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    m["dt_vol"] = m[["buy_vol", "sell_vol"]].min(axis=1)          # 股
    m["mx_vol"] = m[["buy_vol", "sell_vol"]].max(axis=1)
    m["rt"] = m.dt_vol / m.mx_vol.replace(0, np.nan)
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_vol
    m["dt_noti"] = m.dt_vol * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["gross_noti"] = m.buy_amt + m.sell_amt
    m["part"] = (m.buy_vol + m.sell_vol) / m.volume.replace(0, np.nan)
    m["lvl_lot"] = (m.buy_vol + m.sell_vol) / 1000.0 / m.n_lvl.replace(0, np.nan)  # 每價位張數
    return m


def stats(d: pd.DataFrame, lab: str, ndays_total: int) -> dict:
    v = d.dropna(subset=["buy_vwap", "sell_vwap"])
    v = v[v.dt_vol > 0]
    g, n = v.dt_pnl.sum(), v.dt_noti.sum()
    pv = v.dropna(subset=["buy_pos", "sell_pos"])
    pv = pv[pv.buy_pos.between(-.1, 1.1) & pv.sell_pos.between(-.1, 1.1)]
    per = v.groupby("trade_date").size()
    return {
        "label": lab, "n": len(v), "days": v.trade_date.nunique(),
        "presence": v.trade_date.nunique() / ndays_total,
        "stocks": v.stock_id.nunique(),
        "per_day": float(per.median()) if len(per) else np.nan,
        "per_day_cv": float(per.std() / per.mean()) if len(per) > 1 else np.nan,
        "noti_yi": n / 1e8, "gross_pct": g / n * 100 if n else np.nan,
        "win": (v.spread > 0).mean() * 100,
        "part": v.part.median() * 100, "rt": v.rt.median(),
        "buy_pos": pv.buy_pos.median(), "sell_pos": pv.sell_pos.median(),
        "lvl_lot": v.lvl_lot.median(),
        "dt_noti_med": v.dt_noti.median() / 1e4,
        "net18_yi": (g - n * COST_DT_18) / 1e8,
    }


def show(rows: list[dict]) -> None:
    hdr = (f"{'子群':<22}{'n':>6}{'日數':>5}{'出席':>6}{'檔':>5}{'日筆':>6}{'CV':>6}"
           f"{'名目億':>8}{'毛邊際%':>9}{'勝率':>7}{'參與%':>7}{'當沖度':>7}"
           f"{'買位':>6}{'賣位':>6}{'價位張':>7}{'中位額萬':>9}{'淨1.8折億':>10}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['label']:<22}{r['n']:>6,}{r['days']:>5}{r['presence']:>6.2f}{r['stocks']:>5}"
              f"{r['per_day']:>6.1f}{r['per_day_cv']:>6.2f}{r['noti_yi']:>8.1f}"
              f"{r['gross_pct']:>+9.4f}{r['win']:>6.1f}%{r['part']:>7.2f}{r['rt']:>7.3f}"
              f"{r['buy_pos']:>6.3f}{r['sell_pos']:>6.3f}{r['lvl_lot']:>7.1f}"
              f"{r['dt_noti_med']:>9.0f}{r['net18_yi']:>+10.2f}")


def main() -> int:
    m = build()
    nd = m.trade_date.nunique()
    m.to_pickle(DIR / f"branch_{TID}_joined.pkl")

    print("\n=== 全分點 vs 行為子群 ===")
    rows = [stats(m, "全分點", nd)]
    # (a) 純當沖
    rows.append(stats(m[m.rt > 0.95], "(a) rt>0.95 純當沖", nd))
    rows.append(stats(m[m.rt > 0.90], "(a') rt>0.90", nd))
    # (b) 純方向
    rows.append(stats(m[m.rt < 0.30], "(b) rt<0.30 純方向", nd))
    # (c) 部位大小分層（毛名目）
    for lo, hi, lab in [(0, 1e6, "(c1) 名目<100萬"), (1e6, 1e7, "(c2) 100萬~1000萬"),
                        (1e7, 1e8, "(c3) 1000萬~1億"), (1e8, np.inf, "(c4) >1億")]:
        rows.append(stats(m[(m.gross_noti >= lo) & (m.gross_noti < hi)], lab, nd))
    # (d) 純當沖 × 規模
    for lo, hi, lab in [(0, 5e6, "(d1) 純沖 名目<500萬"), (5e6, np.inf, "(d2) 純沖 名目>500萬")]:
        s = m[(m.rt > 0.95) & (m.dt_noti >= lo) & (m.dt_noti < hi)]
        rows.append(stats(s, lab, nd))
    show(rows)

    print("\n=== rt 分布（是否雙峰＝兩群客戶）===")
    r = m.rt.dropna()
    print(pd.cut(r, [0, .05, .2, .4, .6, .8, .95, 1.0]).value_counts().sort_index().to_string())

    print("\n=== 純當沖子群逐月穩定度 ===")
    s = m[(m.rt > 0.95) & (m.dt_vol > 0)].copy()
    s["ym"] = s.trade_date.str[:7]
    gm = s.groupby("ym").apply(lambda x: pd.Series({
        "n": len(x), "days": x.trade_date.nunique(),
        "per_day": len(x) / x.trade_date.nunique(),
        "gross_pct": x.dt_pnl.sum() / x.dt_noti.sum() * 100,
        "win": (x.spread > 0).mean() * 100,
        "noti_yi": x.dt_noti.sum() / 1e8,
    }), include_groups=False)
    print(gm.to_string())

    print("\n=== 全分點逐月 ===")
    a = m[m.dt_vol > 0].copy()
    a["ym"] = a.trade_date.str[:7]
    ga = a.groupby("ym").apply(lambda x: pd.Series({
        "n": len(x), "days": x.trade_date.nunique(),
        "per_day": len(x) / x.trade_date.nunique(),
        "gross_pct": x.dt_pnl.sum() / x.dt_noti.sum() * 100,
        "win": (x.spread > 0).mean() * 100,
    }), include_groups=False)
    print(ga.to_string())

    print("\n=== 單筆規格化檢查：每價位張數分布（純當沖子群）===")
    print(s.lvl_lot.describe(percentiles=[.1, .25, .5, .75, .9]).to_string())
    print("\n買賣張數是否成對（buy_vol == sell_vol 完全對沖比例）:")
    ex = (m.buy_vol == m.sell_vol) & (m.buy_vol > 0)
    print(f"  完全對沖 {ex.sum():,} / {len(m):,} = {ex.mean():.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
