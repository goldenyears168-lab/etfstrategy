#!/usr/bin/env python3
"""H-BRANCH-UNIVERSE-L1H7 · 全分點宇宙 L1H7 篩選（協議 SSOT：build_expert_pool_trade_knowledge.py）。

輸入：/tmp/master_top1_buyer_2024h2_2026.parquet（(trade_date, stock_id, securities_trader_id, buy_amt)，
Top1 買方、金額>=500萬，2024-07-01 起，含全分點宇宙 831~1010 家）。

對每一筆事件用 L1H7 協議算 forward return：
  進場 = 訊號日次一交易日開盤；出場 = 進場起算第 7 個交易日收盤；成本 30bps；
  r_adj = r_股 - 1.15 * r_IX0001（IX0001 同樣用 L1H7 對齊日期，非同曆日相減）。

依分點彙總，套多重比較校正（Benjamini-Hochberg FDR，因為同時篩選 800+ 分點）。

用法（在有完整資料的機器上跑，例如 mini）：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_branch_l1h7_universe.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect

COST, HOLD, DEDUP_DAYS, BETA = 0.003, 7, 5, 1.15
SOURCE = "finmind"
MIN_AMT = 5_000_000.0
MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def load_ix(conn) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code='IX0001' AND date >= '2024-05-01' AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    dates = sorted(out)
    return pd.DataFrame(
        {"date": dates, "open": [out[d][0] for d in dates], "close": [out[d][1] for d in dates]}
    )


def load_stock_bars(conn, stock_ids: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source='{SOURCE}' AND stock_id IN ({placeholders})
          AND trade_date >= '2024-05-01' AND close > 0
        ORDER BY stock_id, trade_date
    """
    df = pd.read_sql_query(q, conn, params=stock_ids)
    df["open"] = df["open"].where(df["open"] > 0, df["close"])
    return df


def build_l1h7_lookup(dates: np.ndarray, opens: np.ndarray, closes: np.ndarray) -> pd.DataFrame:
    """對一支股票（已依日期排序）的 bars，算每個可能訊號日對應的 L1H7 entry/exit。"""
    n = len(dates)
    entry_idx = np.arange(1, n)  # candidate entry = index of the bar itself (as next_open)
    rows = []
    for sig_i in range(n - 1):
        entry_i = sig_i + 1  # next trading day
        exit_i = entry_i + HOLD - 1
        if exit_i >= n:
            continue
        rows.append(
            (
                dates[sig_i],
                dates[entry_i],
                opens[entry_i],
                dates[exit_i],
                closes[exit_i],
            )
        )
    return pd.DataFrame(rows, columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master-csv", default="/tmp/master_top1_buyer_2024h2_2026.csv")
    ap.add_argument("--min-n", type=int, default=10, help="分點至少要有幾筆事件才進入統計檢定")
    args = ap.parse_args()

    master = pd.read_csv(args.master_csv, dtype={"stock_id": str, "securities_trader_id": str})
    master["stock_id"] = master["stock_id"].astype(str)
    mega = load_mega_blacklist()
    master = master[~master["stock_id"].isin(mega)]
    master = master[~master["stock_id"].str.startswith("00")]
    master = master[master["buy_amt"] >= MIN_AMT]
    print(f"[INFO] master events after mega/ETF exclude: {len(master):,}")
    print(f"[INFO] distinct branches: {master['securities_trader_id'].nunique()}")
    print(f"[INFO] distinct stocks: {master['stock_id'].nunique()}")

    conn = connect(DEFAULT_DB_PATH)
    ix = load_ix(conn)
    ix_lookup = build_l1h7_lookup(
        ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy()
    ).set_index("signal_date")

    stock_ids = sorted(master["stock_id"].unique().tolist())
    print(f"[INFO] loading stock bars for {len(stock_ids)} stocks...")
    bars = load_stock_bars(conn, stock_ids)
    conn.close()

    lookups: dict[str, pd.DataFrame] = {}
    for sid, g in bars.groupby("stock_id"):
        g = g.sort_values("trade_date")
        lk = build_l1h7_lookup(
            g["trade_date"].to_numpy(), g["open"].to_numpy(), g["close"].to_numpy()
        )
        if not lk.empty:
            lookups[sid] = lk.set_index("signal_date")

    records = []
    for row in master.itertuples(index=False):
        sid_lookup = lookups.get(row.stock_id)
        if sid_lookup is None or row.trade_date not in sid_lookup.index:
            continue
        s = sid_lookup.loc[row.trade_date]
        if row.trade_date not in ix_lookup.index:
            continue
        b = ix_lookup.loc[row.trade_date]
        r_s = s["exit_close"] / s["entry_open"] - 1 - COST
        r_ix = b["exit_close"] / b["entry_open"] - 1
        r_adj = r_s - BETA * r_ix
        records.append(
            {
                "signal_date": row.trade_date,
                "stock_id": row.stock_id,
                "securities_trader_id": row.securities_trader_id,
                "buy_amt": row.buy_amt,
                "entry_date": s["entry_date"],
                "entry_open": s["entry_open"],
                "exit_date": s["exit_date"],
                "exit_close": s["exit_close"],
                "r_pct": r_s * 100,
                "r_ix_pct": r_ix * 100,
                "r_adj_pct": r_adj * 100,
            }
        )
    events = pd.DataFrame(records)
    print(f"[INFO] events with computable L1H7 forward return: {len(events):,}")

    # 同一 (stock_id, securities_trader_id) 訊號 DEDUP_DAYS 天內只留第一筆（比照既有協議去重）
    events = events.sort_values(["stock_id", "securities_trader_id", "signal_date"])
    keep_rows = []
    for _, g in events.groupby(["stock_id", "securities_trader_id"]):
        last_kept = None
        for _, r in g.iterrows():
            if last_kept is None or (
                pd.Timestamp(r["signal_date"]) - pd.Timestamp(last_kept)
            ).days > DEDUP_DAYS:
                keep_rows.append(r)
                last_kept = r["signal_date"]
    events_dedup = pd.DataFrame(keep_rows)
    print(f"[INFO] events after {DEDUP_DAYS}-day dedup: {len(events_dedup):,}")

    events_dedup.to_csv(OUT_DIR / "whale_branch_l1h7_universe_events.csv", index=False)

    # 分點層級彙總 + 統計檢定
    rows = []
    for tid, g in events_dedup.groupby("securities_trader_id"):
        ra = g["r_adj_pct"].to_numpy()
        n = len(ra)
        row = {
            "securities_trader_id": tid,
            "n": n,
            "n_stocks": g["stock_id"].nunique(),
            "median_r_adj_pct": float(np.median(ra)),
            "mean_r_adj_pct": float(np.mean(ra)),
            "win_rate_pct": float((ra > 0).mean() * 100),
        }
        if n >= args.min_n:
            t_stat, p_val = stats.ttest_1samp(ra, 0.0)
            row["t_stat"] = float(t_stat)
            row["p_value"] = float(p_val)
        else:
            row["t_stat"] = None
            row["p_value"] = None
        rows.append(row)
    branch_summary = pd.DataFrame(rows).sort_values("median_r_adj_pct", ascending=False)

    # Benjamini-Hochberg FDR correction on branches with a valid p_value
    testable = branch_summary[branch_summary["p_value"].notna()].copy()
    testable = testable.sort_values("p_value")
    m = len(testable)
    if m > 0:
        testable["bh_rank"] = np.arange(1, m + 1)
        testable["bh_critical"] = testable["bh_rank"] / m * 0.05
        testable["bh_significant"] = testable["p_value"] <= testable["bh_critical"]
        branch_summary = branch_summary.merge(
            testable[["securities_trader_id", "bh_rank", "bh_critical", "bh_significant"]],
            on="securities_trader_id",
            how="left",
        )
    else:
        branch_summary["bh_significant"] = False

    branch_summary.to_csv(OUT_DIR / "whale_branch_l1h7_universe_branch_summary.csv", index=False)
    n_sig = int(branch_summary["bh_significant"].fillna(False).sum())
    print(f"\n[INFO] 分點數（n>={args.min_n} 可做檢定）: {m}")
    print(f"[INFO] BH-FDR(5%) 顯著分點數: {n_sig}")
    print(branch_summary[branch_summary["bh_significant"].fillna(False)].to_string())

    print(f"\n[OK] 事件明細 → whale_branch_l1h7_universe_events.csv")
    print(f"[OK] 分點彙總 → whale_branch_l1h7_universe_branch_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
