#!/usr/bin/env python3
"""正式對照組：水平 Donchian Channel vs 斜率型 Linear Regression Channel。

背景：使用者發現這幾輪回測（tx_donchian_intraday_faithful.py / _ablation.py）全部都是
水平型 Donchian（rolling max/min），跟參考文章裡的 Linear Regression Channel／Andrews
Pitchfork（斜率型、跟著趨勢角度走）以及 production `causal_engine` 自己的「cell」視覺化
（slow_cell_0806_day_night_8each.png，也是斜率型、每段獨立配適角度的平行通道）是不同的
數學物件。這支腳本做「正式對照組」：同一套訊號狀態機（RSI exit + cooldown + ATR濾網，
完全比照 tx_donchian_intraday_ablation.py），只把「通道怎麼算」這一個變數換掉，兩種
channel geometry 各跑 baseline 與 no_rsi_exit（ablation 已證實影響最大的機制）共 4 組，
同一份 8/6 K棒互相比較。

Linear Regression Channel 算法（來源：先前導讀 Forex Training Group 文章的標準定義，
斜率型三線）：對每個 rolling window（長度與 Donchian 一致，同一個 DONCHIAN_WINDOW=11，
公平比較），用最小平方法對 window 內的 Close 配適一條迴歸線，中線 = 該 window 最後一點
的配適值，上/下軌 = 中線 ± 2 個殘差標準差（經典 ±2 std 慣例）。跟 Donchian 一樣對
Upper/Lower/Mid 做 shift(1)，禁止用到當根未知資訊。

⚠️ 這不是 production causal_engine 的「cell」演算法本身（那是專屬、逐段配適角度的
分段迴歸系統，需要另外讀 src/tmf_channel/causal_engine.py 才能忠實複製，本腳本沒有
移植它）——這裡做的是「斜率型 vs 水平型」這個較大分類下的一個標準代表（Linear
Regression Channel），用來回答「通道形狀本身有沒有差」這個問題。
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_donchian_intraday_faithful import (  # noqa: E402
    ATR_PERIOD,
    DONCHIAN_WINDOW,
    RSI_EXIT,
    RSI_PERIOD,
    calculate_atr,
    calculate_rsi,
    fetch_today_bars,
    simulate_pnl,
)

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
LRC_STD_MULT = 2.0
COOLDOWN_BARS = 8


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def calculate_donchian_channel(data: pd.DataFrame, window: int) -> pd.DataFrame:
    dataset = data.copy()
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    return dataset


def _ols_slope_intercept(y: np.ndarray) -> tuple[float, float]:
    n = len(y)
    x = np.arange(n, dtype=float)
    x_mean, y_mean = x.mean(), y.mean()
    denom = ((x - x_mean) ** 2).sum()
    if denom == 0:
        return 0.0, y_mean
    slope = ((x - x_mean) * (y - y_mean)).sum() / denom
    intercept = y_mean - slope * x_mean
    return slope, intercept


def _lrc_mid(y: np.ndarray) -> float:
    n = len(y)
    slope, intercept = _ols_slope_intercept(y)
    return intercept + slope * (n - 1)  # window 最後一點的配適值


def _lrc_std(y: np.ndarray) -> float:
    n = len(y)
    slope, intercept = _ols_slope_intercept(y)
    x = np.arange(n, dtype=float)
    resid = y - (intercept + slope * x)
    return float(np.sqrt((resid ** 2).mean()))


def calculate_lrc_channel(data: pd.DataFrame, window: int, k: float = LRC_STD_MULT) -> pd.DataFrame:
    dataset = data.copy()
    mid = dataset["Close"].rolling(window).apply(_lrc_mid, raw=True)
    std = dataset["Close"].rolling(window).apply(_lrc_std, raw=True)
    dataset["Mid"] = mid
    dataset["Upper"] = mid + k * std
    dataset["Lower"] = mid - k * std
    dataset[["Upper", "Lower", "Mid"]] = dataset[["Upper", "Lower", "Mid"]].shift(1)
    return dataset


def channel_state_machine(
    data: pd.DataFrame,
    channel_fn,
    atr_threshold: float,
    *,
    use_rsi_exit: bool,
    cooldown_bars: int = COOLDOWN_BARS,
) -> pd.DataFrame:
    """跟 tx_donchian_intraday_ablation.py 的狀態機邏輯完全一致，只把通道來源參數化。"""
    dataset = calculate_rsi(data, RSI_PERIOD)
    dataset = channel_fn(dataset, DONCHIAN_WINDOW)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)

    dataset["Signal"] = 1
    dataset["Entry"] = 0
    short, last_entry = False, -cooldown_bars

    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        rsi = dataset["RSI"].iat[i]

        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue

        if (not short) and (i - last_entry >= cooldown_bars) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            dataset.iat[i, dataset.columns.get_loc("Entry")] = -1
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if use_rsi_exit:
                exit_cond = exit_cond or (rsi < RSI_EXIT)
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
                dataset.iat[i, dataset.columns.get_loc("Entry")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    return dataset


def max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    running_max = running_max.where(running_max > 0, 0)
    return float((equity - running_max).min())


def run_combo(df: pd.DataFrame, channel_fn, atr_threshold: float, use_rsi_exit: bool) -> dict:
    sig = channel_state_machine(df, channel_fn, atr_threshold, use_rsi_exit=use_rsi_exit)
    bt, trades = simulate_pnl(sig)
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    return {
        "n_trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "total_pnl": total_pnl,
        "max_drawdown": max_drawdown(bt["equity"]),
        "trades": trades,
        "bt": bt,
    }


def plot_channel_shapes(df: pd.DataFrame, atr_threshold: float, out_dir: Path, trading_day: str) -> Path:
    """視覺確認：水平 Donchian vs 斜率 LRC 疊在同一段價格上，形狀差異一目了然。"""
    _use_cjk_font()
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    donch = calculate_donchian_channel(df, DONCHIAN_WINDOW)
    lrc = calculate_lrc_channel(df, DONCHIAN_WINDOW)

    # 只取日盤前 2 小時放大看形狀（全天疊圖太密看不出差異）
    window_df = df[(df["Datetime"] >= df["Datetime"].iloc[0]) & (df["Datetime"] <= df["Datetime"].iloc[0] + pd.Timedelta(hours=2))]
    idx = window_df.index

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(df["Datetime"].iloc[idx], df["Close"].iloc[idx], color="black", linewidth=1.1, label="TX/TMF 1分K 收盤")
    ax.plot(df["Datetime"].iloc[idx], donch["Upper"].iloc[idx], color="#1F8A65", linestyle="--", linewidth=1.0, label="Donchian 上/下軌（水平階梯）")
    ax.plot(df["Datetime"].iloc[idx], donch["Lower"].iloc[idx], color="#1F8A65", linestyle="--", linewidth=1.0)
    ax.plot(df["Datetime"].iloc[idx], lrc["Upper"].iloc[idx], color="#C0392B", linestyle="-", linewidth=1.0, label="LRC 上/下軌（斜率型 ±2std）")
    ax.plot(df["Datetime"].iloc[idx], lrc["Lower"].iloc[idx], color="#C0392B", linestyle="-", linewidth=1.0)
    ax.plot(df["Datetime"].iloc[idx], lrc["Mid"].iloc[idx], color="#C0392B", linestyle=":", linewidth=0.8, alpha=0.7, label="LRC 中線（迴歸線）")

    ax.set_title(f"通道形狀對照：水平 Donchian vs 斜率 LRC（同一個 {DONCHIAN_WINDOW} bar window，{trading_day} 日盤前2小時放大）")
    ax.set_ylabel("指數點")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path = fig_dir / f"channel_shape_compare_{trading_day.replace('-', '')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_equity_control_group(results: dict, out_dir: Path, trading_day: str) -> Path:
    _use_cjk_font()
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    colors = {
        "donchian_baseline": "#2C3E50", "donchian_no_rsi_exit": "#7F8C8D",
        "lrc_baseline": "#C0392B", "lrc_no_rsi_exit": "#E67E22",
    }
    styles = {
        "donchian_baseline": "-", "donchian_no_rsi_exit": "--",
        "lrc_baseline": "-", "lrc_no_rsi_exit": "--",
    }

    fig, ax = plt.subplots(figsize=(14, 6))
    for name, r in results.items():
        bt = r["bt"].copy()
        gap = bt["Datetime"].diff() > pd.Timedelta(minutes=5)
        eq = bt["equity"].copy()
        eq[gap] = np.nan
        ax.plot(bt["Datetime"], eq, label=f"{name}（終值 {r['total_pnl']:,.1f}pt, {r['n_trades']}筆）",
                 color=colors[name], linestyle=styles[name], linewidth=1.6)

    ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_title(f"TX/TMF {trading_day} 正式對照組：Donchian(水平) vs LRC(斜率) × baseline/no_rsi_exit")
    ax.set_ylabel("累計損益 (pt)")
    ax.set_xlabel("時間")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path = fig_dir / f"channel_geometry_control_equity_{trading_day.replace('-', '')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def main() -> None:
    df, meta = fetch_today_bars()
    trading_day = meta["trading_day"]
    print(f"trading_day={trading_day}  n_bars={len(df)}")

    atr_probe = calculate_atr(df, ATR_PERIOD)["ATR"].dropna()
    atr_threshold = float(np.percentile(atr_probe, 5))

    combos = {
        "donchian_baseline": (calculate_donchian_channel, True),
        "donchian_no_rsi_exit": (calculate_donchian_channel, False),
        "lrc_baseline": (calculate_lrc_channel, True),
        "lrc_no_rsi_exit": (calculate_lrc_channel, False),
    }

    results = {}
    for name, (channel_fn, use_rsi_exit) in combos.items():
        results[name] = run_combo(df, channel_fn, atr_threshold, use_rsi_exit)

    print("\n=== 正式對照組：Donchian(水平) vs LRC(斜率) ===")
    rows = []
    for name, r in results.items():
        print(f"{name:24s}  trades={r['n_trades']:3d}  win_rate={r['win_rate']:5.1f}%  "
              f"pnl={r['total_pnl']:>10,.1f}pt  max_dd={r['max_drawdown']:>10,.1f}pt")
        rows.append({"combo": name, **{k: v for k, v in r.items() if k not in ("trades", "bt")}})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUT_DIR / f"channel_geometry_control_summary_{trading_day.replace('-', '')}.csv"
    pd.DataFrame(rows).to_csv(summary_path, index=False)
    print(f"\nsummary saved: {summary_path}")

    shape_path = plot_channel_shapes(df, atr_threshold, OUT_DIR, trading_day)
    print(f"shape comparison chart: {shape_path}")

    equity_path = plot_equity_control_group(results, OUT_DIR, trading_day)
    print(f"equity control-group chart: {equity_path}")


if __name__ == "__main__":
    main()
