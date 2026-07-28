#!/usr/bin/env python3
"""WHALE-ROUND3-9268-9800-YEARSPLIT · 拆解 9268/9800 事件依年份的 forward-return 穩定度。

用途：驗證兩個超大樣本負向分點(9268 n=17759, 9800 n=12824)的負向效果是否
穩定存在於2024/2025/2026各年，還是被特定期間主導（比照920F前車之鑑，但這次
反過來確認"持續負向"本身是否穩健）。

純本地運算，讀取既有 whale_branch_top3_multihorizon_events.csv，不需 SSH。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

EVENTS_CSV = ROOT / "reports/research/branch-footprint-screen/whale_branch_top3_multihorizon_events.csv"
OUT_CSV = ROOT / "reports/research/branch-footprint-screen/whale_branch_round3_9268_9800_year_split.csv"

TARGETS = ["9268", "9800"]
HORIZONS = ["h7", "h14", "h20"]


def summarize(sub: pd.DataFrame, h: str) -> dict:
    col = f"r_adj_{h}_pct"
    x = sub[col].dropna()
    if len(x) < 2:
        return {"n": len(x), "median": None, "mean": None, "win_rate": None, "p_value": None, "t_stat": None}
    t_stat, p_val = stats.ttest_1samp(x, 0.0)
    return {
        "n": len(x),
        "median": round(float(x.median()), 4),
        "mean": round(float(x.mean()), 4),
        "win_rate": round(float((x > 0).mean() * 100), 2),
        "p_value": float(p_val),
        "t_stat": round(float(t_stat), 3),
    }


def main() -> int:
    df = pd.read_csv(EVENTS_CSV, dtype={"securities_trader_id": str})
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["year"] = df["signal_date"].dt.year

    rows = []
    for tid in TARGETS:
        sub_all = df[df["securities_trader_id"] == tid]
        for h in HORIZONS:
            overall = summarize(sub_all, h)
            overall.update({"trader_id": tid, "year": "ALL", "horizon": h})
            rows.append(overall)
        for yr, sub_yr in sub_all.groupby("year"):
            for h in HORIZONS:
                r = summarize(sub_yr, h)
                r.update({"trader_id": tid, "year": str(yr), "horizon": h})
                rows.append(r)

    out = pd.DataFrame(rows)[["trader_id", "year", "horizon", "n", "median", "mean", "win_rate", "p_value", "t_stat"]]
    out = out.sort_values(["trader_id", "horizon", "year"])
    out.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 160)
    print(out.to_string(index=False))
    print("saved:", OUT_CSV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
