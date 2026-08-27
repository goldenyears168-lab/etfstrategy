#!/usr/bin/env python3
"""TX/TMF 台指期 8/6 日內高頻回測 — 忠實移植 donchian_strategy (ikonushok) 原作者邏輯。

來源：/tmp/repo-review/donchian_strategy/intra_channel_trading/scripts/strategy.py
（`donchian_rsi_exit_only`）+ scripts/indicators.py + configs/config_donchian_rsi.yaml

刻意「先忠於原作者」——這版不套用先前 tx_donchian_regime_smoketest.py 裡設計的
ADX/regime filter/fixed-fractional risk sizing，訊號邏輯與參數盡量照抄原 repo：

    donchian_window=11, rsi_period=18, rsi_exit=42, cooldown_bars=8,
    atr_enabled=True, atr_period=20

原作者的訊號狀態機（讀原始碼逐行確認，非猜測）：
    - 預設「多單」狀態（Signal=1，在其 backtesting.py 引擎裡 1 對應 long_signal）
    - 價格衝過前一根 Donchian 上軌 → 翻空（淡出突破，賭它會拉回），cooldown_bars 內不可再翻空
    - 翻空後，價格跌破前一根下軌「或」RSI 冷卻到 rsi_exit 以下 → 轉回多單
    - ATR 低於門檻時整根跳過（視為零波動，不產生新訊號，signal 維持前值）
    - 這是「常駐倉位」系統：永遠持有多單或空單，沒有「空手」狀態

唯一必要的調整（原始碼機制性因素，非策略邏輯改動）：
    - atr_threshold 原文 0.0001 是 EURGBP 報價尺度（1 pip），TX/TMF 報價在 44000 點量級，
      數值不能直接套用。改用「今天 ATR 分布的第 5 百分位」重現原作者「幾乎不濾掉東西、只擋
      真正零波動的K棒」的**設計意圖**，而非重新設計濾網邏輯。
    - lot size：原文 FX 固定 100,000 單位是外匯 lot 慣例，這裡改成固定 1 口（常駐多/空各 1 口），
      point value 用 TX 大台 NT$200/點（使用者原始需求就是 TX，這裡的 8/6 價格序列取自
      TMF 富邦即時看板的 TAIEX 期貨報價，數值上 TX/TMF 追蹤同一個標的指數，僅乘數不同）。

資料來源：讀取 TMF 富邦實盤儀表板 http://100.81.2.33:8770/api/state 的 `bars` 陣列（今日 1 分鐘
K棒，日盤+夜盤），純唯讀 GET 公開 JSON，不碰任何下單流程或富邦連線本身。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DASHBOARD_STATE_URL = "http://100.81.2.33:8770/api/state"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
POINT_VALUE_NTD = 200  # TX 大台；資料序列取自 TMF 看板，僅取其追蹤的指數價格

# 原作者 repo configs/config_donchian_rsi.yaml 的「優化後」參數組，逐值照搬
DONCHIAN_WINDOW = 11
RSI_PERIOD = 18
RSI_EXIT = 42
COOLDOWN_BARS = 8
ATR_PERIOD = 20


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def fetch_today_bars() -> pd.DataFrame:
    with urllib.request.urlopen(DASHBOARD_STATE_URL, timeout=15) as resp:
        payload = json.loads(resp.read())
    bars = payload.get("bars") or []
    if not bars:
        raise RuntimeError("dashboard /api/state 沒有回傳 bars")
    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df = df.sort_values("Datetime").drop_duplicates("Datetime").reset_index(drop=True)
    # 只留原作者 data_loader.py 契約要求的欄位；儀表板多帶的 ws/wl/regime/pv 等欄位
    # 幾乎每行都是 NaN，混進來會讓後面忠實照搬的 dataset.dropna() 把整份資料清空。
    df = df[["Datetime", "Open", "High", "Low", "Close", "Volume"]]
    meta = {
        "trading_day": payload.get("trading_day"),
        "summary_all": payload.get("summary_all"),
        "n_bars": payload.get("n_bars"),
        "asof": payload.get("asof"),
    }
    return df, meta


# ---- 原作者 scripts/indicators.py，逐函式照搬 ----

def calculate_donchian(data: pd.DataFrame, donchian_window: int) -> pd.DataFrame:
    dataset = data.copy()
    dataset["Upper"] = dataset["High"].rolling(donchian_window).max()
    dataset["Lower"] = dataset["Low"].rolling(donchian_window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    return dataset


def calculate_rsi(data: pd.DataFrame, rsi_period: int) -> pd.DataFrame:
    dataset = data.copy()
    delta = dataset["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
    rs = gain / loss
    dataset["RSI"] = 100 - (100 / (1 + rs))
    dataset["RSI"] = dataset["RSI"].shift(1)
    return dataset


def calculate_atr(data: pd.DataFrame, atr_period: int) -> pd.DataFrame:
    dataset = data.copy()
    tr = pd.concat(
        [
            dataset["High"] - dataset["Low"],
            (dataset["High"] - dataset["Close"].shift()).abs(),
            (dataset["Low"] - dataset["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    dataset["ATR"] = tr.rolling(atr_period).mean()
    dataset["ATR"] = dataset["ATR"].shift(1)
    return dataset


# ---- 原作者 scripts/strategy.py 的 donchian_rsi_exit_only，逐行照搬狀態機 ----

def donchian_rsi_exit_only(data: pd.DataFrame, atr_threshold: float) -> pd.DataFrame:
    dataset = calculate_rsi(data, RSI_PERIOD)
    dataset = calculate_donchian(dataset, DONCHIAN_WINDOW)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna().reset_index(drop=True)

    dataset["Signal"] = 1  # 原文預設：1 = 多單狀態
    dataset["Entry"] = 0
    short, last_entry = False, -COOLDOWN_BARS

    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        rsi = dataset["RSI"].iat[i]

        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue

        if (not short) and (i - last_entry >= COOLDOWN_BARS) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            dataset.iat[i, dataset.columns.get_loc("Entry")] = -1
            short = True
            last_entry = i
        elif short and (price < dataset["Lower"].iat[i - 1] or rsi < RSI_EXIT):
            short = False
            dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            dataset.iat[i, dataset.columns.get_loc("Entry")] = 1
        else:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    return dataset


def simulate_pnl(dataset: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """常駐倉位：Signal 翻轉當根用該根 Close 平舊倉開新倉（trade_on_close 慣例）。"""
    trades: list[dict] = []
    equity = [0.0]
    pos_dir = dataset["Signal"].iat[0]
    entry_price = dataset["Close"].iat[0]
    entry_time = dataset["Datetime"].iat[0]
    running_pnl = 0.0

    for i in range(1, len(dataset)):
        sig = dataset["Signal"].iat[i]
        price = dataset["Close"].iat[i]
        if sig != pos_dir:
            pnl_pts = (price - entry_price) if pos_dir == 1 else (entry_price - price)
            pnl = pnl_pts  # 單位：指數點（不假設合約乘數，避免 TX/TMF 混用爭議）
            running_pnl += pnl
            trades.append(
                {
                    "direction": "long" if pos_dir == 1 else "short",
                    "entry_time": entry_time,
                    "exit_time": dataset["Datetime"].iat[i],
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl": pnl,
                }
            )
            pos_dir = sig
            entry_price = price
            entry_time = dataset["Datetime"].iat[i]
        equity.append(running_pnl)

    dataset = dataset.copy()
    dataset["equity"] = equity
    return dataset, trades


def plot_results(df: pd.DataFrame, trades: list[dict], out_dir: Path, trading_day: str) -> Path:
    _use_cjk_font()
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [2.2, 1]}, sharex=True
    )

    # 日盤(13:45收)→夜盤(15:00開)中間沒有K棒，用連續時間軸畫線會被直接拉一條假線過去；
    # 在時間跳空處插入 NaN 打斷線段，純視覺修正，不影響訊號/損益計算。
    plot_df = df.copy()
    gap = plot_df["Datetime"].diff() > pd.Timedelta(minutes=5)
    for col in ("Close", "Upper", "Lower"):
        plot_df.loc[gap, col] = np.nan

    ax1.plot(plot_df["Datetime"], plot_df["Close"], color="black", linewidth=0.8, label="TX/TMF 1分K 收盤")
    ax1.plot(plot_df["Datetime"], plot_df["Upper"], color="#1F8A65", linestyle="--", linewidth=0.7,
              label=f"Donchian 上軌 ({DONCHIAN_WINDOW}bar, shift1)")
    ax1.plot(plot_df["Datetime"], plot_df["Lower"], color="#C0392B", linestyle="--", linewidth=0.7,
              label=f"Donchian 下軌 ({DONCHIAN_WINDOW}bar, shift1)")

    for t in trades:
        color = "#1F8A65" if t["direction"] == "long" else "#C0392B"
        marker = "^" if t["direction"] == "long" else "v"
        ax1.scatter(t["exit_time"], t["exit_price"], marker=marker, color=color, s=45, zorder=5, alpha=0.85)

    ax1.set_title(f"TX/TMF 台指期 {trading_day} 日內高頻回測 — 忠實移植 donchian_strategy 原作者邏輯"
                   f"（{len(trades)} 筆翻倉, cooldown={COOLDOWN_BARS}bar, window={DONCHIAN_WINDOW}bar）")
    ax1.set_ylabel("指數點")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)

    plot_equity = df["equity"].copy()
    plot_equity[gap] = np.nan
    ax2.plot(df["Datetime"], plot_equity, color="#2C3E50", linewidth=1.1)
    ax2.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_ylabel("累計損益 (pt)")
    ax2.set_xlabel("時間")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    path = fig_dir / f"tx_donchian_intraday_faithful_{trading_day.replace('-', '')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def main() -> None:
    df, meta = fetch_today_bars()
    trading_day = meta["trading_day"] or datetime.now().strftime("%Y-%m-%d")
    print(f"trading_day={trading_day}  n_bars_fetched={len(df)}  asof={meta['asof']}")
    print(f"production 實盤今日成交筆數（對照組，非本回測結果）: {meta['summary_all']}")

    atr_probe = calculate_atr(df, ATR_PERIOD)["ATR"].dropna()
    atr_threshold = float(np.percentile(atr_probe, 5))
    print(f"ATR({ATR_PERIOD}) 5th percentile threshold（取代原文 EURGBP pip 尺度的 0.0001）: {atr_threshold:.2f} 點")

    sig = donchian_rsi_exit_only(df, atr_threshold)
    bt, trades = simulate_pnl(sig)

    n_flip = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)
    print(f"\n=== 忠實移植版 8/6 日內回測（{DONCHIAN_WINDOW}bar/{COOLDOWN_BARS}cooldown） ===")
    print(f"翻倉次數: {n_flip}  勝率: {(len(wins)/n_flip*100) if n_flip else 0:.1f}%  "
          f"累計損益: {total_pnl:,.1f} 點")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_path = OUT_DIR / f"trades_intraday_faithful_{trading_day.replace('-', '')}.csv"
    pd.DataFrame(trades).to_csv(trades_path, index=False)
    print(f"trades saved: {trades_path}")

    chart_path = plot_results(bt, trades, OUT_DIR, trading_day)
    print(f"chart saved: {chart_path}")


if __name__ == "__main__":
    main()
