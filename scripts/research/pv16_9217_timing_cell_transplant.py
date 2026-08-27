#!/usr/bin/env python3
"""PV16-style joint categorical cell-bucketing transplant on 9217 decisive buy events.

背景：TMF micro-channel 的 PV16 設計把 price x volume 切成少數幾個離散類別狀態（cell），
不同 cell 分開校準，而非單一連續門檻一體適用。本研究把「用聯合類別狀態分桶、看不同桶
outcome 是否系統性不同」這個方法論概念，借來測試 9217（凱基-松山）branch-follow 決定性
買進事件（n=36, whale_9217_5dnet95_trades.csv）—— 目前 branch-follow 訊號只用連續門檻
（buy_5d 金額、net_ratio），從沒有做過聯合類別分桶。

⚠️ 誠實揭露：`stock_broker_branch_daily` 只有日粒度（無 intraday 時間戳），所以「買進集中在
盤中哪個時段」這個維度**無法**用真正的 intraday timing 得到。改用兩個粗代理：
  (a) T0（signal_date，5日滾動窗口完成日）當天價格走勢形狀（上漲收/下跌收）—— 作為
      「買盤是否貫穿全日、還是早盤買午盤賣」的代理。
  (b) T0 是否跳空（open vs 前一交易日 close）—— 作為「買盤是否在開盤前已經反映」的代理。
再加一個第三維：
  (c) T0 當日量能相對 20 日均量的 regime（高量 vs 正常量）。

n=36 切進最多 8 個 cell，每格均值僅 ~4.5 筆，嚴重 underpowered——本腳本明確標註此限制，
不做任何「發現 edge」宣稱，只回報 ANOVA/Kruskal-Wallis 與逐格描述性統計。

輸出：reports/research/pv16_9217_timing_transplant/
  cell_table.csv        （每筆事件的 cell 標籤 + r_adj_pct）
  cell_summary.csv       （2-dim 主分析：4 cell 摘要）
  cell_summary_3dim.csv  （3-dim 探索：最多 8 cell 摘要）
  summary.json           （ANOVA / Kruskal-Wallis 結果 + drop-stats）

用法（唯讀 DB；必須在 mini 上跑）：
  PYTHONPATH=src .venv/bin/python scripts/research/pv16_9217_timing_cell_transplant.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
TRADES_CSV = ROOT / "reports" / "research" / "branch-footprint-screen" / "whale_9217_5dnet95_trades.csv"
OUT_DIR = ROOT / "reports" / "research" / "pv16_9217_timing_transplant"
GAP_THRESH = 0.003       # >=0.3% open-vs-prev-close treated as "gap up"
VOL_HIGH_MULT = 1.3      # T0 volume >= 1.3x 20d avg treated as "high volume"


def load_stock_window(conn, sid: str, before: str, after: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT trade_date, open, close, volume FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ? AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE, before, after),
    ).fetchall()
    return pd.DataFrame(rows, columns=["trade_date", "open", "close", "volume"])


def compute_dims(conn, trades: pd.DataFrame) -> pd.DataFrame:
    out_rows = []
    n_missing = 0
    for row in trades.itertuples(index=False):
        sid = str(row.stock_id)
        sig = row.signal_date
        # window: enough padding to get 20 trading days before signal_date + signal_date itself
        bars = load_stock_window(conn, sid, "2024-01-01", "2026-08-31")
        bars = bars.sort_values("trade_date").reset_index(drop=True)
        idx = bars.index[bars["trade_date"] == sig]
        if len(idx) == 0:
            n_missing += 1
            continue
        i = idx[0]
        if i < 1:
            n_missing += 1
            continue
        t0 = bars.iloc[i]
        prev_close = bars.iloc[i - 1]["close"]
        if not prev_close or prev_close <= 0 or not t0["open"] or t0["open"] <= 0:
            n_missing += 1
            continue

        # (a) T0 same-day price action shape: up-close vs down-close
        t0_shape = "up_close" if t0["close"] >= t0["open"] else "down_close"

        # (b) T0 gap: open vs prior close
        gap_pct = t0["open"] / prev_close - 1.0
        gap_bucket = "gap_up" if gap_pct >= GAP_THRESH else "no_gap"

        # (c) T0 volume regime vs trailing 20d avg (excluding T0 itself)
        prior20 = bars.iloc[max(0, i - 20):i]["volume"]
        if len(prior20) < 10 or prior20.mean() <= 0:
            vol_bucket = "unknown"
        else:
            vol_ratio = t0["volume"] / prior20.mean()
            vol_bucket = "high_vol" if vol_ratio >= VOL_HIGH_MULT else "normal_vol"

        out_rows.append(
            {
                "signal_date": sig,
                "stock_id": sid,
                "r_adj_pct": row.r_adj_pct,
                "t0_shape": t0_shape,
                "gap_bucket": gap_bucket,
                "gap_pct": round(float(gap_pct) * 100, 3),
                "vol_bucket": vol_bucket,
                "vol_ratio": round(float(t0["volume"] / prior20.mean()), 3)
                if len(prior20) >= 10 and prior20.mean() > 0
                else None,
            }
        )
    print(f"[INFO] n_missing_dims (no prior bar / no T0 match) = {n_missing}")
    return pd.DataFrame(out_rows)


def group_stats(df: pd.DataFrame, cell_col: str) -> pd.DataFrame:
    recs = []
    for cell, sub in df.groupby(cell_col):
        vals = sub["r_adj_pct"].to_numpy()
        recs.append(
            {
                "cell": cell,
                "n": len(vals),
                "mean_r_adj_pct": round(float(np.mean(vals)), 3),
                "median_r_adj_pct": round(float(np.median(vals)), 3),
                "win_rate_pct": round(float((vals > 0).mean()) * 100, 1),
                "std_r_adj_pct": round(float(np.std(vals, ddof=1)), 3) if len(vals) > 1 else None,
            }
        )
    return pd.DataFrame(recs).sort_values("cell").reset_index(drop=True)


def anova_kruskal(df: pd.DataFrame, cell_col: str) -> dict:
    groups = [sub["r_adj_pct"].to_numpy() for _, sub in df.groupby(cell_col) if len(sub) >= 2]
    n_cells = df[cell_col].nunique()
    if len(groups) < 2:
        return {"note": "fewer than 2 cells with n>=2; ANOVA/Kruskal not meaningful", "n_cells": n_cells}
    f_stat, f_p = stats.f_oneway(*groups)
    try:
        h_stat, h_p = stats.kruskal(*groups)
    except ValueError:
        h_stat, h_p = None, None
    return {
        "n_cells_total": n_cells,
        "n_cells_used_n_ge_2": len(groups),
        "anova_f": round(float(f_stat), 3),
        "anova_p": round(float(f_p), 4),
        "kruskal_h": round(float(h_stat), 3) if h_stat is not None else None,
        "kruskal_p": round(float(h_p), 4) if h_p is not None else None,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(TRADES_CSV, dtype={"stock_id": str})
    print(f"[INFO] loaded {len(trades)} trades from {TRADES_CSV}")

    conn = connect(DEFAULT_DB_PATH)
    dims = compute_dims(conn, trades)
    conn.close()
    print(f"[INFO] computed dims for {len(dims)}/{len(trades)} trades")

    # 2-dim primary cell: gap_bucket x vol_bucket (drop unknown vol)
    dims2 = dims[dims["vol_bucket"] != "unknown"].copy()
    dims2["cell_2dim"] = dims2["gap_bucket"] + " x " + dims2["vol_bucket"]

    # 3-dim exploratory cell: t0_shape x gap_bucket x vol_bucket
    dims3 = dims2.copy()
    dims3["cell_3dim"] = dims3["t0_shape"] + " x " + dims3["gap_bucket"] + " x " + dims3["vol_bucket"]

    cell_table_path = OUT_DIR / "cell_table.csv"
    dims3.to_csv(cell_table_path, index=False)
    print(f"[OK] wrote {cell_table_path}")

    summary2 = group_stats(dims2, "cell_2dim")
    summary2_path = OUT_DIR / "cell_summary.csv"
    summary2.to_csv(summary2_path, index=False)
    print(f"[OK] wrote {summary2_path}")
    print(summary2.to_string(index=False))

    summary3 = group_stats(dims3, "cell_3dim")
    summary3_path = OUT_DIR / "cell_summary_3dim.csv"
    summary3.to_csv(summary3_path, index=False)
    print(f"[OK] wrote {summary3_path}")
    print(summary3.to_string(index=False))

    stats2 = anova_kruskal(dims2, "cell_2dim")
    stats3 = anova_kruskal(dims3, "cell_3dim")

    # also single-dim breakdowns for context
    single_dim_summaries = {
        "gap_bucket": group_stats(dims2, "gap_bucket").to_dict(orient="records"),
        "vol_bucket": group_stats(dims2, "vol_bucket").to_dict(orient="records"),
        "t0_shape": group_stats(dims2, "t0_shape").to_dict(orient="records"),
    }
    single_dim_tests = {
        "gap_bucket": anova_kruskal(dims2, "gap_bucket"),
        "vol_bucket": anova_kruskal(dims2, "vol_bucket"),
        "t0_shape": anova_kruskal(dims2, "t0_shape"),
    }

    summary = {
        "n_trades_input": len(trades),
        "n_trades_with_dims": len(dims),
        "n_trades_used_2dim_3dim": len(dims2),
        "protocol": {
            "gap_threshold_pct": GAP_THRESH * 100,
            "vol_high_multiplier": VOL_HIGH_MULT,
            "vol_window_days": 20,
            "caveat": (
                "stock_broker_branch_daily is daily-granularity only; no intraday timestamp exists. "
                "'time of day' is NOT directly observable and was substituted with two coarse proxies: "
                "(a) T0 same-day close-vs-open shape (t0_shape) as a stand-in for whether buying pressure "
                "built through the session, and (b) T0 gap (open vs prior close) as a stand-in for whether "
                "buying was already priced in pre-open. A third dim (c) T0 volume regime vs trailing 20d avg "
                "was added as the 'how much conviction' axis."
            ),
        },
        "primary_2dim_anova_kruskal": stats2,
        "exploratory_3dim_anova_kruskal": stats3,
        "single_dim_breakdowns": single_dim_summaries,
        "single_dim_tests": single_dim_tests,
    }
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"[OK] wrote {summary_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
