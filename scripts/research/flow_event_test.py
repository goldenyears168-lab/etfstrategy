#!/usr/bin/env python3
"""事件式流量檢定 —— 橫斷面每日重排走不通，改成「觸發一次、持有 K 日」。

**動機**（2026-08-26）：15 個流量因子沒有一個淨值為正，但投信買賣超的
gross 是全場最高（+0.1037%/日、t=+5.92）。死因不是訊號弱，是**每日重排
造成 51% 換手**。事件式把成本從「每天扣」變成「一趟扣一次 0.471%」，
換手由持有天數決定。

**問的問題變成**：一次投信買超事件，在持有 K 天內賺得到 0.471% 嗎？

三個設計上的要點：
  · **fresh 過濾**：同一檔在 M 天內只算一次事件，否則連續買超會被
    重複計成多次事件、把報酬重複計價。
  · **報酬去均值**：對同一事件日的全市場取超額，否則測到的是大盤。
  · **t 值按事件日 cluster**：同一天觸發的事件共享市場衝擊，
    當成獨立觀測會嚴重高估顯著性。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "reports" / "research" / "chip-signal-daily-horizon" / "flow_panel.pkl"
COST = 0.471


def forward_returns(sids: set[str], start: str, horizons: tuple[int, ...]) -> pd.DataFrame:
    """open(T+1) → close(T+K)。用完整價格序列算，不受面板的流動性濾網打洞影響。"""
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, close FROM stock_daily_bars
            WHERE trade_date >= ? AND close IS NOT NULL""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    px = px[px.stock_id.isin(sids)].sort_values(["stock_id", "trade_date"])
    g = px.groupby("stock_id", group_keys=False)
    px["o1"] = g.open.shift(-1)
    px["d1"] = g.trade_date.shift(-1)
    dates = np.sort(px.trade_date.unique())
    nxt = dict(zip(dates[:-1], dates[1:]))
    px.loc[px.d1 != px.trade_date.map(nxt), "o1"] = np.nan   # 必須是真次日
    for k in horizons:
        px[f"c{k}"] = g.close.shift(-k)
        px[f"r{k}"] = px[f"c{k}"] / px.o1 - 1
    return px[["stock_id", "trade_date", "o1"] + [f"r{k}" for k in horizons]]


def cluster_t(df: pd.DataFrame, col: str) -> tuple[float, float, int]:
    """按事件日聚類的 t：先對每個事件日取平均，再用日層級算標準誤。"""
    v = df.dropna(subset=[col])
    if v.empty:
        return np.nan, np.nan, 0
    per_day = v.groupby("trade_date")[col].mean()
    if len(per_day) < 10:
        return v[col].mean() * 100, np.nan, len(v)
    t = per_day.mean() / (per_day.std(ddof=1) / np.sqrt(len(per_day)))
    return per_day.mean() * 100, t, len(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--col", default="f_itc", help="流量欄位（f_itc 投信 / f_for 外資 / f_3i 三大法人）")
    ap.add_argument("--pct", type=float, default=0.99, help="觸發門檻：當日橫斷面百分位")
    ap.add_argument("--fresh", type=int, default=20, help="同檔幾天內只算一次事件")
    args = ap.parse_args()

    HZ = (1, 3, 5, 10, 20, 40)
    d = pd.read_pickle(PANEL)
    d = d.dropna(subset=[args.col])
    fwd = forward_returns(set(d.stock_id), d.trade_date.min(), HZ)
    d = d.merge(fwd, on=["stock_id", "trade_date"], how="left")
    # 觸發：當日橫斷面百分位 >= pct
    d["rk"] = d.groupby("trade_date")[args.col].rank(pct=True)
    ev = d[d.rk >= args.pct].sort_values(["stock_id", "trade_date"]).copy()
    # fresh 過濾：同檔 N 個「事件」內只留第一次（用交易日序號近似）
    dates = np.sort(d.trade_date.unique())
    pos = {t: i for i, t in enumerate(dates)}
    ev["i"] = ev.trade_date.map(pos)
    keep, last = [], {}
    for r in ev.itertuples():
        if r.stock_id not in last or r.i - last[r.stock_id] >= args.fresh:
            keep.append(r.Index)
            last[r.stock_id] = r.i
    ev = ev.loc[keep]
    # 超額：對同一事件日的全市場取均值差
    for k in HZ:
        mkt = d.groupby("trade_date")[f"r{k}"].mean().rename(f"m{k}")
        ev = ev.join(mkt, on="trade_date")
        ev[f"x{k}"] = ev[f"r{k}"] - ev[f"m{k}"]

    print(f"因子 {args.col}　觸發門檻 前 {(1-args.pct)*100:.0f}%　"
          f"fresh {args.fresh} 日　事件 {len(ev):,} 次 / {ev.trade_date.nunique()} 日")
    print(f"面板 {len(d):,} stock-day · {d.trade_date.min()}~{d.trade_date.max()}\n")
    print(f"{'持有':>5}{'超額報酬':>11}{'t(日聚類)':>11}{'勝率':>8}"
          f"{'每趟成本':>10}{'淨值/趟':>10}{'年化*':>9}{'n':>7}")
    for k in HZ:
        m, t, n = cluster_t(ev, f"x{k}")
        if np.isnan(m):
            continue
        win = (ev[f"x{k}"] > 0).mean() * 100
        net = m - COST
        ann = net * (242 / k)                       # 假設資金可連續投入
        print(f"{k:>4}日{m:>+10.4f}%{t:>+11.2f}{win:>7.1f}%{COST:>9.3f}%"
              f"{net:>+9.4f}%{ann:>+8.2f}%{n:>7,}")
    print("\n* 年化假設資金可連續投入且事件供給充足，是樂觀上界。")
    print("  超額＝對同一事件日全市場取均值差；t 按事件日聚類（同日事件共享市場衝擊）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
