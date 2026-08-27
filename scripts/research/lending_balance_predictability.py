#!/usr/bin/env python3
"""借券餘額變動對 T+1~T+20 橫斷面報酬的預測力（Asquith-Pathak-Ritter 式檢定）。

**假說**：機構空單（借券）餘額上升預測後續負報酬；散戶空單（融券）不具
同等資訊含量（Boehmer, Jones & Zhang 2008）。

**PIT 紀律**：TWSE 借券餘額於 T 日盤後～T+1 早晨才公布，因此訊號日 T 的
部位最早只能在 **T+1 收盤** 建立。所有 forward return 都從 ``close(T+1)``
起算，不含 T+1 當天的跳空——這是保守且可交易的口徑。

**重疊報酬**：k 日 forward return 逐日重疊，t 統計量一律用 Newey-West
(lag=k) HAC 修正，否則 t 值會被高估數倍。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 10, 20)
N_DECILE = 10


def load_panel(db: Path, start: str, end: str) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        px = pd.read_sql_query(
            """
            SELECT stock_id, trade_date, close, adj_close, volume, amount
              FROM stock_daily_bars
             WHERE trade_date BETWEEN ? AND ? AND close > 0
            """,
            conn,
            params=(start, end),
        )
        lend = pd.read_sql_query(
            """
            SELECT stock_id, trade_date, lending_balance, prev_balance,
                   borrow_volume, return_volume
              FROM stock_lending_balance_daily
             WHERE trade_date BETWEEN ? AND ?
            """,
            conn,
            params=(start, end),
        )
        short = pd.read_sql_query(
            """
            SELECT stock_id, trade_date, short_balance, margin_balance
              FROM stock_margin_daily
             WHERE trade_date BETWEEN ? AND ?
            """,
            conn,
            params=(start, end),
        )
    finally:
        conn.close()

    df = px.merge(lend, on=["stock_id", "trade_date"], how="inner")
    df = df.merge(short, on=["stock_id", "trade_date"], how="left")
    # adj_close 覆蓋約 88%；缺的用 close 補，但除權息日會有跳空 —— 之後用
    # winsorize + 極端報酬過濾處理，不讓單一除權日主導分位數。
    df["px"] = df["adj_close"].where(df["adj_close"].notna(), df["close"])
    return df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("stock_id", sort=False)

    df["advol20"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=15).mean())
    df["adamt20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=15).mean())

    # ---- 訊號（全部只用 date <= T 的資料）----
    lb = df["lending_balance"]
    df["lend_d1"] = g["lending_balance"].diff(1)
    df["lend_d5"] = g["lending_balance"].diff(5)
    df["lend_d20"] = g["lending_balance"].diff(20)

    # 用 20 日均量正規化 = 「幾天的量才能回補」，避免大小股不可比
    for k in (1, 5, 20):
        df[f"sig_dlend{k}"] = df[f"lend_d{k}"] / df["advol20"]
    df["sig_dtc"] = lb / df["advol20"]  # days-to-cover 水位
    df["sig_borrow"] = df["borrow_volume"] / df["advol20"]  # 當日借出強度

    # 對照組：融券（散戶空單）
    df["short_d5"] = g["short_balance"].diff(5)
    df["sig_dshort5"] = df["short_d5"] / df["advol20"]

    # 控制變數
    df["mom20"] = g["px"].transform(lambda s: s / s.shift(20) - 1.0)
    df["logamt"] = np.log(df["adamt20"].clip(lower=1.0))

    # ---- Forward return：entry = close(T+1)，exit = close(T+1+k) ----
    for k in HORIZONS:
        df[f"fwd{k}"] = g["px"].transform(lambda s, k=k: s.shift(-1 - k) / s.shift(-1) - 1.0)
    return df


def apply_filters(df: pd.DataFrame, min_amt: float, min_px: float) -> pd.DataFrame:
    n0 = len(df)
    m = (
        df["adamt20"].notna()
        & (df["adamt20"] >= min_amt)
        & (df["close"] >= min_px)
        & df["advol20"].notna()
        & (df["advol20"] > 0)
    )
    out = df[m].copy()
    print(
        f"  流動性過濾：{n0:,} → {len(out):,} 列 "
        f"(20日均額>={min_amt/1e6:.0f}百萬, 價>={min_px})，"
        f"剩 {out['stock_id'].nunique():,} 檔"
    )
    # 極端 forward return 多半是除權息／減資造成的 adj 缺口，砍尾 0.5%
    for k in HORIZONS:
        col = f"fwd{k}"
        lo, hi = out[col].quantile([0.005, 0.995])
        out.loc[out[col].notna() & ((out[col] < lo) | (out[col] > hi)), col] = np.nan
    return out


def newey_west_t(x: pd.Series, lag: int) -> tuple[float, float]:
    """時間序列均值的 Newey-West t 值（重疊報酬必用）。"""
    x = x.dropna().astype(float)
    n = len(x)
    if n < 20:
        return np.nan, np.nan
    mu = x.mean()
    e = (x - mu).to_numpy()
    var = (e @ e) / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        cov = (e[L:] @ e[:-L]) / n
        var += 2.0 * w * cov
    if var <= 0:
        return mu, np.nan
    return mu, mu / np.sqrt(var / n)


def decile_study(df: pd.DataFrame, signal: str, label: str) -> pd.DataFrame:
    rows = []
    sub = df[df[signal].notna()]
    for k in HORIZONS:
        col = f"fwd{k}"
        d = sub[sub[col].notna()].copy()
        if d.empty:
            continue
        # 每日獨立分十等分（橫斷面），至少要有 N_DECILE*3 檔才分
        d = d.groupby("trade_date", sort=False).filter(lambda x: len(x) >= N_DECILE * 3)
        if d.empty:
            continue
        d["dec"] = d.groupby("trade_date", sort=False)[signal].transform(
            lambda s: pd.qcut(s.rank(method="first"), N_DECILE, labels=False) + 1
        )
        daily = d.groupby(["trade_date", "dec"])[col].mean().unstack()
        spread = daily[N_DECILE] - daily[1]
        mu_s, t_s = newey_west_t(spread, lag=k)
        rec = {"horizon": k, "n_obs": len(d), "n_days": daily.shape[0]}
        for q in (1, 2, 5, 9, 10):
            if q in daily:
                rec[f"D{q}"] = daily[q].mean() * 100
        rec["D10-D1_%"] = mu_s * 100
        rec["NW_t"] = t_s
        rows.append(rec)
    out = pd.DataFrame(rows)
    print(f"\n===== {label}  (signal={signal}) =====")
    if out.empty:
        print("  (無足夠資料)")
        return out
    print(out.to_string(index=False, float_format=lambda v: f"{v:8.3f}"))
    return out


def fama_macbeth(df: pd.DataFrame, signal: str, horizon: int) -> None:
    """控制動能與流動性後，訊號是否還有邊際解釋力。"""
    col = f"fwd{horizon}"
    d = df[[signal, col, "mom20", "logamt", "trade_date"]].dropna()
    d = d.groupby("trade_date", sort=False).filter(lambda x: len(x) >= 50)
    if d.empty:
        print("  (Fama-MacBeth 資料不足)")
        return
    coefs = []
    for _, grp in d.groupby("trade_date", sort=False):
        # 橫斷面 z-score，讓係數可比
        X = grp[[signal, "mom20", "logamt"]].apply(
            lambda s: (s - s.mean()) / (s.std(ddof=0) or 1.0)
        )
        X.insert(0, "const", 1.0)
        y = grp[col].to_numpy()
        try:
            beta, *_ = np.linalg.lstsq(X.to_numpy(), y, rcond=None)
        except np.linalg.LinAlgError:
            continue
        coefs.append(beta)
    if not coefs:
        return
    C = pd.DataFrame(coefs, columns=["const", signal, "mom20", "logamt"])
    print(f"\n  Fama-MacBeth (fwd{horizon}, {len(C)} 個橫斷面):")
    for c in [signal, "mom20", "logamt"]:
        mu, t = newey_west_t(C[c], lag=horizon)
        print(f"    {c:<14} 平均係數={mu*100:+7.4f}%   NW_t={t:+6.2f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=os.path.expanduser("~/goldenstocks-data/data/stocks.db"))
    p.add_argument("--start", default="2025-08-19")
    p.add_argument("--end", default="2026-08-19")
    p.add_argument("--min-amt", type=float, default=20e6, help="20日均成交額下限(元)")
    p.add_argument("--min-px", type=float, default=10.0)
    a = p.parse_args(argv)

    print(f"載入 {a.start} .. {a.end}")
    df = load_panel(Path(a.db), a.start, a.end)
    print(f"  合併後 {len(df):,} 列 / {df['stock_id'].nunique():,} 檔")
    df = build_features(df)
    df = apply_filters(df, a.min_amt, a.min_px)

    print("\n" + "=" * 78)
    print("主檢定：借券餘額變動（機構空單）")
    print("=" * 78)
    decile_study(df, "sig_dlend5", "H1a  Δ借券餘額(5日)/20日均量")
    decile_study(df, "sig_dlend1", "H1b  Δ借券餘額(1日)/20日均量")
    decile_study(df, "sig_dlend20", "H1c  Δ借券餘額(20日)/20日均量")
    decile_study(df, "sig_dtc", "H1d  借券餘額水位 days-to-cover")
    decile_study(df, "sig_borrow", "H1e  當日借出量/20日均量")

    print("\n" + "=" * 78)
    print("對照組：融券（散戶空單）—— 若 Boehmer et al. 成立，這裡該弱很多")
    print("=" * 78)
    decile_study(df, "sig_dshort5", "H2  Δ融券餘額(5日)/20日均量")

    print("\n" + "=" * 78)
    print("控制動能與流動性後（Fama-MacBeth）")
    print("=" * 78)
    for h in (5, 20):
        fama_macbeth(df, "sig_dlend5", h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
