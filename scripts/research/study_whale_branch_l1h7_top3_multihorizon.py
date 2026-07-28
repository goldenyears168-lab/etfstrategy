#!/usr/bin/env python3
"""H-BRANCH-TOP3-MULTIHORIZON · 放寬到 Top1~Top3 買方、多天期(H7/H14/H20) L1H7 篩選。

前一輪 study_whale_branch_l1h7_universe.py 只看「當日唯一 Top1 買方」+ 只測 H7，
篩出來的候選很少（只有 9217 存活）。這支腳本做兩個延伸：
  1. 放寬到 Top1/Top2/Top3（分點不必是當日唯一最大買方，只要排名前三且金額>=500萬）
  2. 每個事件同時算 H7/H14/H20 三個持有天期的 forward return

輸入：reports/research/branch-footprint-screen/master_top3_buyer_2024h2_2026.csv
      （trade_date, stock_id, securities_trader_id, buy_amt, rn, total_buy_amt）

協議同 L1H7 SSOT（build_expert_pool_trade_knowledge.py）：進場=訊號日次一交易日開盤，
出場=進場起算第H天收盤，成本30bps，r_adj=r-1.15×r_IX0001（IX0001同步用相同窗口對齊）。

用法（必須在 mini 上跑）：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_branch_l1h7_top3_multihorizon.py
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

COST, HORIZONS, DEDUP_DAYS, BETA = 0.003, (7, 14, 20), 5, 1.15
SOURCE = "finmind"
MIN_AMT = 5_000_000.0
MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
MARKET_MAKER_EXCLUSION_PATH = (
    ROOT / "reports/research/branch-footprint-screen/market_maker_branch_exclusion_v1.json"
)
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def load_market_maker_exclusion() -> set[str]:
    data = json.loads(MARKET_MAKER_EXCLUSION_PATH.read_text(encoding="utf-8"))
    return {s["trader_id"] for s in data["symbols"]}


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


def build_multihorizon_lookup(dates: np.ndarray, opens: np.ndarray, closes: np.ndarray) -> pd.DataFrame:
    """對一支股票（已依日期排序）的 bars，算每個可能訊號日對應的多天期 entry/exit。"""
    n = len(dates)
    max_h = max(HORIZONS)
    rows = []
    for sig_i in range(n - 1):
        entry_i = sig_i + 1
        if entry_i + max_h - 1 >= n:
            continue
        row = {
            "signal_date": dates[sig_i],
            "entry_date": dates[entry_i],
            "entry_open": opens[entry_i],
        }
        for h in HORIZONS:
            exit_i = entry_i + h - 1
            row[f"exit_date_h{h}"] = dates[exit_i]
            row[f"exit_close_h{h}"] = closes[exit_i]
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--master-csv",
        default="reports/research/branch-footprint-screen/master_top3_buyer_2024h2_2026.csv",
    )
    ap.add_argument("--min-n", type=int, default=10)
    ap.add_argument("--max-rank", type=int, default=3, help="只保留 rn<=此值的事件")
    args = ap.parse_args()

    master = pd.read_csv(args.master_csv, dtype={"stock_id": str, "securities_trader_id": str})
    mega = load_mega_blacklist()
    mm = load_market_maker_exclusion()
    master = master[~master["stock_id"].isin(mega)]
    master = master[~master["stock_id"].str.startswith("00")]
    master = master[master["buy_amt"] >= MIN_AMT]
    master = master[master["rn"] <= args.max_rank]
    master = master[~master["securities_trader_id"].isin(mm)]
    master["share_of_day_buy"] = master["buy_amt"] / master["total_buy_amt"]
    print(f"[INFO] master events (rn<=%d, mega/ETF/market-maker excluded): {len(master):,}" % args.max_rank)
    print(f"[INFO] distinct branches: {master['securities_trader_id'].nunique()}")
    print(f"[INFO] distinct stocks: {master['stock_id'].nunique()}")
    print(f"[INFO] rank distribution: {master['rn'].value_counts().to_dict()}")

    conn = connect(DEFAULT_DB_PATH)
    ix = load_ix(conn)
    ix_lookup = build_multihorizon_lookup(
        ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy()
    ).set_index("signal_date")

    stock_ids = sorted(master["stock_id"].unique().tolist())
    print(f"[INFO] loading stock bars for {len(stock_ids)} stocks...")
    bars = load_stock_bars(conn, stock_ids)
    conn.close()

    lookups: dict[str, pd.DataFrame] = {}
    for sid, g in bars.groupby("stock_id"):
        g = g.sort_values("trade_date")
        lk = build_multihorizon_lookup(
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
        rec = {
            "signal_date": row.trade_date,
            "stock_id": row.stock_id,
            "securities_trader_id": row.securities_trader_id,
            "buy_amt": row.buy_amt,
            "rn": row.rn,
            "share_of_day_buy": row.share_of_day_buy,
            "entry_date": s["entry_date"],
            "entry_open": s["entry_open"],
        }
        for h in HORIZONS:
            r_s = s[f"exit_close_h{h}"] / s["entry_open"] - 1 - COST
            r_ix = b[f"exit_close_h{h}"] / b["entry_open"] - 1
            rec[f"r_adj_h{h}_pct"] = (r_s - BETA * r_ix) * 100
        records.append(rec)
    events = pd.DataFrame(records)
    print(f"[INFO] events with computable multi-horizon forward return: {len(events):,}")

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

    events_dedup.to_csv(OUT_DIR / "whale_branch_top3_multihorizon_events.csv", index=False)

    rows = []
    for tid, g in events_dedup.groupby("securities_trader_id"):
        row: dict = {
            "securities_trader_id": tid,
            "n": len(g),
            "n_stocks": g["stock_id"].nunique(),
            "n_rank1": int((g["rn"] == 1).sum()),
            "n_rank2": int((g["rn"] == 2).sum()),
            "n_rank3": int((g["rn"] == 3).sum()),
            "mean_share_of_day_buy": round(float(g["share_of_day_buy"].mean()), 3),
        }
        for h in HORIZONS:
            ra = g[f"r_adj_h{h}_pct"].to_numpy()
            n = len(ra)
            row[f"median_h{h}_pct"] = float(np.median(ra))
            row[f"mean_h{h}_pct"] = float(np.mean(ra))
            row[f"win_rate_h{h}_pct"] = float((ra > 0).mean() * 100)
            if n >= args.min_n:
                t_stat, p_val = stats.ttest_1samp(ra, 0.0)
                row[f"p_value_h{h}"] = float(p_val)
            else:
                row[f"p_value_h{h}"] = None
        rows.append(row)
    branch_summary = pd.DataFrame(rows).sort_values("median_h7_pct", ascending=False)

    # BH-FDR per horizon
    for h in HORIZONS:
        col = f"p_value_h{h}"
        testable = branch_summary[branch_summary[col].notna()].copy().sort_values(col)
        m = len(testable)
        if m > 0:
            testable[f"bh_sig_h{h}"] = testable[col] <= (np.arange(1, m + 1) / m * 0.05)
            branch_summary = branch_summary.merge(
                testable[["securities_trader_id", f"bh_sig_h{h}"]],
                on="securities_trader_id",
                how="left",
            )
        else:
            branch_summary[f"bh_sig_h{h}"] = False

    branch_summary.to_csv(OUT_DIR / "whale_branch_top3_multihorizon_branch_summary.csv", index=False)
    for h in HORIZONS:
        n_sig = int(branch_summary[f"bh_sig_h{h}"].fillna(False).sum())
        print(f"[INFO] H{h} BH-FDR(5%) 顯著分點數: {n_sig}")

    print(f"\n[OK] 事件明細 → whale_branch_top3_multihorizon_events.csv")
    print(f"[OK] 分點彙總 → whale_branch_top3_multihorizon_branch_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
