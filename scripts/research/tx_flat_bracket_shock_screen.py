#!/usr/bin/env python3
"""Regime分層第三輪：Fold2診斷發現Fold1+Fold2虧損集中在2024年8月carry-trade崩跌
事件前後，這是一個離散『衝擊事件』，不是前5輪測過的緩慢trailing regime——20/120天
trailing volatility、VIXTWN 20天趨勢都是『緩慢變化』偵測器，天生對這種事件型態不敏感。

這支腳本改測『事件剛發生』類特徵（PIT-safe，皆用T-1或更早的值）：
  - vixtwn_shock_chg1_t1：VIXTWN單日變化量絕對值（T-1相對T-2）
  - vixtwn_shock_max10_t1 / max20_t1：trailing 10/20天內最大單日VIXTWN變化絕對值
    （衝擊事件發生後會維持elevated一段時間才衰減，比單一天的跳空更穩定）
  - tx_range_shock_max10_t1 / max20_t1：TX自身trailing 10/20天內最大單日High-Low
    區間佔比（不依賴VIXTWN，直接量測TX自己的『最近有沒有出現過異常寬的一天』）

跟前幾輪一樣先做全樣本描述性IC篩選，只有顯著的才進causal walk-forward。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_shock_screen.py
"""
from __future__ import annotations

import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from stock_db import DEFAULT_DB_PATH  # noqa: E402
from tx_channel_geometry_control import ATR_PERIOD, calculate_atr  # noqa: E402
from tx_flat_bracket_engine import run_portfolio_bracket  # noqa: E402

TMF_DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_tick_built_582d"
CANDIDATE = dict(window=89, stop_pts=400.0, target_pts=800.0)
TIME_STOP_BARS = 999


def load_all_days() -> list[str]:
    with sqlite3.connect(f"file:{TMF_DB_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (SOURCE,)).fetchall()
    return [r[0] for r in rows]


def load_day_bars_with_sess(day: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{TMF_DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v, sess FROM bars WHERE source=? AND day=? ORDER BY t",
            conn, params=(SOURCE, day),
        )
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    return df[["Datetime", "Open", "High", "Low", "Close", "Volume", "sess"]]


def load_vixtwn() -> pd.Series:
    with sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT date, close FROM market_vix_daily WHERE symbol='VIXTWN' AND source='computed' ORDER BY date",
            conn,
        )
    return df.set_index("date")["close"]


def main() -> None:
    all_days = load_all_days()
    print(f"全樣本：{len(all_days)}天（{all_days[0]} ~ {all_days[-1]}）")
    print("正在載入bar資料...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    daily_close = pd.Series({d: all_bars[d]["Close"].iat[-1] for d in all_days}).sort_index()
    daily_high = pd.Series({d: all_bars[d]["High"].max() for d in all_days}).sort_index()
    daily_low = pd.Series({d: all_bars[d]["Low"].min() for d in all_days}).sort_index()

    atr_threshold = float(np.percentile(
        pd.concat([calculate_atr(all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]], ATR_PERIOD)["ATR"].dropna()
                   for d in all_days]), 5))
    result = run_portfolio_bracket(all_days, all_bars, [CANDIDATE["window"]], atr_threshold,
                                    CANDIDATE["stop_pts"], CANDIDATE["target_pts"], TIME_STOP_BARS)
    by_day_pnl = pd.Series(result["by_day"]).reindex(all_days).fillna(0.0)
    print(f"候選策略：{result['n_trades']}筆交易，總損益={result['total_pnl']:,.1f}pt\n")

    vixtwn = load_vixtwn().reindex(all_days)
    vixtwn_chg1 = vixtwn.diff().abs()
    tx_range_pct = (daily_high - daily_low) / daily_close * 100

    regime_vars = {}
    regime_vars["vixtwn_shock_chg1_t1"] = vixtwn_chg1.shift(1)
    regime_vars["vixtwn_shock_max10_t1"] = vixtwn_chg1.rolling(10, min_periods=10).max().shift(1)
    regime_vars["vixtwn_shock_max20_t1"] = vixtwn_chg1.rolling(20, min_periods=20).max().shift(1)
    regime_vars["tx_range_shock_max10_t1"] = tx_range_pct.rolling(10, min_periods=10).max().shift(1)
    regime_vars["tx_range_shock_max20_t1"] = tx_range_pct.rolling(20, min_periods=20).max().shift(1)
    # 「距離上次衝擊多少天」：衝擊定義為vixtwn單日變化超過其自身IS期間90百分位
    is_cut = all_days[150]  # 跟walk-forward腳本的INITIAL_IS_DAYS一致
    shock_threshold = np.percentile(vixtwn_chg1.loc[:is_cut].dropna(), 90)
    is_shock_day = (vixtwn_chg1 > shock_threshold).astype(int)
    days_since_shock = pd.Series(index=all_days, dtype=float)
    last_shock_idx = -9999
    for i, d in enumerate(all_days):
        if is_shock_day.get(d, 0) == 1:
            last_shock_idx = i
        days_since_shock.iloc[i] = i - last_shock_idx
    regime_vars["days_since_vixtwn_shock_t1"] = days_since_shock.shift(1)

    print("=== 全樣本描述性 Spearman IC（衝擊型regime變數 -> 當日策略pnl）===")
    print(f"{'variable':30s} {'IC':>8s} {'p':>8s} {'n':>6s}")
    ic_results = {}
    for name, series in regime_vars.items():
        merged = pd.DataFrame({"x": series, "pnl": by_day_pnl}).dropna()
        if len(merged) < 30:
            print(f"{name:30s} 樣本不足(n={len(merged)})")
            continue
        ic, p = spearmanr(merged["x"], merged["pnl"])
        ic_results[name] = (ic, p, len(merged))
        print(f"{name:30s} {ic:8.4f} {p:8.4f} {len(merged):6d}")

    print("\n=== quintile拆解（IC絕對值最大的前2個變數）===")
    top2 = sorted(ic_results.items(), key=lambda kv: abs(kv[1][0]), reverse=True)[:2]
    for name, (ic, p, n) in top2:
        merged = pd.DataFrame({"x": regime_vars[name], "pnl": by_day_pnl}).dropna()
        merged["q"] = pd.qcut(merged["x"], 5, labels=False, duplicates="drop")
        print(f"\n--- {name} (IC={ic:.4f}, p={p:.4f}) ---")
        print(merged.groupby("q")["pnl"].agg(["count", "sum", "mean"]))

    print("\n=== Fold2診斷用：2024-08-09~2024-10-08這段的days_since_vixtwn_shock分布 ===")
    fold2_early = [d for d in all_days if "2024-08" <= d <= "2024-10-08"]
    print(days_since_vixtwn_shock := regime_vars["days_since_vixtwn_shock_t1"].reindex(fold2_early).describe())


if __name__ == "__main__":
    main()
