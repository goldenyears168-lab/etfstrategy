#!/usr/bin/env python3
"""H-BRANCH-PAIR-CONSENSUS step2 · 幫 step1 產出的 joint/solo 事件表算 forward return。

必須在 mini 上跑（要查 stock_daily_bars）。輸入：
  reports/research/branch-footprint-screen/whale_branch_round3_pairconsensus_joint_events.csv
  reports/research/branch-footprint-screen/whale_branch_round3_pairconsensus_solo_events.csv
輸出：
  reports/research/branch-footprint-screen/whale_branch_round3_pairconsensus_joint_events_fwdret.csv
  reports/research/branch-footprint-screen/whale_branch_round3_pairconsensus_solo_events_fwdret.csv

協議同 L1H7 SSOT：進場=訊號日次一交易日開盤，出場=進場起算第H天收盤，成本30bps，
r_adj = r_股 - 1.15*r_IX0001（IX0001 用 daily_bars 表、同步用相同窗口對齊）。

用法（mini）：
  PYTHONPATH=src .venv/bin/python scripts/research/scan_whale_branch_pairs_step2_forward_returns.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

COST, HORIZONS, BETA = 0.003, (7, 14, 20), 1.15
SOURCE = "finmind"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"


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


def annotate(df: pd.DataFrame, lookups: dict, ix_lookup: pd.DataFrame) -> pd.DataFrame:
    records = []
    for row in df.itertuples(index=False):
        sid_lookup = lookups.get(row.stock_id)
        if sid_lookup is None or row.signal_date not in sid_lookup.index:
            continue
        s = sid_lookup.loc[row.signal_date]
        if row.signal_date not in ix_lookup.index:
            continue
        b = ix_lookup.loc[row.signal_date]
        rec = row._asdict()
        rec["entry_date"] = s["entry_date"]
        rec["entry_open"] = s["entry_open"]
        for h in HORIZONS:
            r_s = s[f"exit_close_h{h}"] / s["entry_open"] - 1 - COST
            r_ix = b[f"exit_close_h{h}"] / b["entry_open"] - 1
            rec[f"r_adj_h{h}_pct"] = (r_s - BETA * r_ix) * 100
        records.append(rec)
    return pd.DataFrame(records)


def main() -> int:
    joint = pd.read_csv(OUT_DIR / "whale_branch_round3_pairconsensus_joint_events.csv", dtype={"stock_id": str})
    solo = pd.read_csv(OUT_DIR / "whale_branch_round3_pairconsensus_solo_events.csv", dtype={"stock_id": str})
    stock_ids = sorted(set(joint["stock_id"]).union(set(solo["stock_id"])))
    print(f"[INFO] joint events: {len(joint):,}  solo events: {len(solo):,}  distinct stocks: {len(stock_ids)}")

    conn = connect(DEFAULT_DB_PATH)
    ix = load_ix(conn)
    ix_lookup = build_multihorizon_lookup(
        ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy()
    ).set_index("signal_date")

    print("[INFO] loading stock bars...")
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

    joint_out = annotate(joint, lookups, ix_lookup)
    solo_out = annotate(solo, lookups, ix_lookup)
    print(f"[INFO] joint events w/ forward return: {len(joint_out):,} / {len(joint):,}")
    print(f"[INFO] solo events w/ forward return: {len(solo_out):,} / {len(solo):,}")

    joint_out.to_csv(OUT_DIR / "whale_branch_round3_pairconsensus_joint_events_fwdret.csv", index=False)
    solo_out.to_csv(OUT_DIR / "whale_branch_round3_pairconsensus_solo_events_fwdret.csv", index=False)
    print("[OK] wrote whale_branch_round3_pairconsensus_joint_events_fwdret.csv / whale_branch_round3_pairconsensus_solo_events_fwdret.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
