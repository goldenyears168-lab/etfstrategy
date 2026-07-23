#!/usr/bin/env python3
"""Targeted 5m backfill for 凱基信義／松山 episode T+1 days.

FinMind TaiwanStockKBar (1m) → stock_kbar_1m + aggregate → stock_kbar_5m.
Yahoo 5m fallback for recent gaps (~60d) when FinMind empty/fails.

  PYTHONPATH=src .venv/bin/python scripts/research/backfill_kgi_episode_t1_kbar.py --report-only
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_kgi_episode_t1_kbar.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, finmind_token  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import (  # noqa: E402
    backfill_stock_kbar_5m_from_1m,
    finmind_kbar_rows_to_db,
    upsert_1m_rows_as_5m,
    upsert_stock_kbar_1m,
    upsert_yahoo_5m_rows,
)
from yahoo_chart_sync import fetch_tw_intraday_kbar_rows  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen"
EPISODES = [
    OUT / "kgi_xinyi_105_episodes.json",
    OUT / "kgi_songshan_58_episodes.json",
]
MIN_MORNING = 2  # bars with minute <= 09:30 (FinMind sometimes returns 20m-spaced)


def morning_bars(conn, sid: str, td: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM stock_kbar_5m
            WHERE stock_id=? AND trade_date=?
              AND minute <= '09:30:00'
            """,
            (sid, td),
        ).fetchone()[0]
        or 0
    )


def collect_gaps(conn) -> list[tuple[str, str]]:
    gaps: set[tuple[str, str]] = set()
    for path in EPISODES:
        data = json.loads(path.read_text())
        for ep in data["episodes"]:
            sid = str(ep["stock_id"])
            td = str(ep.get("protocol_entry") or "")[:10]
            if not td:
                continue
            if morning_bars(conn, sid, td) < MIN_MORNING:
                gaps.add((sid, td))
    return sorted(gaps)


def fetch_finmind_day(sid: str, td: str) -> tuple[int, int, str | None]:
    """Returns (n_1m, n_5m, err)."""
    d = date.fromisoformat(td)
    try:
        raw = fetch_finmind("TaiwanStockKBar", sid, d, d)
        rows = finmind_kbar_rows_to_db(sid, raw)
        if not rows:
            return 0, 0, None
        conn = connect(DEFAULT_DB_PATH)
        try:
            n1 = upsert_stock_kbar_1m(conn, rows)
            n5 = backfill_stock_kbar_5m_from_1m(conn, sid, td, source="finmind")
            if n5 == 0:
                n5 = upsert_1m_rows_as_5m(conn, sid, rows)
            return n1, n5, None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return 0, 0, str(exc)


def fetch_yahoo_day(sid: str, td: str) -> tuple[int, str | None]:
    d = date.fromisoformat(td)
    # Yahoo 5m window is short; still try for any gap
    try:
        rows_5m, _ = fetch_tw_intraday_kbar_rows(sid, d, d, interval="5m")
        day_rows = [r for r in rows_5m if str(r.get("trade_date"))[:10] == td]
        if day_rows:
            conn = connect(DEFAULT_DB_PATH)
            try:
                n = upsert_yahoo_5m_rows(conn, sid, day_rows)
                return n, None
            finally:
                conn.close()
        rows_1m, _ = fetch_tw_intraday_kbar_rows(sid, d, d, interval="1m")
        day_1m = [r for r in rows_1m if str(r.get("trade_date"))[:10] == td]
        if day_1m:
            conn = connect(DEFAULT_DB_PATH)
            try:
                upsert_stock_kbar_1m(conn, day_1m)
                n = upsert_1m_rows_as_5m(conn, sid, day_1m)
                return n, None
            finally:
                conn.close()
        return 0, None
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="Backfill KGI episode T+1 5m kbar")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--yahoo-max-age-days", type=int, default=55)
    args = ap.parse_args(argv)

    conn = connect(DEFAULT_DB_PATH)
    gaps = collect_gaps(conn)
    print(f"gaps needing morning≥{MIN_MORNING}: {len(gaps)}")
    if gaps:
        print(f"  range {gaps[0][1]} → {gaps[-1][1]}  sids={len({s for s,_ in gaps})}")
    if args.report_only:
        for sid, td in gaps[:20]:
            print(f"  {sid} {td} morning={morning_bars(conn, sid, td)}")
        if len(gaps) > 20:
            print(f"  ... +{len(gaps)-20} more")
        conn.close()
        return 0

    use_finmind = bool(finmind_token())
    if not use_finmind:
        print("WARN: FINMIND_TOKEN missing — Yahoo-only fallback")

    today = date.today()
    ok = 0
    still = 0
    errs = 0
    t0 = time.time()
    for i, (sid, td) in enumerate(gaps, 1):
        n1 = n5 = 0
        err = None
        if use_finmind:
            n1, n5, err = fetch_finmind_day(sid, td)
            if err and "403" in err:
                print(f"\n403 FinMind quota at {sid} {td} — disable FinMind for rest")
                use_finmind = False
                errs += 1
            elif err:
                errs += 1

        # refresh check
        conn2 = connect(DEFAULT_DB_PATH)
        m = morning_bars(conn2, sid, td)
        conn2.close()

        if m < MIN_MORNING:
            age = (today - date.fromisoformat(td)).days
            # Yahoo ~60d; also retry when FinMind returned empty
            if age <= args.yahoo_max_age_days or n5 == 0:
                ny, yerr = fetch_yahoo_day(sid, td)
                if yerr:
                    errs += 1
                n5 += ny
            conn2 = connect(DEFAULT_DB_PATH)
            m = morning_bars(conn2, sid, td)
            conn2.close()

        if m >= MIN_MORNING:
            ok += 1
            status = "OK"
        else:
            still += 1
            status = "MISS"
        if i % 10 == 0 or status == "MISS" or err:
            print(
                f"[{i:>3}/{len(gaps)}] {status} {sid} {td} "
                f"1m={n1} 5m_up={n5} morning={m} err={err or ''}",
                flush=True,
            )
        time.sleep(args.sleep)

    # final
    conn = connect(DEFAULT_DB_PATH)
    left = collect_gaps(conn)
    conn.close()
    elapsed = time.time() - t0
    print(
        f"\nDone in {elapsed/60:.1f}m · filled_ok≈{ok} still_gap={len(left)} errs≈{errs}"
    )
    if left:
        print("Remaining:")
        for sid, td in left[:30]:
            print(f"  {sid} {td}")
        if len(left) > 30:
            print(f"  ... +{len(left)-30}")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
