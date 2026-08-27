#!/usr/bin/env python3
"""盲點4量化：session結尾未平倉部位被靜默丟棄的記帳缺口。

simulate_pnl_realistic 只記錄「已翻倉平掉」的交易——always-in 系統裡，每個
(day, sess, sleeve) 區塊的最後一個部位（從最後一次翻倉持有到session結束）從來
沒被結算過。83天×2session×3sleeve≈498個區塊，每塊漏掉1個部位，相對1,839筆
已計交易是~27%的部位數缺口。這影響：
  (1) 之前所有輪次的總損益數字（方向未知——尾部位可能賺可能虧）
  (2) mins_to_end/mins_since_open 特徵的IC（倖存者偏差：晚進場且被記錄=快速
      解決的交易，慢的都被丟掉了）

修法：force-close——session最後一根bar用收盤價強制平倉並計成本，跟
channel_lab 鐵律（session邊界機械平倉）一致。這支腳本量化差異。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402

COOLDOWN = 8
SLEEVES = [34, 55, 89]


def simulate_with_forceclose(dataset: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """逐行複製 simulate_pnl_realistic(fill_lag_bars=1) 的邏輯，外加 session 結尾
    force-close。回傳 (原本會記錄的trades, force-close的尾部位trades)。"""
    trades: list[dict] = []
    tail: list[dict] = []
    n = len(dataset)
    pos_dir = dataset["Signal"].iat[0]
    fill_idx0 = min(FILL_LAG_BARS, n - 1)
    entry_price = dataset["Open"].iat[fill_idx0]
    entry_time = dataset["Datetime"].iat[fill_idx0]
    pending = None

    for i in range(1, n):
        sig = dataset["Signal"].iat[i]
        if pending is not None and i == pending["fill_at"]:
            fill_price = dataset["Open"].iat[i]
            pnl_pts = (fill_price - entry_price) if pos_dir == 1 else (entry_price - fill_price)
            trades.append(dict(direction="long" if pos_dir == 1 else "short",
                                 entry_time=entry_time, exit_time=dataset["Datetime"].iat[i],
                                 entry_price=entry_price, exit_price=fill_price,
                                 pnl=pnl_pts - COST_PTS_PER_TRADE))
            pos_dir = pending["new_dir"]
            entry_price = fill_price
            entry_time = dataset["Datetime"].iat[i]
            pending = None
        if sig != pos_dir and pending is None:
            pending = dict(fill_at=min(i + FILL_LAG_BARS, n - 1), new_dir=sig)

    # force-close 最後的未平倉部位（session最後一根收盤價）
    last_close = dataset["Close"].iat[n - 1]
    pnl_pts = (last_close - entry_price) if pos_dir == 1 else (entry_price - last_close)
    tail.append(dict(direction="long" if pos_dir == 1 else "short",
                       entry_time=entry_time, exit_time=dataset["Datetime"].iat[n - 1],
                       entry_price=entry_price, exit_price=last_close,
                       pnl=pnl_pts - COST_PTS_PER_TRADE))
    return trades, tail


def run_block(df: pd.DataFrame, window: int, atr_threshold: float):
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return [], []
    ds = calculate_rsi(df, RSI_PERIOD)
    ds["Upper"] = ds["High"].rolling(window).max()
    ds["Lower"] = ds["Low"].rolling(window).min()
    ds[["Upper", "Lower"]] = ds[["Upper", "Lower"]].shift(1)
    ds = calculate_atr(ds, ATR_PERIOD)
    ds = ds.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(ds) < 20:
        return [], []
    ds["Signal"] = 1
    short, last_entry = False, -COOLDOWN
    for i in range(1, len(ds)):
        price = ds["Close"].iat[i]
        if ds["ATR"].iat[i] < atr_threshold:
            ds.iat[i, ds.columns.get_loc("Signal")] = ds["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > ds["Upper"].iat[i - 1]:
            ds.iat[i, ds.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
        else:
            if short and price < ds["Lower"].iat[i - 1]:
                short = False
                ds.iat[i, ds.columns.get_loc("Signal")] = 1
            else:
                ds.iat[i, ds.columns.get_loc("Signal")] = ds["Signal"].iat[i - 1]
    return simulate_with_forceclose(ds)


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    atr_threshold = compute_global_atr_threshold(days, stripped)

    counted, tails = [], []
    for day in days:
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            for w in SLEEVES:
                tr, tl = run_block(seg, w, atr_threshold)
                for t in tr:
                    t.update(day=day, sess=sess, window=w)
                for t in tl:
                    t.update(day=day, sess=sess, window=w)
                counted.extend(tr)
                tails.extend(tl)

    c_pnl = sum(t["pnl"] for t in counted)
    t_pnl = sum(t["pnl"] for t in tails)
    print(f"已計交易: {len(counted)}筆  總損益 {c_pnl:,.1f}pt  （對照第十一輪 79,863.9pt）")
    print(f"被丟棄的尾部位: {len(tails)}個  未計損益合計 {t_pnl:,.1f}pt")
    print(f"修正後真實總損益: {c_pnl + t_pnl:,.1f}pt  （缺口佔比 {abs(t_pnl)/(abs(c_pnl))*100:.1f}%）")
    tail_df = pd.DataFrame(tails)
    print(f"\n尾部位分布: 均值={tail_df['pnl'].mean():.1f}pt  中位數={tail_df['pnl'].median():.1f}pt  "
          f"勝率={(tail_df['pnl']>0).mean()*100:.1f}%")
    print(f"尾部位持有時間長（從最後一次翻倉到收盤），跟已計交易的分布完全不同——")
    print(f"這正是 mins_to_end 特徵IC被污染的機制：晚進場的『慢部位』全部躺在這裡沒被計入。")
    by_sess = tail_df.groupby('sess')['pnl'].agg(['count','sum','mean'])
    print(f"\n尾部位 by session:\n{by_sess}")


if __name__ == "__main__":
    main()
