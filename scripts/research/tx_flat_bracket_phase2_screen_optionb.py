#!/usr/bin/env python3
"""Phase 2（選項B）：83天粗篩，ATR倍數停損。k_stop網格用83天樣本ATR(20)實測分布校準
（p5=11.2/p50=28.0/p95=77.9），不用design doc建議的{0.75,1.0,1.5,2.0}（那組數字明顯是
假設ATR量級是幾百點才合理，套用在這個ATR尺度上只會產生8~156pt的停損，比選項A已知
全負的<200pt網格還窄，等於重跑一次已知答案）。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_phase2_screen_optionb.py
"""
from __future__ import annotations

import sys
import warnings
from itertools import product
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_multiday import load_days  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402
from tx_flat_bracket_engine_optionb import run_portfolio_bracket_optionb  # noqa: E402

K_STOP_GRID = [5.0, 8.0, 12.0, 16.0, 20.0, 25.0]
RATIO_GRID = [0.75, 1.0, 1.5, 2.0]
WINDOWS = [34, 55, 89, 233]
TIME_STOP_BARS = 999


def newey_west_like_tstat(daily_pnl: pd.Series, maxlags: int = 5) -> float:
    x = daily_pnl.values.astype(float)
    n = len(x)
    if n < 5:
        return 0.0
    mean = x.mean()
    resid = x - mean
    var = (resid @ resid) / n
    for lag in range(1, min(maxlags, n - 1) + 1):
        w = 1 - lag / (maxlags + 1)
        cov = (resid[lag:] @ resid[:-lag]) / n
        var += 2 * w * cov
    se = (var / n) ** 0.5
    return mean / se if se > 0 else 0.0


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    atr_threshold = compute_global_atr_threshold(days, stripped)
    print(f"樣本：{len(days)}天（{days[0]} ~ {days[-1]}），ATR門檻={atr_threshold:.2f}")
    print(f"grid：k_stop×{K_STOP_GRID}  ratio×{RATIO_GRID}  window×{WINDOWS}")
    print(f"共 {len(K_STOP_GRID) * len(RATIO_GRID) * len(WINDOWS)} 組合\n")

    rows = []
    for k_stop, ratio, window in product(K_STOP_GRID, RATIO_GRID, WINDOWS):
        k_target = k_stop * ratio
        result = run_portfolio_bracket_optionb(
            days, all_bars, [window], atr_threshold, k_stop, k_target, TIME_STOP_BARS
        )
        trades = result["trades"]
        if not trades:
            continue
        df = pd.DataFrame(trades)
        total = df["pnl"].sum()
        win_rate = (df["pnl"] > 0).mean() * 100
        n = len(df)
        top5pct_n = max(1, int(n * 0.05))
        denom = df["pnl"].abs().sum()
        top5_share = df.nlargest(top5pct_n, "pnl")["pnl"].sum() / denom * 100 if denom else 0.0
        same_bar_pct = result["stats"]["n_same_bar_ambiguous"] / max(1, result["stats"]["n_entered"]) * 100
        daily = df.groupby("day")["pnl"].sum()
        tstat = newey_west_like_tstat(daily)
        rows.append(
            dict(
                window=window, k_stop=k_stop, k_target=k_target, ratio=ratio, n_trades=n,
                total_pnl=total, avg_pnl=total / n, win_rate=win_rate, top5pct_share=top5_share,
                same_bar_ambiguous_pct=same_bar_pct, nw_tstat_approx=tstat, invariant_ok=result["invariant_ok"],
            )
        )

    res = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 200)

    print("=== 對帳不變量：所有組合是否都通過 ===")
    print(f"  {res['invariant_ok'].all()} ({res['invariant_ok'].sum()}/{len(res)})\n")

    print("=== 前15名 ===")
    print(res.head(15)[["window", "k_stop", "ratio", "n_trades", "total_pnl", "avg_pnl", "win_rate",
                         "top5pct_share", "same_bar_ambiguous_pct", "nw_tstat_approx"]].to_string(index=False))

    print("\n=== 後10名 ===")
    print(res.tail(10)[["window", "k_stop", "ratio", "n_trades", "total_pnl", "avg_pnl", "win_rate",
                         "top5pct_share", "same_bar_ambiguous_pct", "nw_tstat_approx"]].to_string(index=False))

    n_positive = (res["total_pnl"] > 0).sum()
    print(f"\n=== 總覽：{len(res)}組合中 {n_positive} 組總損益為正（{n_positive/len(res)*100:.1f}%）===")

    out_path = Path(__file__).resolve().parent.parent.parent / "reports" / "research" / "tx-donchian-regime" / "phase2_screen_results_optionb.csv"
    res.to_csv(out_path, index=False)
    print(f"\n完整結果已存到 {out_path}")


if __name__ == "__main__":
    main()
