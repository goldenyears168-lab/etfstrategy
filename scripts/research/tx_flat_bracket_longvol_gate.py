#!/usr/bin/env python3
"""Fold2診斷發現：120天fold-aggregate realized vol能把5個fold完全分開（losers全部
16.8~21.3%年化、winners全部>32.8%），比已經被推翻的20天trailing vol gate（被Fold5
持續高vol佔比機制性主導）乾淨得多。但fold-aggregate用的是『這個fold自己未來全部
120天』的資料，不是causal——這支腳本把它改成causal trailing 120天（只用過去資料，
shift(1)避免用到當天），做跟vol/VIXTWN gate同一套walk-forward流程驗證是否成立。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_longvol_gate.py
"""
from __future__ import annotations

import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tx_channel_geometry_control import ATR_PERIOD, calculate_atr  # noqa: E402
from tx_flat_bracket_engine import run_portfolio_bracket  # noqa: E402

DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_tick_built_582d"
CANDIDATE = dict(window=89, stop_pts=400.0, target_pts=800.0)
TIME_STOP_BARS = 999
INITIAL_IS_DAYS = 150
FOLD_SIZE_DAYS = 120
VOL_LOOKBACK_DAYS = 120
GATE_PERCENTILES = [0.0, 30.0, 40.0, 50.0, 60.0]  # 保留 >= 這個百分位的日子(高vol regime)


def load_all_days() -> list[str]:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (SOURCE,)).fetchall()
    return [r[0] for r in rows]


def load_day_bars_with_sess(day: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v, sess FROM bars WHERE source=? AND day=? ORDER BY t",
            conn, params=(SOURCE, day),
        )
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    return df[["Datetime", "Open", "High", "Low", "Close", "Volume", "sess"]]


def compute_atr_threshold_pooled(days: list[str], all_bars: dict[str, pd.DataFrame]) -> float:
    pooled = []
    for day in days:
        stripped = all_bars[day][["Datetime", "Open", "High", "Low", "Close", "Volume"]]
        atr = calculate_atr(stripped, ATR_PERIOD)["ATR"].dropna()
        pooled.append(atr)
    if not pooled:
        return 0.0
    return float(np.percentile(pd.concat(pooled), 5))


def main() -> None:
    all_days = load_all_days()
    print(f"全樣本：{len(all_days)}天（{all_days[0]} ~ {all_days[-1]}）")
    print("正在載入bar資料...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    daily_close = pd.Series({d: all_bars[d]["Close"].iat[-1] for d in all_days}).sort_index()
    daily_rets_abs = daily_close.pct_change().abs()
    trailing_vol_long = daily_rets_abs.rolling(VOL_LOOKBACK_DAYS).mean().shift(1)

    folds = []
    start = INITIAL_IS_DAYS
    while start < len(all_days):
        end = min(start + FOLD_SIZE_DAYS, len(all_days))
        folds.append(all_days[start:end])
        start = end

    for gate_pct in GATE_PERCENTILES:
        print(f"=== gate門檻：trailing_vol{VOL_LOOKBACK_DAYS}d >= IS期間第{gate_pct:.0f}百分位 (0=不gate對照組) ===")
        fold_results = []
        for i, fold_days in enumerate(folds):
            is_days = all_days[: INITIAL_IS_DAYS + i * FOLD_SIZE_DAYS]
            atr_threshold = compute_atr_threshold_pooled(is_days, all_bars)

            if gate_pct <= 0.0:
                allowed_days = fold_days
            else:
                is_vals = trailing_vol_long.loc[is_days].dropna()
                if len(is_vals) < 30:
                    allowed_days = fold_days
                else:
                    threshold_val = np.percentile(is_vals, gate_pct)
                    allowed_days = [
                        d for d in fold_days
                        if d in trailing_vol_long.index
                        and not pd.isna(trailing_vol_long.loc[d])
                        and trailing_vol_long.loc[d] >= threshold_val
                    ]

            if not allowed_days:
                print(f"  Fold{i+1} ({fold_days[0]}~{fold_days[-1]}): gate後0天允許交易")
                continue

            result = run_portfolio_bracket(
                allowed_days, all_bars, [CANDIDATE["window"]], atr_threshold,
                CANDIDATE["stop_pts"], CANDIDATE["target_pts"], TIME_STOP_BARS,
            )
            trades = result["trades"]
            if not trades:
                print(f"  Fold{i+1} ({fold_days[0]}~{fold_days[-1]}, {len(allowed_days)}/{len(fold_days)}天通過gate): 無交易")
                continue
            df = pd.DataFrame(trades)
            total = df["pnl"].sum()
            fold_results.append(dict(fold=i + 1, total_pnl=total, n_days_allowed=len(allowed_days), n_days_total=len(fold_days)))
            print(
                f"  Fold{i+1} ({fold_days[0]}~{fold_days[-1]}, {len(allowed_days)}/{len(fold_days)}天通過gate): "
                f"{len(df)}筆 總損益={total:,.1f}pt"
            )

        if fold_results:
            fr = pd.DataFrame(fold_results)
            n_pos = (fr["total_pnl"] > 0).sum()
            sign_flip = not (fr["total_pnl"] > 0).all() and not (fr["total_pnl"] < 0).all()
            print(f"  正報酬fold：{n_pos}/{len(fr)}  sign_flip={sign_flip}  OOS總損益={fr['total_pnl'].sum():,.1f}pt")
        print()


if __name__ == "__main__":
    main()
