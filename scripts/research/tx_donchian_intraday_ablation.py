#!/usr/bin/env python3
"""8/6 日內高頻版（donchian_strategy 忠實移植）ablation 拆解。

baseline = tx_donchian_intraday_faithful.py 的完整版（Donchian突破淡出 + RSI exit +
cooldown + ATR濾網）。逐一拆掉一個機制，同一份 8/6 K棒資料重跑，比較翻倉次數/勝率/
損益/最大回落，找出 29 筆翻倉裡的虧損主要是哪個機制造成的——而不是瞎調參數。

變體：
  baseline      - 完整版（對照組）
  no_rsi_exit   - 拿掉「RSI<42 提前出場」，只剩「跌破下軌」才轉多單
  no_cooldown   - cooldown_bars 從 8 降到 1（等於不限制翻倉間隔）
  no_atr_filter - 拿掉 ATR 零波動濾網（永遠允許產生新訊號）
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
    POINT_VALUE_NTD,
    RSI_EXIT,
    RSI_PERIOD,
    calculate_atr,
    calculate_donchian,
    calculate_rsi,
    fetch_today_bars,
    simulate_pnl,
)

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"

VARIANTS = {
    "baseline": dict(use_rsi_exit=True, cooldown_bars=8, use_atr_filter=True),
    "no_rsi_exit": dict(use_rsi_exit=False, cooldown_bars=8, use_atr_filter=True),
    "no_cooldown": dict(use_rsi_exit=True, cooldown_bars=1, use_atr_filter=True),
    "no_atr_filter": dict(use_rsi_exit=True, cooldown_bars=8, use_atr_filter=False),
}


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def donchian_rsi_exit_only_variant(
    data: pd.DataFrame,
    atr_threshold: float,
    *,
    use_rsi_exit: bool,
    cooldown_bars: int,
    use_atr_filter: bool,
) -> pd.DataFrame:
    """同一份狀態機，逐一拆掉一個機制（見 module docstring）。"""
    dataset = calculate_rsi(data, RSI_PERIOD)
    dataset = calculate_donchian(dataset, DONCHIAN_WINDOW)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna().reset_index(drop=True)

    dataset["Signal"] = 1
    dataset["Entry"] = 0
    short, last_entry = False, -cooldown_bars

    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        rsi = dataset["RSI"].iat[i]

        if use_atr_filter and dataset["ATR"].iat[i] < atr_threshold:
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
    dd = equity - running_max
    return float(dd.min())


def run_variant(df: pd.DataFrame, atr_threshold: float, params: dict) -> dict:
    sig = donchian_rsi_exit_only_variant(df, atr_threshold, **params)
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


def plot_equity_overlay(results: dict, out_dir: Path, trading_day: str) -> Path:
    """四個變體的權益曲線疊圖——看差異是「何時」出現的，不是只看收盤數字。"""
    _use_cjk_font()
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    colors = {"baseline": "#2C3E50", "no_rsi_exit": "#C0392B", "no_cooldown": "#1F8A65", "no_atr_filter": "#E67E22"}
    styles = {"baseline": "-", "no_rsi_exit": "-", "no_cooldown": "--", "no_atr_filter": ":"}

    fig, ax = plt.subplots(figsize=(14, 6))
    for name, r in results.items():
        bt = r["bt"].copy()
        gap = bt["Datetime"].diff() > pd.Timedelta(minutes=5)
        eq = bt["equity"].copy()
        eq[gap] = np.nan
        lw = 2.0 if name in ("baseline", "no_rsi_exit") else 1.2
        ax.plot(bt["Datetime"], eq, label=f"{name}（終值 {r['total_pnl']:,.1f}pt）",
                 color=colors[name], linestyle=styles[name], linewidth=lw)

    ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_title(f"TX/TMF {trading_day} 日內高頻版 ablation — 權益曲線疊圖（何時開始分道揚鑣）")
    ax.set_ylabel("累計損益 (pt)")
    ax.set_xlabel("時間")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path = fig_dir / f"tx_donchian_intraday_ablation_equity_{trading_day.replace('-', '')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_comparison(results: dict, out_dir: Path, trading_day: str) -> Path:
    _use_cjk_font()
    fig_dir = out_dir / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    names = list(results.keys())
    n_trades = [results[k]["n_trades"] for k in names]
    win_rates = [results[k]["win_rate"] for k in names]
    total_pnl = [results[k]["total_pnl"] for k in names]
    max_dd = [results[k]["max_drawdown"] for k in names]

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    colors = ["#2C3E50", "#C0392B", "#1F8A65", "#E67E22"]

    ax = axes[0]
    bars = ax.bar(names, total_pnl, color=colors)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_title("累計損益 (pt)")
    ax.tick_params(axis="x", rotation=20)
    for b, v in zip(bars, total_pnl):
        ax.annotate(f"{v:,.1f}", (b.get_x() + b.get_width() / 2, v), ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8)

    ax = axes[1]
    bars = ax.bar(names, n_trades, color=colors)
    ax.set_title("翻倉次數")
    ax.tick_params(axis="x", rotation=20)
    for b, v in zip(bars, n_trades):
        ax.annotate(str(v), (b.get_x() + b.get_width() / 2, v), ha="center", va="bottom", fontsize=8)

    ax = axes[2]
    bars = ax.bar(names, max_dd, color=colors)
    ax.set_title("最大回落 (pt, equity低於歷史高點的最大差距)")
    ax.tick_params(axis="x", rotation=20)
    for b, v in zip(bars, max_dd):
        ax.annotate(f"{v:,.1f}", (b.get_x() + b.get_width() / 2, v), ha="center", va="top", fontsize=8)

    fig.suptitle(f"TX/TMF {trading_day} 日內高頻版 ablation 拆解（拿掉單一機制 vs baseline）")
    fig.tight_layout()
    path = fig_dir / f"tx_donchian_intraday_ablation_{trading_day.replace('-', '')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def main() -> None:
    df, meta = fetch_today_bars()
    trading_day = meta["trading_day"]
    print(f"trading_day={trading_day}  n_bars={len(df)}")

    atr_probe = calculate_atr(df, ATR_PERIOD)["ATR"].dropna()
    atr_threshold = float(np.percentile(atr_probe, 5))

    results = {}
    for name, params in VARIANTS.items():
        results[name] = run_variant(df, atr_threshold, params)

    print("\n=== ablation 結果（同一份 8/6 K棒，逐一拿掉一個機制） ===")
    summary_rows = []
    for name, r in results.items():
        print(f"{name:14s}  trades={r['n_trades']:3d}  win_rate={r['win_rate']:5.1f}%  "
              f"pnl=NT${r['total_pnl']:>10,.0f}  max_dd=NT${r['max_drawdown']:>10,.0f}")
        summary_rows.append({
            "variant": name, "n_trades": r["n_trades"], "win_rate": r["win_rate"],
            "total_pnl": r["total_pnl"], "max_drawdown": r["max_drawdown"],
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUT_DIR / f"ablation_summary_{trading_day.replace('-', '')}.csv"
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    print(f"\nsummary saved: {summary_path}")

    for name, r in results.items():
        trades_path = OUT_DIR / f"ablation_trades_{name}_{trading_day.replace('-', '')}.csv"
        pd.DataFrame(r["trades"]).to_csv(trades_path, index=False)

    chart_path = plot_comparison(results, OUT_DIR, trading_day)
    print(f"chart saved: {chart_path}")

    equity_chart_path = plot_equity_overlay(results, OUT_DIR, trading_day)
    print(f"equity overlay chart saved: {equity_chart_path}")


if __name__ == "__main__":
    main()
