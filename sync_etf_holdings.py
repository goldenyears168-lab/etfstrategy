#!/usr/bin/env python3
"""
從 EZMoney（統一投信）或凱基投信官網同步 ETF 每日持股至 SQLite。

官網僅提供最新快照；每日執行可累積歷史，供 share_delta 加減碼分析。
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import requests
import urllib3

from stock_db import (
    DEFAULT_DB_PATH,
    compute_etf_holdings_changes,
    connect,
    list_etf_snapshot_dates,
    load_etf_holdings,
    load_etf_holdings_meta,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
)

EZMONEY_BASE_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info"
EZMONEY_FUND_MAP: dict[str, str] = {
    "00981A": "49YTW",
    "00403A": "63YTW",
    "00988A": "61YTW",
}

KGIFUND_BASE_URL = "https://www.kgifund.com.tw/Fund/Detail"
KGIFUND_FUND_MAP: dict[str, str] = {
    "009816": "J023",
    # 00407A 掛牌後從官網連結確認 fundID 並填入
}

EZMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

KGIFUND_HEADERS = {
    "User-Agent": EZMONEY_HEADERS["User-Agent"],
    "Accept-Language": "zh-TW,zh;q=0.9",
}

HOLDING_PATTERN = re.compile(
    r'\{"FundCode":"(?P<fund_code>[^"]+)"[^}]*?'
    r'"DetailCode":"(?P<stock_id>[^"]*?)"[^}]*?'
    r'"DetailName":"(?P<stock_name>[^"]*?)"[^}]*?'
    r'"Share":(?P<shares>[\d.]+)[^}]*?'
    r'"Amount":(?P<amount>[\d.]+)[^}]*?'
    r'"NavRate":(?P<weight_pct>[\d.]+)[^}]*?'
    r'"EditTime":"(?P<edit_time>[^"]+)"'
)
NAV_PATTERN = re.compile(
    r'"AssetCode":"P_UNIT"[^}]*?"Value":(?P<nav>[\d.]+)[^}]*?"EditDate":"(?P<edit_date>[^"]+)"'
)
KGIFUND_STOCK_ROW = re.compile(
    r'<td[^>]*>\s*(?P<stock_id>\d{4})\s*</td>\s*'
    r'<td[^>]*>(?P<stock_name>[^<]+)</td>\s*'
    r'<td[^>]*>(?P<shares>[\d,]+)</td>\s*'
    r'<td[^>]*>(?P<weight_pct>[\d.]+)</td>',
    re.IGNORECASE,
)
KGIFUND_SNAPSHOT_DATE = re.compile(
    r'持股比重[^<]*</div>\s*<p[^>]*>\((?P<date>\d{4}/\d{2}/\d{2})\)</p>'
    r'|LatestNAVDate"[^>]*value="(?P<nav_date>\d{4}/\d{2}/\d{2})"'
)
KGIFUND_NAV = re.compile(r'基金每單位淨值[^0-9]*(?P<nav>[\d.]+)')


class NotListedError(Exception):
    """ETF 尚未挂牌或官網尚無持股資料。"""


@dataclass(frozen=True)
class EtfHoldingsSnapshot:
    etf_code: str
    fund_code: str
    snapshot_date: str
    source_edit_at: str
    nav: float | None
    holdings: list[dict]


def parse_etf_codes(etf_code: str | None, etf_codes: str | None) -> tuple[str, ...]:
    if etf_codes:
        return tuple(code.strip().upper() for code in etf_codes.split(",") if code.strip())
    if etf_code:
        return (etf_code.upper(),)
    return ("00981A",)


def fund_code_for(etf_code: str) -> str:
    fund_code = EZMONEY_FUND_MAP.get(etf_code.upper())
    if not fund_code:
        known = ", ".join(sorted(EZMONEY_FUND_MAP))
        raise ValueError(f"Unknown EZMoney ETF code {etf_code!r}. Known: {known}")
    return fund_code


def kgifund_id_for(etf_code: str) -> str:
    fund_id = KGIFUND_FUND_MAP.get(etf_code.upper())
    if not fund_id:
        known = ", ".join(sorted(KGIFUND_FUND_MAP))
        raise ValueError(f"Unknown KGIFund ETF code {etf_code!r}. Known: {known}")
    if fund_id.upper() in {"TBD", ""}:
        raise NotListedError(f"{etf_code} 尚未在 KGIFUND_FUND_MAP 設定 fundID")
    return fund_id


def fetch_ezmoney_snapshot(etf_code: str, session: requests.Session | None = None) -> EtfHoldingsSnapshot:
    fund_code = fund_code_for(etf_code)
    sess = session or requests.Session()
    response = sess.get(
        EZMONEY_BASE_URL,
        params={"fundCode": fund_code},
        headers=EZMONEY_HEADERS,
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    if not response.text.strip():
        raise RuntimeError(f"EZMoney returned empty body for fundCode={fund_code}")

    decoded = html.unescape(response.text)
    holdings: list[dict] = []
    source_edit_at = ""

    for match in HOLDING_PATTERN.finditer(decoded):
        if match.group("fund_code") != fund_code:
            continue
        edit_time = match.group("edit_time")
        source_edit_at = edit_time
        holdings.append(
            {
                "stock_id": match.group("stock_id"),
                "stock_name": match.group("stock_name"),
                "shares": float(match.group("shares")),
                "amount": float(match.group("amount")),
                "weight_pct": float(match.group("weight_pct")),
                "edit_time": edit_time,
            }
        )

    if not holdings:
        raise RuntimeError(f"No holdings parsed from EZMoney for {etf_code} ({fund_code})")

    holdings.sort(key=lambda row: row["stock_id"])
    snapshot_date = source_edit_at[:10]
    nav: float | None = None
    nav_match = NAV_PATTERN.search(decoded)
    if nav_match:
        nav = float(nav_match.group("nav"))
        if not source_edit_at:
            source_edit_at = nav_match.group("edit_date")
            snapshot_date = source_edit_at[:10]

    return EtfHoldingsSnapshot(
        etf_code=etf_code.upper(),
        fund_code=fund_code,
        snapshot_date=snapshot_date,
        source_edit_at=source_edit_at,
        nav=nav,
        holdings=holdings,
    )


def _normalize_kgifund_date(raw: str) -> str:
    return raw.replace("/", "-")


def fetch_kgifund_snapshot(etf_code: str, session: requests.Session | None = None) -> EtfHoldingsSnapshot:
    fund_id = kgifund_id_for(etf_code)
    sess = session or requests.Session()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        response = sess.get(
            KGIFUND_BASE_URL,
            params={"fundID": fund_id},
            headers=KGIFUND_HEADERS,
            timeout=30,
            allow_redirects=True,
            verify=False,
        )
    response.raise_for_status()
    if not response.text.strip():
        raise RuntimeError(f"KGIFund returned empty body for fundID={fund_id}")

    decoded = html.unescape(response.text)
    if etf_code.upper() not in decoded and fund_id not in decoded:
        raise NotListedError(f"{etf_code} 官網頁面尚無有效持股資料 (fundID={fund_id})")

    date_match = KGIFUND_SNAPSHOT_DATE.search(decoded)
    if not date_match:
        raise NotListedError(f"{etf_code} 官網尚無持股日期 (fundID={fund_id})")
    raw_date = date_match.group("date") or date_match.group("nav_date")
    snapshot_date = _normalize_kgifund_date(raw_date)
    source_edit_at = snapshot_date

    nav: float | None = None
    nav_match = KGIFUND_NAV.search(decoded)
    if nav_match:
        nav = float(nav_match.group("nav"))

    seen: set[str] = set()
    holdings: list[dict] = []
    for match in KGIFUND_STOCK_ROW.finditer(decoded):
        stock_id = match.group("stock_id")
        if stock_id in seen:
            continue
        seen.add(stock_id)
        holdings.append(
            {
                "stock_id": stock_id,
                "stock_name": html.unescape(match.group("stock_name").strip()),
                "shares": float(match.group("shares").replace(",", "")),
                "amount": None,
                "weight_pct": float(match.group("weight_pct")),
                "edit_time": source_edit_at,
            }
        )

    if not holdings:
        raise NotListedError(f"{etf_code} 官網尚無持股表格 (fundID={fund_id})")

    holdings.sort(key=lambda row: row["stock_id"])
    return EtfHoldingsSnapshot(
        etf_code=etf_code.upper(),
        fund_code=fund_id,
        snapshot_date=snapshot_date,
        source_edit_at=source_edit_at,
        nav=nav,
        holdings=holdings,
    )


def fetch_snapshot(
    etf_code: str,
    source: str,
    session: requests.Session | None = None,
) -> EtfHoldingsSnapshot:
    if source == "kgifund":
        return fetch_kgifund_snapshot(etf_code, session=session)
    if source == "ezmoney":
        return fetch_ezmoney_snapshot(etf_code, session=session)
    if etf_code.upper() in KGIFUND_FUND_MAP:
        return fetch_kgifund_snapshot(etf_code, session=session)
    if etf_code.upper() in EZMONEY_FUND_MAP:
        return fetch_ezmoney_snapshot(etf_code, session=session)
    raise ValueError(f"No holdings source configured for {etf_code}")


def resolve_source(etf_code: str, explicit: str | None) -> str:
    code = etf_code.upper()
    if explicit:
        if explicit == "kgifund" and code not in KGIFUND_FUND_MAP:
            raise NotListedError(f"{code} 尚未設定 KGIFUND fundID")
        if explicit == "ezmoney" and code not in EZMONEY_FUND_MAP:
            raise ValueError(f"{code} 不在 EZMONEY_FUND_MAP")
        return explicit
    if code in KGIFUND_FUND_MAP:
        return "kgifund"
    if code in EZMONEY_FUND_MAP:
        return "ezmoney"
    raise ValueError(f"No holdings source configured for {code}")


def holdings_unchanged(
    conn,
    snapshot: EtfHoldingsSnapshot,
) -> bool:
    """Skip write when official snapshot_date + count + edit time match DB."""
    meta = load_etf_holdings_meta(conn, snapshot.etf_code, snapshot.snapshot_date)
    if meta is None:
        return False
    return (
        meta["holding_count"] == len(snapshot.holdings)
        and meta["source_edit_at"] == snapshot.source_edit_at
    )


def export_holdings_csv(snapshot: EtfHoldingsSnapshot, csv_path: Path) -> Path:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "etf_code",
        "snapshot_date",
        "stock_id",
        "stock_name",
        "shares",
        "weight_pct",
        "amount",
        "source_edit_at",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in snapshot.holdings:
            writer.writerow(
                {
                    "etf_code": snapshot.etf_code,
                    "snapshot_date": snapshot.snapshot_date,
                    "stock_id": item["stock_id"],
                    "stock_name": item["stock_name"],
                    "shares": item["shares"],
                    "weight_pct": item["weight_pct"],
                    "amount": item["amount"],
                    "source_edit_at": snapshot.source_edit_at,
                }
            )
    return csv_path


def sync_snapshot(
    conn,
    snapshot: EtfHoldingsSnapshot,
    source: str = "ezmoney",
    *,
    force: bool = False,
) -> dict:
    if not force and holdings_unchanged(conn, snapshot):
        return {
            "etf_code": snapshot.etf_code,
            "snapshot_date": snapshot.snapshot_date,
            "holding_count": len(snapshot.holdings),
            "nav": snapshot.nav,
            "source_edit_at": snapshot.source_edit_at,
            "skipped": True,
        }
    rows = [
        {
            "etf_code": snapshot.etf_code,
            "snapshot_date": snapshot.snapshot_date,
            "stock_id": item["stock_id"],
            "stock_name": item["stock_name"],
            "shares": item["shares"],
            "weight_pct": item["weight_pct"],
            "amount": item["amount"],
            "source": source,
            "source_edit_at": snapshot.source_edit_at,
        }
        for item in snapshot.holdings
    ]
    upsert_etf_holdings_meta(
        conn,
        {
            "etf_code": snapshot.etf_code,
            "snapshot_date": snapshot.snapshot_date,
            "nav": snapshot.nav,
            "holding_count": len(rows),
            "source": source,
            "source_edit_at": snapshot.source_edit_at,
        },
    )
    count = upsert_etf_holdings(conn, rows)
    return {
        "etf_code": snapshot.etf_code,
        "snapshot_date": snapshot.snapshot_date,
        "holding_count": count,
        "nav": snapshot.nav,
        "source_edit_at": snapshot.source_edit_at,
        "skipped": False,
    }


def print_holdings(conn, etf_code: str, snapshot_date: str | None) -> None:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if not dates:
        print(f"No holdings in DB for {etf_code}")
        return
    date = snapshot_date or dates[0]
    rows = load_etf_holdings(conn, etf_code, date)
    print(f"{etf_code} holdings @ {date} ({len(rows)} stocks)")
    for row in rows:
        print(
            f"  {row['stock_id']:>6} {row['stock_name']:<8} "
            f"shares={row['shares']:>12,.0f} weight={row['weight_pct']:>6.2f}%"
        )


def print_changes(conn, etf_code: str, curr_date: str | None, prev_date: str | None) -> None:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if len(dates) < 2 and curr_date is None:
        print(f"Need at least 2 snapshot dates for {etf_code}; have {len(dates)}")
        print("  → 需連續不同交易日各跑一次；同天重複按不會增加 snapshot 日")
        return

    curr = curr_date or dates[0]
    prev = prev_date
    if prev is None:
        prev = dates[1] if dates[0] == curr else dates[0]

    rows = compute_etf_holdings_changes(conn, etf_code, curr, prev)
    print(f"{etf_code} changes: {prev} -> {curr}")
    changed = [row for row in rows if row["action"] != "不变"]
    if not changed:
        print("  (no share changes)")
        return
    for row in changed:
        print(
            f"  {row['stock_id']:>6} {row['stock_name']:<8} "
            f"{row['action']} delta={row['share_delta']:>+12,.0f} "
            f"({row['shares_prev'] or 0:,.0f} -> {row['shares_curr'] or 0:,.0f})"
        )


def sync_one_etf(
    etf_code: str,
    args: argparse.Namespace,
    *,
    source: str | None = None,
    session: requests.Session | None = None,
) -> int:
    try:
        resolved = resolve_source(etf_code, source or args.source)
        snapshot = fetch_snapshot(etf_code, resolved, session=session)
    except NotListedError as exc:
        print(f"  SKIP {etf_code}: {exc}")
        return 0

    print(
        f"Fetched {snapshot.etf_code} ({snapshot.fund_code}, {resolved}): "
        f"{len(snapshot.holdings)} holdings @ {snapshot.snapshot_date}, "
        f"NAV={snapshot.nav}"
    )

    if args.dry_run:
        for item in snapshot.holdings[:5]:
            print(
                f"  {item['stock_id']} {item['stock_name']} "
                f"shares={item['shares']:,.0f} weight={item['weight_pct']}%"
            )
        if len(snapshot.holdings) > 5:
            print(f"  ... and {len(snapshot.holdings) - 5} more")
        return 0

    conn = connect(args.db_path)
    dates_before = set(list_etf_snapshot_dates(conn, etf_code))
    result = sync_snapshot(conn, snapshot, source=resolved, force=args.force)
    if result.get("skipped"):
        print(
            f"Skipped write: unchanged snapshot {result['snapshot_date']} "
            f"({result['holding_count']} stocks, NAV={result['nav']})"
        )
        print(
            "  → 官網 EditTime 未更新；重複按正常，DB 不會新增 snapshot 日"
        )
    else:
        print(
            f"Synced {result['holding_count']} rows to {args.db_path} "
            f"(snapshot_date={result['snapshot_date']}, NAV={result['nav']})"
        )
        if result["snapshot_date"] in dates_before:
            print("  → 覆寫既有 snapshot 日（重複按安全）")
        else:
            total_dates = len(dates_before) + 1
            print(
                f"  → 新增 snapshot 日；DB 現共 {total_dates} 日"
                + ("，changes 可開始比較" if total_dates >= 2 else "")
            )

    if args.export_csv:
        csv_path = Path("data") / etf_code / f"holdings_{snapshot.snapshot_date}.csv"
        export_holdings_csv(snapshot, csv_path)
        print(f"Exported CSV: {csv_path}")

    dates = list_etf_snapshot_dates(conn, etf_code)
    if len(dates) >= 2 and not args.changes:
        print_changes(conn, etf_code, result["snapshot_date"], None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync ETF holdings to SQLite (EZMoney / KGIFund)")
    parser.add_argument(
        "--etf-code",
        default=None,
        help="Single ETF ticker (default: 00981A if --etf-codes omitted)",
    )
    parser.add_argument(
        "--etf-codes",
        default=None,
        help="Comma-separated ETF tickers",
    )
    parser.add_argument(
        "--source",
        choices=("ezmoney", "kgifund"),
        default=None,
        help="Force holdings source for all codes in this invocation",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print holdings sorted by stock_id (skip fetch)",
    )
    parser.add_argument(
        "--date",
        help="Snapshot date for --list / --changes (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--changes",
        action="store_true",
        help="Print share_delta vs previous snapshot (skip fetch)",
    )
    parser.add_argument(
        "--prev-date",
        help="Previous snapshot date for --changes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only; do not write SQLite",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write even if snapshot unchanged in DB",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Also save data/{etf_code}/holdings_{snapshot_date}.csv",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    codes = parse_etf_codes(args.etf_code, args.etf_codes)

    if args.list or args.changes:
        conn = connect(args.db_path)
        for etf_code in codes:
            if args.list:
                print_holdings(conn, etf_code, args.date)
            if args.changes:
                print_changes(conn, etf_code, args.date, args.prev_date)
        return 0

    exit_code = 0
    session = requests.Session()
    for etf_code in codes:
        try:
            result = sync_one_etf(etf_code, args, session=session)
            if result != 0:
                exit_code = result
        except NotListedError as exc:
            print(f"  SKIP {etf_code}: {exc}")
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"ERROR {etf_code}: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
