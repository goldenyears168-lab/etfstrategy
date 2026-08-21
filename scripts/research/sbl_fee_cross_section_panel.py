#!/usr/bin/env python3
"""借券／資券研究的共用面板建構器（2011-2026 全市場）。

**為什麼要這一支**：`chip_daily_horizon_null_test.py` 原本只讀 `source='finmind'`
的價格，而該來源在 2024-05 以前每日僅約 100~160 檔、且是存活者偏誤宇宙
（2015 年的 156 檔有 156 檔活到 2026、零下市）。2026-08-20 回補完
`twse_mi_index`（上市 2,671 交易日）與 `tpex_daily`（上櫃 3,291 交易日）之後，
橫斷面可以拉到 2011 起、每日約 1,350~2,170 檔。

**價格來源優先序**：`finmind`（有 adj_close）→ `twse_mi_index` → `tpex_daily`。
同一 stock-day 只取一列。

**除權息還原**：三個來源的 close 都是原始價，直接算日報酬會在除息日低估掉整個
股利（台股殖利率 3~4% 且集中 7~9 月）。用 `stock_ex_adjust_event` 還原——
除權息日當天的總報酬 = ``close(ex) / ref_price - 1``。兩個來源的錨點不同：

* ``anchor_kind='ex'``（TWSE TWT49U）→ anchor_date 就是除權息日
* ``anchor_kind='cum'``（TPEX 日行情「次日參考價」）→ anchor_date 是除權息**前一**
  交易日，需往後推一個交易日

**市值**：上市用 TWT93U 融券限額×4（實測 2330 比值 0.2498~0.2500），上櫃用
櫃買日行情的發行股數欄。停止融券的處置股融券限額為 0，會得 inf，一律排除。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

PRICE_SOURCE_RANK = {"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}
DEFAULT_START = "2011-01-01"


def load_prices(start: str = DEFAULT_START, ordinary_only: bool = True) -> pd.DataFrame:
    """合併三來源、去重、套除權息還原，回傳含 ``ret`` 的日線面板。"""
    conn = connect_ro()
    px = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, open, close, volume, amount,
               shares_outstanding, source
          FROM stock_daily_bars
         WHERE source IN ('finmind','twse_mi_index','tpex_daily')
           AND trade_date >= ? AND close > 0
        """,
        conn, params=(start,),
    )
    if ordinary_only:                       # 4 碼純數字＝普通股，排除 ETF／權證／債券
        px = px[px.stock_id.str.fullmatch(r"\d{4}")]
    px["_rank"] = px.source.map(PRICE_SOURCE_RANK)
    px = (px.sort_values(["stock_id", "trade_date", "_rank"])
            .drop_duplicates(["stock_id", "trade_date"], keep="first")
            .drop(columns=["_rank"]))

    ev = pd.read_sql_query(
        """SELECT stock_id, anchor_date, anchor_kind, ref_price, factor
             FROM stock_ex_adjust_event WHERE anchor_date >= ?""",
        conn, params=(start,),
    )
    px = _apply_ex_dividend(px, ev)
    return px


def _apply_ex_dividend(px: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    """把還原參考價對齊到「除權息日」，再據以計算含息日報酬。"""
    px = px.sort_values(["stock_id", "trade_date"]).copy()
    px["prev_close"] = px.groupby("stock_id", group_keys=False).close.shift(1)

    # TWSE：anchor_date 已是除權息日
    twse = ev[ev.anchor_kind == "ex"][["stock_id", "anchor_date", "ref_price"]]
    twse = twse.rename(columns={"anchor_date": "trade_date"})

    # TPEX：anchor_date 是除權息前一交易日 → 往後推一個「該股的」交易日
    cum = ev[ev.anchor_kind == "cum"][["stock_id", "anchor_date", "ref_price"]]
    if not cum.empty:
        cal = px[["stock_id", "trade_date"]].copy()
        cal["next_date"] = cal.groupby("stock_id", group_keys=False).trade_date.shift(-1)
        cum = cum.merge(cal, left_on=["stock_id", "anchor_date"],
                        right_on=["stock_id", "trade_date"], how="inner")
        cum = cum[["stock_id", "next_date", "ref_price"]].rename(
            columns={"next_date": "trade_date"})
    adj = pd.concat([twse, cum], ignore_index=True).dropna(subset=["trade_date"])
    adj = adj.drop_duplicates(["stock_id", "trade_date"], keep="first")

    px = px.merge(adj, on=["stock_id", "trade_date"], how="left")
    # 除權息日用參考價當分母；其餘日用前一日收盤
    base = px.ref_price.where(px.ref_price.notna() & (px.ref_price > 0), px.prev_close)
    px["ret"] = px.close / base - 1
    px["is_ex_day"] = px.ref_price.notna()
    return px.drop(columns=["ref_price"])


def load_chips(start: str = DEFAULT_START) -> pd.DataFrame:
    """借券賣出餘額（TWT93U）＋ 借券費率（t13sa710）＋ 融資融券（去重）。"""
    conn = connect_ro()
    si = pd.read_sql_query(
        """SELECT stock_id, trade_date, sbl_balance, sbl_sell, sbl_return,
                  sbl_next_limit, short_balance, short_limit
             FROM stock_short_interest_daily WHERE trade_date >= ?""",
        conn, params=(start,),
    )
    fee = pd.read_sql_query(
        """SELECT stock_id, trade_date, fee_rate_vw, volume AS fee_volume
             FROM stock_sbl_fee_daily
            WHERE deal_type='ALL' AND trade_date >= ?""",
        conn, params=(start,),
    )
    # ⚠️ 2026-06 起 finmind 與 twse_mi_margn 對同一 stock-day 各有一列，必須去重
    mg = pd.read_sql_query(
        """SELECT stock_id, trade_date, margin_balance, mg_short FROM (
               SELECT stock_id, trade_date, margin_balance,
                      short_balance AS mg_short,
                      ROW_NUMBER() OVER (
                          PARTITION BY stock_id, trade_date
                          ORDER BY CASE source WHEN 'twse_mi_margn' THEN 0 ELSE 1 END
                      ) AS rn
                 FROM stock_margin_daily WHERE trade_date >= ?
           ) WHERE rn = 1""",
        conn, params=(start,),
    )
    return (si.merge(fee, on=["stock_id", "trade_date"], how="left")
              .merge(mg, on=["stock_id", "trade_date"], how="left"))


def build(start: str = DEFAULT_START, min_close: float = 10.0,
          min_vol20: float = 300_000, cache: Path | None = None) -> pd.DataFrame:
    """建面板。5M 列的 groupby-rolling 很重（單次約 15 分鐘），故支援 pickle 快取。"""
    if cache is not None and cache.exists():
        return pd.read_pickle(cache)
    px, ch = load_prices(start), load_chips(start)
    # 先 inner join 縮到「有借券資料」的子集再做 rolling，否則要對 5M 列做
    d = px.merge(ch, on=["stock_id", "trade_date"], how="inner")
    dup = d.duplicated(subset=["stock_id", "trade_date"]).sum()
    if dup:
        raise RuntimeError(f"panel 有 {dup:,} 筆重複 stock-day；先去重再用")
    d = d.sort_values(["stock_id", "trade_date"])
    g = d.groupby("stock_id", group_keys=False)

    d["vol20"] = g.volume.transform(lambda s: s.rolling(20, min_periods=10).mean())
    # 市值：上市＝融券限額×4；上櫃＝發行股數欄。停止融券者限額為 0 → 排除
    shares = (d.short_limit * 4).replace(0, np.nan)
    d["shares"] = shares.fillna(d.shares_outstanding)
    d["mcap"] = d.close * d.shares
    d["sbl_pct"] = d.sbl_balance / d.shares
    d["util"] = d.sbl_balance / (d.sbl_balance + d.sbl_next_limit)
    d["dtc"] = d.sbl_balance / d.vol20
    d["fwd_ex"] = g.ret.shift(-1)            # 隔日報酬（含息還原）
    d = d[(d.close >= min_close) & (d.vol20 > min_vol20) & d.shares.notna()]
    d = d[np.isfinite(d.sbl_pct)].copy()
    d["fwd_ex"] = d.fwd_ex - d.groupby("trade_date").fwd_ex.transform("mean")
    if cache is not None:
        d.to_pickle(cache)
    return d


def add_signals(d: pd.DataFrame) -> pd.DataFrame:
    """補上 5 個三態訊號（+1 空 / 0 中性 / −1 多），與
    ``chip_daily_horizon_null_test.build_score`` 的定義一致。

    ⚠️ S4（融券餘額變化）經 2026-08-20 檢定確認是**反指標**
    （空−多 = +0.1085%/日、t=+3.99），保留在此僅為與舊結果可比；
    實際使用時應剔除，見 config/research.yaml 的 do_not。
    """
    d = d.sort_values(["stock_id", "trade_date"]).copy()
    g = d.groupby("stock_id", group_keys=False)
    d["d_sbl"] = g.sbl_balance.diff()
    d["d_util"] = g.util.diff()
    d["d_short"] = g.short_balance.diff()
    d["fee_med20"] = g.fee_rate_vw.transform(
        lambda s: s.rolling(20, min_periods=5).median())
    d["pct_rank"] = g.sbl_pct.transform(
        lambda s: s.rolling(243, min_periods=60).rank(pct=True))

    def sgn(bear, bull):
        return np.where(bear, 1, np.where(bull, -1, 0))

    d["S1"] = sgn(d.d_sbl > 0, d.d_sbl < 0)
    d["S2"] = sgn(d.pct_rank >= 0.8, d.pct_rank <= 0.2)
    d["S3"] = sgn(d.d_util > 0, d.d_util < 0)
    d["S4"] = sgn(d.d_short > 0, d.d_short < 0)
    d["S5"] = sgn(d.fee_rate_vw > d.fee_med20,
                  d.fee_rate_vw.isna() | (d.fee_rate_vw < d.fee_med20))
    return d


if __name__ == "__main__":
    import sys
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    d = build(cache=cache)
    print(f"面板：{len(d):,} 個 stock-day · {d.stock_id.nunique():,} 檔 · "
          f"{d.trade_date.nunique():,} 個交易日 · {d.trade_date.min()}~{d.trade_date.max()}")
    yr = d.assign(yr=d.trade_date.str[:4]).groupby("yr").agg(
        檔數=("stock_id", "nunique"), 日均檔數=("stock_id", lambda s: len(s)),
        除息日筆數=("is_ex_day", "sum"))
    yr["日均檔數"] = (yr.日均檔數 / d.assign(yr=d.trade_date.str[:4])
                      .groupby("yr").trade_date.nunique()).round(0)
    print(yr.to_string())
