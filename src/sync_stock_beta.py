#!/usr/bin/env python3
"""
同步上市櫃股票 Beta 因子查表（vs ^TWII，250 交易日滾動計算）。

清單：FinMind TaiwanStockInfo（twse → TSE、tpex → OTC）
Beta：Yahoo 日線自算，小數 2 位，source=yahoo_computed

建議每週手動更新（非 daily_sync 一環）：
  .venv/bin/python sync_stock_beta.py --sync-db --csv data/stock_beta.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_beta

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
BENCHMARK_SYMBOL = "^TWII"
BETA_WINDOW = ""
BETA_SOURCE = "yahoo_computed"
BETA_DECIMALS = 2
BETA_TRADING_DAYS = 250
DOWNLOAD_PERIOD = "400d"
BATCH_SIZE = 40
MARKET_YAHOO = {"TSE": "TW", "OTC": "TWO"}


@dataclass(frozen=True)
class StockRow:
    stock_id: str
    name: str
    market: str


def fetch_stock_universe(include_emerging: bool = False) -> list[StockRow]:
    """從 FinMind 取得上市櫃普通股清單（4 碼、依最新 date 去重）。"""
    resp = requests.get(
        FINMIND_URL,
        params={"dataset": "TaiwanStockInfo"},
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(payload.get("msg", "FinMind TaiwanStockInfo error"))

    allowed_types = {"twse", "tpex"}
    if include_emerging:
        allowed_types.add("emerging")

    latest: dict[str, dict] = {}
    for row in payload.get("data") or []:
        stock_type = str(row.get("type", "")).lower()
        if stock_type not in allowed_types:
            continue
        stock_id = str(row.get("stock_id", "")).strip()
        if not (stock_id.isdigit() and len(stock_id) == 4):
            continue
        snap_date = str(row.get("date", ""))
        prev = latest.get(stock_id)
        if prev is None or snap_date > prev["date"]:
            latest[stock_id] = row

    rows: list[StockRow] = []
    for stock_id, row in sorted(latest.items()):
        if row["type"] == "twse":
            market = "TSE"
        elif row["type"] == "tpex":
            market = "OTC"
        else:
            market = "EMERGING"
        rows.append(
            StockRow(
                stock_id=stock_id,
                name=str(row.get("stock_name", "")).strip(),
                market=market,
            )
        )
    return rows


def yahoo_symbol(stock: StockRow) -> str:
    suffix = MARKET_YAHOO.get(stock.market)
    if not suffix:
        raise ValueError(f"Unsupported market for Yahoo: {stock.market}")
    return f"{stock.stock_id}.{suffix}"


def download_market_returns() -> pd.Series:
    raw = yf.download(
        BENCHMARK_SYMBOL,
        period=DOWNLOAD_PERIOD,
        progress=False,
        auto_adjust=True,
    )
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    returns = close.pct_change().dropna().tail(BETA_TRADING_DAYS)
    if len(returns) < BETA_TRADING_DAYS // 2:
        raise RuntimeError(f"基準 {BENCHMARK_SYMBOL} 資料不足（{len(returns)} 日）")
    return returns


def compute_batch_beta(
    stocks: list[StockRow],
    market_returns: pd.Series,
) -> list[dict]:
    symbols = [yahoo_symbol(s) for s in stocks]
    symbol_map = dict(zip(symbols, stocks))
    raw = yf.download(
        " ".join(symbols),
        period=DOWNLOAD_PERIOD,
        progress=False,
        auto_adjust=True,
        threads=True,
    )
    closes = raw["Close"]
    if isinstance(closes, pd.Series):
        closes = closes.to_frame()

    as_of = date.today().isoformat()
    rows: list[dict] = []
    for sym in symbols:
        stock = symbol_map[sym]
        try:
            if sym not in closes.columns:
                continue
            series = closes[sym].dropna()
            if series.empty:
                continue
            stock_returns = series.pct_change().dropna()
            aligned = pd.concat(
                [stock_returns.rename("stock"), market_returns.rename("market")],
                axis=1,
                join="inner",
            ).dropna()
            window = aligned.tail(BETA_TRADING_DAYS)
            if len(window) < BETA_TRADING_DAYS // 2:
                continue
            var = window["market"].var()
            if var == 0 or pd.isna(var):
                continue
            beta = float(window["stock"].cov(window["market"]) / var)
            rows.append(
                {
                    "stock_id": stock.stock_id,
                    "name": stock.name,
                    "market": stock.market,
                    "beta": round(beta, BETA_DECIMALS),
                    "beta_window": BETA_WINDOW,
                    "benchmark": BENCHMARK_SYMBOL,
                    "source": BETA_SOURCE,
                    "as_of_date": as_of,
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return rows


def sync_computed_beta(
    stocks: list[StockRow],
    batch_size: int,
    sleep_sec: float,
) -> list[dict]:
    print(f"  下載基準 {BENCHMARK_SYMBOL}…")
    market_returns = download_market_returns()
    all_rows: list[dict] = []
    total_batches = (len(stocks) + batch_size - 1) // batch_size
    for i in range(0, len(stocks), batch_size):
        batch = stocks[i : i + batch_size]
        batch_no = i // batch_size + 1
        print(f"  Beta 批次 {batch_no}/{total_batches}（{len(batch)} 檔）…")
        all_rows.extend(compute_batch_beta(batch, market_returns))
        if sleep_sec > 0 and i + batch_size < len(stocks):
            time.sleep(sleep_sec)
    return all_rows


def _normalize_header(name: str) -> str:
    return re.sub(r"\s+", "", name.strip().lower())


def _pick_column(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    normalized = {_normalize_header(h): h for h in fieldnames}
    for cand in candidates:
        key = _normalize_header(cand)
        if key in normalized:
            return normalized[key]
    return None


def load_csv_beta(path: Path) -> list[dict]:
    """匯入含 Beta 欄的 CSV（寫入 source=yahoo_computed）。"""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"CSV 無欄位：{path}")

        col_id = _pick_column(
            reader.fieldnames,
            ("代號", "股票代號", "stock_id", "code", "symbol"),
        )
        col_name = _pick_column(
            reader.fieldnames,
            ("名稱", "股票名稱", "name", "description", "公司名稱"),
        )
        col_beta = _pick_column(
            reader.fieldnames,
            ("beta", "β", "Beta", "BETA", "貝他係數", "貝塔", "beta_250d"),
        )
        col_market = _pick_column(
            reader.fieldnames,
            ("market", "市場", "交易所", "上市別"),
        )
        if not col_id or not col_beta:
            raise ValueError(f"CSV 需含代號與 Beta 欄（找到：{reader.fieldnames}）")

        as_of = date.today().isoformat()
        rows: list[dict] = []
        for line in reader:
            raw_id = str(line.get(col_id, "")).strip()
            match = re.match(r"^(\d{4})", raw_id)
            if not match:
                continue
            stock_id = match.group(1)
            beta_raw = str(line.get(col_beta, "")).strip().replace(",", "")
            if not beta_raw or beta_raw in {"-", "--", "N/A", "nan"}:
                continue
            try:
                beta = round(float(beta_raw), BETA_DECIMALS)
            except ValueError:
                continue
            name = str(line.get(col_name or "", "")).strip() if col_name else ""
            market_raw = str(line.get(col_market or "", "")).strip() if col_market else ""
            if market_raw.upper() in {"TSE", "TWSE", "上市"}:
                market = "TSE"
            elif market_raw.upper() in {"OTC", "TPEX", "上櫃"}:
                market = "OTC"
            else:
                market = "TSE"
            rows.append(
                {
                    "stock_id": stock_id,
                    "name": name,
                    "market": market,
                    "beta": beta,
                    "beta_window": BETA_WINDOW,
                    "benchmark": BENCHMARK_SYMBOL,
                    "source": BETA_SOURCE,
                    "as_of_date": as_of,
                }
            )
    return rows


def enrich_names_from_universe(rows: list[dict], universe: list[StockRow]) -> list[dict]:
    by_id = {s.stock_id: s for s in universe}
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        ref = by_id.get(item["stock_id"])
        if ref:
            if not item.get("name"):
                item["name"] = ref.name
            if item.get("market") not in {"TSE", "OTC"}:
                item["market"] = ref.market
        out.append(item)
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "stock_id",
        "name",
        "market",
        "beta",
        "beta_window",
        "benchmark",
        "source",
        "as_of_date",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: row.get(k) for k in fields} for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="同步上市櫃 Beta 至 stock_beta")
    parser.add_argument(
        "--csv-input",
        type=Path,
        help="改由 CSV 匯入",
    )
    parser.add_argument(
        "--include-emerging",
        action="store_true",
        help="含興櫃（預設僅 TSE + OTC）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="僅處理前 N 檔（測試用，0=全部）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Yahoo 批次大小（預設 {BATCH_SIZE}）",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="批次間 sleep 秒數（預設 0.5）",
    )
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="寫入 SQLite stock_beta 表",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="輸出 CSV（例 data/stock_beta.csv）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 輸出至 stdout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不寫入 DB",
    )
    args = parser.parse_args()

    print("取得股票清單（FinMind TaiwanStockInfo）…")
    universe = fetch_stock_universe(include_emerging=args.include_emerging)
    universe = [s for s in universe if s.market in {"TSE", "OTC"} or args.include_emerging]
    if args.limit > 0:
        universe = universe[: args.limit]
    print(
        f"  共 {len(universe)} 檔（TSE {sum(1 for s in universe if s.market == 'TSE')} / "
        f"OTC {sum(1 for s in universe if s.market == 'OTC')}）"
    )

    if args.csv_input:
        rows = enrich_names_from_universe(load_csv_beta(args.csv_input), universe)
        print(f"完成：自 CSV 匯入 {len(rows)} 筆 Beta")
    else:
        rows = sync_computed_beta(
            universe,
            max(1, args.batch_size),
            max(0.0, args.sleep),
        )
        print(f"完成：計算 {len(rows)} 筆 Beta（vs {BENCHMARK_SYMBOL}）")

    if args.csv:
        write_csv(args.csv, rows)
        print(f"已寫入 CSV：{args.csv}")

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    if args.sync_db and not args.dry_run:
        conn = connect(args.db)
        count = upsert_stock_beta(conn, rows)
        conn.close()
        print(f"已寫入 DB：{count} 筆 → {args.db} stock_beta")
    elif args.sync_db and args.dry_run:
        print("dry-run：略過 DB 寫入")

    if not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
