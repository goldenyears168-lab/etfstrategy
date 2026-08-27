#!/usr/bin/env python3
"""Diagnostic scratch script for Fold2 investigation, 2026-08-07 — not part of the tested pipeline.

Reuses the exact loading/threshold pattern from tx_flat_bracket_phase5_walkforward.py
(candidate: window=89, stop=400pt, target=800pt, time_stop=999) to pull trade-level and
price-regime diagnostics for all 5 OOS folds, with focus on Fold2 (2024-08-09~2025-02-11).
"""
from __future__ import annotations

import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

from tx_channel_geometry_control import ATR_PERIOD, calculate_atr
from tx_flat_bracket_engine import run_portfolio_bracket

DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_tick_built_582d"

WINDOW = 89
STOP_PTS = 400.0
TARGET_PTS = 800.0
TIME_STOP_BARS = 999
INITIAL_IS_DAYS = 150
FOLD_SIZE_DAYS = 120


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


def daily_close(day_bars: pd.DataFrame) -> float:
    # last Close of the day (across both sessions, whichever is later in time)
    return float(day_bars.sort_values("Datetime")["Close"].iloc[-1])


def price_regime_stats(fold_days: list[str], all_bars: dict[str, pd.DataFrame]) -> dict:
    closes = [daily_close(all_bars[d]) for d in fold_days]
    closes = pd.Series(closes, index=fold_days)
    diffs = closes.diff().dropna()
    net_move = closes.iloc[-1] - closes.iloc[0]
    sum_abs_moves = diffs.abs().sum()
    efficiency_ratio = net_move / sum_abs_moves if sum_abs_moves > 0 else np.nan
    daily_ret = closes.pct_change().dropna()
    realized_vol_ann = daily_ret.std() * np.sqrt(252) * 100  # percent
    return dict(
        start=fold_days[0], end=fold_days[-1], n_days=len(fold_days),
        close_start=closes.iloc[0], close_end=closes.iloc[-1],
        net_move=net_move, sum_abs_moves=sum_abs_moves,
        efficiency_ratio=efficiency_ratio, realized_vol_ann_pct=realized_vol_ann,
    )


def main() -> None:
    all_days = load_all_days()
    print(f"全樣本：{len(all_days)}天（{all_days[0]} ~ {all_days[-1]}）\n")

    folds = []
    start = INITIAL_IS_DAYS
    while start < len(all_days):
        end = min(start + FOLD_SIZE_DAYS, len(all_days))
        folds.append(all_days[start:end])
        start = end

    print("正在載入全部bar資料...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    print("=== 價格regime特徵（全部fold） ===")
    regime_rows = []
    for i, fold_days in enumerate(folds):
        stats = price_regime_stats(fold_days, all_bars)
        stats["fold"] = i + 1
        regime_rows.append(stats)
        print(
            f"Fold{i+1} ({stats['start']}~{stats['end']}, n={stats['n_days']}): "
            f"close {stats['close_start']:.0f}->{stats['close_end']:.0f}  "
            f"net_move={stats['net_move']:+.0f}pt  sum_abs_daily_moves={stats['sum_abs_moves']:.0f}pt  "
            f"efficiency_ratio={stats['efficiency_ratio']:.3f}  ann_vol={stats['realized_vol_ann_pct']:.1f}%"
        )
    print()

    print("=== Trade-level診斷（每個fold） ===")
    all_fold_trades = {}
    for i, fold_days in enumerate(folds):
        is_days = all_days[: INITIAL_IS_DAYS + i * FOLD_SIZE_DAYS]
        atr_threshold = compute_atr_threshold_pooled(is_days, all_bars)
        result = run_portfolio_bracket(
            fold_days, all_bars, [WINDOW], atr_threshold, STOP_PTS, TARGET_PTS, TIME_STOP_BARS,
        )
        trades = result["trades"]
        if not trades:
            print(f"Fold{i+1}: 無交易")
            continue
        df = pd.DataFrame(trades)
        all_fold_trades[i + 1] = df

        n = len(df)
        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]
        win_rate = len(wins) / n * 100
        avg_win = wins["pnl"].mean() if len(wins) else 0.0
        avg_loss = losses["pnl"].mean() if len(losses) else 0.0
        total = df["pnl"].sum()
        avg_trade = df["pnl"].mean()

        # concentration: top 3 losses as % of total loss
        loss_sorted = losses["pnl"].sort_values()  # most negative first
        total_loss = losses["pnl"].sum()
        top3_loss = loss_sorted.head(3).sum()
        top3_pct = (top3_loss / total_loss * 100) if total_loss != 0 else 0.0

        reason_counts = df["reason"].value_counts()
        reason_pnl = df.groupby("reason")["pnl"].sum()

        by_sess = df.groupby("sess")["pnl"].agg(["sum", "count", "mean"])

        print(f"\n--- Fold{i+1} ({fold_days[0]}~{fold_days[-1]}), ATR門檻={atr_threshold:.1f} ---")
        print(f"  n_trades={n}  total_pnl={total:,.1f}  win_rate={win_rate:.1f}%  avg_trade={avg_trade:.1f}")
        print(f"  avg_win={avg_win:.1f}  avg_loss={avg_loss:.1f}  |avg_win/avg_loss|={abs(avg_win/avg_loss) if avg_loss else float('nan'):.2f}")
        print(f"  top3 losses sum={top3_loss:.1f} ({top3_pct:.1f}% of total loss, total_loss={total_loss:.1f})")
        print(f"  exit reasons (count): {reason_counts.to_dict()}")
        print(f"  exit reasons (pnl sum): {reason_pnl.round(1).to_dict()}")
        print(f"  by session:\n{by_sess.round(1)}")

        # timing clustering: split fold into thirds by day order
        day_order = {d: idx for idx, d in enumerate(fold_days)}
        df["day_idx"] = df["day"].map(day_order)
        third = max(1, len(fold_days) // 3)
        df["chunk"] = pd.cut(df["day_idx"], bins=[-1, third, 2 * third, len(fold_days)], labels=["first3rd", "mid3rd", "last3rd"])
        chunk_pnl = df.groupby("chunk", observed=True)["pnl"].agg(["sum", "count"])
        print(f"  by time-thirds:\n{chunk_pnl.round(1)}")

        # daily pnl cumulative low point / worst 10-day window
        daily = df.groupby("day")["pnl"].sum().reindex(fold_days, fill_value=0.0)
        cum = daily.cumsum()
        print(f"  cum_pnl path: start=0 min={cum.min():.1f} (at {cum.idxmin()}) end={cum.iloc[-1]:.1f}")

    print("\n=== 直接比較 Fold1/2/4（輸家）vs Fold3/5（贏家） ===")
    for i in [1, 2, 3, 4, 5]:
        if i not in all_fold_trades:
            continue
        df = all_fold_trades[i]
        n = len(df)
        win_rate = (df["pnl"] > 0).mean() * 100
        avg_trade = df["pnl"].mean()
        target_pct = (df["reason"] == "target").mean() * 100
        stop_pct = (df["reason"] == "stop").mean() * 100
        ts_pct = (df["reason"] == "time_stop").mean() * 100
        sef_pct = (df["reason"] == "session_end_forced").mean() * 100
        label = "WIN" if df["pnl"].sum() > 0 else "LOSE"
        print(
            f"Fold{i} [{label}]: n={n} win_rate={win_rate:.1f}% avg_trade={avg_trade:+.1f} "
            f"reasons target={target_pct:.1f}% stop={stop_pct:.1f}% time_stop={ts_pct:.1f}% session_end={sef_pct:.1f}%"
        )


if __name__ == "__main__":
    main()
