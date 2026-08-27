#!/usr/bin/env python3
"""選項A/B都在750天walk-forward上以同一個fold pattern失敗（Fold3/5正、Fold1/2/4負）
之後，做的regime分層診斷：quintile拆解顯示trailing 20天realized vol最高的20%那組
單獨貢獻了+9,031.8pt（其餘80%合計-15,609.7pt），呈現的是門檻效應而非線性關係
（Spearman IC只有0.0627/p=0.09，因為quintile 0~3之間不是單調的，只有最頂端那組
特別突出）。

這支腳本把這個描述性發現變成一個**因果、可walk-forward驗證**的regime gate，避免
重蹈這條線一再犯的錯（單一切分/描述性統計當結論，沒有真正做OOS驗證）：
  - trailing_vol_pct[day] = trailing 20天|daily return|均值，在『這一天以前的全部
    歷史』（expanding，只用過去資料，不含當天）中的百分位排名
  - gate：trailing_vol_pct[day] >= threshold 才允許當天（日盤+夜盤）進場，否則整天
    跳過——這是flat-default架構下第一次真正測試design doc 1.2節提到的『新架構下
    veto原則上可行，但要實測』
  - threshold本身也要在walk-forward意義下合理：這裡先用『整個IS期間』的百分位分佈
    定義門檻（expanding IS-only，跟ATR門檻的算法一致），不用未來資料

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_vol_regime_gate.py
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
VOL_LOOKBACK_DAYS = 20
GATE_PERCENTILES = [0.0, 50.0, 60.0, 70.0, 80.0]  # 0.0 = 不gate（對照組）


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
    print("正在載入全部bar資料...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    # 因果trailing vol：每天用『這天以前』的20天|daily return|均值
    daily_close = pd.Series({d: all_bars[d]["Close"].iat[-1] for d in all_days}).sort_index()
    daily_rets_abs = daily_close.pct_change().abs()
    trailing_vol20 = daily_rets_abs.rolling(VOL_LOOKBACK_DAYS).mean().shift(1)

    folds = []
    start = INITIAL_IS_DAYS
    while start < len(all_days):
        end = min(start + FOLD_SIZE_DAYS, len(all_days))
        folds.append(all_days[start:end])
        start = end

    for gate_pct in GATE_PERCENTILES:
        print(f"=== gate門檻：trailing_vol20 percentile >= {gate_pct:.0f} (0=不gate對照組) ===")
        fold_results = []
        for i, fold_days in enumerate(folds):
            is_days = all_days[: INITIAL_IS_DAYS + i * FOLD_SIZE_DAYS]
            atr_threshold = compute_atr_threshold_pooled(is_days, all_bars)

            if gate_pct <= 0.0:
                allowed_days = fold_days
            else:
                # 用IS期間的trailing_vol20分佈定義門檻值(expanding IS-only，不用未來資料)
                is_vol = trailing_vol20.loc[is_days].dropna()
                if len(is_vol) < 30:
                    allowed_days = fold_days
                else:
                    threshold_val = np.percentile(is_vol, gate_pct)
                    allowed_days = [
                        d for d in fold_days
                        if d in trailing_vol20.index
                        and not pd.isna(trailing_vol20.loc[d])
                        and trailing_vol20.loc[d] >= threshold_val
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
