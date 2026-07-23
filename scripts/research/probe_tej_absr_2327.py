#!/usr/bin/env python3
"""Probe TEJ TWN/ABSR（主要券商進出明細-券商別）for 2327，交叉核對 FinMind 分點。

Research only：僅寫 CSV cache，不寫 DB、不採納進 Strategy/Order layer。
先抓一小段（預設近 30 個交易日）驗證欄位結構與涵蓋率，再決定是否值得擴大窗口。

  PYTHONPATH=src .venv/bin/python scripts/research/probe_tej_absr_2327.py
  PYTHONPATH=src .venv/bin/python scripts/research/probe_tej_absr_2327.py \\
      --start 2026-06-01 --end 2026-07-20
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from query_stock_prices import tej_get_json  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

STOCK = "2327"
OUT_CACHE = ROOT / "reports/research/whale_chip_precursor/cache"
OUT_CACHE.mkdir(parents=True, exist_ok=True)
RAW_PATH = OUT_CACHE / "tej_absr_2327_probe.csv"


def fetch_tej_absr(coid: str, start: date, end: date) -> pd.DataFrame:
    """抓 TWN/ABSR 原始列（券商別買賣明細），欄位以 TEJ 實際回傳為準。"""
    payload = tej_get_json(
        "/TWN/ABSR.json",
        {
            "coid": coid,
            "mdate.gte": start.isoformat(),
            "mdate.lte": end.isoformat(),
            "opts.sort": "mdate.asc",
        },
    )
    if payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"TEJ {err.get('code')}: {err.get('message')}")
    datatable = payload.get("datatable") or {}
    cols = [c.get("name") for c in datatable.get("columns") or []]
    rows = datatable.get("data") or []
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=cols)


def load_finmind_branch(stock_id: str, start: date, end: date) -> pd.DataFrame:
    """讀本庫既有 FinMind 分點資料（stock_broker_branch_daily, source='finmind'）。"""
    conn = connect(DEFAULT_DB_PATH)
    try:
        return pd.read_sql_query(
            """
            SELECT trade_date, securities_trader_id, securities_trader, buy, sell, net
            FROM stock_broker_branch_daily
            WHERE stock_id = ? AND source = 'finmind'
              AND trade_date >= ? AND trade_date <= ?
            """,
            conn,
            params=(stock_id, start.isoformat(), end.isoformat()),
        )
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coid", default=STOCK)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument(
        "--start",
        default=None,
        help="預設 = --end 往前 30 個日曆日（一小段探測窗）",
    )
    args = ap.parse_args()
    end = date.fromisoformat(args.end)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=30)

    print(f"[TEJ] TWN/ABSR coid={args.coid} {start}..{end} …")
    try:
        tej = fetch_tej_absr(args.coid, start, end)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL TEJ fetch: {exc}", file=sys.stderr)
        return 1
    if tej.empty:
        print("TEJ 回傳 0 列（代碼可能不對，或該窗無資料）", file=sys.stderr)
        return 1
    tej.to_csv(RAW_PATH, index=False)
    n_days = tej["mdate"].nunique() if "mdate" in tej.columns else -1
    print(
        f"  TEJ rows={len(tej)} days={n_days} avg_rows_per_day="
        f"{len(tej) / max(n_days, 1):.1f} cols={list(tej.columns)}"
    )
    print(f"  → wrote {RAW_PATH}")

    fm = load_finmind_branch(args.coid, start, end)
    print(f"[FinMind] 既有 stock_broker_branch_daily 同窗 rows={len(fm)}")
    if fm.empty:
        print("FinMind 無同窗資料可比對，僅完成 TEJ 探測", file=sys.stderr)
        return 0

    date_col = next((c for c in ("mdate", "mdate_c") if c in tej.columns), None)
    buy_col = next((c for c in ("buy_s", "buy") if c in tej.columns), None)
    sell_col = next((c for c in ("sell_s", "sell") if c in tej.columns), None)
    if not (date_col and buy_col and sell_col):
        print(
            f"WARN 欄位跟預期（mdate/buy_s/sell_s）對不上，實際欄位={list(tej.columns)}，"
            "略過每日合計比對，請打開 CSV 人工核對欄位名稱",
            file=sys.stderr,
        )
        return 0

    tej["_d"] = tej[date_col].astype(str).str[:10]
    tej["_net"] = pd.to_numeric(tej[buy_col], errors="coerce").fillna(0) - pd.to_numeric(
        tej[sell_col], errors="coerce"
    ).fillna(0)
    tej_daily = tej.groupby("_d")["_net"].sum().rename("tej_net")
    fm_daily = fm.groupby("trade_date")["net"].sum().rename("finmind_net")

    merged = pd.concat([tej_daily, fm_daily], axis=1).dropna()
    if merged.empty:
        print("兩邊日期無交集，無法比對", file=sys.stderr)
        return 0

    corr = merged["tej_net"].corr(merged["finmind_net"])
    same_sign = (merged["tej_net"] * merged["finmind_net"] > 0).mean()
    print(f"\n共同交易日 n={len(merged)}")
    print(f"日合計淨買賣 相關係數 r={corr:.3f}")
    print(f"同方向比例（正負號一致）={same_sign:.0%}")
    print(
        "\n注意：TEJ 券商進出明細常僅列「當日進出前 N 大券商」而非全券商，"
        "FinMind 是近全量分點；若 TEJ rows/day 遠小於 FinMind 券商數，"
        "以上合計比對僅供參考，不代表兩邊資料矛盾。"
    )
    print(merged.tail(10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
