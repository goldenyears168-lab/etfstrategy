#!/usr/bin/env python3
"""針對 TX/TMF 重新校準 Donchian 參數——用 IS/OOS 切分，避免落入前幾輪一直警告的「同一份
資料上找到就當結論」的陷阱。

背景（第六輪結論）：原始移植參數（window=11, rsi_exit=42, cooldown=8）在 83 天寫實回測
（延遲1根K棒成交 + 5.9pt/筆成本）打平邊緣，平均每筆虧損幾乎等於固定成本，代表訊號本身沒
方向性 edge、純粹被交易成本吃掉。最直接的校準槓桿：window/cooldown 加大 → 交易次數變少 →
攤薄固定成本佔比。這支腳本做網格搜尋，鎖定 rsi_period=18／atr filter 沿用第五輪設定不變。

切分方式：83 個交易日依時間順序切 70/30（前 58 天 IS 找參數，後 25 天 OOS 驗證），
**只在 IS 排名，OOS 只用來驗證，不用 OOS 結果回頭選參數**——避免用未來資料挑參數的
look-ahead。
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import ATR_PERIOD, calculate_atr, calculate_rsi, RSI_PERIOD  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_day_bars, load_days  # noqa: E402
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402
from tx_donchian_intraday_faithful import RSI_EXIT  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
IS_FRACTION = 0.70
MIN_IS_TRADES = 30

WINDOW_GRID = [11, 20, 34, 55]
COOLDOWN_GRID = [8, 15, 30]
USE_RSI_EXIT_GRID = [True, False]


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


def compute_global_atr_threshold(days: list[str], day_bars: dict[str, pd.DataFrame]) -> float:
    """5th percentile of ATR(20) 池化自「這些天」的完整分布，只能餵 IS 天，不能碰 OOS——
    之前每支腳本都是『用當天自己的全天ATR算門檻』，等於每個時間點都用到了當天尚未發生的
    未來K棒的ATR資訊，是貨真價實的 look-ahead。修法：門檻只用 IS 天池化資料算一次、
    固定下來，IS 排名和 OOS 驗證都套同一個固定值，不再逐天用自己的未來重算。
    """
    pooled = []
    for day in days:
        atr = calculate_atr(day_bars[day], ATR_PERIOD)["ATR"].dropna()
        pooled.append(atr)
    all_atr = pd.concat(pooled)
    return float(np.percentile(all_atr, 5))


def run_combo_on_day(df: pd.DataFrame, window: int, cooldown_bars: int, use_rsi_exit: bool, atr_threshold: float) -> dict:
    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset = calculate_donchian_channel(dataset, window)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return {"n_trades": 0, "total_pnl": 0.0}

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
            short = True
            last_entry = i
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if use_rsi_exit:
                exit_cond = exit_cond or (rsi < RSI_EXIT)
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    _, trades = simulate_pnl_realistic(dataset, fill_lag_bars=FILL_LAG_BARS, cost_pts_per_trade=COST_PTS_PER_TRADE)
    total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    return {"n_trades": len(trades), "total_pnl": total_pnl}


def evaluate_combo(days: list[str], day_bars: dict, window: int, cooldown: int, use_rsi_exit: bool, atr_threshold: float) -> dict:
    daily_pnls = []
    total_trades = 0
    for day in days:
        r = run_combo_on_day(day_bars[day], window, cooldown, use_rsi_exit, atr_threshold)
        daily_pnls.append(r["total_pnl"])
        total_trades += r["n_trades"]
    arr = np.array(daily_pnls)
    mean_d, std_d = arr.mean(), arr.std()
    sharpe_like = (mean_d / std_d * np.sqrt(252)) if std_d else float("nan")
    return {
        "window": window, "cooldown": cooldown, "use_rsi_exit": use_rsi_exit,
        "total_pnl": arr.sum(), "mean_daily_pnl": mean_d, "std_daily_pnl": std_d,
        "sharpe_like": sharpe_like, "total_trades": total_trades,
        "positive_days": int((arr > 0).sum()), "n_days": len(arr),
    }


def main() -> None:
    days = load_days()
    split = int(len(days) * IS_FRACTION)
    is_days, oos_days = days[:split], days[split:]
    print(f"總天數={len(days)}  IS={len(is_days)}天({is_days[0]}~{is_days[-1]})  "
          f"OOS={len(oos_days)}天({oos_days[0]}~{oos_days[-1]})")
    print(f"執行假設沿用：延遲{FILL_LAG_BARS}根K棒 + {COST_PTS_PER_TRADE}pt/筆成本\n")

    print("載入K棒資料...")
    all_bars = {day: load_day_bars(day) for day in days}

    atr_threshold = compute_global_atr_threshold(is_days, all_bars)
    print(f"ATR濾網門檻（只用IS天池化計算一次，IS/OOS共用同一個固定值，修正之前逐天用當天"
          f"未來K棒算門檻的look-ahead）: {atr_threshold:.2f}\n")

    grid = [
        (w, c, r)
        for w in WINDOW_GRID
        for c in COOLDOWN_GRID
        for r in USE_RSI_EXIT_GRID
    ]
    print(f"網格搜尋：{len(grid)} 組參數 × {len(is_days)} 天 IS（只在 IS 排名，OOS 只驗證不選參數）")

    is_results = []
    for i, (w, c, r) in enumerate(grid):
        res = evaluate_combo(is_days, all_bars, w, c, r, atr_threshold)
        is_results.append(res)
        if (i + 1) % 6 == 0:
            print(f"  ...{i+1}/{len(grid)} 組完成")

    is_df = pd.DataFrame(is_results)
    is_df_filtered = is_df[is_df["total_trades"] >= MIN_IS_TRADES].copy()
    is_df_filtered = is_df_filtered.sort_values("sharpe_like", ascending=False)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    is_path = OUT_DIR / "recalibrate_is_grid.csv"
    is_df.sort_values("sharpe_like", ascending=False).to_csv(is_path, index=False)
    print(f"\nIS 網格結果存檔: {is_path}")
    print(f"\n=== IS 排名前5（總交易數 >= {MIN_IS_TRADES} 篩選後）===")
    print(is_df_filtered.head(5).to_string(index=False))

    # 原始移植參數當基準，一起放進 OOS 驗證
    baseline = {"window": 11, "cooldown": 8, "use_rsi_exit": True}
    top5 = is_df_filtered.head(5)[["window", "cooldown", "use_rsi_exit"]].to_dict("records")
    candidates = [baseline] + [c for c in top5 if c != baseline]

    print(f"\n=== OOS 驗證（{len(oos_days)}天，未參與參數挑選）===")
    oos_results = []
    for cand in candidates:
        res = evaluate_combo(oos_days, all_bars, cand["window"], cand["cooldown"], cand["use_rsi_exit"], atr_threshold)
        label = f"w={cand['window']} cd={cand['cooldown']} rsi_exit={cand['use_rsi_exit']}"
        is_row = is_df[(is_df["window"] == cand["window"]) & (is_df["cooldown"] == cand["cooldown"]) & (is_df["use_rsi_exit"] == cand["use_rsi_exit"])].iloc[0]
        print(f"{label:32s}  IS_pnl={is_row['total_pnl']:>10,.1f}pt  OOS_pnl={res['total_pnl']:>10,.1f}pt  "
              f"OOS正報酬天數={res['positive_days']}/{res['n_days']}  OOS_sharpe={res['sharpe_like']:.2f}")
        oos_results.append({"label": label, "is_pnl": is_row["total_pnl"], **res})

    oos_path = OUT_DIR / "recalibrate_oos_validation.csv"
    pd.DataFrame(oos_results).to_csv(oos_path, index=False)
    print(f"\nOOS 驗證結果存檔: {oos_path}")

    # 圖：IS heatmap（window x cooldown，rsi_exit=True 那半）+ OOS 累計權益比較
    _use_cjk_font()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    for ax, use_rsi in zip(axes, [True, False]):
        sub = is_df[is_df["use_rsi_exit"] == use_rsi]
        pivot = sub.pivot(index="cooldown", columns="window", values="sharpe_like")
        im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto", vmin=-15, vmax=15)
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels(pivot.columns)
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels(pivot.index)
        ax.set_xlabel("donchian_window")
        ax.set_ylabel("cooldown_bars")
        ax.set_title(f"IS Sharpe-like（use_rsi_exit={use_rsi}）")
        for yi in range(len(pivot.index)):
            for xi in range(len(pivot.columns)):
                v = pivot.values[yi, xi]
                if not np.isnan(v):
                    ax.text(xi, yi, f"{v:.1f}", ha="center", va="center", fontsize=9)
        fig.colorbar(im, ax=ax, shrink=0.8)
    fig.suptitle("IS（前58天）參數網格 Sharpe-like 熱力圖")
    fig.tight_layout()
    heatmap_path = fig_dir / "recalibrate_is_heatmap.png"
    fig.savefig(heatmap_path, dpi=120)
    plt.close(fig)
    print(f"\nIS heatmap: {heatmap_path}")


if __name__ == "__main__":
    main()
