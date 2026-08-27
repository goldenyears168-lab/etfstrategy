#!/usr/bin/env python3
"""Phase 2：83天粗篩。掃描設計文件選項A的參數矩陣（STOP_PTS × TARGET/STOP比 × window），
day/night分開跑（但這裡先報告合併與拆開兩種視角）。

⚠️ 這一階段只用來抓bug、排除明顯失敗組合，83天樣本不當正面證據（會拿去Phase 5的582/750天
walk-forward才是正式驗證）。每個候選都報告P&L集中度（top5%交易佔總損益比例，設計文件
kill criterion 7 的紅線是50%）跟同根K棒stop/target同時觸及比例（>20%代表bar級近似不可信，
需要tick級複驗，設計文件3.5節）。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_phase2_screen.py
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
from tx_flat_bracket_engine import run_portfolio_bracket  # noqa: E402

STOP_GRID = [80.0, 120.0, 160.0, 200.0]
RATIO_GRID = [0.75, 1.0, 1.5, 2.0]  # target_pts / stop_pts
WINDOWS = [34, 55, 89, 233]
TIME_STOP_BARS = 999  # 不綁定的時間停損backstop，這輪只掃stop/target幾何


def newey_west_like_tstat(daily_pnl: pd.Series, maxlags: int = 5) -> float:
    """簡化版 Newey-West HAC t 統計量（正式版留給Phase 3的statsmodels實作，這裡只做
    Phase 2粗篩排序用的近似分數，不當作正式顯著性判準）。"""
    x = daily_pnl.values.astype(float)
    n = len(x)
    if n < 5:
        return 0.0
    mean = x.mean()
    resid = x - mean
    gamma0 = (resid @ resid) / n
    var = gamma0
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
    print(f"grid：stop_pts×{STOP_GRID}  ratio(target/stop)×{RATIO_GRID}  window×{WINDOWS}")
    print(f"共 {len(STOP_GRID) * len(RATIO_GRID) * len(WINDOWS)} 組合\n")

    rows = []
    for stop_pts, ratio, window in product(STOP_GRID, RATIO_GRID, WINDOWS):
        target_pts = stop_pts * ratio
        result = run_portfolio_bracket(
            days, all_bars, [window], atr_threshold, stop_pts, target_pts, TIME_STOP_BARS
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
                window=window,
                stop_pts=stop_pts,
                target_pts=target_pts,
                ratio=ratio,
                n_trades=n,
                total_pnl=total,
                avg_pnl=total / n,
                win_rate=win_rate,
                top5pct_share=top5_share,
                same_bar_ambiguous_pct=same_bar_pct,
                nw_tstat_approx=tstat,
                invariant_ok=result["invariant_ok"],
            )
        )

    res = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 200)

    print("=== 對帳不變量：所有組合是否都通過 ===")
    print(f"  {res['invariant_ok'].all()} ({res['invariant_ok'].sum()}/{len(res)})\n")

    print("=== 前15名（依總損益排序）===")
    print(
        res.head(15)[
            ["window", "stop_pts", "ratio", "n_trades", "total_pnl", "avg_pnl", "win_rate",
             "top5pct_share", "same_bar_ambiguous_pct", "nw_tstat_approx"]
        ].to_string(index=False)
    )

    print("\n=== 後10名（依總損益排序）===")
    print(
        res.tail(10)[
            ["window", "stop_pts", "ratio", "n_trades", "total_pnl", "avg_pnl", "win_rate",
             "top5pct_share", "same_bar_ambiguous_pct", "nw_tstat_approx"]
        ].to_string(index=False)
    )

    n_positive = (res["total_pnl"] > 0).sum()
    print(f"\n=== 總覽：{len(res)}組合中 {n_positive} 組總損益為正（{n_positive/len(res)*100:.1f}%）===")

    out_path = Path(__file__).resolve().parent.parent.parent / "reports" / "research" / "tx-donchian-regime" / "phase2_screen_results.csv"
    res.to_csv(out_path, index=False)
    print(f"\n完整結果已存到 {out_path}")


if __name__ == "__main__":
    main()
