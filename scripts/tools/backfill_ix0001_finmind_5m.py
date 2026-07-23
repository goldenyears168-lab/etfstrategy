#!/usr/bin/env python3
"""FinMind TaiwanVariousIndicators5Seconds → IX0001 stock_kbar_5m (source=finmind).

Resume-safe day loop · reconcile vs Yahoo IX0001 · optional NQ=F Yahoo seed.

  PYTHONPATH=src python scripts/tools/backfill_ix0001_finmind_5m.py --days 60
  PYTHONPATH=src python scripts/tools/backfill_ix0001_finmind_5m.py --reconcile-only
  PYTHONPATH=src python scripts/tools/backfill_ix0001_finmind_5m.py --days 60 --extend-days 180
  PYTHONPATH=src python scripts/tools/backfill_ix0001_finmind_5m.py --seed-nq
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from project_dotenv import load_project_dotenv  # noqa: E402

load_project_dotenv()

from finmind_client import (  # noqa: E402
    fetch_taiwan_various_indicators_5s,
    finmind_token,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import (  # noqa: E402
    MIN_BARS_5M_PER_DAY,
    aggregate_taiex_5s_rows_to_5m,
    kbar_5m_day_coverage,
    kbar_bars_to_db_rows,
    upsert_stock_kbar_5m,
    upsert_yahoo_5m_rows,
)

STOCK_ID = "IX0001"
SOURCE = "finmind"
DATASET = "TaiwanVariousIndicators5Seconds"
DEFAULT_MIN_BARS = 40
DEFAULT_MIN_CORR = 0.99
DEFAULT_MAX_MEDIAN_REL = 1e-4


def _trade_dates(
    conn: sqlite3.Connection,
    *,
    date_end: str,
    n_days: int,
    date_start: str | None = None,
) -> list[str]:
    if date_start:
        rows = conn.execute(
            """
            SELECT DISTINCT d AS trade_date FROM (
                SELECT date AS d FROM daily_bars
                WHERE code = 'IX0001' AND date BETWEEN ? AND ?
                UNION
                SELECT trade_date AS d FROM stock_kbar_5m
                WHERE stock_id = 'IX0001' AND trade_date BETWEEN ? AND ?
            )
            ORDER BY trade_date
            """,
            (date_start, date_end, date_start, date_end),
        ).fetchall()
        return [str(r[0]) for r in rows]

    rows = conn.execute(
        """
        SELECT DISTINCT d AS trade_date FROM (
            SELECT date AS d FROM daily_bars
            WHERE code = 'IX0001' AND date <= ?
            UNION
            SELECT trade_date AS d FROM stock_kbar_5m
            WHERE stock_id = 'IX0001' AND trade_date <= ?
        )
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (date_end, date_end, n_days),
    ).fetchall()
    out = [str(r[0]) for r in rows]
    out.reverse()
    return out


def _backfill_days(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    force: bool = False,
    sleep_sec: float = 0.45,
    min_bars: int = DEFAULT_MIN_BARS,
) -> dict[str, Any]:
    if not finmind_token():
        raise RuntimeError("FINMIND_TOKEN not set")

    n_skip = 0
    n_ok = 0
    n_empty = 0
    n_err = 0
    bars_upserted = 0
    errors: list[str] = []

    for i, td in enumerate(trade_dates, start=1):
        if not force and kbar_5m_day_coverage(conn, STOCK_ID, td, source=SOURCE) >= min_bars:
            n_skip += 1
            continue
        try:
            raw = fetch_taiwan_various_indicators_5s(date.fromisoformat(td))
            td_found, bars = aggregate_taiex_5s_rows_to_5m(raw)
            if not bars:
                n_empty += 1
                print(f"  [{i}/{len(trade_dates)}] {td} empty", flush=True)
            else:
                use_td = td_found or td
                n = upsert_stock_kbar_5m(
                    conn,
                    kbar_bars_to_db_rows(STOCK_ID, use_td, bars, source=SOURCE),
                )
                bars_upserted += n
                n_ok += 1
                print(
                    f"  [{i}/{len(trade_dates)}] {use_td} bars={n} pts={len(raw)}",
                    flush=True,
                )
        except Exception as exc:
            n_err += 1
            msg = f"{td}: {exc}"
            errors.append(msg)
            print(f"  [{i}/{len(trade_dates)}] ERR {msg}", flush=True)
            if "403" in str(exc) or "ip banned" in str(exc).lower():
                print("  stop: rate limit / ban", flush=True)
                break
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    return {
        "days_requested": len(trade_dates),
        "days_upserted": n_ok,
        "days_skipped": n_skip,
        "days_empty": n_empty,
        "days_errors": n_err,
        "bars_upserted": bars_upserted,
        "errors": errors[:10],
    }


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    if den <= 0:
        return None
    return num / den


def reconcile_vs_yahoo(
    conn: sqlite3.Connection,
    *,
    date_start: str | None = None,
    date_end: str | None = None,
) -> dict[str, Any]:
    q = """
        SELECT f.trade_date, f.minute, f.close AS finmind_close, y.close AS yahoo_close
        FROM stock_kbar_5m f
        INNER JOIN stock_kbar_5m y
          ON y.stock_id = f.stock_id
         AND y.trade_date = f.trade_date
         AND y.minute = f.minute
         AND y.source = 'yahoo'
        WHERE f.stock_id = ? AND f.source = ?
    """
    params: list[str] = [STOCK_ID, SOURCE]
    if date_start:
        q += " AND f.trade_date >= ?"
        params.append(date_start)
    if date_end:
        q += " AND f.trade_date <= ?"
        params.append(date_end)
    q += " ORDER BY f.trade_date, f.minute"
    rows = conn.execute(q, params).fetchall()

    fm_days = conn.execute(
        """
        SELECT COUNT(DISTINCT trade_date) FROM stock_kbar_5m
        WHERE stock_id = ? AND source = ?
        """,
        (STOCK_ID, SOURCE),
    ).fetchone()[0]
    yh_days = conn.execute(
        """
        SELECT COUNT(DISTINCT trade_date) FROM stock_kbar_5m
        WHERE stock_id = ? AND source = 'yahoo'
        """,
        (STOCK_ID,),
    ).fetchone()[0]

    by_day: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        td = str(r[0])
        by_day.setdefault(td, []).append((float(r[2]), float(r[3])))

    day_corrs: list[float] = []
    day_med_rel: list[float] = []
    for pairs in by_day.values():
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        c = _pearson(xs, ys)
        if c is not None:
            day_corrs.append(c)
        rels = [abs(a - b) / abs(b) for a, b in pairs if b != 0]
        if rels:
            day_med_rel.append(statistics.median(rels))

    all_x = [p[0] for pairs in by_day.values() for p in pairs]
    all_y = [p[1] for pairs in by_day.values() for p in pairs]
    sample_days = sorted(by_day)[:3] + (
        sorted(by_day)[-3:] if len(by_day) > 6 else sorted(by_day)[3:]
    )
    sample_days = sorted(set(sample_days))

    return {
        "finmind_days": int(fm_days or 0),
        "yahoo_days": int(yh_days or 0),
        "overlap_days": len(by_day),
        "overlap_bars": len(rows),
        "close_corr_all": _pearson(all_x, all_y),
        "close_corr_median_day": statistics.median(day_corrs) if day_corrs else None,
        "close_corr_min_day": min(day_corrs) if day_corrs else None,
        "median_rel_abs_err": statistics.median(day_med_rel) if day_med_rel else None,
        "max_day_median_rel_abs_err": max(day_med_rel) if day_med_rel else None,
        "sample_days": {
            td: {
                "n": len(by_day[td]),
                "corr": _pearson(
                    [p[0] for p in by_day[td]],
                    [p[1] for p in by_day[td]],
                ),
            }
            for td in sample_days
        },
    }


def reconcile_ok(
    summary: dict[str, Any],
    *,
    min_corr: float = DEFAULT_MIN_CORR,
    max_median_rel: float = DEFAULT_MAX_MEDIAN_REL,
    min_overlap_days: int = 20,
) -> bool:
    corr = summary.get("close_corr_all")
    med_rel = summary.get("median_rel_abs_err")
    if summary.get("overlap_days", 0) < min_overlap_days:
        return False
    if corr is None or corr < min_corr:
        return False
    if med_rel is None or med_rel > max_median_rel:
        return False
    return True


def seed_nq_yahoo(
    conn: sqlite3.Connection,
    *,
    date_end: date | None = None,
) -> dict[str, Any]:
    """Cheap Yahoo ~60d NQ=F 5m upsert into stock_kbar_5m (source=yahoo)."""
    try:
        from yahoo_chart_sync import fetch_yahoo_intraday_df, yahoo_intraday_rows_to_db
    except ImportError as exc:
        return {"bars_upserted": 0, "error": f"import: {exc}"}

    end = date_end or date.today()
    # Yahoo 5m hard-cap ~60 calendar days; keep a small buffer under the edge.
    start = end - timedelta(days=55)
    try:
        df = fetch_yahoo_intraday_df("NQ=F", start, end, interval="5m")
    except ImportError as exc:
        return {"bars_upserted": 0, "error": f"yfinance: {exc}"}
    rows = yahoo_intraday_rows_to_db("NQ=F", df, source="yahoo")
    n = upsert_yahoo_5m_rows(conn, "NQ=F", rows) if rows else 0
    days = conn.execute(
        """
        SELECT COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date)
        FROM stock_kbar_5m WHERE stock_id = 'NQ=F' AND source = 'yahoo'
        """
    ).fetchone()
    return {
        "bars_upserted": n,
        "yahoo_rows": len(rows),
        "nq_days": int(days[0] or 0),
        "nq_min": days[1],
        "nq_max": days[2],
    }


def _print_reconcile(summary: dict[str, Any]) -> None:
    print("=== reconcile FinMind vs Yahoo IX0001 ===")
    for k in (
        "finmind_days",
        "yahoo_days",
        "overlap_days",
        "overlap_bars",
        "close_corr_all",
        "close_corr_median_day",
        "close_corr_min_day",
        "median_rel_abs_err",
        "max_day_median_rel_abs_err",
    ):
        print(f"  {k}: {summary.get(k)}")
    print("  sample_days:")
    for td, meta in (summary.get("sample_days") or {}).items():
        print(f"    {td}: n={meta.get('n')} corr={meta.get('corr')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Backfill {STOCK_ID} 5m from FinMind {DATASET}"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--days", type=int, default=60, help="Initial trading days")
    parser.add_argument(
        "--extend-days",
        type=int,
        default=0,
        help="After reconcile OK, backfill this many total trading days (e.g. 180)",
    )
    parser.add_argument("--date-end", default=None, help="YYYY-MM-DD (default today)")
    parser.add_argument("--date-start", default=None, help="Explicit start (overrides --days)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.45, help="Seconds between day calls")
    parser.add_argument("--min-bars", type=int, default=DEFAULT_MIN_BARS)
    parser.add_argument("--reconcile-only", action="store_true")
    parser.add_argument("--skip-reconcile", action="store_true")
    parser.add_argument("--seed-nq", action="store_true", help="Also refresh Yahoo NQ=F 5m")
    parser.add_argument("--min-corr", type=float, default=DEFAULT_MIN_CORR)
    args = parser.parse_args(argv)

    date_end = args.date_end or date.today().isoformat()
    conn = connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.seed_nq and args.reconcile_only:
            nq = seed_nq_yahoo(conn, date_end=date.fromisoformat(date_end))
            print("NQ seed:", nq)

        if args.reconcile_only:
            summary = reconcile_vs_yahoo(conn)
            _print_reconcile(summary)
            print("reconcile_ok:", reconcile_ok(summary, min_corr=args.min_corr))
            return 0

        trade_dates = _trade_dates(
            conn,
            date_end=date_end,
            n_days=args.days,
            date_start=args.date_start,
        )
        print(
            f"Backfill {STOCK_ID} ← {DATASET} · days={len(trade_dates)} "
            f"[{trade_dates[0] if trade_dates else '?'} → {trade_dates[-1] if trade_dates else '?'}]",
            flush=True,
        )
        stats = _backfill_days(
            conn,
            trade_dates,
            force=args.force,
            sleep_sec=args.sleep,
            min_bars=args.min_bars,
        )
        print("backfill:", {k: v for k, v in stats.items() if k != "errors"})
        if stats.get("errors"):
            print("errors_sample:", stats["errors"])

        ok = True
        if not args.skip_reconcile:
            summary = reconcile_vs_yahoo(
                conn,
                date_start=trade_dates[0] if trade_dates else None,
                date_end=trade_dates[-1] if trade_dates else None,
            )
            _print_reconcile(summary)
            ok = reconcile_ok(summary, min_corr=args.min_corr)
            print("reconcile_ok:", ok)

        if ok and args.extend_days and args.extend_days > args.days and not args.date_start:
            extend_dates = _trade_dates(conn, date_end=date_end, n_days=args.extend_days)
            print(
                f"Extend to {len(extend_dates)} trading days "
                f"[{extend_dates[0]} → {extend_dates[-1]}]",
                flush=True,
            )
            ext = _backfill_days(
                conn,
                extend_dates,
                force=args.force,
                sleep_sec=args.sleep,
                min_bars=args.min_bars,
            )
            print("extend:", {k: v for k, v in ext.items() if k != "errors"})
            summary2 = reconcile_vs_yahoo(conn)
            _print_reconcile(summary2)
            print("reconcile_ok_after_extend:", reconcile_ok(summary2, min_corr=args.min_corr))

        if args.seed_nq:
            nq = seed_nq_yahoo(conn, date_end=date.fromisoformat(date_end))
            print("NQ seed:", nq)

        cov = conn.execute(
            """
            SELECT COUNT(*) bars, COUNT(DISTINCT trade_date) days,
                   MIN(trade_date) mn, MAX(trade_date) mx
            FROM stock_kbar_5m WHERE stock_id = ? AND source = ?
            """,
            (STOCK_ID, SOURCE),
        ).fetchone()
        print(
            f"final finmind IX0001: bars={cov[0]} days={cov[1]} "
            f"[{cov[2]} → {cov[3]}]"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
