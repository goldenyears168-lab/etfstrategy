#!/usr/bin/env python3
"""券商分點日資料 → 每日個股特徵（``stock_broker_branch_daily`` 2.24 億列彙總）。

對應 topic ``chip-signal-daily-horizon`` 的 ``H-CHIP-BRANCH-INCREMENT``。

**唯一有效的特徵是「買賣家數差」，而且方向是反的**：
``(買超家數 − 賣超家數) ÷ 有進出的分點家數`` 越大 → 隔日超額報酬越低（t=−7.11）。
經濟意義是**買方結構**——家數多＝散戶分散進場；家數少但金額大＝主力集中進場。

**所有「主力買超」類特徵全部不顯著**（前 5 大買超÷量 t=0.24、前 15 大 t=0.42、
主力淨額 t=1.64、分點集中度 t=−0.06），與本 repo 既有的「外資分點跟單 10 支
全滅」「松山 copytrade G2 rejected」一致。差別在於：那些測的是「誰買了多少」，
有資訊的是「買方是分散還是集中」。

⚠️ 資料自 2021-06-01 起（1,272 日 · 2,446 檔），是本研究線樣本最短的一塊。

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/branch_features.py --out /tmp/branch_feat.pkl
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from stock_db import connect_ro

SQL = """
WITH b AS (
  SELECT trade_date, stock_id, net,
         ROW_NUMBER() OVER (PARTITION BY trade_date, stock_id ORDER BY net DESC) rn_buy,
         ROW_NUMBER() OVER (PARTITION BY trade_date, stock_id ORDER BY net ASC)  rn_sell,
         COUNT(*)    OVER (PARTITION BY trade_date, stock_id) n_br
    FROM stock_broker_branch_daily
   WHERE trade_date >= ? AND net IS NOT NULL AND net <> 0
)
SELECT trade_date, stock_id,
       MAX(n_br)                                     AS n_branches,
       SUM(CASE WHEN net>0 THEN 1 ELSE 0 END)        AS n_buy_br,
       SUM(CASE WHEN net<0 THEN 1 ELSE 0 END)        AS n_sell_br,
       SUM(CASE WHEN rn_buy<=5  THEN net ELSE 0 END) AS top5_net,
       SUM(CASE WHEN rn_sell<=5 THEN net ELSE 0 END) AS bot5_net,
       SUM(CASE WHEN rn_buy<=15 THEN net ELSE 0 END) AS top15_net,
       SUM(ABS(net))                                 AS gross_net
  FROM b GROUP BY trade_date, stock_id
"""


def build(start: str = "2021-06-01") -> pd.DataFrame:
    """彙總耗時約 60 分鐘（2.24 億列的 window function），建議背景執行。"""
    t0 = time.time()
    df = pd.read_sql_query(SQL, connect_ro(), params=(start,))
    print(f"彙總完成 {len(df):,} 個 stock-day（{time.time() - t0:.0f}s）", flush=True)
    return df


def add_score(m: pd.DataFrame) -> pd.DataFrame:
    """把買賣家數差轉成與籌碼分數同向的三態 S6（正 = 偏空）。

    每日橫斷面五分位：最高分位（買超家數最多 → 散戶分散進場）判 +1 偏空，
    最低分位判 −1 偏多，中間三分位為 0。
    """
    m = m.copy()
    m["brdiff"] = (m.n_buy_br - m.n_sell_br) / m.n_branches
    m["S6"] = m.groupby("trade_date").brdiff.transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
    ).map({0: -1, 1: 0, 2: 0, 3: 0, 4: 1}).fillna(0)
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2021-06-01")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    df = build(args.start)
    df.to_pickle(args.out)
    print(f"→ {args.out} · {df.stock_id.nunique():,} 檔 · {df.trade_date.nunique():,} 日 "
          f"· {df.trade_date.min()}~{df.trade_date.max()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
