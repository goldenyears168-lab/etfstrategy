#!/usr/bin/env python3
"""日盤/夜盤拆開驗證——之前10輪全部是day+night合併成一條連續序列跑，這支腳本做兩件事：

1. 回答「edge是不是兩個session都有，還是集中在單一session」（之前完全沒拆過）。
2. 順便修一個潛在方法論漏洞：合併版本的 rolling window（尤其 window=233 這種大窗格）
   會跨過日盤收盤(13:45)→夜盤開盤(15:00)那個沒有K棒的空檔連續滾動，等於把日盤收盤前
   的價格資訊直接帶進夜盤開盤的通道計算——這不是look-ahead(不是用未來資訊)，但混合了
   兩個不連續交易時段的價格關係，不完全乾淨。這裡日盤/夜盤各自重新起算(每個session
   自己的channel/RSI/ATR從該session第一根bar重新累積)，互不污染。

測試對象：w233(單一window，第九輪最佳單點)、w34+w55+w89併行組合(第十輪)。
執行假設沿用：延遲1根K棒成交+5.9pt/筆成本、rsi_exit=False、cooldown=8。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, DB_PATH, FILL_LAG_BARS, SOURCE, load_days  # noqa: E402
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COOLDOWN = 8
SLEEVES = [34, 55, 89]
W233 = 233


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def load_day_bars_with_sess(day: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v, sess FROM bars WHERE source=? AND day=? ORDER BY t",
            conn, params=(SOURCE, day),
        )
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df["Datetime"] = pd.to_datetime(df["Datetime"], format="%H:%M")
    return df[["Datetime", "Open", "High", "Low", "Close", "Volume", "sess"]]


def run_sleeve(df: pd.DataFrame, window: int, atr_threshold: float) -> list[dict]:
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


def run_day_session_only(df_full: pd.DataFrame, sess: str, windows: list[int], atr_threshold: float) -> list[dict]:
    seg = df_full[df_full["sess"] == sess].reset_index(drop=True)
    all_trades = []
    for w in windows:
        all_trades.extend(run_sleeve(seg, w, atr_threshold))
    return all_trades


def evaluate(days: list[str], all_bars: dict, sess: str, windows: list[int], atr_threshold: float) -> dict:
    daily_pnls, all_trades = [], []
    for day in days:
        trades = run_day_session_only(all_bars[day], sess, windows, atr_threshold)
        daily_pnls.append(sum(t["pnl"] for t in trades))
        all_trades.extend(trades)
    arr = np.array(daily_pnls)
    mean_d, std_d = arr.mean(), arr.std()
    sharpe_like = (mean_d / std_d * np.sqrt(252)) if std_d else float("nan")
    return dict(
        total_pnl=float(arr.sum()), mean_daily_pnl=float(mean_d), sharpe_like=float(sharpe_like),
        total_trades=len(all_trades), positive_days=int((arr > 0).sum()), n_days=len(arr),
        trades_per_day=len(all_trades) / len(arr) if len(arr) else 0.0,
        daily_pnls=arr.tolist(),
    )


def compute_atr_threshold_for_days(days: list[str], all_bars: dict) -> float:
    """沿用 tx_channel_recalibrate.compute_global_atr_threshold 的池化邏輯，
    但吃 sess-tagged 的 dataframe（丟掉 sess 欄位餵給共用函式）。"""
    bars_stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    return compute_global_atr_threshold(days, bars_stripped)


def main() -> None:
    days = load_days()
    print(f"total days: {len(days)}")
    all_bars = {d: load_day_bars_with_sess(d) for d in days}

    configs = {"w233": [W233], "w34+w55+w89": SLEEVES}

    print("\n=== 83天全樣本，日盤/夜盤各自獨立計算（單一固定門檻，descriptive） ===")
    atr_th_full = compute_atr_threshold_for_days(days, all_bars)
    full_summary = {}
    for cfg_name, windows in configs.items():
        for sess in ("day", "night", "combined_split"):
            if sess == "combined_split":
                d_res = evaluate(days, all_bars, "day", windows, atr_th_full)
                n_res = evaluate(days, all_bars, "night", windows, atr_th_full)
                res = dict(
                    total_pnl=d_res["total_pnl"] + n_res["total_pnl"],
                    total_trades=d_res["total_trades"] + n_res["total_trades"],
                    trades_per_day=d_res["trades_per_day"] + n_res["trades_per_day"],
                    n_days=d_res["n_days"],
                )
                print(f"[{cfg_name}][day+night分開算再相加] trades/day={res['trades_per_day']:.2f}  "
                      f"總損益={res['total_pnl']:,.1f}pt")
            else:
                res = evaluate(days, all_bars, sess, windows, atr_th_full)
                print(f"[{cfg_name}][{sess}單獨] trades/day={res['trades_per_day']:.2f}  "
                      f"總損益={res['total_pnl']:,.1f}pt  日均={res['mean_daily_pnl']:,.1f}pt  "
                      f"正報酬天={res['positive_days']}/{res['n_days']}  Sharpe={res['sharpe_like']:.2f}")
            full_summary[f"{cfg_name}_{sess}"] = res

    print("\n=== 5摺滾動驗證（跟先前同樣切法），日盤/夜盤分開 ===")
    bounds = [30, 40, 50, 60, 70, len(days)]
    folds = [(days[:bounds[i]], days[bounds[i]:bounds[i + 1]]) for i in range(len(bounds) - 1)]

    fold_summary = {}
    for cfg_name, windows in configs.items():
        for sess in ("day", "night"):
            fold_results = []
            for fi, (is_days, oos_days) in enumerate(folds, 1):
                atr_th = compute_atr_threshold_for_days(is_days, all_bars)
                r = evaluate(oos_days, all_bars, sess, windows, atr_th)
                fold_results.append(r)
            n_pos = sum(1 for r in fold_results if r["total_pnl"] > 0)
            mean_sharpe = np.mean([r["sharpe_like"] for r in fold_results if not np.isnan(r["sharpe_like"])])
            print(f"[{cfg_name}][{sess}] 5摺: {n_pos}/5正報酬  平均Sharpe={mean_sharpe:.2f}  "
                  f"各摺pnl={[round(r['total_pnl'],0) for r in fold_results]}")
            fold_summary[f"{cfg_name}_{sess}"] = dict(
                n_folds_positive=n_pos, mean_sharpe=float(mean_sharpe),
                fold_pnls=[r["total_pnl"] for r in fold_results],
                fold_sharpes=[r["sharpe_like"] for r in fold_results],
            )

    # 圖
    _use_cjk_font()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    for idx, cfg_name in enumerate(configs):
        ax = axes[idx][0]
        d_pnls = fold_summary[f"{cfg_name}_day"]["fold_pnls"]
        n_pnls = fold_summary[f"{cfg_name}_night"]["fold_pnls"]
        x = np.arange(5)
        width = 0.35
        ax.bar(x - width/2, d_pnls, width, label="日盤", color="#E67E22")
        ax.bar(x + width/2, n_pnls, width, label="夜盤", color="#2C3E50")
        ax.axhline(0, color="gray", linewidth=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels([f"Fold{i+1}" for i in range(5)])
        ax.set_title(f"{cfg_name}：5摺 OOS 損益，日盤 vs 夜盤分開")
        ax.set_ylabel("OOS期間損益(pt)")
        ax.legend()
        ax.grid(True, alpha=0.3)

        ax2 = axes[idx][1]
        labels = ["day", "night"]
        totals = [full_summary[f"{cfg_name}_day"]["total_pnl"], full_summary[f"{cfg_name}_night"]["total_pnl"]]
        colors = ["#1F8A65" if v > 0 else "#C0392B" for v in totals]
        ax2.bar(labels, totals, color=colors)
        ax2.axhline(0, color="gray", linewidth=0.8)
        ax2.set_title(f"{cfg_name}：83天總損益，日盤 vs 夜盤")
        ax2.set_ylabel("總損益(pt)")
        ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    chart_path = fig_dir / "daynight_split_comparison.png"
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)
    print(f"\nchart saved: {chart_path}")

    import json
    out = dict(full_sample_summary=full_summary, fold_summary=fold_summary)
    out_path = OUT_DIR / "daynight_split_results.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"json saved: {out_path}")


if __name__ == "__main__":
    main()
