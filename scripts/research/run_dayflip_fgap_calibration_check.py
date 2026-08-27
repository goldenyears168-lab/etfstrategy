#!/usr/bin/env python3
"""dayflip-futures-short FGAP_MIN=6% 校準檢查：對照 gap_open_pattern_proxy 研究的
缺口回補（fade）base rate 曲線，看 6% 這條線切在哪裡合理不合理。

沿用 `scripts/research/run_gap_open_pattern_proxy_study.py` 的 A1 定義（完全相同
計算方式，只是換更細的分桶 + 限縮到 dayflip-short 的 251 檔期貨可空股票宇宙）：
  - gap_pct = (open - prev_close) / prev_close * 100（stock_daily_bars, source='finmind'）
  - 開高 fade（=A1 的「回補」）：當日 low <= prev_close
  - 清理規則同 A1：zero price / 無前收 / 前一有效交易日間隔>10天 / |gap|>20% 全部剔除

只做開高（gap-up）方向，因為 dayflip-futures-short 只在開高時放空。分桶對齊
FGAP_MIN=0.06 附近做細切：3-4/4-5/5-6/6-7/7-8/8-10/10%+（另外保留 <3% 當對照）。

Research only，純讀取，不寫入/不修改任何現有 DB 表，不碰 order 層程式碼。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

UNIVERSE_PATH = (
    ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
)
OUT_DIR = ROOT / "reports/research/dayflip_fgap_calibration"

MAX_PLAUSIBLE_GAP_ABS = 20.0
MAX_PREV_DATE_GAP_DAYS = 10

BUCKET_EDGES = [0.0, 1.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, np.inf]
BUCKET_LABELS = ["<1%", "1-3%", "3-4%", "4-5%", "5-6%", "6-7%", "7-8%", "8-10%", "10%+"]


def load_universe_stock_ids() -> list[str]:
    d = json.loads(UNIVERSE_PATH.read_text())
    return sorted(d["map"].keys())


def load_daily_bars(conn, stock_ids: list[str]) -> pd.DataFrame:
    placeholders = ",".join(["?"] * len(stock_ids))
    sql = (
        "SELECT stock_id, trade_date, open, high, low, close FROM stock_daily_bars "
        f"WHERE source = 'finmind' AND stock_id IN ({placeholders}) ORDER BY stock_id, trade_date"
    )
    df = pd.read_sql_query(sql, conn, params=stock_ids)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def add_gap_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = df.sort_values(["stock_id", "trade_date"]).copy()
    grp = df.groupby("stock_id")
    df["prev_close"] = grp["close"].shift(1)
    df["prev_trade_date"] = grp["trade_date"].shift(1)
    df["day_gap"] = (df["trade_date"] - df["prev_trade_date"]).dt.days

    n_total = len(df)
    zero_price = (df["open"] <= 0) | (df["close"] <= 0) | (df["high"] <= 0) | (df["low"] <= 0)
    n_zero = int(zero_price.sum())
    df = df.loc[~zero_price].copy()

    has_prev = df["prev_close"].notna() & (df["prev_close"] > 0)
    n_no_prev = int((~has_prev).sum())
    df = df.loc[has_prev].copy()

    stale = df["day_gap"] > MAX_PREV_DATE_GAP_DAYS
    n_stale = int(stale.sum())
    df = df.loc[~stale].copy()

    df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100.0
    extreme = df["gap_pct"].abs() > MAX_PLAUSIBLE_GAP_ABS
    n_extreme = int(extreme.sum())
    df = df.loc[~extreme].copy()

    quality = dict(
        n_total_rows=n_total, n_zero_price_dropped=n_zero, n_no_prev_close_dropped=n_no_prev,
        n_stale_break_dropped=n_stale, n_extreme_gap_dropped=n_extreme, n_clean_rows=len(df),
    )
    return df, quality


def bucket_gap(gap_pct: pd.Series) -> pd.Series:
    mag = gap_pct.abs()
    idx = np.searchsorted(BUCKET_EDGES, mag, side="right") - 1
    idx = np.clip(idx, 0, len(BUCKET_LABELS) - 1)
    return pd.Series(np.array(BUCKET_LABELS)[idx], index=gap_pct.index)


def main() -> int:
    stock_ids = load_universe_stock_ids()
    print(f"universe stocks (from stock_futures_universe.json) = {len(stock_ids)}")

    conn = connect(DEFAULT_DB_PATH)
    try:
        daily = load_daily_bars(conn, stock_ids)
        raw_n_stocks = daily["stock_id"].nunique()
        daily, quality = add_gap_columns(daily)
        quality["raw_n_stocks_in_universe"] = raw_n_stocks
        n_stocks_usable = daily["stock_id"].nunique()
        print(f"clean rows={len(daily):,} stocks_with_gap_data={n_stocks_usable} "
              f"(of {len(stock_ids)} universe / {raw_n_stocks} with any daily_bars rows)")

        gapup = daily.loc[daily["gap_pct"] > 0].copy()
        gapup["gap_bucket"] = bucket_gap(gapup["gap_pct"])
        # A1-identical "填補/回補" definition: full round-trip back to prev_close intraday.
        gapup["fill_fade"] = gapup["low"] <= gapup["prev_close"]
        # A3-identical "續漲/反轉" definition (open->close return sign); the background's
        # "continuation probability <50%" language maps directly to this one.
        gapup["ret_close_pct"] = (gapup["close"] - gapup["open"]) / gapup["open"] * 100.0
        gapup["cont_close"] = gapup["ret_close_pct"] > 0
        gapup["close_fade"] = ~gapup["cont_close"]

        tbl = (
            gapup.groupby("gap_bucket", observed=True)
            .agg(
                n=("fill_fade", "size"),
                fill_fade_prob=("fill_fade", "mean"),
                close_fade_prob=("close_fade", "mean"),
                avg_ret_close_pct=("ret_close_pct", "mean"),
                avg_gap_pct=("gap_pct", "mean"),
            )
            .reindex(BUCKET_LABELS)
            .reset_index()
        )
        tbl["gap_bucket"] = tbl["gap_bucket"].astype(str)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        result = dict(
            generated=pd.Timestamp.now().isoformat(timespec="seconds"),
            universe_n=len(stock_ids),
            universe_stocks_with_gap_data=n_stocks_usable,
            quality=quality,
            fgap_min_reference=0.06,
            table=tbl.to_dict(orient="records"),
        )
        out_json = OUT_DIR / "fgap_calibration_result.json"
        out_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote {out_json}\n")

        print(tbl.to_string(index=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
