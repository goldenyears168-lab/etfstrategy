#!/usr/bin/env python3
"""tickrev_verify — re-run the bar-vs-tick trigger engine with an EX-ANTE
(exchange-calendar) front-month contract selection instead of the engine's
whole-day argmax.

Why: slow_cell_tick_latency_lab._dominant_outright_contract() picks the
single-month contract with the highest tick count over the WHOLE calendar-day
file, then uses that series for the 08:45 day session as well. On the ~39
settlement days in the corpus the whole-day argmax is the NEXT month (its
night session inflates the count) while the morning is still ~50/50 between
the expiring and the next month. Choosing the morning's price series with a
statistic that is only observable after 13:45 (indeed after 05:00 the next
day) is look-ahead.

This script does NOT modify the engine or the runner. It imports the runner,
monkeypatches TL.build_sessions with a causal per-session variant, and calls
runner.main().

Ex-ante rule (fully knowable before each session opens):
  last trading day of month M = 3rd Wednesday (TAIFEX).
  day session of D   : front = M   if D <= w3(M) else M+1
  night session of D : front = M   if D <  w3(M) else M+1
  fallback (holiday / thin / missing) : that session's own argmax, recorded.

Usage (same CLI as tickrev_t3_runner.py):
  PYTHONPATH=src .venv/bin/python scripts/research/tickrev_verify_causal_contract_run.py \
      --days-file reports/research/channel_lab/tickrev_t3_coverage.json \
      --days-file-key usable_days --tag causal_contract --out /tmp/x.json
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts/research/tickrev_t3_runner.py"
AUDIT_OUT = Path("/tmp/tickrev/causal_contract_audit.json")

spec = importlib.util.spec_from_file_location("tickrev_t3_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
sys.modules["tickrev_t3_runner"] = runner
spec.loader.exec_module(runner)

TL = runner.TL
MIN_SESSION_TICKS = 1000
AUDIT: dict[str, dict] = {}


def _w3(y: int, m: int) -> dt.date:
    d = dt.date(y, m, 1)
    while d.weekday() != 2:      # Wednesday
        d += dt.timedelta(days=1)
    return d + dt.timedelta(days=14)


def _nxt(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _calendar_fronts(date: str) -> tuple[str, str]:
    D = dt.date.fromisoformat(date)
    W = _w3(D.year, D.month)
    ym_day = (D.year, D.month) if D <= W else _nxt(D.year, D.month)
    ym_night = (D.year, D.month) if D < W else _nxt(D.year, D.month)
    return "%04d%02d" % ym_day, "%04d%02d" % ym_night


def _argmax_single(rows, lo=None, hi=None) -> str | None:
    c: Counter = Counter()
    for r in rows:
        cd = r["contract_date"]
        if "/" in cd:
            continue
        if lo is not None:
            hhmm = r["date"][11:16]
            if not (lo <= hhmm <= hi):
                continue
        c[cd] += 1
    return c.most_common(1)[0][0] if c else None


def causal_build_sessions(date: str) -> list[dict]:
    """Same shape/semantics as TL.build_sessions, but the contract for each
    session is chosen from information available before that session opens."""
    cur = TL._load_day_file(date)
    if cur is None:
        return []
    d0 = dt.date.fromisoformat(date)
    d1 = (d0 + dt.timedelta(days=1)).isoformat()
    nxt = TL._load_day_file(d1)

    cal_day, cal_night = _calendar_fronts(date)
    n_day = sum(1 for r in cur if r["contract_date"] == cal_day
                and "08:45" <= r["date"][11:16] <= "13:45")
    n_night = sum(1 for r in cur if r["contract_date"] == cal_night
                  and r["date"][11:16] >= "15:00")
    n_night_all = sum(1 for r in cur if "/" not in r["contract_date"]
                      and r["date"][11:16] >= "15:00")

    fb = []
    day_ref = cal_day
    if n_day < MIN_SESSION_TICKS:
        alt = _argmax_single(cur, "08:45", "13:45")
        if alt:
            day_ref, _ = alt, fb.append("day")
    night_ref = cal_night
    if n_night < MIN_SESSION_TICKS and n_night_all >= MIN_SESSION_TICKS:
        alt = _argmax_single(cur, "15:00", "23:59")
        if alt:
            night_ref, _ = alt, fb.append("night")

    engine_ref = TL._dominant_outright_contract(cur)
    AUDIT[date] = dict(engine_ref=engine_ref, day_ref=day_ref, night_ref=night_ref,
                       n_day_cal=n_day, n_night_cal=n_night, fallback=fb,
                       day_changed=day_ref != engine_ref, night_changed=night_ref != engine_ref)

    def parse(rows):
        out = []
        for r in rows:
            out.append((dt.datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S"),
                        float(r["price"]), int(r["volume"])))
        return out

    day_lo = dt.datetime.strptime(f"{date} {TL.DAY_SESSION[0]}", "%Y-%m-%d %H:%M:%S")
    day_hi = dt.datetime.strptime(f"{date} {TL.DAY_SESSION[1]}", "%Y-%m-%d %H:%M:%S")
    night_lo = dt.datetime.strptime(f"{date} {TL.NIGHT_SESSION_START}", "%Y-%m-%d %H:%M:%S")
    night_hi = dt.datetime.strptime(f"{d1} {TL.NIGHT_SESSION_END}", "%Y-%m-%d %H:%M:%S")

    day_rows = parse([r for r in cur if r["contract_date"] == day_ref])
    night_rows = parse([r for r in cur if r["contract_date"] == night_ref])
    nxt_rows = parse([r for r in nxt if r["contract_date"] == night_ref]) if nxt else []

    day_ticks = sorted(t for t in day_rows if day_lo <= t[0] <= day_hi)
    night_ticks = sorted([t for t in night_rows if t[0] >= night_lo]
                         + [t for t in nxt_rows if t[0] <= night_hi])

    sessions = []
    if day_ticks:
        sessions.append({"session": "day", "date": date, "ticks": day_ticks})
    if night_ticks:
        sessions.append({"session": "night", "date": date, "ticks": night_ticks})
    return sessions


TL.build_sessions = causal_build_sessions

if __name__ == "__main__":
    try:
        runner.main()
    finally:
        AUDIT_OUT.parent.mkdir(parents=True, exist_ok=True)
        AUDIT_OUT.write_text(json.dumps(AUDIT, indent=1))
        print(f"contract audit -> {AUDIT_OUT} "
              f"(day_changed={sum(1 for v in AUDIT.values() if v['day_changed'])}, "
              f"night_changed={sum(1 for v in AUDIT.values() if v['night_changed'])}, "
              f"fallbacks={sum(1 for v in AUDIT.values() if v['fallback'])})")
