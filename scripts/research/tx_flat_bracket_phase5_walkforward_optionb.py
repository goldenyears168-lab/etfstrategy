#!/usr/bin/env python3
"""Phase 5（選項B）：750天walk-forward，驗證Phase 2 83天粗篩找到的ATR倍數停損候選。

跟選項A的 Phase 5（`tx_flat_bracket_phase5_walkforward.py`）結構相同（前150天當初始IS，
之後切5個不重疊120天OOS fold，ATR門檻用expanding IS pool算），差別只在候選參數改成
k_stop/k_target（ATR倍數，理論上會隨regime自動縮放停損距離，這正是選項B被design doc
點名用來解決選項A「固定點數在3倍price regime上不穩健」這個已證實失敗模式的原因）。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_phase5_walkforward_optionb.py
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
from tx_flat_bracket_engine_optionb import run_portfolio_bracket_optionb  # noqa: E402

DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_tick_built_582d"

CANDIDATES = [
    dict(window=89, k_stop=20.0, k_target=30.0, label="w89 k_stop20 ratio1.5"),
    dict(window=89, k_stop=25.0, k_target=50.0, label="w89 k_stop25 ratio2.0"),
    dict(window=89, k_stop=20.0, k_target=40.0, label="w89 k_stop20 ratio2.0"),
]
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


def main() -> None:
    all_days = load_all_days()
    print(f"全樣本：{len(all_days)}天（{all_days[0]} ~ {all_days[-1]}）")

    folds = []
    start = INITIAL_IS_DAYS
    while start < len(all_days):
        end = min(start + FOLD_SIZE_DAYS, len(all_days))
        folds.append(all_days[start:end])
        start = end
    print(f"初始IS={INITIAL_IS_DAYS}天，之後切 {len(folds)} 個OOS fold\n")

    print("正在載入全部bar資料...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    for cand in CANDIDATES:
        print(f"=== 候選：{cand['label']} ===")
        fold_results = []
        for i, fold_days in enumerate(folds):
            is_days = all_days[: INITIAL_IS_DAYS + i * FOLD_SIZE_DAYS]
            atr_threshold = compute_atr_threshold_pooled(is_days, all_bars)
            result = run_portfolio_bracket_optionb(
                fold_days, all_bars, [cand["window"]], atr_threshold,
                cand["k_stop"], cand["k_target"], TIME_STOP_BARS,
            )
            trades = result["trades"]
            if not trades:
                print(f"  Fold{i+1} ({fold_days[0]}~{fold_days[-1]}): 無交易")
                continue
            df = pd.DataFrame(trades)
            total = df["pnl"].sum()
            win_rate = (df["pnl"] > 0).mean() * 100
            by_sess = df.groupby("sess")["pnl"].sum()
            day_pnl = by_sess.get("day", 0.0)
            night_pnl = by_sess.get("night", 0.0)
            fold_results.append(dict(fold=i + 1, total_pnl=total, day_pnl=day_pnl, night_pnl=night_pnl))
            print(
                f"  Fold{i+1} ({fold_days[0]}~{fold_days[-1]}, ATR門檻={atr_threshold:.1f}): "
                f"{len(df)}筆 總損益={total:,.1f}pt 勝率={win_rate:.1f}% "
                f"(day={day_pnl:,.1f} / night={night_pnl:,.1f}) invariant={result['invariant_ok']}"
            )

        if not fold_results:
            print("  全部fold無交易。\n")
            continue
        fr = pd.DataFrame(fold_results)
        n_pos = (fr["total_pnl"] > 0).sum()
        n_tot = len(fr)
        sign_flip = not (fr["total_pnl"] > 0).all() and not (fr["total_pnl"] < 0).all()
        kc6 = (fr["day_pnl"] < 0).all() or (fr["night_pnl"] < 0).all()
        print(f"\n  正報酬fold：{n_pos}/{n_tot}")
        print(f"  Kill criterion 5（fold正負號翻轉）：{'觸發' if sign_flip else '未觸發'}")
        print(f"  Kill criterion 6（day或night全部fold皆負）：{'觸發' if kc6 else '未觸發'}")
        print(f"  全樣本OOS總損益加總：{fr['total_pnl'].sum():,.1f}pt\n")


if __name__ == "__main__":
    main()
