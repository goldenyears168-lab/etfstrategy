#!/usr/bin/env python3
"""Build stock_price_adjustment_events + stock_close_adjusted for the RESEARCH UNIVERSE only
(RRG universe (all-time) ∪ VCP picks), intersected with raw finmind coverage — NOT the full
667 raw set. Same event source (TaiwanStockDividendResult + TaiwanStockCapitalReductionReferencePrice)
and identical cum_factor logic as scripts/backfill_price_adjustment_events.py (reuses
stock_db.price_adjustment). Writes ONLY those two tables. Idempotent (skips stocks that already
have events unless --force). Per-stock error is caught so a single failure never aborts the run.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/build_adjusted_for_universe.py [--max-stocks N] [--force] [--delay 0.35]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind_dataset, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import connect  # noqa: E402
from stock_db.price_adjustment import (  # noqa: E402
    compute_adjusted_close,
    load_adjustment_events,
    upsert_stock_close_adjusted,
    upsert_stock_price_adjustment_events,
)

EARLIEST = date(2010, 1, 1)


def _norm_div(sid, rows):
    out = []
    for r in rows:
        ex = str(r.get("date") or "")[:10]
        b = r.get("before_price")
        a = r.get("after_price")
        if not ex or b is None or a is None:
            continue
        out.append({"stock_id": sid, "ex_date": ex, "event_type": "dividend",
                    "before_price": float(b), "after_price": float(a),
                    "detail": str(r.get("stock_and_cache_dividend") or ""), "source": "finmind"})
    return out


def _norm_cap(sid, rows):
    out = []
    for r in rows:
        ex = str(r.get("date") or "")[:10]
        b = r.get("ClosingPriceonTheLastTradingDay")
        a = r.get("PostReductionReferencePrice")
        if not ex or b is None or a is None:
            continue
        out.append({"stock_id": sid, "ex_date": ex, "event_type": "capital_reduction",
                    "before_price": float(b), "after_price": float(a),
                    "detail": str(r.get("ReasonforCapitalReduction") or ""), "source": "finmind"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-stocks", type=int, default=0, help="0 = all universe stocks (smoke test with e.g. 3)")
    ap.add_argument("--force", action="store_true", help="rebuild even if the stock already has events")
    ap.add_argument("--delay", type=float, default=0.35)
    args = ap.parse_args()

    load_project_dotenv()
    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    conn = connect()
    universe = [
        r[0]
        for r in conn.execute(
            "SELECT stock_id FROM ("
            "  SELECT stock_id FROM rrg_universe_scores"
            "  UNION SELECT stock_id FROM vcp_screen_scores_v2) u"
            " WHERE stock_id IN (SELECT DISTINCT stock_id FROM stock_daily_bars WHERE source='finmind')"
            " ORDER BY stock_id"
        ).fetchall()
    ]
    print(f"research universe (rrg union vcp, in raw): {len(universe)} stocks", flush=True)
    if args.max_stocks > 0:
        universe = universe[: args.max_stocks]

    stock_ids = universe
    if not args.force:
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT stock_id FROM stock_price_adjustment_events").fetchall()}
        stock_ids = [s for s in universe if s not in done]
        print(f"already have events for {len(done)} stocks; todo={len(stock_ids)}", flush=True)

    n_ok = n_err = n_events = n_adj = 0
    t0 = time.time()
    for i, sid in enumerate(stock_ids, 1):
        try:
            div = fetch_finmind_dataset("TaiwanStockDividendResult", data_id=sid, start=EARLIEST, end=date.today())
            time.sleep(args.delay)
            cap = fetch_finmind_dataset("TaiwanStockCapitalReductionReferencePrice", data_id=sid, start=EARLIEST, end=date.today())
            time.sleep(args.delay)
        except Exception as exc:  # broad on purpose: resumable, never abort the whole run
            n_err += 1
            print(f"  WARN {sid}: {exc}", file=sys.stderr)
            continue

        events = _norm_div(sid, div) + _norm_cap(sid, cap)
        n_events += upsert_stock_price_adjustment_events(conn, events)

        raw = conn.execute(
            "SELECT trade_date, close FROM stock_daily_bars WHERE stock_id=? AND source='finmind' AND close IS NOT NULL",
            (sid,),
        ).fetchall()
        if raw:
            rc = {r[0]: r[1] for r in raw}
            ev = load_adjustment_events(conn, sid, rc)
            adj = compute_adjusted_close(rc, ev)
            rows = [
                {"stock_id": sid, "trade_date": d, "adj_close_v2": v[0], "cum_factor": v[1],
                 "n_events_applied": v[2], "source": "finmind"}
                for d, v in adj.items()
            ]
            n_adj += upsert_stock_close_adjusted(conn, rows)

        n_ok += 1
        if i % 25 == 0 or i == len(stock_ids):
            el = time.time() - t0
            rate = i / max(el, 1)
            eta = (len(stock_ids) - i) / max(rate, 1e-9) / 60
            print(f"[{i}/{len(stock_ids)}] ok={n_ok} err={n_err} events={n_events} "
                  f"adj_rows={n_adj} elapsed={el/60:.1f}m eta={eta:.1f}m", flush=True)

    conn.close()
    print(f"DONE ok={n_ok} err={n_err} events_written={n_events} adj_rows_written={n_adj}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
