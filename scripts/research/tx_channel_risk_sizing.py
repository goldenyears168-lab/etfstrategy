#!/usr/bin/env python3
"""部位大小/風控邏輯設計——離下單層的第五個缺口。

用第十一輪驗證過最好的設定（w34+w55+w89併行、day/night各自獨立起算）的83天真實交易
明細，設計並回測：(1) 固定風險比例部位大小(這個架構沒有明確停損價，改用實際單筆虧損
分布的分位數估風險)；(2) 單日虧損熔斷（今天累計虧到某個門檻就停止新訊號，觀察對總報酬
與最大回落的影響）。

⚠️ 這輪只做設計與回測驗證，不接任何實際下單能力。
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import (  # noqa: E402
    SLEEVES,
    compute_atr_threshold_for_days,
    load_day_bars_with_sess,
    run_day_session_only,
)
from tx_channel_geometry_multiday import load_days  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
POINT_VALUE_TX = 200  # NT$/點，大台；標的身分本身仍未定案，見README caveats
STARTING_CAPITAL = 10_000_000.0  # NT$1000萬，概估值——多window組合需要3倍保證金緩衝


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def collect_all_trades(days: list[str], all_bars: dict, atr_threshold: float) -> list[dict]:
    trades = []
    for day in days:
        for sess in ("day", "night"):
            t = run_day_session_only(all_bars[day], sess, SLEEVES, atr_threshold)
            for tr in t:
                tr["day"] = day
                tr["sess"] = sess
            trades.extend(t)
    return trades


def daily_pnl_series(trades: list[dict], days: list[str]) -> pd.Series:
    df = pd.DataFrame(trades)
    daily = df.groupby("day")["pnl"].sum()
    return daily.reindex(days, fill_value=0.0)


def apply_circuit_breaker(trades: list[dict], days: list[str], daily_loss_limit_pts: float) -> pd.Series:
    """單日累計虧損碰到門檻後，當天剩餘訊號全部忽略（用點數計，不含未實現部位）。"""
    df = pd.DataFrame(trades).sort_values(["day", "exit_time"])
    daily_pnl = {d: 0.0 for d in days}
    for day, group in df.groupby("day"):
        running = 0.0
        halted = False
        for _, row in group.iterrows():
            if halted:
                continue
            running += row["pnl"]
            if running <= -daily_loss_limit_pts:
                halted = True
        daily_pnl[day] = running
    return pd.Series(daily_pnl).reindex(days, fill_value=0.0)


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    atr_threshold = compute_atr_threshold_for_days(days, all_bars)

    print("收集83天完整交易明細（w34+w55+w89, day/night分開起算）...")
    trades = collect_all_trades(days, all_bars, atr_threshold)
    pnls = np.array([t["pnl"] for t in trades])
    print(f"總交易數: {len(trades)}")

    # ---- 1. 單筆虧損分布（沒有明確停損價，改用經驗分位數估風險） ----
    losses = pnls[pnls < 0]
    print("\n=== 單筆虧損分布（pt） ===")
    for p in (50, 75, 90, 95, 99, 100):
        print(f"  {p}th percentile 虧損: {np.percentile(-losses, p):.1f}pt")
    worst_single_trade = -losses.min() if len(losses) else 0.0
    p99_single_trade = np.percentile(-losses, 99)
    print(f"\n最差單筆虧損: {worst_single_trade:.1f}pt  99th百分位單筆虧損: {p99_single_trade:.1f}pt")

    # ---- 2. 固定風險比例部位大小建議 ----
    risk_pct = 0.01  # 1%/筆，用99th百分位單筆虧損當風險估計基準（不是理論停損距離）
    risk_budget = STARTING_CAPITAL * risk_pct
    suggested_contracts = int(risk_budget / (p99_single_trade * POINT_VALUE_TX))
    print(f"\n=== 建議部位大小（假設本金 NT${STARTING_CAPITAL:,.0f}） ===")
    print(f"單筆風險預算(1%): NT${risk_budget:,.0f}")
    print(f"用99th百分位單筆虧損({p99_single_trade:.1f}pt)反推可承受口數: {suggested_contracts}口")
    print("⚠️ 這是「組合」的口數（三個window sleeve加總），不是每個sleeve各自可以開這麼多口")

    # ---- 3. 單日熔斷回測 ----
    baseline_daily = daily_pnl_series(trades, days)
    print(f"\n=== 無熔斷基準 ===")
    print(f"83天總損益: {baseline_daily.sum():,.1f}pt  最大單日虧損: {baseline_daily.min():,.1f}pt  "
          f"最大回落(累計): {(baseline_daily.cumsum() - baseline_daily.cumsum().cummax()).min():,.1f}pt")

    print(f"\n=== 熔斷門檻掃描 ===")
    breaker_results = {}
    for limit in (500, 1000, 1500, 2000, 3000, 999999):
        series = apply_circuit_breaker(trades, days, limit)
        cum = series.cumsum()
        dd = (cum - cum.cummax()).min()
        label = f"{limit}pt" if limit < 999999 else "無熔斷"
        breaker_results[label] = dict(total=series.sum(), max_dd=dd, min_day=series.min())
        print(f"  熔斷門檻={label:>8s}  總損益={series.sum():>10,.1f}pt  "
              f"最大回落={dd:>10,.1f}pt  最差單日={series.min():>9,.1f}pt")

    # 圖
    _use_cjk_font()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))

    ax = axes[0]
    ax.hist(-losses, bins=40, color="#C0392B", alpha=0.8)
    ax.axvline(p99_single_trade, color="black", linestyle="--", label=f"99th百分位={p99_single_trade:.0f}pt")
    ax.set_title("單筆虧損分布")
    ax.set_xlabel("虧損(pt)")
    ax.legend()

    ax = axes[1]
    labels = list(breaker_results.keys())
    totals = [breaker_results[k]["total"] for k in labels]
    dds = [breaker_results[k]["max_dd"] for k in labels]
    x = np.arange(len(labels))
    ax.bar(x - 0.2, totals, 0.4, label="總損益", color="#1F8A65")
    ax.bar(x + 0.2, dds, 0.4, label="最大回落", color="#C0392B")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_title("單日熔斷門檻掃描")
    ax.legend()

    fig.tight_layout()
    chart_path = fig_dir / "risk_sizing_circuit_breaker.png"
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)
    print(f"\nchart saved: {chart_path}")


if __name__ == "__main__":
    main()
