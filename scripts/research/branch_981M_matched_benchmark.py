#!/usr/bin/env python3
"""981M vs 9661 / 8888：同一批 stock-day 上的配對比較（控掉容量／流動性）。"""
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
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    assert not px.duplicated(["stock_id", "trade_date"]).any()
    return px


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


def agg(d: pd.DataFrame) -> dict:
    v = d.dropna(subset=["buy_pos", "sell_pos"])
    v = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    return {"n": len(d), "gross_pct": d.dt_pnl.sum() / d.dt_noti.sum() * 100,
            "spread_med": d.spread.median(), "win": (d.spread > 0).mean() * 100,
            "buy_pos": v.buy_pos.median(), "sell_pos": v.sell_pos.median(),
            "part": d.part.median() * 100}


def main() -> int:
    px = load_px()
    frames = {t: build(t, px) for t in ("981M", "9661", "8888")}
    for t, f in frames.items():
        print(f"{t}: {len(f):,} stock-day · {f.trade_date.nunique()} 日 · {f.stock_id.nunique()} 檔")
    a = frames["981M"]
    for other in ("9661", "8888"):
        b = frames[other]
        key = ["stock_id", "trade_date"]
        j = a.merge(b, on=key, suffixes=("_a", "_b"), validate="one_to_one")
        print(f"\n=== 981M vs {other}：配對 {len(j):,} 個 stock-day "
              f"（{j.stock_id.nunique()} 檔 / {j.trade_date.nunique()} 日）===")
        sa = agg(a[a.set_index(key).index.isin(j.set_index(key).index)])
        sb = agg(b[b.set_index(key).index.isin(j.set_index(key).index)])
        print(f"{'':<10}{'毛%':>10}{'中位價差%':>12}{'勝率':>9}{'買位':>8}{'賣位':>8}{'參與%':>8}")
        for lab, s in (("981M", sa), (other, sb)):
            print(f"{lab:<10}{s['gross_pct']:>+10.4f}{s['spread_med']:>+12.4f}"
                  f"{s['win']:>8.1f}%{s['buy_pos']:>8.3f}{s['sell_pos']:>8.3f}{s['part']:>8.3f}")
        d = (j.spread_a - j.spread_b).dropna()
        w = sps.wilcoxon(d) if len(d) > 20 else None
        print(f"同日同股價差差額 中位 {d.median():+.4f}%　平均 {d.mean():+.4f}%　"
              f"981M 較優比例 {(d>0).mean()*100:.1f}%"
              + (f"　Wilcoxon p={w.pvalue:.2e}" if w else ""))
        db = (j.buy_pos_a - j.buy_pos_b).dropna()
        ds = (j.sell_pos_a - j.sell_pos_b).dropna()
        print(f"買位差額 中位 {db.median():+.4f}（負=981M買更低）　"
              f"賣位差額 中位 {ds.median():+.4f}（正=981M賣更高）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
