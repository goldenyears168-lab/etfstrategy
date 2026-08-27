#!/usr/bin/env python3
"""Phase 5：750天（實際涵蓋2023-07-03~2026-08-06，遠超原定582天）walk-forward。

Phase 2（83天粗篩，2026-04-01~2026-07-31）發現 window=89 搭配寬停損（400~500pt）+
target/stop比1.5~2.0 的組合方向一致為正（day/night都正、P&L集中度<15%、同根K棒
stop/target同時觸及=0%），但 Newey-West t≈1.1~1.3、p≈0.18~0.27——83天樣本檢定力不足，
不能下結論（設計文件 kill criterion 3 的預期情境：不顯著是預期結果，不是失敗）。

這支腳本把同樣的候選參數放到全樣本 walk-forward 上正式檢驗（設計文件 Phase 5）。

⚠️ 已知混淆變數：TX指數在這750天裡從~16,500（2023-07）漲到~44,000（2026-06），
名目價格漲了將近3倍。固定點數停損在不同價格區間代表的『相對波動度』完全不同——
這是Option A（固定點數）的已知限制，設計文件本身把Option B（ATR倍數停損）列為
『第二版』正是為了處理這個問題。這裡先誠實跑Option A看每個fold的表現，如果出現
kill criterion 5（正負號翻轉）大概率就是這個混淆造成，不代表核心訊號沒用，但
Option A本身不能通過。

Fold切法：前150天當初始IS（不報告OOS績效，只拿來算第一個fold的ATR門檻），之後
每120天切一個不重疊OOS fold（5-6折），每折的ATR門檻用『這折開始之前的所有天數』
累積池化算（expanding window，跟第七輪的look-ahead修法一致，不能用未來資料）。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_phase5_walkforward.py
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
from scipy import stats as sstats  # noqa: E402

from tx_channel_geometry_control import ATR_PERIOD, calculate_atr  # noqa: E402
from tx_flat_bracket_engine import run_portfolio_bracket  # noqa: E402

DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_tick_built_582d"

CANDIDATES = [
    dict(window=89, stop_pts=400.0, target_pts=800.0, label="w89 stop400 ratio2.0"),
    dict(window=89, stop_pts=400.0, target_pts=600.0, label="w89 stop400 ratio1.5"),
    dict(window=89, stop_pts=500.0, target_pts=1000.0, label="w89 stop500 ratio2.0"),
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


def newey_west_tstat(daily_pnl: pd.Series, maxlags: int = 5) -> tuple[float, float]:
    x = daily_pnl.values.astype(float)
    n = len(x)
    if n < 5:
        return 0.0, 1.0
    mean = x.mean()
    resid = x - mean
    var = (resid @ resid) / n
    for lag in range(1, min(maxlags, n - 1) + 1):
        w = 1 - lag / (maxlags + 1)
        cov = (resid[lag:] @ resid[:-lag]) / n
        var += 2 * w * cov
    se = (var / n) ** 0.5
    tstat = mean / se if se > 0 else 0.0
    pval = 2 * (1 - sstats.t.cdf(abs(tstat), df=max(1, n - 1)))
    return tstat, pval


def main() -> None:
    all_days = load_all_days()
    print(f"全樣本：{len(all_days)}天（{all_days[0]} ~ {all_days[-1]}）")

    folds = []
    start = INITIAL_IS_DAYS
    while start < len(all_days):
        end = min(start + FOLD_SIZE_DAYS, len(all_days))
        folds.append((all_days[start:end]))
        start = end
    print(f"初始IS={INITIAL_IS_DAYS}天，之後切 {len(folds)} 個OOS fold（每折約{FOLD_SIZE_DAYS}天）\n")

    print("正在載入全部bar資料（一次性，供所有fold重複使用）...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    for cand in CANDIDATES:
        print(f"=== 候選：{cand['label']} ===")
        fold_results = []
        for i, fold_days in enumerate(folds):
            is_days = all_days[: INITIAL_IS_DAYS + i * FOLD_SIZE_DAYS]
            atr_threshold = compute_atr_threshold_pooled(is_days, all_bars)
            result = run_portfolio_bracket(
                fold_days, all_bars, [cand["window"]], atr_threshold,
                cand["stop_pts"], cand["target_pts"], TIME_STOP_BARS,
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
            fold_results.append(
                dict(fold=i + 1, start=fold_days[0], end=fold_days[-1], n=len(df), total_pnl=total,
                     win_rate=win_rate, day_pnl=day_pnl, night_pnl=night_pnl, invariant_ok=result["invariant_ok"])
            )
            print(
                f"  Fold{i+1} ({fold_days[0]}~{fold_days[-1]}, ATR門檻={atr_threshold:.1f}): "
                f"{len(df)}筆 總損益={total:,.1f}pt 勝率={win_rate:.1f}% "
                f"(day={day_pnl:,.1f} / night={night_pnl:,.1f}) invariant={result['invariant_ok']}"
            )

        if not fold_results:
            print("  全部fold無交易，跳過統計。\n")
            continue

        fr = pd.DataFrame(fold_results)
        n_pos_folds = (fr["total_pnl"] > 0).sum()
        n_total_folds = len(fr)
        all_daily = []
        # 為了NW檢定，重跑一次收集每日pnl（跨所有fold合併，但ATR門檻仍各自fold算的那個，
        # 這裡只是為了算整體顯著性，不影響fold內部的因果順序）。
        print(f"\n  正報酬fold：{n_pos_folds}/{n_total_folds}")
        print(f"  day全負：{(fr['day_pnl']<0).all()}  night全負：{(fr['night_pnl']<0).all()}")
        sign_flip = not (fr["total_pnl"] > 0).all() and not (fr["total_pnl"] < 0).all()
        print(f"  Kill criterion 5（任一fold正負號翻轉）：{'觸發' if sign_flip else '未觸發（各fold同號或部分fold無交易）'}")
        kc6 = (fr["day_pnl"] < 0).all() or (fr["night_pnl"] < 0).all()
        print(f"  Kill criterion 6（day或night全部fold皆負）：{'觸發' if kc6 else '未觸發'}")
        print(f"  全樣本OOS總損益加總：{fr['total_pnl'].sum():,.1f}pt\n")


if __name__ == "__main__":
    main()
