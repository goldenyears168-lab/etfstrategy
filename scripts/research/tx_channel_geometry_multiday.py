#!/usr/bin/env python3
"""多天版：Donchian vs LRC，用最接近原作者假設的執行條件（延遲一根K棒成交 + 5.9pt/筆成本），
在 tmf_channel 離線快取（2026-04-01 ~ 2026-07-31，83 個交易日）逐日回測。

單日 8/6 的結論是「LRC 的優勢是零成本零延遲造出來的假象，換成寫實假設後三組都虧」——這支
腳本檢查這個結論是不是穩定的，還是 8/6 剛好是特例。每天各自獨立跑（channel 用當天K棒重新
配適，不跨日結轉部位——收盤/隔夜跳空不計入同一筆交易，避免引入額外的隔夜風險變數）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_control import (  # noqa: E402
    ATR_PERIOD,
    calculate_atr,
    calculate_donchian_channel,
    calculate_lrc_channel,
    channel_state_machine,
)
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402

DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
SOURCE = "tx_1m_daynight_cache.json"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COST_PTS_PER_TRADE = 5.9
FILL_LAG_BARS = 1


def _use_cjk_font() -> None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return


def load_days() -> list[str]:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (SOURCE,)
        ).fetchall()
    return [r[0] for r in rows]


def load_day_bars(day: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v FROM bars WHERE source=? AND day=? ORDER BY t",
            conn, params=(SOURCE, day),
        )
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    return df[["Datetime", "Open", "High", "Low", "Close", "Volume"]]


def run_day(df: pd.DataFrame, channel_fn, use_rsi_exit: bool) -> dict:
    atr_probe = calculate_atr(df, ATR_PERIOD)["ATR"].dropna()
    if len(atr_probe) < 30:
        return {"n_trades": 0, "win_rate": 0.0, "total_pnl": 0.0}
    atr_threshold = float(np.percentile(atr_probe, 5))
    sig = channel_state_machine(df, channel_fn, atr_threshold, use_rsi_exit=use_rsi_exit)
    if len(sig) < 20:
        return {"n_trades": 0, "win_rate": 0.0, "total_pnl": 0.0}
    _, trades = simulate_pnl_realistic(sig, fill_lag_bars=FILL_LAG_BARS, cost_pts_per_trade=COST_PTS_PER_TRADE)
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    return {"n_trades": n, "win_rate": (len(wins) / n * 100) if n else 0.0, "total_pnl": total_pnl}


def main() -> None:
    days = load_days()
    print(f"days available: {len(days)}  ({days[0]} ~ {days[-1]})")
    print(f"執行假設：延遲{FILL_LAG_BARS}根K棒成交 + {COST_PTS_PER_TRADE}pt/筆成本（最接近原作者假設，8/6稽核結論用的同一組）\n")

    combos = {
        "donchian_baseline": (calculate_donchian_channel, True),
        "lrc_baseline": (calculate_lrc_channel, True),
        "lrc_no_rsi_exit": (calculate_lrc_channel, False),
    }

    daily_rows = []
    for i, day in enumerate(days):
        df = load_day_bars(day)
        if len(df) < 100:
            continue
        for name, (channel_fn, use_rsi_exit) in combos.items():
            r = run_day(df, channel_fn, use_rsi_exit)
            daily_rows.append({"day": day, "combo": name, **r})
        if (i + 1) % 20 == 0:
            print(f"  ...{i+1}/{len(days)} 天處理完")

    daily_df = pd.DataFrame(daily_rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily_path = OUT_DIR / "multiday_daily_pnl.csv"
    daily_df.to_csv(daily_path, index=False)
    print(f"\ndaily detail saved: {daily_path}")

    print(f"\n=== 83天彙總（{FILL_LAG_BARS}根延遲 + {COST_PTS_PER_TRADE}pt成本）===")
    summary_rows = []
    for name in combos:
        sub = daily_df[daily_df["combo"] == name]
        total = sub["total_pnl"].sum()
        pos_days = (sub["total_pnl"] > 0).sum()
        n_days = len(sub)
        mean_daily = sub["total_pnl"].mean()
        std_daily = sub["total_pnl"].std()
        sharpe_like = (mean_daily / std_daily * np.sqrt(252)) if std_daily else float("nan")
        cum = sub.sort_values("day")["total_pnl"].cumsum()
        running_max = cum.cummax().clip(lower=0)
        max_dd = (cum - running_max).min()
        total_trades = sub["n_trades"].sum()
        print(f"{name:22s}  總損益={total:>12,.1f}pt  正報酬天數={pos_days}/{n_days} "
              f"({pos_days/n_days*100:.0f}%)  日均={mean_daily:>9,.1f}pt  "
              f"年化Sharpe-like={sharpe_like:.2f}  最大回落={max_dd:>12,.1f}pt  總交易={total_trades}")
        summary_rows.append({
            "combo": name, "total_pnl": total, "positive_days": pos_days, "n_days": n_days,
            "mean_daily_pnl": mean_daily, "std_daily_pnl": std_daily, "sharpe_like": sharpe_like,
            "max_drawdown": max_dd, "total_trades": total_trades,
        })

    summary_path = OUT_DIR / "multiday_summary.csv"
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    print(f"\nsummary saved: {summary_path}")

    # 圖：83天累計權益曲線
    _use_cjk_font()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 9))
    colors = {"donchian_baseline": "#2C3E50", "lrc_baseline": "#C0392B", "lrc_no_rsi_exit": "#E67E22"}
    for name in combos:
        sub = daily_df[daily_df["combo"] == name].sort_values("day")
        cum = sub["total_pnl"].cumsum()
        ax1.plot(pd.to_datetime(sub["day"]), cum, label=name, color=colors[name], linewidth=1.5)
    ax1.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax1.set_title(f"83天累計損益（{FILL_LAG_BARS}根延遲成交 + {COST_PTS_PER_TRADE}pt/筆成本，最接近原作者假設）")
    ax1.set_ylabel("累計損益 (pt)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    for name in combos:
        sub = daily_df[daily_df["combo"] == name].sort_values("day")
        ax2.plot(pd.to_datetime(sub["day"]), sub["total_pnl"], label=name, color=colors[name],
                  linewidth=0.8, alpha=0.7, marker="o", markersize=2)
    ax2.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_title("每日損益")
    ax2.set_ylabel("當日損益 (pt)")
    ax2.set_xlabel("日期")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    chart_path = fig_dir / "multiday_equity.png"
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)
    print(f"chart saved: {chart_path}")


if __name__ == "__main__":
    main()
