"""
E3/E4 訊號參數穩健性網格分析（研究用，不下單，不改任何 config）。

訊號定義（single-day event 版本，對齊對話中回報的 n=309）：
  - 外資 TX net_oi_vol 的「近3年百分位」<= P
  - 且「過去 W 個交易日內淨OI較 W 日前更負」(net_oi[t] < net_oi[t-W])，即10日加速惡化條件的窗口化版本
  -> 當日事件：用當日 TAIEX (IX0001) 日報酬的「負號」(放空) 作為單日 pnl 近似
     （E3/E4 原始回測用的就是「當日放空、regime style續抱」；此處做的是 single-day event 版，
     跟對話中 n=309 的版本一致，不是 regime 續抱版，避免 regime 版對視窗定義的路徑依賴污染網格比較）

網格：P in {3,5,10,15}(%)  x  W in {5,10,15,20}(交易日)

百分位計算方式：對每個日期 t，取 net_oi_vol 在 [t - 3年, t] 這個 rolling window 內的
經驗百分位 rank（不用未來資料，PIT安全）。

E4 grid：在 E3 條件上再加「close < MA20」三重確認，同一組 P×W 網格重跑一次。

輸出：
  - 每個 (P,W) 組合的 n事件數、平均單日空單報酬(%)、std、t-stat
  - walk-forward：以事件時間序列中位數日期切前後兩段，比較兩段平均報酬與t-stat
  - 熱力圖數值列印（文字網格）＋ 判斷「5%/10日」是否為網格局部最優或只是眾多相近組合之一
"""
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path

DB_PATH = "/Users/jackm4/goldenstocks-data/data/stocks.db"

PERCENTILE_THRESHOLDS = [3, 5, 10, 15]
WINDOWS = [5, 10, 15, 20]


def load_foreign_oi(conn):
    df = pd.read_sql_query(
        """
        SELECT trade_date AS date, net_oi_vol
        FROM futures_institutional_daily
        WHERE futures_id='TX' AND inst_name='外資'
        ORDER BY trade_date
        """,
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    return df


def load_taiex_ohlc(conn):
    df = pd.read_sql_query(
        """
        SELECT date, close
        FROM daily_bars
        WHERE code='IX0001' AND source='yahoo'
        ORDER BY date
        """,
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    # tej close for most recent tail beyond yahoo's 2026-08-10 cutoff
    df2 = pd.read_sql_query(
        """
        SELECT date, close
        FROM daily_bars
        WHERE code='IX0001' AND source='tej' AND close IS NOT NULL
        ORDER BY date
        """,
        conn,
    )
    df2["date"] = pd.to_datetime(df2["date"])
    df2 = df2.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    merged = pd.concat([df, df2[df2["date"] > df["date"].max()]], ignore_index=True)
    merged = merged.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    merged["ret"] = merged["close"].pct_change()
    return merged[["date", "close", "ret"]]


def rolling_percentile_3y(oi: pd.Series, dates: pd.Series) -> pd.Series:
    """PIT rolling percentile of oi[t] within trailing 3-calendar-year window ending at t (inclusive)."""
    vals = oi.values
    dts = dates.values
    n = len(vals)
    out = np.full(n, np.nan)
    window_days = np.timedelta64(3 * 365, "D")
    start_idx = 0
    for i in range(n):
        cutoff = dts[i] - window_days
        while dts[start_idx] < cutoff:
            start_idx += 1
        window = vals[start_idx : i + 1]
        rank = (window <= vals[i]).sum()
        out[i] = rank / len(window) * 100.0
    return pd.Series(out, index=oi.index)


def t_stat(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 2:
        return float("nan")
    return x.mean() / (x.std(ddof=1) / np.sqrt(n))


def eval_events(events: pd.DataFrame, P: int, W: int) -> dict:
    # PIT fix: 外資OI日報在T日收盤後才發布，訊號用T日(含)以前的資料形成，
    # 實際下單/持有的是「下一個交易日」T+1，pnl 用 next_ret（=T+1的報酬）。
    events = events.dropna(subset=["next_ret"])
    short_pnl = -events["next_ret"].values * 100.0  # % pnl of short position, entered next trading day
    n = len(short_pnl)
    if n == 0:
        return dict(P=P, W=W, n=0, mean_pct=np.nan, std_pct=np.nan, t=np.nan,
                     n1=0, wf1_mean=np.nan, wf1_t=np.nan, n2=0, wf2_mean=np.nan, wf2_t=np.nan)
    mean_pct = short_pnl.mean()
    std_pct = short_pnl.std(ddof=1) if n > 1 else np.nan
    t = t_stat(short_pnl)
    dates_evt = events["trade_date"].values
    median_date = np.median(dates_evt.astype("datetime64[ns]").astype(np.int64))
    median_date = np.datetime64(int(median_date), "ns")
    mask1 = events["trade_date"].values <= median_date
    mask2 = ~mask1
    wf1 = short_pnl[mask1]
    wf2 = short_pnl[mask2]
    return dict(
        P=P, W=W, n=n, mean_pct=mean_pct, std_pct=std_pct, t=t,
        n1=len(wf1), wf1_mean=wf1.mean() if len(wf1) else np.nan, wf1_t=t_stat(wf1) if len(wf1) > 1 else np.nan,
        n2=len(wf2), wf2_mean=wf2.mean() if len(wf2) else np.nan, wf2_t=t_stat(wf2) if len(wf2) > 1 else np.nan,
    )


def run_grid(merged: pd.DataFrame, extra_filter: pd.Series | None = None) -> pd.DataFrame:
    rows = []
    for P in PERCENTILE_THRESHOLDS:
        for W in WINDOWS:
            accel = merged["net_oi_vol"] < merged["net_oi_vol"].shift(W)
            sig = (merged["pctile"] <= P) & accel
            if extra_filter is not None:
                sig = sig & extra_filter
            sig = sig.fillna(False)
            events = merged.loc[sig].copy()
            rows.append(eval_events(events, P, W))
    return pd.DataFrame(rows)


def print_grid(rdf: pd.DataFrame, label: str):
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(f"\n========== {label}: full grid ==========")
    print(rdf.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print(f"\n--- {label}: mean_pct grid (rows=P, cols=W) ---")
    print(rdf.pivot(index="P", columns="W", values="mean_pct").to_string(float_format=lambda v: f"{v:.4f}"))

    print(f"\n--- {label}: t-stat grid (rows=P, cols=W) ---")
    print(rdf.pivot(index="P", columns="W", values="t").to_string(float_format=lambda v: f"{v:.3f}"))

    print(f"\n--- {label}: n grid (rows=P, cols=W) ---")
    print(rdf.pivot(index="P", columns="W", values="n").to_string(float_format=lambda v: f"{v:.0f}"))


def main():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    oi = load_foreign_oi(conn)
    px = load_taiex_ohlc(conn)
    conn.close()

    merged = pd.merge(oi, px, on="date", how="inner").sort_values("date").reset_index(drop=True)
    merged["pctile"] = rolling_percentile_3y(merged["net_oi_vol"], merged["date"])
    merged["ma20"] = merged["close"].rolling(20).mean()
    merged["below_ma20"] = merged["close"] < merged["ma20"]
    # PIT: OI report for day T published after T closes -> signal formed using data through T
    # is only tradable on T+1. next_ret/trade_date carry T+1's return/date onto row T.
    merged["next_ret"] = merged["ret"].shift(-1)
    merged["trade_date"] = merged["date"].shift(-1)

    print(f"merged rows: {len(merged)}  date range: {merged['date'].min().date()} ~ {merged['date'].max().date()}")

    e3_grid = run_grid(merged, extra_filter=None)
    print_grid(e3_grid, "E3 (percentile + accel only)")

    e4_grid = run_grid(merged, extra_filter=merged["below_ma20"])
    print_grid(e4_grid, "E4 (E3 + close<MA20)")

    e3_grid.to_csv("/tmp/e3_sensitivity_grid.csv", index=False)
    e4_grid.to_csv("/tmp/e4_sensitivity_grid.csv", index=False)
    print("\nsaved CSV -> /tmp/e3_sensitivity_grid.csv , /tmp/e4_sensitivity_grid.csv")


if __name__ == "__main__":
    main()
