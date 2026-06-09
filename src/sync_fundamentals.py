#!/usr/bin/env python3
"""
L8 / L8.5 基本面批次同步（FinMind）→ stock_fundamental、stock_consensus、stock_financial_history。

Universe：ETF 最新持股聯集。建議週跑（weekly_sync.sh），勿併入每日 score 路徑。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_etf_constituent_watchlist,
    upsert_stock_consensus,
    upsert_stock_financial_history,
    upsert_stock_fundamental,
)
from sync_etf_signal import SOURCE, fetch_finmind

FIN_LOOKBACK_DAYS = 800
REQUEST_DELAY_SEC = 0.4

FIN_TYPES_EPS = frozenset({"EPS"})
FIN_TYPES_REV = frozenset({"Revenue"})
FIN_TYPES_NI = frozenset({"IncomeFromContinuingOperations"})
FIN_TYPES_EQ = frozenset({"EquityAttributableToOwnersOfParent"})


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_per_rows(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    latest = max(rows, key=lambda r: str(r["date"])[:10])
    d = str(latest["date"])[:10]
    return {
        "as_of_date": d,
        "pe": _float(latest.get("PER") or latest.get("pe")),
        "pb": _float(latest.get("PBR") or latest.get("pb")),
        "dividend_yield": _float(latest.get("dividend_yield")),
    }


def parse_revenue_rows(rows: list[dict]) -> tuple[list[dict], dict | None]:
    """月營收序列 + 最新 YoY / 加速度（百分點）。"""
    hist: list[dict] = []
    by_ym: dict[tuple[int, int], float] = {}
    for row in rows:
        y = int(row["revenue_year"])
        m = int(row["revenue_month"])
        rev = _float(row.get("revenue"))
        if rev is None:
            continue
        pd = f"{y:04d}-{m:02d}-01"
        hist.append(
            {
                "period_date": pd,
                "period_type": "month",
                "metric": "revenue",
                "value": rev,
                "source": SOURCE,
            }
        )
        by_ym[(y, m)] = rev

    if not by_ym:
        return hist, None

    latest_ym = max(by_ym.keys())
    y, m = latest_ym
    prev_y_key = (y - 1, m)
    if prev_y_key not in by_ym:
        return hist, None

    yoy = (by_ym[latest_ym] / by_ym[prev_y_key] - 1.0) * 100.0
    accel = None
    months_sorted = sorted(by_ym.keys())
    if len(months_sorted) >= 2:
        prev_ym = months_sorted[-2]
        py, pm = prev_ym
        prev_yoy_key = (py - 1, pm)
        if prev_yoy_key in by_ym:
            prev_yoy = (by_ym[prev_ym] / by_ym[prev_yoy_key] - 1.0) * 100.0
            accel = yoy - prev_yoy

    return hist, {
        "revenue_yoy_pct": round(yoy, 2),
        "revenue_mom_accel_pp": round(accel, 2) if accel is not None else None,
    }


def parse_financial_rows(rows: list[dict]) -> tuple[list[dict], dict, list[float]]:
    """季報序列 + 衍生共識/實際指標。"""
    hist: list[dict] = []
    by_q: dict[str, dict[str, float]] = {}

    for row in rows:
        t = str(row.get("type", ""))
        if t not in FIN_TYPES_EPS | FIN_TYPES_REV | FIN_TYPES_NI | FIN_TYPES_EQ:
            continue
        qd = str(row["date"])[:10]
        val = _float(row.get("value"))
        if val is None:
            continue
        metric = t.lower() if t in FIN_TYPES_EPS | FIN_TYPES_REV else t
        if t in FIN_TYPES_NI:
            metric = "net_income"
        if t in FIN_TYPES_EQ:
            metric = "equity"
        hist.append(
            {
                "period_date": qd,
                "period_type": "quarter",
                "metric": metric,
                "value": val,
                "source": SOURCE,
            }
        )
        by_q.setdefault(qd, {})[metric] = val

    roe_by_q: list[tuple[str, float]] = []
    eps_by_q: list[tuple[str, float]] = []
    for qd in sorted(by_q.keys()):
        bucket = by_q[qd]
        if "eps" in bucket:
            eps_by_q.append((qd, bucket["eps"]))
        ni = bucket.get("net_income")
        eq = bucket.get("equity")
        if ni is not None and eq is not None and eq > 0:
            roe_by_q.append((qd, ni / eq * 100.0))

    derived: dict = {}
    if eps_by_q:
        latest_q, latest_eps = eps_by_q[-1]
        derived["eps_latest_q"] = latest_eps
        if len(eps_by_q) >= 5:
            derived["eps_ttm"] = sum(v for _, v in eps_by_q[-4:])
        prior_year_q = f"{int(latest_q[:4]) - 1:04d}{latest_q[4:]}"
        prior = next((v for d, v in eps_by_q if d == prior_year_q), None)
        if prior is None and len(eps_by_q) >= 2:
            prior = eps_by_q[-2][1]
        if prior is not None:
            derived["consensus_eps"] = prior

    if roe_by_q:
        _, latest_roe = roe_by_q[-1]
        derived["roe_latest_q"] = round(latest_roe, 2)
        prior_roes = [v for _, v in roe_by_q[:-1]]
        if len(prior_roes) >= 3:
            derived["consensus_roe"] = round(statistics.median(prior_roes[-4:]), 2)
        if len(roe_by_q) >= 4:
            derived["roe_ttm"] = round(
                statistics.mean([v for _, v in roe_by_q[-4:]]), 2
            )
        else:
            derived["roe_ttm"] = round(latest_roe, 2)

    return hist, derived, [v for _, v in roe_by_q]


def build_stock_fundamentals(
    stock_id: str,
    start: date,
    end: date,
) -> tuple[dict | None, list[dict], list[dict]]:
    per_rows = fetch_finmind("TaiwanStockPER", stock_id, start, end)
    rev_rows = fetch_finmind("TaiwanStockMonthRevenue", stock_id, start, end)
    fin_rows = fetch_finmind("TaiwanStockFinancialStatements", stock_id, start, end)

    per = parse_per_rows(per_rows)
    rev_hist, rev_stats = parse_revenue_rows(rev_rows)
    fin_hist, fin_derived, _ = parse_financial_rows(fin_rows)

    if not per and not rev_stats and not fin_derived:
        return None, rev_hist + fin_hist, []

    as_of = (per or {}).get("as_of_date") or end.isoformat()
    fund_row = {
        "stock_id": stock_id,
        "as_of_date": as_of,
        "pe": (per or {}).get("pe"),
        "pb": (per or {}).get("pb"),
        "dividend_yield": (per or {}).get("dividend_yield"),
        "roe_ttm": fin_derived.get("roe_ttm"),
        "eps_ttm": fin_derived.get("eps_ttm"),
        "eps_latest_q": fin_derived.get("eps_latest_q"),
        "roe_latest_q": fin_derived.get("roe_latest_q"),
        "revenue_yoy_pct": (rev_stats or {}).get("revenue_yoy_pct"),
        "revenue_mom_accel_pp": (rev_stats or {}).get("revenue_mom_accel_pp"),
        "source": SOURCE,
    }

    consensus_rows: list[dict] = []
    if fin_derived.get("consensus_roe") is not None:
        consensus_rows.append(
            {
                "stock_id": stock_id,
                "as_of_date": as_of,
                "metric": "roe",
                "consensus_value": fin_derived["consensus_roe"],
                "source": SOURCE,
            }
        )
    if fin_derived.get("consensus_eps") is not None:
        consensus_rows.append(
            {
                "stock_id": stock_id,
                "as_of_date": as_of,
                "metric": "eps",
                "consensus_value": fin_derived["consensus_eps"],
                "source": SOURCE,
            }
        )

    history = rev_hist + fin_hist
    for h in history:
        h["stock_id"] = stock_id
    return fund_row, history, consensus_rows


def sync_fundamentals(
    db_path: Path,
    *,
    dry_run: bool = False,
    quiet: bool = False,
    max_stocks: int = 0,
    request_delay: float = REQUEST_DELAY_SEC,
) -> dict[str, int]:
    end = date.today()
    start = end - timedelta(days=FIN_LOOKBACK_DAYS)

    conn = connect(db_path)
    try:
        watchlist = load_etf_constituent_watchlist(conn)
    finally:
        conn.close()

    if not watchlist:
        raise RuntimeError("持股聯集為空：請先跑收盤持股同步")

    if max_stocks > 0:
        watchlist = watchlist[:max_stocks]

    stats = {
        "stocks": len(watchlist),
        "fundamental": 0,
        "history": 0,
        "consensus": 0,
        "ok": 0,
        "warn": 0,
    }

    for i, item in enumerate(watchlist):
        stock_id = item["stock_id"]
        if i > 0 and request_delay > 0:
            time.sleep(request_delay)
        try:
            fund, history, consensus = build_stock_fundamentals(stock_id, start, end)
            if fund is None:
                stats["warn"] += 1
                if not quiet:
                    print(f"  WARN {stock_id}: 無基本面資料", file=sys.stderr)
                continue
            stats["ok"] += 1
            if dry_run:
                if not quiet:
                    print(
                        f"  DRY {stock_id}: fund=1 hist={len(history)} "
                        f"cons={len(consensus)}"
                    )
                stats["fundamental"] += 1
                stats["history"] += len(history)
                stats["consensus"] += len(consensus)
                continue
            conn = connect(db_path)
            try:
                stats["fundamental"] += upsert_stock_fundamental(conn, [fund])
                if history:
                    stats["history"] += upsert_stock_financial_history(conn, history)
                if consensus:
                    stats["consensus"] += upsert_stock_consensus(conn, consensus)
            finally:
                conn.close()
            if quiet:
                print(f"  {stock_id}: hist={len(history)} cons={len(consensus)}")
        except requests.HTTPError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: HTTP {exc}", file=sys.stderr)
        except RuntimeError as exc:
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            stats["warn"] += 1
            print(f"  WARN {stock_id}: {exc}", file=sys.stderr)

    if not quiet and not dry_run:
        print(
            f"基本面 sync：{stats['ok']}/{stats['stocks']} 檔 OK，"
            f"fund={stats['fundamental']} hist={stats['history']} "
            f"consensus={stats['consensus']} warn={stats['warn']}"
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="同步成分股 L8/L8.5 至 SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--request-delay", type=float, default=REQUEST_DELAY_SEC)
    args = parser.parse_args()

    dry_run = args.dry_run or not args.sync_db
    try:
        sync_fundamentals(
            args.db,
            dry_run=dry_run,
            quiet=args.quiet,
            max_stocks=args.max_stocks,
            request_delay=args.request_delay,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
