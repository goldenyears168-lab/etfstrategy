#!/usr/bin/env python3
"""Regime分層第二輪：trailing volatility gate已證實是Fold5佔比假象（rejected，見
`config/research.yaml` H-TXFB-VOL-REGIME-GATE），改測其他regime變數：VIXTWN水準、
外資期貨OI z60（champion因子）、TX自身趨勢posture（N日均線位置）。

方法論教訓（沿用自vol gate那輪）：全樣本描述性IC只拿來做便宜的第一輪篩選，不能單獨
當結論；任何看起來有希望的變數都必須進到causal walk-forward gate驗證，且要檢查
「OOS總損益改善」是不是又是某個fold（尤其Fold5）佔比機械上升造成的假象，不是真的
把其他fold的regime子區間篩選成正報酬。

PIT規約：兩個外部資料源都是「T日收盤後才發布」——VIXTWN（隔日盤中才看得到前一日
收盤）、外資期貨OI（財報式inst數據，T日收盤後公布）——用來gate T日的交易時，一律
用T-1的值（shift(1)），不得用T日當天的值。

跑法：
  PYTHONPATH=src .venv/bin/python -W ignore scripts/research/tx_flat_bracket_regime_screen.py
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


def load_foreign_oi() -> pd.Series:
    with sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT trade_date, net_oi_vol FROM futures_institutional_daily "
            "WHERE futures_id='TX' AND inst_name='外資' ORDER BY trade_date",
            conn,
        )
    return df.set_index("trade_date")["net_oi_vol"]


def main() -> None:
    all_days = load_all_days()
    print(f"全樣本：{len(all_days)}天（{all_days[0]} ~ {all_days[-1]}）")
    print("正在載入bar資料...")
    all_bars = {d: load_day_bars_with_sess(d) for d in all_days}
    print("載入完成。\n")

    daily_close = pd.Series({d: all_bars[d]["Close"].iat[-1] for d in all_days}).sort_index()

    atr_threshold = float(np.percentile(
        pd.concat([calculate_atr(all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]], ATR_PERIOD)["ATR"].dropna()
                   for d in all_days]), 5))
    result = run_portfolio_bracket(all_days, all_bars, [CANDIDATE["window"]], atr_threshold,
                                    CANDIDATE["stop_pts"], CANDIDATE["target_pts"], TIME_STOP_BARS)
    by_day_pnl = pd.Series(result["by_day"]).reindex(all_days).fillna(0.0)
    print(f"候選策略：{result['n_trades']}筆交易，總損益={result['total_pnl']:,.1f}pt\n")

    vixtwn = load_vixtwn().reindex(all_days)
    foreign_oi = load_foreign_oi().reindex(all_days)

    regime_vars = {}
    # (1) VIXTWN水準（T-1收盤值，PIT安全）
    regime_vars["vixtwn_level_t1"] = vixtwn.shift(1)
    # (2) VIXTWN 20天變化率（T-1相對T-21，衡量趨勢方向而非單純水準）
    regime_vars["vixtwn_chg20_t1"] = vixtwn.shift(1) - vixtwn.shift(21)
    # (3) 外資期貨OI z60（champion因子，T-1值，PIT安全）
    foreign_oi_z60 = (foreign_oi - foreign_oi.rolling(60, min_periods=60).mean()) / foreign_oi.rolling(60, min_periods=60).std()
    regime_vars["foreign_oi_z60_t1"] = foreign_oi_z60.shift(1)
    regime_vars["foreign_oi_z60_abs_t1"] = foreign_oi_z60.shift(1).abs()
    # (4) TX自身趨勢posture：收盤價相對60日均線的乖離%（用日收盤，因果，shift(1)避免用到當天）
    ma60 = daily_close.rolling(60, min_periods=60).mean()
    regime_vars["tx_ma60_dev_pct_t1"] = ((daily_close - ma60) / ma60 * 100).shift(1)
    ma20 = daily_close.rolling(20, min_periods=20).mean()
    regime_vars["tx_ma20_dev_pct_t1"] = ((daily_close - ma20) / ma20 * 100).shift(1)
    # (5) TX自身20日趨勢強度(|20日報酬|，衡量近期是否在單邊趨勢中)
    regime_vars["tx_trend_strength20_t1"] = daily_close.pct_change(20).abs().shift(1)

    print("=== 全樣本描述性 Spearman IC（regime變數 -> 當日策略pnl）===")
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
        print(f"\n--- {name} (IC={ic:.4f}) ---")
        print(merged.groupby("q")["pnl"].agg(["count", "sum", "mean"]))


if __name__ == "__main__":
    main()
