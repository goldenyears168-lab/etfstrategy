#!/usr/bin/env python3
"""
從 FinMind 同步 ETF 日價 + 三大法人淨買賣至 etf_daily_signal_snapshot。

lookback 視窗內有資料的交易日皆 upsert（可補漏日）。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

from stock_db import DEFAULT_DB_PATH, connect, upsert_etf_daily_signal_snapshots

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
SOURCE = "finmind"

INSTITUTIONAL_FIELDS: dict[str, str] = {
    "Foreign_Investor": "foreign_net",
    "Investment_Trust": "investment_trust_net",
    "Dealer_self": "dealer_self_net",
}


def finmind_headers() -> dict[str, str]:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if token.startswith("eyJ") and len(token) > 100:
        return {"Authorization": f"Bearer {token}"}
    return {}


def parse_etf_codes(etf_code: str | None, etf_codes: str | None) -> tuple[str, ...]:
    if etf_codes:
        return tuple(code.strip().upper() for code in etf_codes.split(",") if code.strip())
    if etf_code:
        return (etf_code.upper(),)
    return ("00981A",)


def fetch_finmind(dataset: str, code: str, start: date, end: date) -> list[dict]:
    resp = requests.get(
        FINMIND_URL,
        params={
            "dataset": dataset,
            "data_id": code,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        headers=finmind_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(payload.get("msg", "FinMind error"))
    return payload.get("data") or []


def aggregate_institutional(rows: list[dict]) -> dict[str, dict[str, float]]:
    """依交易日彙總三大法人淨買賣（不含避險、外資自營）。"""
    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        field = INSTITUTIONAL_FIELDS.get(row.get("name", ""))
        if not field:
            continue
        snap_date = str(row["date"])[:10]
        net = float(row.get("buy") or 0) - float(row.get("sell") or 0)
        bucket = by_date.setdefault(
            snap_date,
            {
                "foreign_net": 0.0,
                "investment_trust_net": 0.0,
                "dealer_self_net": 0.0,
            },
        )
        bucket[field] += net
    for snap_date, nets in by_date.items():
        nets["three_institution_net"] = (
            nets["foreign_net"] + nets["investment_trust_net"] + nets["dealer_self_net"]
        )
    return by_date


def build_snapshots(code: str, start: date, end: date) -> list[dict]:
    inst_rows = fetch_finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start, end)
    if not inst_rows:
        return []

    inst_by_date = aggregate_institutional(inst_rows)
    price_rows = fetch_finmind("TaiwanStockPrice", code, start, end)
    close_by_date = {str(row["date"])[:10]: float(row["close"]) for row in price_rows}

    snapshots: list[dict] = []
    for snap_date in sorted(inst_by_date):
        close = close_by_date.get(snap_date)
        nets = inst_by_date[snap_date]
        snapshots.append(
            {
                "code": code,
                "snapshot_date": snap_date,
                "close_price": close,
                "foreign_net": nets["foreign_net"],
                "investment_trust_net": nets["investment_trust_net"],
                "dealer_self_net": nets["dealer_self_net"],
                "three_institution_net": nets["three_institution_net"],
                "source": SOURCE,
            }
        )
    return snapshots


def sync_etf_signal(
    code: str,
    db_path: Path,
    lookback_days: int,
    dry_run: bool = False,
    *,
    quiet: bool = False,
) -> int:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    snapshots = build_snapshots(code, start, end)
    if not snapshots:
        raise RuntimeError(f"{code} 在 {start}～{end} 無三大法人資料")

    if dry_run:
        latest = snapshots[-1]
        print(
            f"DRY RUN {code} {latest['snapshot_date']}: "
            f"close={latest['close_price']} "
            f"三大法人={latest['three_institution_net']:,.0f} "
            f"({len(snapshots)} 筆待寫入)"
        )
        return len(snapshots)

    conn = connect(db_path)
    try:
        before = conn.execute(
            "SELECT MAX(snapshot_date) FROM etf_daily_signal_snapshot WHERE code = ? AND source = ?",
            (code, SOURCE),
        ).fetchone()[0]
        count = upsert_etf_daily_signal_snapshots(conn, snapshots)
    finally:
        conn.close()

    latest = snapshots[-1]
    date_range = f"{snapshots[0]['snapshot_date']} ～ {latest['snapshot_date']}"
    if quiet:
        if before == latest["snapshot_date"]:
            print(f"  {code}: {count} rows @ {latest['snapshot_date']} (unchanged)")
        else:
            print(
                f"  {code}: {count} rows @ {latest['snapshot_date']} "
                f"close={latest['close_price']}"
            )
    elif before == latest["snapshot_date"]:
        print(
            f"  {code} signal snapshot：upsert {count} 筆（{date_range}），"
            f"最新日 {latest['snapshot_date']} 未變，僅刷新 synced_at"
        )
    else:
        print(
            f"  {code} signal snapshot：upsert {count} 筆（{date_range}），"
            f"最新 {latest['snapshot_date']} close={latest['close_price']} "
            f"三大法人={latest['three_institution_net']:,.0f}"
        )
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 ETF 日價 + 三大法人至 SQLite")
    parser.add_argument("--etf-code", default=None, help="單一 ETF 代號")
    parser.add_argument(
        "--etf-codes",
        default=None,
        help="逗號分隔 ETF 代號（優先於 --etf-code）",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite 路徑")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="回溯天數，視窗內有資料的交易日皆 upsert（預設 14）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只抓取不寫入")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="每檔 ETF 一行摘要",
    )
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_code, args.etf_codes)
    exit_code = 0
    for code in codes:
        try:
            sync_etf_signal(
                code,
                args.db,
                args.lookback_days,
                dry_run=args.dry_run,
                quiet=args.quiet,
            )
        except RuntimeError as exc:
            print(f"  WARN {code}: {exc}", file=sys.stderr)
        except requests.HTTPError as exc:
            print(f"  WARN {code}: FinMind 不可用（{exc}）", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN {code}: {exc}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
