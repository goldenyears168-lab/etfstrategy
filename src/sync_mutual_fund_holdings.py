#!/usr/bin/env python3
"""同步境內基金持股至 SQLite（投信公會 SITCA 歷史 + MoneyDJ 最新）。"""

from __future__ import annotations

import argparse
import calendar
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import requests
import urllib3

from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    list_mutual_fund_snapshot_dates,
    upsert_mutual_fund_holdings,
    upsert_mutual_fund_holdings_meta,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MONEYDJ_WR04_URL = "https://www.moneydj.com/jsondata/funddj/fundjsondata.xdjjson"
SITCA_MONTH_URL = "https://www.sitca.org.tw/ROC/Industry/IN2629.aspx?pid=IN22601_04"
SITCA_QUARTER_URL = "https://www.sitca.org.tw/ROC/Industry/IN2630.aspx?pid=IN22601_05"

DISCLOSURE_MONTHLY = "monthly_top10"
DISCLOSURE_QUARTERLY = "quarterly_full"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}


@dataclass(frozen=True)
class FundProfile:
    fund_code: str
    fund_name: str
    moneydj_id: str
    sitca_company_id: str


ALLIANZ_TW_TECH = FundProfile(
    fund_code="ACDD04",
    fund_name="安聯台灣科技基金",
    moneydj_id="acdd04",
    sitca_company_id="A0036",
)

_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)


def ym_values(start: date, end: date) -> list[str]:
    """Inclusive month range as YYYYMM (SITCA ddlQ_YM)."""
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def month_end_from_ym(ym: str) -> str:
    year = int(ym[:4])
    month = int(ym[4:6])
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"N/A", "—", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_stock_id(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().upper()
    if re.fullmatch(r"\d{4}", text):
        return text
    if text.startswith("AS") and re.fullmatch(r"AS\d{4}", text):
        return text[2:]
    return text


def _extract_form_fields(html: str) -> dict[str, str]:
    viewstate = re.search(r'id="__VIEWSTATE" value="([^"]+)"', html)
    validation = re.search(r'id="__EVENTVALIDATION" value="([^"]+)"', html)
    generator = re.search(r'id="__VIEWSTATEGENERATOR" value="([^"]+)"', html)
    if not (viewstate and validation and generator):
        raise RuntimeError("SITCA form fields missing")
    return {
        "__VIEWSTATE": viewstate.group(1),
        "__EVENTVALIDATION": validation.group(1),
        "__VIEWSTATEGENERATOR": generator.group(1),
    }


def _sitca_select_company(session: requests.Session, url: str, ym: str, company_id: str) -> str:
    html = session.get(url, headers=HEADERS, timeout=60, verify=False).text
    fields = _extract_form_fields(html)
    payload = {
        **fields,
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlQ_Comid",
        "__EVENTARGUMENT": "",
        "ctl00$ContentPlaceHolder1$ddlQ_YM": ym,
        "ctl00$ContentPlaceHolder1$rdo1": "rbComid",
        "ctl00$ContentPlaceHolder1$ddlQ_Comid": company_id,
    }
    return session.post(
        url,
        data=payload,
        headers=HEADERS,
        timeout=120,
        verify=False,
    ).text


def _sitca_query_company(session: requests.Session, url: str, ym: str, company_id: str) -> str:
    html = _sitca_select_company(session, url, ym, company_id)
    fields = _extract_form_fields(html)
    payload = {
        **fields,
        "ctl00$ContentPlaceHolder1$ddlQ_YM": ym,
        "ctl00$ContentPlaceHolder1$rdo1": "rbComid",
        "ctl00$ContentPlaceHolder1$ddlQ_Comid": company_id,
        "ctl00$ContentPlaceHolder1$BtnQuery": "查詢",
    }
    return session.post(
        url,
        data=payload,
        headers=HEADERS,
        timeout=180,
        verify=False,
    ).text


def _clean_cell(cell_html: str) -> str:
    return _TAG_RE.sub("", cell_html).strip()


def _looks_like_stock_id(text: str | None) -> bool:
    stock_id = normalize_stock_id(text)
    return bool(stock_id and re.fullmatch(r"\d{4}", stock_id))


def parse_sitca_fund_block(html: str, fund_name: str) -> list[dict]:
    marker = f">{fund_name}</td>"
    idx = html.find(marker)
    if idx < 0:
        return []

    tr_start = html.rfind("<tr", 0, idx)
    next_fund = html.find("rowspan='", idx + len(marker))
    chunk = html[tr_start:] if next_fund < 0 else html[tr_start:next_fund]

    rows: list[dict] = []
    rank_no = 0
    for row_html in _ROW_RE.findall(chunk):
        cells = [_clean_cell(cell) for cell in _CELL_RE.findall(row_html)]
        if len(cells) < 5:
            continue
        if cells[0] == "合計":
            continue

        rank_raw: str | None = None
        stock_id_raw: str | None = None
        stock_name: str | None = None
        amount_raw: str | None = None
        asset_type: str | None = None

        if cells[0] == fund_name and len(cells) >= 5 and not cells[1].isdigit():
            asset_type = cells[1]
            stock_id_raw = cells[2]
            stock_name = cells[3]
            amount_raw = cells[4]
        elif len(cells) >= 4 and not cells[0].isdigit() and _looks_like_stock_id(cells[1]):
            asset_type = cells[0]
            stock_id_raw = cells[1]
            stock_name = cells[2]
            amount_raw = cells[3]
        elif cells[0].isdigit():
            rank_raw = cells[0]
            stock_id_raw = cells[2] if len(cells) >= 4 else None
            stock_name = cells[3] if len(cells) >= 4 else None
            amount_raw = cells[4] if len(cells) >= 5 else None
            asset_type = cells[1] if len(cells) >= 2 else None
        elif len(cells) >= 6 and cells[1].isdigit() and not _looks_like_stock_id(cells[1]):
            rank_raw = cells[1]
            stock_id_raw = cells[3]
            stock_name = cells[4]
            amount_raw = cells[5]
            asset_type = cells[2]
        else:
            continue

        stock_id = normalize_stock_id(stock_id_raw)
        if not stock_id or not re.fullmatch(r"\d{4}", stock_id):
            continue

        if rank_raw is None:
            rank_no += 1
            rank_value = rank_no
        else:
            rank_value = int(rank_raw)

        amount = parse_number(amount_raw)
        weight_pct = parse_number(cells[-1])
        rows.append(
            {
                "rank_no": rank_value,
                "stock_id": stock_id,
                "stock_name": stock_name,
                "amount": amount,
                "weight_pct": weight_pct,
                "asset_type": asset_type,
                "shares": None,
            }
        )
    return rows


def fetch_moneydj_holdings(profile: FundProfile, session: requests.Session | None = None) -> tuple[str, list[dict], float | None]:
    sess = session or requests.Session()
    params = {"x": "wr04", "a": profile.moneydj_id}
    resp = sess.get(
        MONEYDJ_WR04_URL,
        params=params,
        headers=HEADERS,
        timeout=60,
        verify=False,
    )
    resp.raise_for_status()
    payload = resp.json()["ResultSet"]
    result = payload.get("Result") or []
    if not result:
        raise RuntimeError(f"MoneyDJ wr04 empty for {profile.fund_code}")

    snapshot_raw = str(result[0].get("V1", "")).strip()
    parts = snapshot_raw.split("/")
    if len(parts) == 3:
        snapshot_date = f"{parts[0]}-{parts[1]}-{parts[2]}"
    else:
        snapshot_date = snapshot_raw

    fund_size = parse_number(str(result[0].get("V7", "")).strip() or None)
    rows: list[dict] = []
    for idx, item in enumerate(result, start=1):
        stock_id = normalize_stock_id(str(item.get("V2", "")).strip())
        if not stock_id:
            continue
        shares_thousands = parse_number(str(item.get("V4", "")).strip() or None)
        rows.append(
            {
                "rank_no": idx,
                "stock_id": stock_id,
                "stock_name": str(item.get("V3", "")).strip() or None,
                "shares": shares_thousands * 1000 if shares_thousands is not None else None,
                "weight_pct": parse_number(str(item.get("V5", "")).strip() or None),
                "amount": None,
                "asset_type": "國內上市" if stock_id else None,
            }
        )
    return snapshot_date, rows, fund_size


def fetch_sitca_month(profile: FundProfile, ym: str, session: requests.Session) -> tuple[str, list[dict]]:
    html = _sitca_query_company(session, SITCA_MONTH_URL, ym, profile.sitca_company_id)
    rows = parse_sitca_fund_block(html, profile.fund_name)
    return month_end_from_ym(ym), rows


def fetch_sitca_quarter(profile: FundProfile, ym: str, session: requests.Session) -> tuple[str, list[dict]]:
    html = _sitca_query_company(session, SITCA_QUARTER_URL, ym, profile.sitca_company_id)
    rows = parse_sitca_fund_block(html, profile.fund_name)
    return month_end_from_ym(ym), rows


def _write_snapshot(
    conn,
    profile: FundProfile,
    snapshot_date: str,
    disclosure_type: str,
    rows: list[dict],
    *,
    source: str,
    fund_size_billion: float | None = None,
    source_edit_at: str | None = None,
) -> int:
    if not rows:
        return 0

    holding_rows = [
        {
            "fund_code": profile.fund_code,
            "snapshot_date": snapshot_date,
            "disclosure_type": disclosure_type,
            "stock_id": row["stock_id"],
            "stock_name": row.get("stock_name"),
            "rank_no": row.get("rank_no"),
            "shares": row.get("shares"),
            "weight_pct": row.get("weight_pct"),
            "amount": row.get("amount"),
            "asset_type": row.get("asset_type"),
            "source": source,
            "source_edit_at": source_edit_at or snapshot_date,
        }
        for row in rows
    ]
    upsert_mutual_fund_holdings_meta(
        conn,
        {
            "fund_code": profile.fund_code,
            "snapshot_date": snapshot_date,
            "fund_name": profile.fund_name,
            "disclosure_type": disclosure_type,
            "fund_size_billion": fund_size_billion,
            "holding_count": len(holding_rows),
            "source": source,
            "source_edit_at": source_edit_at or snapshot_date,
        },
    )
    return upsert_mutual_fund_holdings(conn, holding_rows)


def backfill_sitca(
    conn,
    profile: FundProfile,
    months: Iterable[str],
    *,
    sleep_s: float = 0.8,
    include_quarterly: bool = True,
) -> dict[str, int]:
    session = requests.Session()
    stats = {"monthly": 0, "quarterly": 0, "skipped": 0}
    for ym in months:
        snapshot_date = month_end_from_ym(ym)
        month = int(ym[4:6])

        _, month_rows = fetch_sitca_month(profile, ym, session)
        if month_rows:
            written = _write_snapshot(
                conn,
                profile,
                snapshot_date,
                DISCLOSURE_MONTHLY,
                month_rows,
                source="sitca_monthly_top10",
            )
            if written:
                stats["monthly"] += 1
                print(f"  {ym} monthly_top10: {written} holdings")
        else:
            stats["skipped"] += 1
            print(f"  {ym} monthly_top10: no data", file=sys.stderr)

        if include_quarterly and month in {3, 6, 9, 12}:
            _, quarter_rows = fetch_sitca_quarter(profile, ym, session)
            if quarter_rows:
                written = _write_snapshot(
                    conn,
                    profile,
                    snapshot_date,
                    DISCLOSURE_QUARTERLY,
                    quarter_rows,
                    source="sitca_quarterly_full",
                )
                if written:
                    stats["quarterly"] += 1
                    print(f"  {ym} quarterly_full: {written} holdings")
            else:
                print(f"  {ym} quarterly_full: no data", file=sys.stderr)

        time.sleep(sleep_s)
    return stats


def sync_latest_moneydj(conn, profile: FundProfile) -> int:
    snapshot_date, rows, fund_size = fetch_moneydj_holdings(profile)
    written = _write_snapshot(
        conn,
        profile,
        snapshot_date,
        DISCLOSURE_MONTHLY,
        rows,
        source="moneydj_wr04",
        fund_size_billion=fund_size,
        source_edit_at=snapshot_date,
    )
    print(f"MoneyDJ latest {snapshot_date}: {written} holdings")
    return written


def default_backfill_start(years: int = 2) -> date:
    today = date.today()
    start_year = today.year - years
    return date(start_year, today.month, 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync domestic mutual fund holdings to SQLite")
    parser.add_argument("--fund", default=ALLIANZ_TW_TECH.fund_code, help="Fund code (default: ACDD04)")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH))
    parser.add_argument("--years", type=int, default=2, help="Backfill horizon in years")
    parser.add_argument("--start-ym", type=str, help="Backfill start YYYYMM")
    parser.add_argument("--end-ym", type=str, help="Backfill end YYYYMM")
    parser.add_argument("--latest-only", action="store_true", help="Only fetch MoneyDJ latest snapshot")
    parser.add_argument("--backfill-only", action="store_true", help="Only backfill SITCA history")
    parser.add_argument("--no-quarterly", action="store_true", help="Skip quarterly full holdings")
    parser.add_argument("--sleep", type=float, default=0.8, help="Pause between SITCA requests (seconds)")
    return parser.parse_args(argv)


def resolve_profile(fund_code: str) -> FundProfile:
    code = fund_code.upper()
    if code == ALLIANZ_TW_TECH.fund_code:
        return ALLIANZ_TW_TECH
    raise ValueError(f"Unsupported fund code {fund_code!r}. Known: {ALLIANZ_TW_TECH.fund_code}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = resolve_profile(args.fund)
    conn = connect(Path(args.db))

    if not args.backfill_only:
        sync_latest_moneydj(conn, profile)

    if not args.latest_only:
        if args.start_ym:
            start = date(int(args.start_ym[:4]), int(args.start_ym[4:6]), 1)
        else:
            start = default_backfill_start(args.years)
        if args.end_ym:
            end = date(int(args.end_ym[:4]), int(args.end_ym[4:6]), 1)
        else:
            end = date.today().replace(day=1)
        months = ym_values(start, end)
        print(
            f"Backfill {profile.fund_name} ({profile.fund_code}) "
            f"{months[0]}..{months[-1]} ({len(months)} months)"
        )
        stats = backfill_sitca(
            conn,
            profile,
            months,
            sleep_s=args.sleep,
            include_quarterly=not args.no_quarterly,
        )
        print(f"SITCA done: monthly={stats['monthly']} quarterly={stats['quarterly']} skipped={stats['skipped']}")

    dates = list_mutual_fund_snapshot_dates(conn, profile.fund_code)
    print(f"DB snapshots: {len(dates)} — latest {dates[0] if dates else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
