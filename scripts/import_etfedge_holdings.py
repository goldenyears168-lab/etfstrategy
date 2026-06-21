#!/usr/bin/env python3
"""Backfill ETF holdings from etfedge MCP into data/stocks.db."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from etfedge_holdings_import import (  # noqa: E402
    DEFAULT_LISTING_DATE,
    DEFAULT_CACHE_DIR,
    import_etf_holdings_from_etfedge,
    listing_date_or_default,
)
from etfedge_mcp_client import DEFAULT_MCP_URL, EtfedgeMcpClient  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env_token = os.environ.get("ETFEDGE_MCP_TOKEN", "").strip()
    if env_token:
        return env_token
    token_file = os.environ.get("ETFEDGE_MCP_TOKEN_FILE", "").strip()
    if token_file:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import ETF daily holdings history from etfedge MCP."
    )
    parser.add_argument("--etf-code", default="00981A", help="ETF ticker (default: 00981A)")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("ETFEDGE_MCP_URL", DEFAULT_MCP_URL),
        help="etfedge MCP endpoint",
    )
    parser.add_argument("--token", help="Bearer token (or ETFEDGE_MCP_TOKEN in .env)")
    parser.add_argument(
        "--start-date",
        default=None,
        help=f"Import from date YYYY-MM-DD (default: {DEFAULT_LISTING_DATE} for 00981A)",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=365,
        help="Days of per-stock history per MCP call (max 365)",
    )
    parser.add_argument(
        "--min-holdings",
        type=int,
        default=20,
        help="Skip snapshot dates with fewer holdings (incomplete)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=3.2,
        help="Min seconds between MCP calls (rate limit: 20/min)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and summarize without writing SQLite",
    )
    parser.add_argument(
        "--overwrite-local",
        action="store_true",
        help="Replace dates even if already synced from ezmoney/kgifund/etc.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Local MCP response cache (default: data/etfedge_cache)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read/write local stock history cache",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Import from cache only (no MCP API calls)",
    )
    parser.add_argument(
        "--allow-partial-cache",
        action="store_true",
        help="With --cache-only: import even if some stock caches are missing",
    )
    args = parser.parse_args()

    _load_dotenv(ROOT / ".env")
    token = _resolve_token(args.token)
    etf_code = args.etf_code.upper()
    start_date = args.start_date or listing_date_or_default(etf_code)
    history_days = max(1, min(int(args.history_days), 365))

    client = EtfedgeMcpClient(
        token,
        url=args.mcp_url,
        min_interval_s=max(0.0, float(args.sleep)),
    )

    cache_root = args.cache_dir or DEFAULT_CACHE_DIR

    conn = connect(args.db)
    try:
        result = import_etf_holdings_from_etfedge(
            conn,
            client,
            etf_code,
            start_date=start_date,
            history_days=history_days,
            min_holdings=max(1, int(args.min_holdings)),
            prefer_existing_source=not args.overwrite_local,
            dry_run=args.dry_run,
            cache_root=cache_root,
            use_cache=not args.no_cache,
            cache_only=args.cache_only,
            allow_partial_cache=args.allow_partial_cache,
        )
    finally:
        conn.close()

    mode = "DRY-RUN" if args.dry_run else "IMPORTED"
    print(f"{mode} {result.etf_code}")
    print(f"  MCP calls (est.): {result.mcp_calls}")
    print(f"  cache hits:       {result.cache_hits}")
    print(f"  API fetches:      {result.api_fetches}")
    print(f"  snapshot dates: {result.dates_imported}")
    print(f"  holding rows:   {result.rows_written}")
    if result.date_range:
        print(f"  date range:     {result.date_range[0]} .. {result.date_range[1]}")
    if result.skipped_sparse_dates:
        print(f"  skipped sparse: {result.skipped_sparse_dates} dates (<{args.min_holdings} stocks)")
    if args.dry_run:
        print("  (no DB writes — remove --dry-run to import)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
