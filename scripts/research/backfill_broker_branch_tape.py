#!/usr/bin/env python3
"""Generic FinMind backfill → stock_broker_branch_daily for one or more branches.

  PYTHONPATH=src .venv/bin/python scripts/research/backfill_broker_branch_tape.py \\
      --branches kgi-taipei,guopiao-anhe,jpmorgan \\
      --start 2024-07-01 --end 2026-07-16
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from backfill_yuanta_songjiang_tape import (  # noqa: E402
    aggregate_day_rows,
    existing_tape_dates,
    list_calendar_dates,
)
from finmind_client import (  # noqa: E402
    fetch_finmind_dataset,
    fetch_taiwan_stock_trading_daily_report,
    finmind_token,
)
from stock_db import DEFAULT_DB_PATH, connect, upsert_stock_broker_branch_daily  # noqa: E402

SOURCE = "finmind"
REQUEST_DELAY_SEC = 0.5


class FinMindHardStop(RuntimeError):
    """Raised on 403/ip-ban or 402/quota — stop immediately, do not retry."""


# Back-compat alias used by older call sites / progress markers.
FinMindIpBanned = FinMindHardStop


def _hard_stop_reason(exc: BaseException) -> str | None:
    """Return stop reason ('ip_banned' / 'quota') or None if retryable."""
    text = str(exc).lower()
    if "ip banned" in text or "ip ban" in text:
        return "ip_banned"
    if "upper limit" in text or "payment required" in text:
        return "quota"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = int(exc.response.status_code)
        body = (exc.response.text or "").lower()
        if code == 403:
            return "ip_banned"
        if code == 402 or "upper limit" in body:
            return "quota"
    return None


def _is_ip_ban(exc: BaseException) -> bool:
    return _hard_stop_reason(exc) is not None

# slug → (prefer_id, name_parts_all_required)
KNOWN_BRANCHES: dict[str, tuple[str, tuple[str, ...]]] = {
    "yuanta-songjiang": ("9801", ("元大", "松江")),
    "fubon-xindian": ("9661", ("富邦", "新店")),
    "kgi-chengzhong": ("9227", ("凱基", "城中")),
    "kgi-xinyi": ("9216", ("凱基", "信義")),
    "kgi-taipei": ("9268", ("凱基", "台北")),
    "guopiao-anhe": ("779Z", ("國票", "安和")),
    "jpmorgan": ("8440", ("摩根大通",)),
    "merrill": ("1440", ("美林",)),
    "cathay-dunnan": ("8888", ("國泰", "敦南")),
    # Expansion batch (20260718): community-cited day-trade / branch / foreign.
    "fubon-jianguo": ("9658", ("富邦", "建國")),
    "yuanfu-chiayi": ("592I", ("元富", "嘉義")),
    "kgi-songshan": ("9217", ("凱基", "松山")),
    "yuanta-ximen": ("9873", ("元大", "西門")),
    "uni-tucheng": ("585Y", ("統一", "土城")),
    "kgi-huwei": ("9236", ("凱基", "虎尾")),
    "fubon-taoyuan": ("9665", ("富邦", "桃園")),
    "goldman": ("1480", ("美商高盛",)),
    "ms-taiwan": ("1470", ("台灣摩根士丹利",)),
    "nomura": ("1560", ("港商野村",)),
    "ubs-sg": ("1650", ("新加坡商瑞銀",)),
    "credit-suisse": ("1520", ("瑞士信貸",)),
}


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def resolve_trader(
    *,
    prefer_id: str,
    name_parts: tuple[str, ...],
) -> tuple[str, str]:
    rows = fetch_finmind_dataset("TaiwanSecuritiesTraderInfo")
    matches: list[tuple[str, str]] = []
    for r in rows:
        name = str(r.get("securities_trader") or r.get("name") or "")
        tid = str(r.get("securities_trader_id") or r.get("id") or "")
        if not tid or not name:
            continue
        if all(p in name for p in name_parts):
            matches.append((tid, name))
    if not matches:
        raise RuntimeError(f"no trader matching parts={name_parts}")
    for tid, name in matches:
        if tid == prefer_id:
            return tid, name
    return matches[0]


def backfill_one(
    conn,
    *,
    trader_id: str,
    trader_name: str,
    start: str,
    end: str,
    delay: float,
    force: bool,
    max_days: int,
    todo_dates: list[str] | None = None,
    fetch_workers: int = 1,
) -> tuple[int, int, list[str]]:
    calendar = list_calendar_dates(conn, start, end)
    if todo_dates is None:
        have = set() if force else existing_tape_dates(conn, trader_id, start, end)
        todo = [d for d in calendar if d not in have]
    else:
        todo = list(todo_dates)
        have = set(calendar) - set(todo)
    if max_days > 0:
        todo = todo[:max_days]
    print(
        f"[{trader_id} {trader_name}] window {start}..{end} "
        f"calendar={len(calendar)} have={len(have)} fetch={len(todo)}"
    )
    n_rows = 0
    n_days = 0
    errors: list[str] = []

    def fetch_day(day: str) -> tuple[str, list[dict], str | None]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                raw = fetch_taiwan_stock_trading_daily_report(
                    trade_date=day,
                    securities_trader_id=trader_id,
                )
                rows = aggregate_day_rows(
                    raw,
                    securities_trader_id=trader_id,
                    trade_date=day,
                )
                # Prefer canonical hyphenated label from resolver when API omits name.
                for row in rows:
                    if not row.get("securities_trader"):
                        row["securities_trader"] = trader_name
                return day, rows, None
            except FinMindHardStop:
                raise
            except Exception as exc:  # noqa: BLE001 — retry transient API errors
                reason = _hard_stop_reason(exc)
                if reason is not None:
                    raise FinMindHardStop(f"{reason}: {exc}") from exc
                last_error = exc
                time.sleep(max(1.0, float(delay) * (attempt + 1)))
            finally:
                time.sleep(max(0.0, float(delay)))
        return day, [], str(last_error)

    workers = max(1, int(fetch_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(fetch_day, todo)
        for i, (day, rows, error) in enumerate(results, 1):
            if error is not None:
                errors.append(f"{day}: {error}")
                print(f"  WARN {day}: {error}", file=sys.stderr)
                continue
            if not rows:
                # Persist a sentinel so empty/holiday fetches are not re-queued on resume.
                rows = [
                    {
                        "trade_date": day,
                        "securities_trader_id": trader_id,
                        "securities_trader": trader_name,
                        "stock_id": "__EMPTY__",
                        "buy": 0,
                        "sell": 0,
                        "net": 0,
                        "source": SOURCE,
                    }
                ]
            write_error: Exception | None = None
            for attempt in range(3):
                try:
                    n_rows += upsert_stock_broker_branch_daily(conn, rows)
                    write_error = None
                    break
                except Exception as exc:  # noqa: BLE001 — retry transient DB locks
                    write_error = exc
                    time.sleep(1.0 * (attempt + 1))
            if write_error is not None:
                errors.append(f"{day}: {write_error}")
                print(f"  WARN {day}: {write_error}", file=sys.stderr)
                continue
            n_days += 1
            if i % 20 == 0 or i == len(todo):
                is_empty = len(rows) == 1 and rows[0].get("stock_id") == "__EMPTY__"
                print(
                    f"  [{i}/{len(todo)}] {day} stocks={0 if is_empty else len(rows)} "
                    f"cum_rows={n_rows}"
                )
    return n_days, n_rows, errors


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill broker branch tapes")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument(
        "--branches",
        default="",
        help=f"Comma slugs from {sorted(KNOWN_BRANCHES)} (ignored if --universe-json)",
    )
    ap.add_argument(
        "--universe-json",
        type=Path,
        default=None,
        help="JSON list of {id,name} (or {id,name,region}) for bulk backfill",
    )
    ap.add_argument(
        "--progress",
        type=Path,
        default=None,
        help="Resume progress JSON path (bulk mode)",
    )
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--list", action="store_true", help="Print known slugs and exit")
    ap.add_argument(
        "--min-missing",
        type=int,
        default=1,
        help="Skip traders with fewer than this many missing calendar days",
    )
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument(
        "--fetch-workers",
        type=int,
        default=1,
        help="Concurrent FinMind requests; SQLite writes remain single-threaded",
    )
    args = ap.parse_args()

    if args.list:
        for slug, (tid, parts) in sorted(KNOWN_BRANCHES.items()):
            print(f"{slug}\t{tid}\t{'+'.join(parts)}")
        return 0

    if not finmind_token():
        print("ERROR: FINMIND_TOKEN unset", file=sys.stderr)
        return 2

    start = _parse_day(args.start).isoformat()
    end = _parse_day(args.end).isoformat()
    conn = connect(args.db)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        if args.universe_json is not None:
            import json

            raw = json.loads(Path(args.universe_json).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = raw["all"]
            universe = [(str(x["id"]), str(x["name"])) for x in raw]
            if args.shard_count < 1:
                print("ERROR: --shard-count must be >= 1", file=sys.stderr)
                return 1
            if not 0 <= args.shard_index < args.shard_count:
                print(
                    "ERROR: --shard-index must be in [0, shard-count)",
                    file=sys.stderr,
                )
                return 1
            universe = [
                item
                for index, item in enumerate(universe)
                if index % args.shard_count == args.shard_index
            ]
            progress_path = args.progress
            done: set[str] = set()
            if progress_path and progress_path.exists():
                try:
                    done = set(
                        json.loads(progress_path.read_text(encoding="utf-8")).get(
                            "completed_ids", []
                        )
                    )
                    print(f"resume completed={len(done)}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"WARN progress: {exc}", flush=True)

            calendar = list_calendar_dates(conn, start, end)
            trader_ids = [tid for tid, _ in universe]
            have_by_trader: dict[str, set[str]] = {
                tid: set() for tid in trader_ids
            }
            print(
                f"universe={len(universe)} calendar={len(calendar)} "
                f"scanning existing coverage …",
                flush=True,
            )
            if not args.force and trader_ids:
                placeholders = ",".join("?" for _ in trader_ids)
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT securities_trader_id, trade_date
                    FROM stock_broker_branch_daily
                    WHERE source = ? AND trade_date >= ? AND trade_date <= ?
                      AND securities_trader_id IN ({placeholders})
                    """,
                    (SOURCE, start, end, *trader_ids),
                ).fetchall()
                for row in rows:
                    have_by_trader[str(row[0])].add(str(row[1]))
                print(f"coverage rows={len(rows):,}", flush=True)
            todo = []
            for tid, name in universe:
                if tid in done:
                    continue
                have = set() if args.force else have_by_trader[tid]
                missing = [d for d in calendar if d not in have]
                if len(missing) < int(args.min_missing):
                    done.add(tid)
                    continue
                todo.append((tid, name, missing))
            # Prefer nearly-complete branches first (faster wins), then empty
            todo.sort(key=lambda x: (len(x[2]), x[0]))
            print(
                f"bulk universe={len(universe)} todo={len(todo)} "
                f"window={start}..{end} cal={len(calendar)}",
                flush=True,
            )
            t0 = time.time()
            errors_all: list[str] = []

            def _write_progress(*, i: int, extra: dict | None = None) -> None:
                if not progress_path:
                    return
                payload = {
                    "completed_ids": sorted(done),
                    "i": i,
                    "n": len(todo),
                    "errors_tail": errors_all[-40:],
                    "elapsed_min": (time.time() - t0) / 60,
                }
                if extra:
                    payload.update(extra)
                progress_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            for i, (tid, name, missing) in enumerate(todo, 1):
                try:
                    n_days, n_rows, errors = backfill_one(
                        conn,
                        trader_id=tid,
                        trader_name=name,
                        start=start,
                        end=end,
                        delay=args.delay,
                        force=args.force,
                        max_days=args.max_days,
                        todo_dates=missing,
                        fetch_workers=args.fetch_workers,
                    )
                except FinMindHardStop as exc:
                    reason = "quota" if "quota" in str(exc).lower() else "ip_banned"
                    errors_all.append(f"{tid} HARD_STOP({reason}): {exc}")
                    print(
                        f"STOP {reason} at {tid} {name}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    _write_progress(i=i, extra={"stopped": reason, "at": tid})
                    return 3 if reason == "ip_banned" else 4
                errors_all.extend(f"{tid} {e}" for e in errors)
                # Only mark complete when this pass covered every missing day
                # (avoid --max-days smoke tests poisoning resume progress).
                if not errors and n_days >= len(missing):
                    done.add(tid)
                elapsed = time.time() - t0
                rate = i / max(elapsed, 1)
                eta_h = (len(todo) - i) / max(rate, 1e-9) / 3600
                print(
                    f"[{i}/{len(todo)}] {tid} {name} days_ok={n_days} "
                    f"rows={n_rows} missing_was={len(missing)} "
                    f"elapsed={elapsed/60:.1f}m eta={eta_h:.1f}h",
                    flush=True,
                )
                if progress_path and (i % 1 == 0 or i == len(todo)):
                    _write_progress(i=i)
            print(
                f"bulk done todo={len(todo)} errors={len(errors_all)} "
                f"elapsed={(time.time()-t0)/60:.1f}m",
                flush=True,
            )
            return 0

        slugs = [s.strip() for s in str(args.branches).split(",") if s.strip()]
        if not slugs:
            print("ERROR: need --branches or --universe-json", file=sys.stderr)
            return 1
        for slug in slugs:
            if slug not in KNOWN_BRANCHES:
                print(f"ERROR: unknown branch slug {slug}", file=sys.stderr)
                return 1
            prefer_id, parts = KNOWN_BRANCHES[slug]
            trader_id, trader_name = resolve_trader(
                prefer_id=prefer_id, name_parts=parts
            )
            n_days, n_rows, errors = backfill_one(
                conn,
                trader_id=trader_id,
                trader_name=trader_name,
                start=start,
                end=end,
                delay=args.delay,
                force=args.force,
                max_days=args.max_days,
                fetch_workers=args.fetch_workers,
            )
            print(
                f"done {slug} days_ok={n_days} rows_upserted={n_rows} "
                f"errors={len(errors)}"
            )
            for e in errors[:5]:
                print(f"  err: {e}", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
