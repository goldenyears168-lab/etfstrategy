#!/usr/bin/env python3
"""多 window 併行版：w34 + w55 + w89 各自獨立開倉，不縮小單一 window 換頻率。

沿用 tx_channel_recalibrate.py 已驗證的訊號邏輯（同一套 rsi_exit=False, cooldown=8,
ATR零波動濾網）與 tx_channel_geometry_realism_check.py 的寫實執行假設（延遲1根K棒
成交 + 5.9pt/筆成本）。三組 window 各自獨立記帳（各自的 Signal/position/pnl 完全
不共用狀態），像 config/backtest_standard.yaml 的 simulate_slot_portfolio() 那樣
把三本獨立帳的損益直接加總——頻率是疊加出來的，不是壓縮單一 window 換來的。

用跟 window=233 那輪同樣的 5摺滾動驗證（IS=累積歷史 expanding window，每摺各自
用 IS-only 計算 ATR 門檻，OOS=10~13天不重疊區塊）驗證這個組合的日均損益/頻率是否
穩定，並且做一個先前承諾要查的診斷：三個 window 會不會常常同時間點同方向觸發
（如果高度重疊，"併行"帶來的其實不是真分散，是同一個訊號被放大三倍）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD, calculate_atr  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_day_bars, load_days  # noqa: E402
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
SLEEVES = [34, 55, 89]
COOLDOWN = 8


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def run_sleeve_on_day_direct(df: pd.DataFrame, window: int, atr_threshold: float) -> list[dict]:
    """跟 tx_channel_recalibrate.run_combo_on_day 完全同邏輯，直接內嵌避免 import 綁死 window 常數。"""
    from tx_channel_geometry_control import RSI_PERIOD, calculate_rsi

    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return []

    dataset["Signal"] = 1
    short, last_entry = False, -COOLDOWN
    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    _, trades = simulate_pnl_realistic(dataset, fill_lag_bars=FILL_LAG_BARS, cost_pts_per_trade=COST_PTS_PER_TRADE)
    for t in trades:
        t["window"] = window
    return trades


def run_portfolio_on_day(df: pd.DataFrame, atr_threshold: float) -> dict:
    all_trades = []
    for w in SLEEVES:
        all_trades.extend(run_sleeve_on_day_direct(df, w, atr_threshold))
    total_pnl = sum(t["pnl"] for t in all_trades)
    return {"n_trades": len(all_trades), "total_pnl": total_pnl, "trades": all_trades}


def evaluate_portfolio(days: list[str], day_bars: dict, atr_threshold: float) -> dict:
    daily_pnls, all_trades = [], []
    for day in days:
        r = run_portfolio_on_day(day_bars[day], atr_threshold)
        daily_pnls.append(r["total_pnl"])
        for t in r["trades"]:
            t["day"] = day
        all_trades.extend(r["trades"])
    arr = np.array(daily_pnls)
    mean_d, std_d = arr.mean(), arr.std()
    sharpe_like = (mean_d / std_d * np.sqrt(252)) if std_d else float("nan")
    return dict(
        total_pnl=arr.sum(), mean_daily_pnl=mean_d, std_daily_pnl=std_d,
        sharpe_like=sharpe_like, total_trades=len(all_trades),
        positive_days=int((arr > 0).sum()), n_days=len(arr),
        trades_per_day=len(all_trades) / len(arr) if len(arr) else 0.0,
        trades=all_trades,
    )


def overlap_diagnostic(trades: list[dict]) -> dict:
    """同一天不同 window 的持倉，是否常常時間重疊、方向相同——檢查『併行=分散』是否成立。"""
    by_day = {}
    for t in trades:
        by_day.setdefault(t["day"], []).append(t)

    same_dir_overlap_minutes = 0.0
    total_position_minutes = 0.0
    for day, day_trades in by_day.items():
        intervals = [(t["entry_time"], t["exit_time"], t["direction"], t["window"]) for t in day_trades]
        for et, xt, d, w in intervals:
            dur = (xt - et).total_seconds() / 60.0
            total_position_minutes += dur
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                e1, x1, d1, w1 = intervals[i]
                e2, x2, d2, w2 = intervals[j]
                if w1 == w2:
                    continue
                lo = max(e1, e2)
                hi = min(x1, x2)
                if lo < hi:
                    overlap_min = (hi - lo).total_seconds() / 60.0
                    if d1 == d2:
                        same_dir_overlap_minutes += overlap_min

    return dict(
        total_position_minutes=round(total_position_minutes, 1),
        same_direction_overlap_minutes=round(same_dir_overlap_minutes, 1),
        overlap_ratio_pct=round(100 * same_dir_overlap_minutes / total_position_minutes, 1) if total_position_minutes else 0.0,
    )


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars(d) for d in days}
    n = len(days)

    print(f"=== 83天全樣本（單一固定門檻，descriptive） ===")
    atr_th_full = compute_global_atr_threshold(days, all_bars)
    full_result = evaluate_portfolio(days, all_bars, atr_th_full)
    print(f"trades/day={full_result['trades_per_day']:.2f}  總損益={full_result['total_pnl']:,.1f}pt  "
          f"日均={full_result['mean_daily_pnl']:,.1f}pt  正報酬天數={full_result['positive_days']}/{full_result['n_days']}  "
          f"Sharpe-like={full_result['sharpe_like']:.2f}")

    overlap = overlap_diagnostic(full_result["trades"])
    print(f"\n=== 重疊診斷 ===")
    print(f"三個window持倉時間總和={overlap['total_position_minutes']:.0f}分鐘, "
          f"其中同方向重疊={overlap['same_direction_overlap_minutes']:.0f}分鐘 "
          f"({overlap['overlap_ratio_pct']:.1f}%)")

    print(f"\n=== 5摺滾動驗證（跟 window=233 那輪同樣的切法） ===")
    bounds = [30, 40, 50, 60, 70, n]
    folds = [(days[:bounds[i]], days[bounds[i]:bounds[i + 1]]) for i in range(len(bounds) - 1)]

    fold_results = []
    for fi, (is_days, oos_days) in enumerate(folds, 1):
        atr_th = compute_global_atr_threshold(is_days, all_bars)
        r = evaluate_portfolio(oos_days, all_bars, atr_th)
        fold_results.append(r)
        print(f"Fold{fi}: OOS={len(oos_days)}天({oos_days[0]}~{oos_days[-1]})  "
              f"trades/day={r['trades_per_day']:.2f}  total_pnl={r['total_pnl']:,.1f}pt  "
              f"sharpe={r['sharpe_like']:.2f}  正報酬天數={r['positive_days']}/{r['n_days']}")

    n_folds_positive = sum(1 for r in fold_results if r["total_pnl"] > 0)
    mean_sharpe = np.mean([r["sharpe_like"] for r in fold_results])
    print(f"\n5摺聯合：{n_folds_positive}/5 摺正報酬，平均Sharpe={mean_sharpe:.2f}")

    # 圖：83天累計權益 + 每摺 OOS 對照
    _use_cjk_font()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 9))

    trades_by_day = {}
    for t in full_result["trades"]:
        trades_by_day.setdefault(t["day"], 0.0)
        trades_by_day[t["day"]] += t["pnl"]
    daily_series = pd.Series({d: trades_by_day.get(d, 0.0) for d in days})
    ax1.plot(pd.to_datetime(daily_series.index), daily_series.cumsum(), color="#2C3E50", linewidth=1.5)
    ax1.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax1.set_title(f"w34+w55+w89 併行組合 — 83天累計損益（單一固定門檻版，非5摺）")
    ax1.set_ylabel("累計損益 (pt)")
    ax1.grid(True, alpha=0.3)

    fold_labels = [f"Fold{i+1}" for i in range(5)]
    fold_pnls = [r["total_pnl"] for r in fold_results]
    fold_sharpes = [r["sharpe_like"] for r in fold_results]
    colors = ["#1F8A65" if p > 0 else "#C0392B" for p in fold_pnls]
    bars = ax2.bar(fold_labels, fold_pnls, color=colors)
    ax2.axhline(0, color="gray", linewidth=0.8)
    for b, p, s in zip(bars, fold_pnls, fold_sharpes):
        ax2.annotate(f"{p:,.0f}pt\nSharpe={s:.2f}", (b.get_x() + b.get_width() / 2, p),
                     ha="center", va="bottom" if p >= 0 else "top", fontsize=8)
    ax2.set_title("5摺 OOS 損益（跟 window=233 單摺同樣切法）")
    ax2.set_ylabel("OOS期間總損益 (pt)")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    chart_path = fig_dir / "multiwindow_portfolio_5fold.png"
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)
    print(f"\nchart saved: {chart_path}")


if __name__ == "__main__":
    main()
