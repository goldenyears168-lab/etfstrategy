#!/usr/bin/env python3
"""songshan_m2 · 附錄：混合執行載具（現股 tradable → 現股；被處置擋住 → 個股期貨）.

主研究（songshan_m2_tradability_study.py）發現：
  - 盤前過濾剔掉的是贏家（被擋 9 筆 mean +6.66% / 勝率 77.8%）
  - 但被擋的 9 筆裡有 8 筆有流動的個股期貨

所以真正該問的不是「過濾後還剩多少」，而是「換載具能不能把被擋的贏家吃回來」。
本腳本量化三種執行政策，並檢查漲停鎖死時期貨是否同樣鎖死（期貨也有 ±10% 漲跌幅）。

輸入／輸出皆在 reports/research/branch-footprint-screen/songshan_m2/。
用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m2_hybrid_execution.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen" / "songshan_m2"


def full_stats(v: pd.Series, label: str) -> dict:
    vals = pd.Series(v).dropna().to_numpy() / 100.0
    n = len(vals)
    out = {"label": label, "n": n}
    if n == 0:
        return out
    out.update(
        {
            "mean_pct": round(float(np.mean(vals)) * 100, 3),
            "median_pct": round(float(np.median(vals)) * 100, 3),
            "win_rate_pct": round(float((vals > 0).mean()) * 100, 1),
        }
    )
    if n >= 2 and np.std(vals) > 0:
        t, p = stats.ttest_1samp(vals, 0)
        out["t_stat"] = round(float(t), 3)
        out["t_p"] = round(float(p), 4)
    if n < 15:
        out["caveat"] = "樣本不足(n<15)"
    return out


def main() -> int:
    df = pd.read_csv(OUT_DIR / "tradability_events.csv", dtype={"stock_id": str})
    fut = json.loads((OUT_DIR / "futures_daily_cache.json").read_text())

    blocked_disp = df["disp_t1_prefund"] == "prefund_blanket"
    locked = df["locked_limit_up_all_day"]
    liq_fut = df["fut_liquid_ge800"] & df["fut_r_adj_pct"].notna()

    # --- 漲停鎖死日，期貨是否也鎖死？ ---
    lock_check = []
    for r in df[locked].itertuples():
        d = r.entry_date
        f = fut.get(r.stock_id, {}).get(d)
        if not f:
            lock_check.append({"stock_id": r.stock_id, "entry_date": d, "futures": "無資料"})
            continue
        rng = (f["h"] - f["l"]) / f["l"] * 100 if f["l"] > 0 else None
        lock_check.append(
            {
                "stock_id": r.stock_id,
                "entry_date": d,
                "fut_open": f["o"], "fut_high": f["h"], "fut_low": f["l"], "fut_close": f["c"],
                "fut_intraday_range_pct": round(rng, 2) if rng is not None else None,
                "fut_locked_flat": bool(f["h"] == f["l"] == f["o"] == f["c"]),
                "fut_vol_near": f["v_near"],
                "stock_r_adj_pct": r.r_adj_pct,
                "fut_r_adj_pct": r.fut_r_adj_pct,
            }
        )

    # --- 三種執行政策 ---
    policies: dict[str, pd.Series] = {}

    # P0 紙上（現況研究母體，假裝全都吃得到）
    policies["P0_paper_all_stock"] = df["r_adj_pct"]

    # P1 現股 + 盤前硬過濾（吃不到就放棄）
    p1 = df["r_adj_pct"].where(~(blocked_disp | locked))
    policies["P1_stock_with_pretrade_gate"] = p1

    # P2 全部改用個股期貨（沒期貨/不流動就放棄）
    policies["P2_futures_only"] = df["fut_r_adj_pct"].where(liq_fut)

    # P3 混合：可買就買現股；被處置/漲停擋住且有流動期貨 → 走期貨
    fallback = (blocked_disp | locked) & liq_fut
    p3 = df["r_adj_pct"].where(~(blocked_disp | locked))
    p3 = p3.where(~fallback, df["fut_r_adj_pct"])
    policies["P3_hybrid_stock_then_futures"] = p3

    # P3b 混合但漲停鎖死日不搶（期貨可能同鎖）
    fallback_b = blocked_disp & liq_fut
    p3b = df["r_adj_pct"].where(~(blocked_disp | locked))
    p3b = p3b.where(~fallback_b, df["fut_r_adj_pct"])
    policies["P3b_hybrid_disp_only_skip_limitup"] = p3b

    rep = {
        "n_events": int(len(df)),
        "policy_stats": {k: full_stats(v, k) for k, v in policies.items()},
        "policy_coverage": {
            k: {"n_executable": int(v.notna().sum()),
                "pct_of_signals": round(v.notna().mean() * 100, 1)}
            for k, v in policies.items()
        },
        "limit_up_lock_futures_check": lock_check,
        "hybrid_fallback_legs": df[fallback][
            ["stock_id", "signal_date", "entry_date", "disp_t1_prefund",
             "locked_limit_up_all_day", "r_adj_pct", "fut_r_adj_pct", "fut_adv20_lots_front2"]
        ].to_dict("records"),
        "note": "P0 是不可實現的紙上母體；P1/P2/P3 才是可執行的。放棄的訊號記為 NaN（不計入分母報酬，但 coverage 有記）。",
    }
    (OUT_DIR / "hybrid_execution_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
