#!/usr/bin/env python3
"""Phase 2（選項E）：83天粗篩，擺動點停損+可選中線出場。swing_buffer網格參考Phase1
診斷——buffer=20pt時stop主宰69%的出場（太緊），改用更寬的buffer網格重新掃描。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_phase2_screen_optione.py
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
from tx_flat_bracket_engine_optione import run_portfolio_bracket_optione  # noqa: E402

SWING_LOOKBACK_GRID = [5, 10, 20, 40]
SWING_BUFFER_GRID = [20.0, 50.0, 100.0, 150.0]
TARGET_GRID = [400.0, 800.0]
MEDIAN_EXIT_GRID = [False, True]
WINDOW = 89
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
    print(f"樣本：{len(days)}天（{days[0]} ~ {days[-1]}），ATR門檻={atr_threshold:.2f}, window={WINDOW}")
    total = len(SWING_LOOKBACK_GRID) * len(SWING_BUFFER_GRID) * len(TARGET_GRID) * len(MEDIAN_EXIT_GRID)
    print(f"共 {total} 組合\n")

    rows = []
    for lb, buf, tgt, med in product(SWING_LOOKBACK_GRID, SWING_BUFFER_GRID, TARGET_GRID, MEDIAN_EXIT_GRID):
        result = run_portfolio_bracket_optione(
            days, all_bars, [WINDOW], atr_threshold, lb, buf, tgt, TIME_STOP_BARS, med
        )
        trades = result["trades"]
        if not trades:
            continue
        df = pd.DataFrame(trades)
        total_pnl = df["pnl"].sum()
        win_rate = (df["pnl"] > 0).mean() * 100
        n = len(df)
        top5pct_n = max(1, int(n * 0.05))
        denom = df["pnl"].abs().sum()
        top5_share = df.nlargest(top5pct_n, "pnl")["pnl"].sum() / denom * 100 if denom else 0.0
        same_bar_pct = result["stats"]["n_same_bar_ambiguous"] / max(1, result["stats"]["n_entered"]) * 100
        daily = df.groupby("day")["pnl"].sum()
        tstat = newey_west_like_tstat(daily)
        reason_dist = df["reason"].value_counts(normalize=True).round(3).to_dict()
        rows.append(
            dict(
                swing_lookback=lb, swing_buffer=buf, target=tgt, median_exit=med, n_trades=n,
                total_pnl=total_pnl, avg_pnl=total_pnl / n, win_rate=win_rate, top5pct_share=top5_share,
                same_bar_ambiguous_pct=same_bar_pct, nw_tstat_approx=tstat, invariant_ok=result["invariant_ok"],
                stop_pct=reason_dist.get("stop", 0) * 100, target_pct=reason_dist.get("target", 0) * 100,
                median_pct=reason_dist.get("median_exit", 0) * 100,
            )
        )

    res = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)

    print("=== 對帳不變量：所有組合是否都通過 ===")
    print(f"  {res['invariant_ok'].all()} ({res['invariant_ok'].sum()}/{len(res)})\n")

    cols = ["swing_lookback", "swing_buffer", "target", "median_exit", "n_trades", "total_pnl", "avg_pnl",
            "win_rate", "stop_pct", "target_pct", "median_pct", "top5pct_share", "nw_tstat_approx"]
    print("=== 前15名 ===")
    print(res.head(15)[cols].to_string(index=False))

    print("\n=== 後10名 ===")
    print(res.tail(10)[cols].to_string(index=False))

    n_positive = (res["total_pnl"] > 0).sum()
    print(f"\n=== 總覽：{len(res)}組合中 {n_positive} 組總損益為正（{n_positive/len(res)*100:.1f}%）===")

    out_path = Path(__file__).resolve().parent.parent.parent / "reports" / "research" / "tx-donchian-regime" / "phase2_screen_results_optione.csv"
    res.to_csv(out_path, index=False)
    print(f"\n完整結果已存到 {out_path}")


if __name__ == "__main__":
    main()
