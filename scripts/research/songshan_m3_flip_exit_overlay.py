#!/usr/bin/env python3
"""M3 addendum · flip 當「出場 kill switch」而非進場過濾（純研究 · DB 唯讀）.

flip = T+1 賣出股數 / T0 買進股數 必須等 T+1 盤後（~21:00）才知道，
已經來不及當 T+1 開盤進場的過濾條件；但可以當「T+2 開盤提早出場」的觸發。
本檔量化這個唯一 live-可行的用法。

用法：
    PYTHONPATH=src .venv/bin/python scripts/research/songshan_m3_flip_exit_overlay.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE, BENCH_CODE = "finmind", "IX0001"
COST, HOLD, BETA = 0.003, 7, 1.15
BFS = ROOT / "reports" / "research" / "branch-footprint-screen"
LABELED = BFS / "songshan_m3_trades_labeled.csv"
FLIP_CUTS = (0.20, 0.30, 0.40, 0.50)


def stat_block(v_pct) -> dict:
    v = pd.Series(v_pct).dropna().to_numpy() / 100.0
    n = len(v)
    if n == 0:
        return {"n": 0}
    d = {"n": n, "mean_pct": round(float(v.mean()) * 100, 2),
         "median_pct": round(float(np.median(v)) * 100, 2),
         "win_rate_pct": round(float((v > 0).mean()) * 100, 1)}
    if n >= 3 and v.std() > 0:
        t, p = stats.ttest_1samp(v, 0)
        d["t_stat"], d["t_p"] = round(float(t), 2), round(float(p), 4)
    if n < 15:
        d["small_sample_warning"] = "樣本不足（n<15）"
    return d


def main() -> None:
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    t = pd.read_csv(LABELED, dtype={"stock_id": str})

    bars: dict[str, list] = {}
    for sid in t["stock_id"].unique():
        bars[sid] = conn.execute(
            """SELECT trade_date, open, close FROM stock_daily_bars
               WHERE stock_id=? AND source=? AND close>0 AND trade_date BETWEEN ? AND ?
               ORDER BY trade_date""",
            (sid, SOURCE, "2024-05-01", "2026-12-31")).fetchall()
    ixr = conn.execute(
        """SELECT date, open, close FROM daily_bars WHERE code=? AND open>0 AND close>0
           AND date BETWEEN ? AND ?
           ORDER BY date, CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                                      WHEN 'finmind' THEN 2 ELSE 3 END""",
        (BENCH_CODE, "2024-05-01", "2026-12-31")).fetchall()
    ixd: dict[str, tuple[float, float]] = {}
    for d, o, c in ixr:
        ixd.setdefault(d, (float(o), float(c)))
    ix = sorted(ixd.items())

    def leg(series, entry_date, exit_i):
        """entry=entry_date open, exit=第 exit_i 個交易日（1=當天收盤? 見下）"""
        ds = [x[0] for x in series]
        if entry_date not in ds:
            return None
        i = ds.index(entry_date)
        j = i + exit_i - 1
        if j >= len(series):
            return None
        return float(series[i][1]), float(series[j][2])

    rows = []
    for r in t.itertuples(index=False):
        b = bars[r.stock_id]
        ds = [x[0] for x in b]
        if r.entry_date not in ds:
            continue
        i = ds.index(r.entry_date)
        ixds = [x[0] for x in ix]
        if r.entry_date not in ixds:
            continue
        k = ixds.index(r.entry_date)
        rec = {"signal_date": r.signal_date, "stock_id": r.stock_id, "flip": r.flip,
               "r_adj_h7": r.r_adj_pct}
        # 早出場：T+2 開盤（= entry 的下一個交易日開盤）
        if i + 1 < len(b) and k + 1 < len(ix):
            r_s = float(b[i + 1][1]) / float(b[i][1]) - 1 - COST
            r_ix = float(ix[k + 1][1][0]) / float(ix[k][1][0]) - 1
            rec["r_adj_earlyexit"] = round((r_s - BETA * r_ix) * 100, 3)
        rows.append(rec)
    d = pd.DataFrame(rows)

    print(f"n={len(d)}  可算早出場 n={d['r_adj_earlyexit'].notna().sum()}")
    out = {}
    for cut in FLIP_CUTS:
        hi = d[d["flip"] >= cut]
        lo = d[d["flip"] < cut]
        blk = {
            "n_high_flip": int(len(hi)),
            "pct_of_pop": round(100 * len(hi) / max(len(d), 1), 1),
            "high_flip_hold_h7": stat_block(hi["r_adj_h7"]),
            "high_flip_early_exit": stat_block(hi["r_adj_earlyexit"]),
            "low_flip_hold_h7": stat_block(lo["r_adj_h7"]),
        }
        overlay = pd.concat([lo["r_adj_h7"], hi["r_adj_earlyexit"]])
        blk["portfolio_with_overlay"] = stat_block(overlay)
        blk["portfolio_baseline"] = stat_block(d["r_adj_h7"])
        out[f"flip_ge_{cut}"] = blk
        print(f"\n--- flip >= {cut}  (n={len(hi)}, {blk['pct_of_pop']}% of pop)")
        print(f"  高flip 抱到 H7      : {blk['high_flip_hold_h7']}")
        print(f"  高flip T+2開盤出    : {blk['high_flip_early_exit']}")
        print(f"  疊加後全母體        : {blk['portfolio_with_overlay']}")
        print(f"  對照(全部抱H7)      : {blk['portfolio_baseline']}")

    print("\n--- 高 flip (>=0.40) 逐筆")
    print(d[d["flip"] >= 0.40][["signal_date", "stock_id", "flip", "r_adj_h7", "r_adj_earlyexit"]]
          .round(3).to_string(index=False))
    out["high_flip_040_events"] = json.loads(d[d["flip"] >= 0.40].to_json(orient="records"))
    (BFS / "songshan_m3_flip_exit_summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n[OUT] {BFS / 'songshan_m3_flip_exit_summary.json'}")


if __name__ == "__main__":
    main()
