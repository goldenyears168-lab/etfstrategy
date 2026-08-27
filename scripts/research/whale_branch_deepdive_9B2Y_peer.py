#!/usr/bin/env python3
"""同窗期同儕對照：把已快取的分點全部限制在 2026-02-02~2026-08-26 再比。

若這段期間**每個**分點的毛邊際都被推高，9B2Y 的 +0.23% 就不是它的本事。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
W0, W1 = "2026-02-02", "2026-08-26"

from stock_db import connect_ro  # noqa: E402

c = connect_ro()
px = pd.read_sql_query(
    """SELECT stock_id, trade_date, source, open, high, low, close, volume
         FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(W0,))
px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
        .drop(columns=["rk", "source"]))
assert not px.duplicated(["stock_id", "trade_date"]).any()

nm = {}
for dd in ("2026-08-25", "2026-06-10"):
    for t, n in c.execute("SELECT DISTINCT securities_trader_id, securities_trader "
                          "FROM stock_broker_branch_daily WHERE trade_date=?", (dd,)):
        nm.setdefault(t, n)


def row(tid: str) -> dict | None:
    f = DIR / f"branch_{tid}_pricelevels.pkl"
    if not f.exists():
        return None
    d = pd.read_pickle(f)
    d = d[(d.trade_date >= W0) & (d.trade_date <= W1)]
    if d.empty:
        return None
    d = d.drop_duplicates(["stock_id", "trade_date"])
    m = d.merge(px, on=["stock_id", "trade_date"], how="inner", validate="one_to_one")
    m["buy_vwap"] = m.buy_amt / m.buy_vol.replace(0, np.nan)
    m["sell_vwap"] = m.sell_amt / m.sell_vol.replace(0, np.nan)
    m["dt_vol"] = m[["buy_vol", "sell_vol"]].min(axis=1)
    m["rt"] = m.dt_vol / m[["buy_vol", "sell_vol"]].max(axis=1).replace(0, np.nan)
    m["dt_pnl"] = (m.sell_vwap - m.buy_vwap) * m.dt_vol
    m["dt_noti"] = m.dt_vol * (m.buy_vwap + m.sell_vwap) / 2
    m["spread"] = (m.sell_vwap / m.buy_vwap - 1) * 100
    m["rng"] = (m.high - m.low).replace(0, np.nan)
    m["buy_pos"] = (m.buy_vwap - m.low) / m.rng
    m["sell_pos"] = (m.sell_vwap - m.low) / m.rng
    m["part"] = (m.buy_vol + m.sell_vol) / m.volume.replace(0, np.nan)
    v = m.dropna(subset=["buy_vwap", "sell_vwap"])
    v = v[v.dt_vol > 0]
    if len(v) < 200:
        return None
    pv = v[v.buy_pos.between(-.1, 1.1) & v.sell_pos.between(-.1, 1.1)]
    s = v[v.rt > 0.95]
    out = {
        "tid": tid, "name": str(nm.get(tid, "?"))[:12], "n": len(v),
        "days": v.trade_date.nunique(), "per_day": v.groupby("trade_date").size().median(),
        "noti_yi": v.dt_noti.sum() / 1e8,
        "gross": v.dt_pnl.sum() / v.dt_noti.sum() * 100,
        "win": (v.spread > 0).mean() * 100, "part": v.part.median() * 100,
        "rt": v.rt.median(), "buy_pos": pv.buy_pos.median(), "sell_pos": pv.sell_pos.median(),
        "sub_n": len(s),
        "sub_gross": s.dt_pnl.sum() / s.dt_noti.sum() * 100 if len(s) else np.nan,
        "sub_win": (s.spread > 0).mean() * 100 if len(s) else np.nan,
    }
    return out


tids = ["9B2Y", "9661", "8888", "9268", "9800", "1480", "1650"]
rows = [r for r in (row(t) for t in tids) if r]
d = pd.DataFrame(rows)
print(f"同窗期 {W0} ~ {W1}\n")
print(f"{'分點':<6}{'名稱':<13}{'n':>7}{'日數':>5}{'日筆':>6}{'名目億':>8}{'毛邊際%':>9}"
      f"{'勝率':>7}{'參與%':>7}{'當沖度':>7}{'買位':>6}{'賣位':>6}"
      f"{'|純沖n':>8}{'純沖毛%':>9}{'純沖勝率':>9}")
for r in d.sort_values("gross", ascending=False).itertuples():
    print(f"{r.tid:<6}{r.name:<13}{r.n:>7,}{r.days:>5}{r.per_day:>6.0f}{r.noti_yi:>8.0f}"
          f"{r.gross:>+9.4f}{r.win:>6.1f}%{r.part:>7.2f}{r.rt:>7.3f}{r.buy_pos:>6.3f}"
          f"{r.sell_pos:>6.3f}{r.sub_n:>8,}{r.sub_gross:>+9.4f}{r.sub_win:>8.1f}%")

print("\n=== 全窗期（各檔自己的完整快取）對照 ===")
for tid in tids:
    f = DIR / f"branch_{tid}_pricelevels.pkl"
    if not f.exists():
        continue
    d0 = pd.read_pickle(f)
    print(f"  {tid}: {d0.trade_date.min()} ~ {d0.trade_date.max()} "
          f"({d0.trade_date.nunique()} 日, {len(d0):,} 列)")
