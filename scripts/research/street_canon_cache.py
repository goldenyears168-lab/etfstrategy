"""chip-street-canon A/B 線共用快取（純 ETL，不做檢定）。

產出兩份 pandas pickle 到 reports/research/chip-street-canon/cache/：

1. top15_daily.pkl — 由 stock_broker_branch_daily（全市場 tape）聚合的每日每股：
   - top15_net / top5_net：買超前 N 分點淨額合計 − 賣超前 N 分點淨額合計（單位：張）
     ＝ SUM(net | rn_buy<=N) + SUM(net | rn_sell<=N)（後者為負值，故相加即相減）
   - n_buy_houses / n_sell_houses：net>0 / net<0 的分點家數
   - n_branches / gross_net（張）：輔助欄，供後續家數差正規化與集中度分母檢查
   注意：DB 內 buy/sell/net 原始單位是「股」（以 2330 對照 stock_daily_bars.volume
   驗證），輸出前一律 /1000 轉「張」。

2. price_panel.pkl — 去重後全市場價格面板：
   stock_id, trade_date, open, close, volume, amount, vwap=amount/volume, source。
   stock_daily_bars 同 stock-day 多來源多列（finmind / twse_mi_index / tpex_daily /
   yfinance），以 ROW_NUMBER 依來源優先序去重：
   twse_mi_index(上市官方) > tpex_daily(上櫃官方) > finmind > yfinance。
   ⚠️ 2026-08-27 實測：tpex_daily 只覆蓋窗內 26/544 天，其餘日子上櫃股靠 finmind；
   finmind 每日檔數 2,156 → 近月 ~665（backfill 殘影退潮，見 memory
   stock-daily-bars-coverage-break-20260714）。上櫃近月覆蓋缺口由本快取的
   per-day 分佈輸出如實回報，下游 join 時必須回報 join 前後列數。

窗：trade_date >= 2024-06-01（預註記窗 2024-07-01 起，前留一個月暖身）。

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/street_canon_cache.py
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd

from stock_db import DEFAULT_DB_PATH

CACHE_DIR = Path(__file__).resolve().parents[2] / "reports/research/chip-street-canon/cache"

# 參考 scripts/research/branch_features.py 的 rn_buy<=15 寫法，僅縮窗並補 top5 賣邊。
BRANCH_SQL = """
WITH b AS (
  SELECT trade_date, stock_id, net,
         ROW_NUMBER() OVER (PARTITION BY trade_date, stock_id ORDER BY net DESC) rn_buy,
         ROW_NUMBER() OVER (PARTITION BY trade_date, stock_id ORDER BY net ASC)  rn_sell,
         COUNT(*)    OVER (PARTITION BY trade_date, stock_id) n_br
    FROM stock_broker_branch_daily
   WHERE trade_date >= ? AND net IS NOT NULL AND net <> 0
)
SELECT trade_date, stock_id,
       MAX(n_br)                                      AS n_branches,
       SUM(CASE WHEN net>0 THEN 1 ELSE 0 END)         AS n_buy_houses,
       SUM(CASE WHEN net<0 THEN 1 ELSE 0 END)         AS n_sell_houses,
       SUM(CASE WHEN rn_buy<=15 OR rn_sell<=15 THEN net ELSE 0 END) AS top15_net_sh,
       SUM(CASE WHEN rn_buy<=5  OR rn_sell<=5  THEN net ELSE 0 END) AS top5_net_sh,
       SUM(ABS(net))                                  AS gross_net_sh
  FROM b GROUP BY trade_date, stock_id
"""

PRICE_SQL = """
WITH p AS (
  SELECT stock_id, trade_date, open, close, volume, amount, source,
         COUNT(*) OVER (PARTITION BY stock_id, trade_date) AS n_src,
         ROW_NUMBER() OVER (
           PARTITION BY stock_id, trade_date
           ORDER BY CASE source
                      WHEN 'twse_mi_index' THEN 0
                      WHEN 'tpex_daily'    THEN 1
                      WHEN 'finmind'       THEN 2
                      ELSE 3
                    END, synced_at DESC
         ) rk
    FROM stock_daily_bars
   WHERE trade_date >= ?
)
SELECT stock_id, trade_date, open, close, volume, amount, source, n_src
  FROM p WHERE rk = 1
"""


def connect_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)


def describe_daily_counts(df: pd.DataFrame, name: str) -> None:
    per_day = df.groupby("trade_date")["stock_id"].nunique()
    q = per_day.quantile([0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0])
    print(f"[{name}] {len(df):,} 列 · {df.stock_id.nunique():,} 檔 · "
          f"{per_day.size} 日 · {df.trade_date.min()} ~ {df.trade_date.max()}")
    print(f"[{name}] 每日檔數分佈 min/p5/p25/med/p75/p95/max = "
          + "/".join(f"{int(v)}" for v in q))


def build_top15(conn: sqlite3.Connection, start: str) -> pd.DataFrame:
    t0 = time.time()
    df = pd.read_sql_query(BRANCH_SQL, conn, params=(start,))
    print(f"[top15] 聚合完成（{time.time() - t0:.0f}s）", flush=True)
    # 股 → 張
    for col in ("top15_net_sh", "top5_net_sh", "gross_net_sh"):
        df[col.replace("_sh", "")] = df[col] / 1000.0
    df = df.drop(columns=["top15_net_sh", "top5_net_sh", "gross_net_sh"])
    return df.sort_values(["trade_date", "stock_id"]).reset_index(drop=True)


def build_price_panel(conn: sqlite3.Connection, start: str) -> pd.DataFrame:
    t0 = time.time()
    raw_rows = conn.execute(
        "SELECT COUNT(*) FROM stock_daily_bars WHERE trade_date >= ?", (start,)
    ).fetchone()[0]
    df = pd.read_sql_query(PRICE_SQL, conn, params=(start,))
    print(f"[price] 去重前 {raw_rows:,} 列 → 去重後 {len(df):,} 列"
          f"（含多來源 stock-day {int((df.n_src > 1).sum()):,} 個 · "
          f"{time.time() - t0:.0f}s）", flush=True)
    df["vwap"] = df["amount"] / df["volume"].where(df["volume"] > 0)
    return df.drop(columns=["n_src"]).sort_values(
        ["trade_date", "stock_id"]).reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2024-06-01")
    ap.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = ap.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro()

    top15 = build_top15(conn, args.start)
    describe_daily_counts(top15, "top15")
    top15_path = args.cache_dir / "top15_daily.pkl"
    top15.to_pickle(top15_path)
    print(f"→ {top15_path}")

    price = build_price_panel(conn, args.start)
    describe_daily_counts(price, "price")
    null_vwap = price["vwap"].isna()
    print(f"[price] vwap NULL 率 = {null_vwap.mean():.2%}（{int(null_vwap.sum()):,} 列；"
          f"amount NULL {int(price.amount.isna().sum()):,} · volume<=0 "
          f"{int((~(price.volume > 0)).sum()):,}）")
    print("[price] 來源占比：")
    print(price.source.value_counts().to_string())
    price_path = args.cache_dir / "price_panel.pkl"
    price.to_pickle(price_path)
    print(f"→ {price_path}")

    # 交叉覆蓋：分點 stock-day 有無對應價格列（下游 join 護欄的基準值）
    key_t = set(map(tuple, top15[["stock_id", "trade_date"]].itertuples(index=False)))
    key_p = set(map(tuple, price[["stock_id", "trade_date"]].itertuples(index=False)))
    inter = len(key_t & key_p)
    print(f"[cross] 分點 stock-day {len(key_t):,} 中有價格列者 {inter:,} "
          f"（{inter / len(key_t):.2%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
