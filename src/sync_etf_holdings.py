#!/usr/bin/env python3
"""
從投信官網或 EZMoney 同步 ETF 每日持股至 SQLite。

來源：EZMoney（統一）、凱基官網、群益 CFWeb、野村 ETFAPI。
官網僅提供最新快照；每日執行可累積歷史，供 share_delta 加減碼分析。
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import requests
import urllib3

from holdings_research import (
    format_research_suffix,
    load_implied_closes,
    print_cross_etf_consensus,
    print_cross_etf_flow_intent_report,
    print_sync_baseline_header,
)
from stock_db import (
    DATA_DIR,
    DEFAULT_DB_PATH,
    compute_etf_holdings_changes,
    connect,
    list_etf_snapshot_dates,
    load_etf_holdings,
    load_etf_holdings_meta,
    load_stock_beta_map,
    upsert_etf_holdings,
    upsert_etf_holdings_meta,
)

_BETA_ACTIONS = frozenset({"新进", "加码"})
_TRANSIENT_HTTP_STATUS = frozenset({429, 502, 503, 504})
_FETCH_RETRY_ATTEMPTS = 3
_FETCH_RETRY_BACKOFF_S = 2.0
_CACHE_FALLBACK_MAX_AGE_DAYS = 7


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


EZMONEY_BASE_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info"
EZMONEY_FUND_MAP: dict[str, str] = {
    "00981A": "49YTW",
    "00403A": "63YTW",
}

KGIFUND_BASE_URL = "https://www.kgifund.com.tw/Fund/Detail"
KGIFUND_FUND_MAP: dict[str, str] = {
    "009816": "J023",
    # 00407A 掛牌後從官網連結確認 fundID 並填入
}

# 群益投信 CFWeb product detail fundId（非上市代號）
CAPITALFUND_FUND_MAP: dict[str, str] = {
    "00982A": "399",
    "00992A": "500",
}
CAPITALFUND_BUYBACK_URL = "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback"

# 野村 ETFAPI FundID（與上市代號相同）
NOMURA_FUND_MAP: dict[str, str] = {
    "00980A": "00980A",
}
NOMURA_ASSETS_URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"

MIN_HOLDINGS_COUNT = 40

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

CAPITALFUND_HEADERS = {
    "User-Agent": EZMONEY_HEADERS["User-Agent"],
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

NOMURA_HEADERS = {
    "User-Agent": EZMONEY_HEADERS["User-Agent"],
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

TW_STOCK_ID = re.compile(r"^\d{4}$")


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


def capitalfund_id_for(etf_code: str) -> str:
    fund_id = CAPITALFUND_FUND_MAP.get(etf_code.upper())
    if not fund_id:
        known = ", ".join(sorted(CAPITALFUND_FUND_MAP))
        raise ValueError(f"Unknown CapitalFund ETF code {etf_code!r}. Known: {known}")
    return fund_id


def _post_with_retry(
    session: requests.Session,
    url: str,
    *,
    max_attempts: int = _FETCH_RETRY_ATTEMPTS,
    backoff_s: float = _FETCH_RETRY_BACKOFF_S,
    **kwargs,
) -> requests.Response:
    last_response: requests.Response | None = None
    for attempt in range(max_attempts):
        response = session.post(url, **kwargs)
        last_response = response
        if response.status_code in _TRANSIENT_HTTP_STATUS and attempt + 1 < max_attempts:
            time.sleep(backoff_s * (2**attempt))
            continue
        response.raise_for_status()
        return response
    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError(f"POST {url} failed after {max_attempts} attempts")


def _cached_snapshot_fallback(conn, etf_code: str) -> tuple[str, dict] | None:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if not dates:
        return None
    latest = dates[0]
    try:
        snap_date = date.fromisoformat(latest)
    except ValueError:
        return None
    if (date.today() - snap_date).days > _CACHE_FALLBACK_MAX_AGE_DAYS:
        return None
    meta = load_etf_holdings_meta(conn, etf_code, latest)
    if meta is None:
        return None
    return latest, dict(meta)


def nomura_fund_id_for(etf_code: str) -> str:
    fund_id = NOMURA_FUND_MAP.get(etf_code.upper())
    if not fund_id:
        known = ", ".join(sorted(NOMURA_FUND_MAP))
        raise ValueError(f"Unknown Nomura ETF code {etf_code!r}. Known: {known}")
    return fund_id


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


def _normalize_snapshot_date(raw: str) -> str:
    """YYYY-MM-DD from YYYY/MM/DD, YYYY-MM-DD, or ISO-ish prefixes."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty snapshot date")
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    if " " in raw:
        raw = raw.split(" ", 1)[0]
    return _normalize_kgifund_date(raw)


def _capitalfund_referer(fund_id: str) -> str:
    return f"https://www.capitalfund.com.tw/etf/product/detail/{fund_id}/portfolio"


def _nomura_referer(fund_id: str) -> str:
    return (
        f"https://www.nomurafunds.com.tw/ETFWEB/product-description?fundNo={fund_id}"
    )


def _parse_capitalfund_payload(
    etf_code: str, fund_id: str, payload: dict
) -> EtfHoldingsSnapshot:
    data = payload.get("data") or {}
    pcf = data.get("pcf") or {}
    raw_stocks = data.get("stocks") or []
    if not raw_stocks:
        raise NotListedError(f"{etf_code} 群益 API 尚無持股 (fundId={fund_id})")

    raw_date = pcf.get("date1") or pcf.get("date2") or ""
    snapshot_date = _normalize_snapshot_date(raw_date)
    source_edit_at = snapshot_date

    nav: float | None = None
    if pcf.get("pUnit") is not None:
        nav = float(pcf["pUnit"])
    elif pcf.get("pUnitFormat"):
        nav = float(str(pcf["pUnitFormat"]).replace(",", ""))

    holdings: list[dict] = []
    for row in raw_stocks:
        stock_id = str(row.get("stocNo", "")).strip()
        if not TW_STOCK_ID.match(stock_id):
            continue
        holdings.append(
            {
                "stock_id": stock_id,
                "stock_name": str(row.get("stocName", "")).strip(),
                "shares": float(row.get("share") or 0),
                "amount": None,
                "weight_pct": float(row.get("weightRound") or row.get("weight") or 0),
                "edit_time": source_edit_at,
            }
        )

    if len(holdings) < MIN_HOLDINGS_COUNT:
        raise RuntimeError(
            f"CapitalFund {etf_code} only {len(holdings)} TW stocks "
            f"(need >={MIN_HOLDINGS_COUNT}); incomplete portfolio?"
        )

    holdings.sort(key=lambda item: item["stock_id"])
    return EtfHoldingsSnapshot(
        etf_code=etf_code.upper(),
        fund_code=fund_id,
        snapshot_date=snapshot_date,
        source_edit_at=source_edit_at,
        nav=nav,
        holdings=holdings,
    )


def fetch_capitalfund_snapshot(
    etf_code: str, session: requests.Session | None = None
) -> EtfHoldingsSnapshot:
    fund_id = capitalfund_id_for(etf_code)
    sess = session or requests.Session()
    headers = {
        **CAPITALFUND_HEADERS,
        "Referer": _capitalfund_referer(fund_id),
    }
    response = sess.post(
        CAPITALFUND_BUYBACK_URL,
        json={"fundId": fund_id, "date": None},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 200:
        raise RuntimeError(
            f"CapitalFund buyback API error for {etf_code}: {payload.get('message')}"
        )
    return _parse_capitalfund_payload(etf_code, fund_id, payload)


def _parse_nomura_payload(
    etf_code: str, fund_id: str, payload: dict
) -> EtfHoldingsSnapshot:
    entries = payload.get("Entries") or {}
    if not isinstance(entries, dict):
        raise RuntimeError(f"Unexpected Nomura Entries for {etf_code}")

    data = entries.get("Data") or {}
    fund_asset = data.get("FundAsset") or {}
    raw_date = fund_asset.get("NavDate") or ""
    snapshot_date = _normalize_snapshot_date(raw_date)
    source_edit_at = snapshot_date

    nav: float | None = None
    if fund_asset.get("Nav"):
        nav = float(str(fund_asset["Nav"]).replace(",", ""))

    stock_table: dict | None = None
    for table in data.get("Table") or []:
        if table.get("TableTitle") == "股票":
            stock_table = table
            break
    if stock_table is None:
        raise NotListedError(f"{etf_code} 野村 API 尚無股票持股表 (FundID={fund_id})")

    holdings: list[dict] = []
    for row in stock_table.get("Rows") or []:
        if not row or len(row) < 4:
            continue
        stock_id = str(row[0]).strip()
        if not TW_STOCK_ID.match(stock_id):
            continue
        shares_raw = str(row[2]).replace(",", "")
        weight_raw = str(row[3]).replace("%", "").strip()
        holdings.append(
            {
                "stock_id": stock_id,
                "stock_name": str(row[1]).strip(),
                "shares": float(shares_raw),
                "amount": None,
                "weight_pct": float(weight_raw),
                "edit_time": source_edit_at,
            }
        )

    if len(holdings) < MIN_HOLDINGS_COUNT:
        raise RuntimeError(
            f"Nomura {etf_code} only {len(holdings)} TW stocks "
            f"(need >={MIN_HOLDINGS_COUNT}); incomplete portfolio?"
        )

    holdings.sort(key=lambda item: item["stock_id"])
    return EtfHoldingsSnapshot(
        etf_code=etf_code.upper(),
        fund_code=fund_id,
        snapshot_date=snapshot_date,
        source_edit_at=source_edit_at,
        nav=nav,
        holdings=holdings,
    )


def fetch_nomura_snapshot(
    etf_code: str, session: requests.Session | None = None
) -> EtfHoldingsSnapshot:
    fund_id = nomura_fund_id_for(etf_code)
    sess = session or requests.Session()
    headers = {**NOMURA_HEADERS, "Referer": _nomura_referer(fund_id)}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        response = _post_with_retry(
            sess,
            NOMURA_ASSETS_URL,
            json={"FundID": fund_id, "SearchDate": None},
            headers=headers,
            timeout=30,
            verify=False,
        )
    payload = response.json()
    if payload.get("StatusCode") not in (None, 0, 200) and payload.get("Message"):
        raise RuntimeError(f"Nomura API error for {etf_code}: {payload.get('Message')}")
    return _parse_nomura_payload(etf_code, fund_id, payload)


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
    if source == "capitalfund":
        return fetch_capitalfund_snapshot(etf_code, session=session)
    if source == "nomura":
        return fetch_nomura_snapshot(etf_code, session=session)
    if etf_code.upper() in KGIFUND_FUND_MAP:
        return fetch_kgifund_snapshot(etf_code, session=session)
    if etf_code.upper() in CAPITALFUND_FUND_MAP:
        return fetch_capitalfund_snapshot(etf_code, session=session)
    if etf_code.upper() in NOMURA_FUND_MAP:
        return fetch_nomura_snapshot(etf_code, session=session)
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
        if explicit == "capitalfund" and code not in CAPITALFUND_FUND_MAP:
            raise ValueError(f"{code} 不在 CAPITALFUND_FUND_MAP")
        if explicit == "nomura" and code not in NOMURA_FUND_MAP:
            raise ValueError(f"{code} 不在 NOMURA_FUND_MAP")
        return explicit
    if code in KGIFUND_FUND_MAP:
        return "kgifund"
    if code in CAPITALFUND_FUND_MAP:
        return "capitalfund"
    if code in NOMURA_FUND_MAP:
        return "nomura"
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


def _sync_status_line(etf_code: str, result: dict) -> str:
    date_s = result["snapshot_date"]
    nav = result.get("nav")
    if result.get("skipped"):
        return f"  {etf_code}: skip unchanged @ {date_s} (NAV={nav})"
    return (
        f"  {etf_code}: synced {result['holding_count']} holdings @ {date_s} (NAV={nav})"
    )


def print_changes(conn, etf_code: str, curr_date: str | None, prev_date: str | None) -> None:
    dates = list_etf_snapshot_dates(conn, etf_code)
    if len(dates) < 2 and curr_date is None:
        print(f"Need at least 2 snapshot dates for {etf_code}; have {len(dates)}")
        return

    curr = curr_date or dates[0]
    prev = prev_date
    if prev is None:
        prev = dates[1] if dates[0] == curr else dates[0]

    rows = compute_etf_holdings_changes(conn, etf_code, curr, prev)
    beta_map, beta_as_of = load_stock_beta_map(conn)
    print(f"{etf_code} changes: {prev} -> {curr}")
    if beta_as_of:
        print(f"  stock_beta as_of={beta_as_of} ({len(beta_map)} stocks)")
    changed = [row for row in rows if row["action"] != "不变"]
    if not changed:
        print("  (no share changes)")
        return

    stock_ids = [row["stock_id"] for row in changed]
    print(f"  研究欄位：grow=持股變化率；flow=Δ股×單價（持股 amount/shares，錨點 {prev}）")
    close_map = load_implied_closes(conn, stock_ids, prev, curr)

    for row in changed:
        wt_prev = row["weight_pct_prev"]
        wt_curr = row["weight_pct_curr"]
        wt_delta = row["weight_delta"]
        if row["action"] == "新进":
            w_curr = wt_curr if wt_curr is not None else 0.0
            wt_line = f"wt —→{_fmt_pct(wt_curr)} (+{w_curr:.2f}pp)"
        elif row["action"] == "出清":
            w_prev = wt_prev if wt_prev is not None else 0.0
            wt_line = f"wt {_fmt_pct(wt_prev)}→— ({-w_prev:.2f}pp)"
        else:
            delta_pp = wt_delta if wt_delta is not None else 0.0
            wt_line = (
                f"wt {_fmt_pct(wt_prev)}→{_fmt_pct(wt_curr)} "
                f"({delta_pp:+.2f}pp)"
            )

        line = (
            f"  {row['stock_id']:>6} {row['stock_name']:<8} "
            f"{row['action']} {wt_line} "
            f"sh={row['share_delta']:>+12,.0f} "
            f"({row['shares_prev'] or 0:,.0f}->{row['shares_curr'] or 0:,.0f})"
        )
        line += format_research_suffix(row, close_map.get(row["stock_id"]))
        if row["action"] in _BETA_ACTIONS:
            beta_row = beta_map.get(row["stock_id"])
            if beta_row is not None and beta_row["beta"] is not None:
                line += f" beta={beta_row['beta']:.2f}"
        print(line)


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
    except requests.RequestException as exc:
        conn = connect(args.db_path)
        cached = _cached_snapshot_fallback(conn, etf_code)
        if cached is None:
            raise
        latest, meta = cached
        nav = meta.get("nav")
        print(
            f"  SKIP {etf_code}: 官網暫時不可用 ({exc})，"
            f"沿用 DB @ {latest} (NAV={nav})"
        )
        return 0

    if not args.quiet:
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
    from holdings_provenance import record_holdings_fetch

    fetch_status = "skipped_unchanged" if result.get("skipped") else "synced"
    record_holdings_fetch(
        conn,
        etf_code=snapshot.etf_code,
        snapshot_date=snapshot.snapshot_date,
        source=resolved,
        source_edit_at=snapshot.source_edit_at,
        nav=snapshot.nav,
        holdings=snapshot.holdings,
        sync_status=fetch_status,
    )
    if args.quiet:
        print(_sync_status_line(etf_code, result))
    elif result.get("skipped"):
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
        csv_path = DATA_DIR / etf_code / f"holdings_{snapshot.snapshot_date}.csv"
        export_holdings_csv(snapshot, csv_path)
        print(f"Exported CSV: {csv_path}")

    dates = list_etf_snapshot_dates(conn, etf_code)
    if len(dates) >= 2 and not args.changes and not args.no_auto_changes:
        print_changes(conn, etf_code, result["snapshot_date"], None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync ETF holdings to SQLite (EZMoney / KGIFund / CapitalFund / Nomura)"
    )
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
        choices=("ezmoney", "kgifund", "capitalfund", "nomura"),
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
    parser.add_argument(
        "--no-auto-changes",
        action="store_true",
        help="同步後不自動印 changes（daily_sync 改由最後 --changes 統一輸出）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="同步時一行摘要；不影響 --changes 詳細輸出",
    )
    parser.add_argument(
        "--intent",
        action="store_true",
        help="--changes 時額外輸出跨 ETF 對齊日之 L3–L5 部位意圖與註解",
    )
    parser.add_argument(
        "--intent-debug",
        action="store_true",
        help="部位意圖附 [TAG] 列與 rank / conviction / Δwt 除錯列",
    )
    parser.add_argument(
        "--universe",
        action="store_true",
        default=True,
        help="--changes 且含 --intent 時輸出 Research Universe（預設開）",
    )
    parser.add_argument(
        "--no-universe",
        action="store_false",
        dest="universe",
        help="關閉 Research Universe 區塊",
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="收盤 digest 模式：--changes 不印終端詳表（仍可比對 DB）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    codes = parse_etf_codes(args.etf_code, args.etf_codes)

    if args.list or args.changes:
        conn = connect(args.db_path)
        code_tuple = tuple(codes)
        if args.human and args.changes:
            return 0
        if args.changes:
            print_sync_baseline_header(conn, code_tuple)
        for etf_code in codes:
            if args.list:
                print_holdings(conn, etf_code, args.date)
            if args.changes:
                print_changes(conn, etf_code, args.date, args.prev_date)
        if args.changes and len(codes) > 1:
            if args.intent or args.intent_debug:
                print_cross_etf_flow_intent_report(
                    conn, code_tuple, debug=args.intent_debug
                )
            else:
                print_cross_etf_consensus(conn, code_tuple)
            if args.universe and (args.intent or args.intent_debug):
                from research_universe import print_research_universe_report

                uni = print_research_universe_report(conn, code_tuple)
                if uni is not None:
                    from execution_context_report import print_execution_context_report

                    print_execution_context_report(
                        conn, code_tuple, universe=uni
                    )
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
