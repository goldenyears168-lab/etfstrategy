#!/usr/bin/env python3
"""Track D+A.1：用 permutation/placebo 檢定重新評估 9217 live 訊號(n=36)的顯著性.

背景：whale_9217_5dnet95_summary.json 的 t-test/Wilcoxon 結論在去除極端值後不穩健
（t_p 0.043->0.24），這種小n厚尾情境用漸進假設的檢定容易誤判。這支腳本改用
src/research/branch_signal_validation.py 的 permutation_test()：對9217的每個真實
觸發事件，從『同一檔股票』的全部合法L1H7訊號日中隨機抽一個當假訊號日，重複5000次
建立null分布，看真實觀察值(mean+5.66%/median+6.28%)落在第幾百分位——測的是「9217
選的時機，是否比同一批股票上的隨機時機更好」，不依賴t分布假設。

用法（必須在 mini 上跑，需要 stock_daily_bars/daily_bars）：
    PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_permutation_significance_test.py

輸入：reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv（已存在，n=36）
輸出：reports/research/branch-footprint-screen/whale_9217_permutation_test_result.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect
from research.branch_signal_validation import (
    build_l1h7_signal_dict,
    permutation_test,
    outlier_trim_sensitivity,
)

SOURCE = "finmind"
BENCH_CODE = "IX0001"
TRADES_CSV = ROOT / "reports" / "research" / "branch-footprint-screen" / "whale_9217_5dnet95_trades.csv"
OUT_JSON = ROOT / "reports" / "research" / "branch-footprint-screen" / "whale_9217_permutation_test_result.json"
N_PERM = 5000
SEED = 20260728


def load_stock_bars(conn, sid: str) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN '2024-05-01' AND '2026-08-31'
          AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE),
    ).fetchall()
    out = []
    for r in rows:
        o = float(r[1]) if r[1] else float(r[2])
        out.append((r[0], o, float(r[2])))
    return out


def load_ix_bars(conn) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date BETWEEN '2024-05-01' AND '2026-08-31' AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_CODE,),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


def main() -> int:
    trades = pd.read_csv(TRADES_CSV, dtype={"stock_id": str, "signal_date": str})
    print(f"[INFO] 載入真實事件 n={len(trades)}, 涵蓋 {trades['stock_id'].nunique()} 檔股票")

    conn = connect(DEFAULT_DB_PATH)
    ix_bars = load_ix_bars(conn)
    ix_dict = build_l1h7_signal_dict(ix_bars)
    print(f"[INFO] IX0001 bars={len(ix_bars)}, 可用訊號日={len(ix_dict)}")

    stock_dicts = {}
    for sid in sorted(trades["stock_id"].unique()):
        bars = load_stock_bars(conn, sid)
        d = build_l1h7_signal_dict(bars)
        stock_dicts[sid] = d
        print(f"[INFO] {sid}: bars={len(bars)}, 可用L1H7訊號日={len(d)}")
    conn.close()

    result = permutation_test(
        trades[["stock_id", "signal_date"]],
        stock_dicts,
        ix_dict,
        n_perm=N_PERM,
        seed=SEED,
    )
    print("\n" + "=" * 88)
    print(f"[RESULT] n_events={result['n_events']}, n_perm={result['n_perm']} "
          f"(有效={result['n_valid_perms']})")
    print(f"[RESULT] 觀察值 mean={result['observed_mean_pct']:.3f}%, "
          f"median={result['observed_median_pct']:.3f}%")
    print(f"[RESULT] Placebo null分布 mean of means={result['placebo_mean_of_means_pct']:.3f}% "
          f"(std={result['placebo_std_of_means_pct']:.3f}%), "
          f"mean of medians={result['placebo_mean_of_medians_pct']:.3f}% "
          f"(std={result['placebo_std_of_medians_pct']:.3f}%)")
    print(f"[RESULT] 單尾經驗p值：mean p={result['p_value_mean_onesided']:.4f}, "
          f"median p={result['p_value_median_onesided']:.4f}")
    print("=" * 88)

    trim = outlier_trim_sensitivity(trades["r_adj_pct"].to_numpy(), trim_ns=(3, 5))
    print(f"[INFO] 去極值敏感度（沿用trades.csv的r_adj_pct，與summary.json的t-test版本對照）: {trim}")

    payload = {
        "trader_id": "9217",
        "method": "permutation/placebo one-sided empirical p-value; per-event random alternative "
        "signal date drawn from the SAME stock's full L1H7-valid date pool, excluding real dates",
        "n_perm": N_PERM,
        "seed": SEED,
        "permutation_result": result,
        "outlier_trim_sensitivity_r_adj_pct": trim,
        "comparison_note": "對照 whale_9217_5dnet95_summary.json 的 t-test p=0.0426(全樣本)/"
        "0.239(去top3極值)——若permutation p值同樣在去極值前後有大幅落差，代表兩種方法一致認為"
        "顯著性不穩健；若permutation p值本身就遠高於0.05，代表連原始t-test的『顯著』都可能是"
        "小樣本+漸進假設不成立造成的高估。",
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[OK] 結果寫入 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
