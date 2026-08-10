#!/usr/bin/env python3
"""ATR進場濾網——5摺滾動walk-forward驗證，正式化成策略的一部分。

上一輪的60/40單次切分顯示：用IS期算出的ATR門檻(pooled，不分day/night)套到held-out期，
均pnl從59.54pt提升到79.99pt(+34%)。這輪比照window=233當初的驗證規格（5摺、每摺
10-13天OOS、IS用累積歷史expanding window），確認這個濾網是不是在多段獨立時期都穩定，
不是單次切分的運氣。

新增濾網：在既有的結構性ATR零波動濾網(atr_threshold~11.2，防止死盤)之上，額外加一道
「品質門檻」(quality_threshold，用IS期的atr_at_entry中位數校準，pooled不分day/night)，
擋掉波動度偏低的訊號轉換(不管是短單進場或轉回多單)。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COOLDOWN = 8
SLEEVES = [34, 55, 89]


def run_sleeve_filtered(df: pd.DataFrame, window: int, floor_threshold: float,
                          quality_threshold: float | None) -> list[dict]:
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return []
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
        atr = dataset["ATR"].iat[i]
        if atr < floor_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue
        # 品質濾網：額外要求 atr >= quality_threshold 才允許訊號轉換（進場或轉回多單）
        quality_ok = quality_threshold is None or atr >= quality_threshold
        if (not short) and (i - last_entry >= COOLDOWN) and price > dataset["Upper"].iat[i - 1] and quality_ok:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if short and exit_cond and quality_ok:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    from tx_channel_geometry_realism_check import simulate_pnl_realistic
    _, trades = simulate_pnl_realistic(dataset, fill_lag_bars=FILL_LAG_BARS, cost_pts_per_trade=COST_PTS_PER_TRADE)
    return trades


def evaluate(days: list[str], all_bars: dict, floor_threshold: float, quality_threshold: float | None) -> dict:
    daily_pnls, all_trades = [], []
    for day in days:
        day_trades = []
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            for w in SLEEVES:
                day_trades.extend(run_sleeve_filtered(seg, w, floor_threshold, quality_threshold))
        daily_pnls.append(sum(t["pnl"] for t in day_trades))
        all_trades.extend(day_trades)
    arr = np.array(daily_pnls)
    mean_d, std_d = arr.mean(), arr.std()
    sharpe_like = (mean_d / std_d * np.sqrt(252)) if std_d else float("nan")
    return dict(total_pnl=float(arr.sum()), sharpe_like=float(sharpe_like),
                total_trades=len(all_trades), n_days=len(arr),
                positive_days=int((arr > 0).sum()))


def compute_quality_threshold(days: list[str], all_bars: dict, floor_threshold: float) -> float:
    """從IS天池化算atr_at_entry中位數（pooled，不分day/night——上一輪驗證過這樣比較好）。"""
    atrs = []
    for day in days:
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            for w in SLEEVES:
                trades_with_atr = _collect_entry_atrs(seg, w, floor_threshold)
                atrs.extend(trades_with_atr)
    return float(np.median(atrs)) if atrs else floor_threshold


def _collect_entry_atrs(df: pd.DataFrame, window: int, floor_threshold: float) -> list[float]:
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return []
    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return []
    out = []
    short, last_entry = False, -COOLDOWN
    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        atr = dataset["ATR"].iat[i]
        if atr < floor_threshold:
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > dataset["Upper"].iat[i - 1]:
            short = True; last_entry = i; out.append(atr)
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if short and exit_cond:
                short = False; out.append(atr)
    return out


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    n = len(days)
    bounds = [30, 40, 50, 60, 70, n]
    folds = [(days[:bounds[i]], days[bounds[i]:bounds[i + 1]]) for i in range(len(bounds) - 1)]

    fold_results = []
    for fi, (is_days, oos_days) in enumerate(folds, 1):
        floor_th = compute_global_atr_threshold(is_days, {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in is_days})
        quality_th = compute_quality_threshold(is_days, all_bars, floor_th)

        baseline = evaluate(oos_days, all_bars, floor_th, None)
        filtered = evaluate(oos_days, all_bars, floor_th, quality_th)

        lift_pct = (filtered["total_pnl"] / baseline["total_pnl"] - 1) * 100 if baseline["total_pnl"] else float("nan")
        print(f"Fold{fi}: OOS={len(oos_days)}天  quality_th={quality_th:.2f}")
        print(f"  baseline : trades={baseline['total_trades']:4d}  pnl={baseline['total_pnl']:>10,.1f}pt  "
              f"sharpe={baseline['sharpe_like']:.2f}")
        print(f"  +ATR濾網 : trades={filtered['total_trades']:4d}  pnl={filtered['total_pnl']:>10,.1f}pt  "
              f"sharpe={filtered['sharpe_like']:.2f}  (vs baseline {lift_pct:+.1f}%)")
        fold_results.append(dict(fold=fi, baseline=baseline, filtered=filtered, quality_th=quality_th, lift_pct=lift_pct))

    n_folds_improved = sum(1 for r in fold_results if r["filtered"]["total_pnl"] > r["baseline"]["total_pnl"])
    print(f"\n=== 5摺總結：{n_folds_improved}/5 摺加了ATR濾網後總損益改善 ===")
    baseline_total = sum(r["baseline"]["total_pnl"] for r in fold_results)
    filtered_total = sum(r["filtered"]["total_pnl"] for r in fold_results)
    print(f"5摺合計 baseline={baseline_total:,.1f}pt  +ATR濾網={filtered_total:,.1f}pt  "
          f"整體改善={100*(filtered_total/baseline_total-1):+.1f}%")

    # 圖
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            break
    fig, ax = plt.subplots(figsize=(11, 6))
    x = np.arange(5)
    width = 0.35
    b_vals = [r["baseline"]["total_pnl"] for r in fold_results]
    f_vals = [r["filtered"]["total_pnl"] for r in fold_results]
    ax.bar(x - width / 2, b_vals, width, label="baseline(無ATR品質濾網)", color="#7F8C8D")
    ax.bar(x + width / 2, f_vals, width, label="+ATR品質濾網(IS校準,pooled)", color="#1F8A65")
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels([f"Fold{i+1}" for i in range(5)])
    ax.set_ylabel("OOS期間總損益(pt)")
    ax.set_title("ATR進場品質濾網 5摺滾動walk-forward驗證")
    ax.legend()
    fig.tight_layout()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    chart_path = fig_dir / "atr_filter_5fold_walkforward.png"
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)
    print(f"\nchart saved: {chart_path}")


if __name__ == "__main__":
    main()
