"""Import ETF daily holdings from etfedge MCP into SQLite."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from etfedge_mcp_client import EtfedgeMcpClient, EtfedgeMcpError
from stock_db import DATA_DIR, upsert_etf_holdings, upsert_etf_holdings_meta

DEFAULT_LISTING_DATE = "2025-05-27"
SOURCE = "etfedge"
DEFAULT_CACHE_DIR = DATA_DIR / "etfedge_cache"


def _fill_missing_weight_pct_from_shares(rows: list[dict]) -> None:
    """缺 close 時以股數占比估算 weight_pct，避免 NULL 進 downstream。"""
    missing = [r for r in rows if r.get("weight_pct") is None and float(r.get("shares") or 0) > 0]
    if not missing:
        return
    total_sh = sum(float(r["shares"] or 0) for r in rows if float(r.get("shares") or 0) > 0)
    if total_sh <= 0:
        return
    for r in missing:
        r["weight_pct"] = float(r["shares"]) / total_sh * 100.0


@dataclass
class ImportPlan:
    etf_code: str
    stock_codes: list[str]
    stock_names: dict[str, str]
    start_date: str
    end_date: str
    history_days: int
    mcp_calls: int


@dataclass
class ImportResult:
    etf_code: str
    dates_imported: int
    rows_written: int
    date_range: tuple[str, str] | None
    skipped_sparse_dates: int
    mcp_calls: int
    cache_hits: int = 0
    api_fetches: int = 0


def cache_dir_for(etf_code: str, root: Path | None = None) -> Path:
    return (root or DEFAULT_CACHE_DIR) / etf_code.upper()


def _plan_cache_path(etf_code: str, root: Path | None = None) -> Path:
    return cache_dir_for(etf_code, root) / "plan.json"


def _stock_cache_path(etf_code: str, stock_code: str, root: Path | None = None) -> Path:
    return cache_dir_for(etf_code, root) / "stocks" / f"{stock_code}.json"


def save_plan_cache(plan: ImportPlan, root: Path | None = None) -> Path:
    path = _plan_cache_path(plan.etf_code, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "etf_code": plan.etf_code,
                "stock_codes": plan.stock_codes,
                "stock_names": plan.stock_names,
                "start_date": plan.start_date,
                "end_date": plan.end_date,
                "history_days": plan.history_days,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_plan_cache(etf_code: str, root: Path | None = None) -> ImportPlan | None:
    path = _plan_cache_path(etf_code, root)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    stock_codes = list(raw.get("stock_codes") or [])
    return ImportPlan(
        etf_code=str(raw.get("etf_code") or etf_code).upper(),
        stock_codes=stock_codes,
        stock_names={str(k): str(v) for k, v in (raw.get("stock_names") or {}).items()},
        start_date=str(raw.get("start_date") or DEFAULT_LISTING_DATE),
        end_date=str(raw.get("end_date") or ""),
        history_days=int(raw.get("history_days") or 365),
        mcp_calls=2 + len(stock_codes),
    )


def save_stock_cache(
    etf_code: str,
    stock_code: str,
    rows: list[dict[str, Any]],
    *,
    root: Path | None = None,
) -> Path:
    path = _stock_cache_path(etf_code, stock_code, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def load_stock_cache(
    etf_code: str,
    stock_code: str,
    *,
    root: Path | None = None,
) -> list[dict[str, Any]] | None:
    path = _stock_cache_path(etf_code, stock_code, root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return None
    return payload


def discover_stock_universe(
    client: EtfedgeMcpClient,
    etf_code: str,
    *,
    start_date: str = DEFAULT_LISTING_DATE,
) -> tuple[dict[str, str], str]:
    """Return (stock_code -> name, latest_as_of) from holdings + buy-delta scan."""
    names: dict[str, str] = {}
    holdings = client.get_etf_holdings(etf_code)
    as_of = str(holdings.get("as_of") or "")
    for row in holdings.get("holdings") or []:
        code = str(row.get("stock_code") or "").strip()
        if not code:
            continue
        names[code] = str(row.get("stock_name") or code)

    if not as_of:
        raise RuntimeError(f"get_etf_holdings returned no as_of for {etf_code}")

    delta = client.get_etf_buy_delta(etf_code, start_date, as_of)
    for row in delta.get("deltas") or []:
        code = str(row.get("stock_code") or "").strip()
        if not code:
            continue
        names.setdefault(code, str(row.get("stock_name") or code))
    return names, as_of


def build_import_plan(
    client: EtfedgeMcpClient,
    etf_code: str,
    *,
    start_date: str = DEFAULT_LISTING_DATE,
    history_days: int = 365,
) -> ImportPlan:
    names, as_of = discover_stock_universe(client, etf_code, start_date=start_date)
    stock_codes = sorted(names)
    mcp_calls = 2 + len(stock_codes)
    return ImportPlan(
        etf_code=etf_code,
        stock_codes=stock_codes,
        stock_names=names,
        start_date=start_date,
        end_date=as_of,
        history_days=history_days,
        mcp_calls=mcp_calls,
    )


def build_snapshots_from_histories(
    etf_code: str,
    stock_histories: dict[str, list[dict[str, Any]]],
    stock_names: dict[str, str],
    *,
    min_holdings: int = 20,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[dict[str, list[dict]], int]:
    """Group per-stock history into daily portfolio snapshots."""
    by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for stock_code, rows in stock_histories.items():
        for row in rows:
            snap_date = str(row.get("trade_date") or "")[:10]
            if not snap_date:
                continue
            if start_date and snap_date < start_date:
                continue
            if end_date and snap_date > end_date:
                continue
            shares = row.get("share_count")
            if shares is None:
                continue
            shares_f = float(shares)
            if shares_f <= 0:
                continue
            close = row.get("close")
            close_f = float(close) if close is not None else None
            by_date[snap_date][stock_code] = {
                "stock_id": stock_code,
                "stock_name": stock_names.get(stock_code, stock_code),
                "shares": shares_f,
                "close": close_f,
            }

    snapshots: dict[str, list[dict]] = {}
    skipped_sparse = 0
    for snap_date in sorted(by_date):
        stocks = list(by_date[snap_date].values())
        if len(stocks) < min_holdings:
            skipped_sparse += 1
            continue
        total_value = sum(
            s["shares"] * s["close"] for s in stocks if s.get("close") is not None
        )
        rows: list[dict] = []
        for s in sorted(stocks, key=lambda x: x["stock_id"]):
            amount = None
            weight_pct = None
            if s.get("close") is not None:
                amount = s["shares"] * s["close"]
                if total_value > 0:
                    weight_pct = amount / total_value * 100.0
            rows.append(
                {
                    "etf_code": etf_code,
                    "snapshot_date": snap_date,
                    "stock_id": s["stock_id"],
                    "stock_name": s["stock_name"],
                    "shares": s["shares"],
                    "weight_pct": weight_pct,
                    "amount": amount,
                    "source": SOURCE,
                    "source_edit_at": snap_date,
                }
            )
        _fill_missing_weight_pct_from_shares(rows)
        snapshots[snap_date] = rows
    return snapshots, skipped_sparse


def fetch_stock_histories(
    client: EtfedgeMcpClient,
    etf_code: str,
    stock_codes: list[str],
    *,
    history_days: int = 365,
    cache_root: Path | None = None,
    use_cache: bool = True,
    cache_only: bool = False,
    allow_partial_cache: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    histories: dict[str, list[dict[str, Any]]] = {}
    cache_hits = 0
    api_fetches = 0
    missing: list[str] = []
    for code in stock_codes:
        cached = load_stock_cache(etf_code, code, root=cache_root) if use_cache else None
        if cached is not None:
            histories[code] = cached
            cache_hits += 1
            continue
        if cache_only:
            missing.append(code)
            continue
        try:
            rows = client.get_stock_history(etf_code, code, days=history_days)
        except EtfedgeMcpError:
            if use_cache:
                missing.append(code)
            raise
        histories[code] = rows
        api_fetches += 1
        if use_cache:
            save_stock_cache(etf_code, code, rows, root=cache_root)
    if missing and not allow_partial_cache:
        raise EtfedgeMcpError(
            f"cache missing {len(missing)} stocks (e.g. {missing[:3]}). "
            "Re-run without --cache-only after quota resets, use --allow-partial-cache, "
            "or finish fetching first."
        )
    if missing and allow_partial_cache:
        pass  # proceed with partial histories
    return histories, cache_hits, api_fetches


def write_snapshots(
    conn: sqlite3.Connection,
    snapshots: dict[str, list[dict]],
    *,
    prefer_existing_source: bool = True,
) -> tuple[int, int]:
    """Upsert snapshots. Skip dates already from ezmoney when prefer_existing_source."""
    dates_written = 0
    rows_written = 0
    for snap_date, rows in sorted(snapshots.items()):
        if prefer_existing_source:
            meta = conn.execute(
                "SELECT source FROM etf_holdings_meta WHERE etf_code=? AND snapshot_date=?",
                (rows[0]["etf_code"], snap_date),
            ).fetchone()
            if meta and meta[0] != SOURCE:
                continue
        etf_code = rows[0]["etf_code"]
        upsert_etf_holdings_meta(
            conn,
            {
                "etf_code": etf_code,
                "snapshot_date": snap_date,
                "nav": None,
                "holding_count": len(rows),
                "source": SOURCE,
                "source_edit_at": snap_date,
            },
        )
        rows_written += upsert_etf_holdings(conn, rows)
        dates_written += 1
    return dates_written, rows_written


def import_etf_holdings_from_etfedge(
    conn: sqlite3.Connection,
    client: EtfedgeMcpClient,
    etf_code: str,
    *,
    start_date: str = DEFAULT_LISTING_DATE,
    history_days: int = 365,
    min_holdings: int = 20,
    prefer_existing_source: bool = True,
    dry_run: bool = False,
    cache_root: Path | None = None,
    use_cache: bool = True,
    cache_only: bool = False,
    allow_partial_cache: bool = False,
) -> ImportResult:
    if not cache_only:
        client.connect()
        plan = build_import_plan(
            client, etf_code, start_date=start_date, history_days=history_days
        )
        if use_cache:
            save_plan_cache(plan, cache_root)
    else:
        plan = load_plan_cache(etf_code, cache_root)
        if plan is None:
            raise EtfedgeMcpError(
                f"no cached plan for {etf_code}; run once without --cache-only first"
            )

    histories, cache_hits, api_fetches = fetch_stock_histories(
        client,
        etf_code,
        plan.stock_codes,
        history_days=history_days,
        cache_root=cache_root,
        use_cache=use_cache,
        cache_only=cache_only,
        allow_partial_cache=allow_partial_cache,
    )
    snapshots, skipped_sparse = build_snapshots_from_histories(
        etf_code,
        histories,
        plan.stock_names,
        min_holdings=min_holdings,
        start_date=start_date,
        end_date=plan.end_date,
    )
    if dry_run:
        date_range = None
        if snapshots:
            dates = sorted(snapshots)
            date_range = (dates[0], dates[-1])
        return ImportResult(
            etf_code=etf_code,
            dates_imported=len(snapshots),
            rows_written=sum(len(v) for v in snapshots.values()),
            date_range=date_range,
            skipped_sparse_dates=skipped_sparse,
            mcp_calls=plan.mcp_calls,
            cache_hits=cache_hits,
            api_fetches=api_fetches,
        )

    dates_written, rows_written = write_snapshots(
        conn,
        snapshots,
        prefer_existing_source=prefer_existing_source,
    )
    date_range = None
    if snapshots:
        dates = sorted(snapshots)
        date_range = (dates[0], dates[-1])
    return ImportResult(
        etf_code=etf_code,
        dates_imported=dates_written,
        rows_written=rows_written,
        date_range=date_range,
        skipped_sparse_dates=skipped_sparse,
        mcp_calls=plan.mcp_calls,
        cache_hits=cache_hits,
        api_fetches=api_fetches,
    )


def listing_date_or_default(etf_code: str) -> str:
    if etf_code.upper() == "00981A":
        return DEFAULT_LISTING_DATE
    return date.today().replace(year=date.today().year - 1).isoformat()
