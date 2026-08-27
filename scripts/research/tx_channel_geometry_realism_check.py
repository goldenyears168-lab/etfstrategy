#!/usr/bin/env python3
"""稽核 tx_channel_geometry_control.py 的邏輯是否合理：加回原作者自己用的執行假設。

發現的兩個問題：
1. 原作者 auxiliary/utils.py 的 backtesting.py 設定是 `trade_on_close=False`——訊號在
   K棒收盤才確認，實際成交在「下一根K棒的開盤」，不是同一根收盤價。我先前寫的
   simulate_pnl() 卻是「訊號那根收盤價當場成交」，等於假設零延遲、零滑價，比原作者自己
   的假設更樂觀。這對 LRC 這種訊號反應快、持倉週期短（median 8-10分鐘 vs Donchian
   25分鐘）的版本影響應該更大。
2. 兩版回測完全沒有交易成本。production TMF 看板今天實際 23 筆成交的 cost_pts=136.3
   （約 5.9 點/筆，含價差+手續費+期交稅），這個「點數成本」量級在同一個標的上可以直接
   拿來套用（TX/TMF 追蹤同一指數、tick size 相同，用點數而非金額比較不受乘數影響）。
   LRC 67 筆 vs Donchian 29 筆，交易頻率高 2.3 倍，成本應該也等比例侵蝕獲利更多——但原本
   兩版都是 0 成本，等於系統性偏袒交易次數多的那一組。

這支腳本把這兩個假設加回去，同一組訊號重新結算損益，看 LRC 的優勢還在不在。
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import (  # noqa: E402
    ATR_PERIOD,
    calculate_atr,
    calculate_donchian_channel,
    calculate_lrc_channel,
    channel_state_machine,
)
from tx_donchian_intraday_faithful import fetch_today_bars  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COST_PTS_PER_TRADE = 5.9  # production TMF 今日實測 cost_pts/trade，同標的點數成本可直接套用


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def simulate_pnl_realistic(
    dataset: pd.DataFrame,
    *,
    fill_lag_bars: int,
    cost_pts_per_trade: float,
) -> tuple[pd.DataFrame, list[dict]]:
    """fill_lag_bars=0 等同先前版本（訊號當根收盤成交）；
    fill_lag_bars=1 比照原作者 trade_on_close=False：訊號確認後，下一根K棒開盤才成交。
    """
    trades: list[dict] = []
    n = len(dataset)
    equity = [0.0] * n
    pos_dir = dataset["Signal"].iat[0]

    fill_idx0 = min(fill_lag_bars, n - 1)
    entry_price = dataset["Open"].iat[fill_idx0] if fill_lag_bars else dataset["Close"].iat[0]
    entry_time = dataset["Datetime"].iat[fill_idx0]
    running_pnl = 0.0
    pending_flip = None  # 若 fill_lag_bars>0，訊號在 i 出現、成交延到 i+fill_lag_bars

    for i in range(1, n):
        sig = dataset["Signal"].iat[i]

        if pending_flip is not None and i == pending_flip["fill_at"]:
            fill_price = dataset["Open"].iat[i] if fill_lag_bars else dataset["Close"].iat[i]
            pnl_pts = (fill_price - entry_price) if pos_dir == 1 else (entry_price - fill_price)
            pnl = pnl_pts - cost_pts_per_trade  # 單位：指數點
            running_pnl += pnl
            trades.append(
                {
                    "direction": "long" if pos_dir == 1 else "short",
                    "entry_time": entry_time,
                    "exit_time": dataset["Datetime"].iat[i],
                    "entry_price": entry_price,
                    "exit_price": fill_price,
                    "pnl": pnl,
                }
            )
            pos_dir = pending_flip["new_dir"]
            entry_price = fill_price
            entry_time = dataset["Datetime"].iat[i]
            pending_flip = None

        if sig != pos_dir and pending_flip is None:
            fill_at = min(i + fill_lag_bars, n - 1) if fill_lag_bars else i
            if fill_lag_bars == 0:
                fill_price = dataset["Close"].iat[i]
                pnl_pts = (fill_price - entry_price) if pos_dir == 1 else (entry_price - fill_price)
                pnl = pnl_pts - cost_pts_per_trade  # 單位：指數點
                running_pnl += pnl
                trades.append(
                    {
                        "direction": "long" if pos_dir == 1 else "short",
                        "entry_time": entry_time,
                        "exit_time": dataset["Datetime"].iat[i],
                        "entry_price": entry_price,
                        "exit_price": fill_price,
                        "pnl": pnl,
                    }
                )
                pos_dir = sig
                entry_price = fill_price
                entry_time = dataset["Datetime"].iat[i]
            else:
                pending_flip = {"fill_at": fill_at, "new_dir": sig}

        equity[i] = running_pnl

    dataset = dataset.copy()
    dataset["equity"] = equity
    return dataset, trades


def max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    running_max = running_max.where(running_max > 0, 0)
    return float((equity - running_max).min())


def run(df, channel_fn, atr_threshold, use_rsi_exit, fill_lag_bars, cost_pts):
    sig = channel_state_machine(df, channel_fn, atr_threshold, use_rsi_exit=use_rsi_exit)
    bt, trades = simulate_pnl_realistic(sig, fill_lag_bars=fill_lag_bars, cost_pts_per_trade=cost_pts)
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    return {
        "n_trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "total_pnl": total_pnl,
        "max_drawdown": max_drawdown(bt["equity"]),
    }


def main() -> None:
    df, meta = fetch_today_bars()
    trading_day = meta["trading_day"]
    print(f"trading_day={trading_day}  n_bars={len(df)}")
    print(f"cost assumption: {COST_PTS_PER_TRADE} pts/trade (production TMF 今日實測值)\n")

    atr_probe = calculate_atr(df, ATR_PERIOD)["ATR"].dropna()
    atr_threshold = float(np.percentile(atr_probe, 5))

    combos = {
        "donchian_baseline": (calculate_donchian_channel, True),
        "lrc_baseline": (calculate_lrc_channel, True),
        "lrc_no_rsi_exit": (calculate_lrc_channel, False),
    }
    scenarios = [
        ("原版（同根K棒收盤成交 · 零成本）", 0, 0.0),
        ("加成本（同根收盤成交 · 5.9pt/筆）", 0, COST_PTS_PER_TRADE),
        ("加延遲成交（下一根開盤 · 零成本）", 1, 0.0),
        ("加延遲+成本（下一根開盤 · 5.9pt/筆，最接近原作者假設）", 1, COST_PTS_PER_TRADE),
    ]

    rows = []
    print(f"{'combo':22s} {'scenario':45s} {'n':>4s} {'win%':>7s} {'pnl':>12s}")
    for name, (channel_fn, use_rsi_exit) in combos.items():
        for label, lag, cost in scenarios:
            r = run(df, channel_fn, atr_threshold, use_rsi_exit, lag, cost)
            print(f"{name:22s} {label:45s} {r['n_trades']:4d} {r['win_rate']:6.1f}% {r['total_pnl']:12,.0f}")
            rows.append({"combo": name, "scenario": label, "fill_lag_bars": lag, "cost_pts": cost, **r})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"realism_check_{trading_day.replace('-', '')}.csv"
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"\nsaved: {out_path}")

    # 圖：每個 combo 在四種假設下的 pnl 變化
    _use_cjk_font()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(scenarios))
    width = 0.25
    colors = {"donchian_baseline": "#2C3E50", "lrc_baseline": "#C0392B", "lrc_no_rsi_exit": "#E67E22"}
    for i, (name, _) in enumerate(combos.items()):
        vals = [r["total_pnl"] for r in rows if r["combo"] == name]
        ax.bar(x + (i - 1) * width, vals, width, label=name, color=colors[name])
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels([s[0] for s in scenarios], rotation=15, ha="right", fontsize=8)
    ax.set_ylabel("累計損益 (pt)")
    ax.set_title(f"TX/TMF {trading_day} 邏輯稽核：加回原作者執行假設 + 實測交易成本後，優勢還在嗎？")
    ax.legend()
    fig.tight_layout()
    path = fig_dir / f"realism_check_{trading_day.replace('-', '')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"chart: {path}")


if __name__ == "__main__":
    main()
