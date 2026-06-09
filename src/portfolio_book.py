#!/usr/bin/env python3
"""多帳本持倉：YAML → portfolio_books / portfolio_positions。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from project_config import ETF_CODES_HOLDINGS
from stock_db import (
    DATA_DIR,
    DEFAULT_DB_PATH,
    connect,
    load_portfolio_books,
    load_portfolio_positions,
    replace_portfolio_positions,
    upsert_portfolio_books,
)

DEFAULT_YAML = DATA_DIR / "portfolio_books.yaml"
EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "config" / "portfolio_books.example.yaml"

_ETF_CODES = frozenset(c.upper() for c in ETF_CODES_HOLDINGS)
_ETF_LIKE = re.compile(r"^00\d{3,4}[A-Z]?$", re.I)


def normalize_symbol(raw: str) -> str:
    return str(raw).strip().upper()


def infer_asset_type(symbol: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip().lower()
    if symbol in _ETF_CODES or _ETF_LIKE.match(symbol):
        return "etf"
    return "stock"


def _parse_position(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        symbol = normalize_symbol(item)
        return {
            "symbol": symbol,
            "asset_type": infer_asset_type(symbol),
        }
    if not isinstance(item, dict):
        raise ValueError(f"無效持倉項目: {item!r}")
    symbol = normalize_symbol(item.get("symbol") or item.get("stock_id") or "")
    if not symbol:
        raise ValueError(f"持倉缺少 symbol: {item}")
    row: dict[str, Any] = {
        "symbol": symbol,
        "asset_type": infer_asset_type(symbol, item.get("asset_type")),
    }
    for key in (
        "stock_name",
        "shares",
        "cost_basis",
        "entry_date",
        "market_value",
        "weight_pct",
        "notes",
    ):
        if item.get(key) is not None:
            row[key] = item[key]
    return row


def load_books_yaml(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "books" not in data:
        raise ValueError(f"{path}: 需要頂層 books 列表")
    books = data["books"]
    if not isinstance(books, list):
        raise ValueError(f"{path}: books 必須為列表")
    out: list[dict[str, Any]] = []
    for book in books:
        if not isinstance(book, dict) or not book.get("book_id"):
            raise ValueError(f"每個 book 需要 book_id: {book}")
        positions = [_parse_position(p) for p in book.get("positions") or []]
        etf_codes = book.get("etf_codes")
        out.append(
            {
                "book_id": str(book["book_id"]).strip().lower(),
                "display_name": book.get("display_name") or book["book_id"],
                "book_type": book.get("book_type") or "discretionary",
                "etf_codes_json": json.dumps(etf_codes, ensure_ascii=False)
                if etf_codes
                else None,
                "notes": book.get("notes"),
                "is_active": 1 if book.get("is_active", True) else 0,
                "positions": positions,
            }
        )
    return out


def sync_books_from_yaml(conn, path: Path) -> dict[str, int]:
    parsed = load_books_yaml(path)
    book_rows = [
        {k: v for k, v in b.items() if k != "positions"}
        for b in parsed
    ]
    upsert_portfolio_books(conn, book_rows)
    pos_counts: dict[str, int] = {}
    for book in parsed:
        bid = book["book_id"]
        n = replace_portfolio_positions(conn, bid, book["positions"])
        pos_counts[bid] = n
    return pos_counts


def print_books_summary(conn) -> None:
    books = load_portfolio_books(conn, active_only=False)
    if not books:
        print("（尚無 portfolio_books；請 --sync-db）")
        return
    for b in books:
        positions = load_portfolio_positions(conn, b["book_id"])
        stocks = [p for p in positions if p["asset_type"] == "stock"]
        etfs = [p for p in positions if p["asset_type"] == "etf"]
        print(
            f"  {b['book_id']:<8} {b['display_name']:<8} "
            f"股 {len(stocks)} · ETF {len(etfs)}"
        )
        for p in positions:
            extra = ""
            if p["shares"] is not None:
                extra = f"  {p['shares']} 股"
            print(f"    {p['symbol']:<8} {p['asset_type']:<5}{extra}")


def main() -> int:
    parser = argparse.ArgumentParser(description="多帳本持倉 YAML 同步")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--yaml",
        type=Path,
        default=None,
        help=f"預設 data/portfolio_books.yaml，不存在則用 {EXAMPLE_YAML.name}",
    )
    parser.add_argument("--sync-db", action="store_true", help="寫入 portfolio_books / positions")
    parser.add_argument("--list", action="store_true", help="列出 DB 內帳本持倉")
    args = parser.parse_args()

    yaml_path = args.yaml
    if yaml_path is None:
        yaml_path = DEFAULT_YAML if DEFAULT_YAML.exists() else EXAMPLE_YAML

    conn = connect(args.db)
    try:
        if args.sync_db:
            if not yaml_path.exists():
                print(f"找不到 {yaml_path}", file=sys.stderr)
                return 1
            counts = sync_books_from_yaml(conn, yaml_path)
            print(f"已同步 {yaml_path.name} → {args.db}")
            for bid, n in counts.items():
                print(f"  {bid}: {n} 檔")
        if args.list or not args.sync_db:
            print("=== portfolio_books ===")
            print_books_summary(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
