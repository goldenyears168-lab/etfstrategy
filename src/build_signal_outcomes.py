"""build_signal_outcomes — compute forward-outcome labels for daily signals.

Engine behind the signal_outcomes ledger. For each daily signal at date T
(RRG close leading/improving pick, or VCP entry_ready pick) it grades the
realized forward return over trading-day horizons H5/H10/H20 and the EXCESS
vs the IX0001 benchmark, following the canonical copytrade convention:

    entry  = T+1 OPEN,  exit = T+H CLOSE
    stock leg (ADJUSTED):  fwd_ret_H = adj_close[T+H] / adj_open[T+1] - 1
      where adj_open[T+1] = raw_open[T+1] * cum_factor[T+1]   (verified:
      raw_close * cum_factor == adj_close_v2 to full precision)
    bench leg:  ix_ret_H  = bench_close[T+H] / bench_open[T+1] - 1
    excess_H  = fwd_ret_H - ix_ret_H

DELIBERATE DIVERGENCE FROM src/research/backtest/copytrade_backtest.py:
copytrade uses RAW stock open/close; this ledger uses ADJUSTED (adj_open
derived, adj_close_v2) so ex-div / split boundaries yield the true realized
total return. Timing (T+1 open -> T+H close) and the IX0001 leg are identical.

BENCHMARK TIMING ASYMMETRY (house convention — documented, kept for
cross-consistency): analytics.bench.bench_open() prefers the `tej` IX0001
source, whose rows have no `open`, so for entry dates >= 2021-06 the benchmark
ENTRY basis is tej CLOSE, not open (pre-2021 uses yahoo real opens). Thus
alpha_pct is really (stock open->close) - (index close->close), NOT a clean
open-to-open excess. It is the same leg copytrade/tracks use, so it stays.

PIT / look-ahead: a T-day signal is graded only once T+H is a real *past*
trading day with price present; otherwise the row is upserted with a non-
complete status and NULL returns (never fabricated). Trading-day offsets use
the actual finmind trade calendar, not calendar days. Idempotent: re-runs
upsert and promote pending_* rows.

STATUS vocabulary (free-text, no DDL):
  complete       fully labeled; return columns non-NULL
  pending_future T+H is not yet a past trading day
  pending_adj    T+H reached but stock_close_adjusted absent at entry and/or
                 exit while a finmind raw bar IS present (the ~2-day adj lag)
  skip_no_trade  market open on the offset day but this stock has no finmind
                 raw bar (halt/suspension)
  skip_delisted  raw bar only under source='finmind_delisted'; adjusted series
                 absent -> structurally ungradable (survivorship)
  skip_no_bench  IX0001 endpoint missing at entry or exit
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache

from analytics.bench import bench_close, bench_open

HORIZONS: tuple[int, ...] = (5, 10, 20)


# --------------------------------------------------------------------------- #
# Trade calendar + offsets
# --------------------------------------------------------------------------- #
def _load_trade_calendar(conn: sqlite3.Connection) -> list[str]:
    """Ascending distinct finmind trading days (the global trade calendar)."""
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d FROM stock_daily_bars
        WHERE source='finmind' ORDER BY d ASC
        """
    ).fetchall()
    return [str(r["d"]) for r in rows]


def _offset(cal: list[str], cal_index: dict[str, int], t: str, h: int) -> tuple[str, str] | None:
    """Return (entry=T+1, exit=T+H) trading days, or None if T+H is not yet
    a real trading day in the calendar (-> pending_future)."""
    i = cal_index.get(t)
    if i is None:
        return None
    exit_idx = i + h
    if exit_idx >= len(cal):
        return None
    return cal[i + 1], cal[exit_idx]


# --------------------------------------------------------------------------- #
# Per-stock raw / adjusted lookups + status classification
# --------------------------------------------------------------------------- #
def _has_finmind_bar(conn: sqlite3.Connection, sid: str, day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' LIMIT 1",
        (sid, day),
    ).fetchone()
    return row is not None


def _has_delisted_bar(conn: sqlite3.Connection, sid: str, day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind_delisted' LIMIT 1",
        (sid, day),
    ).fetchone()
    return row is not None


def _no_data_status(conn: sqlite3.Connection, sid: str, day: str) -> str:
    """Classify why price is unavailable for (sid, day):
    - finmind raw bar present  -> 'pending_adj'   (adjusted simply not computed yet)
    - only finmind_delisted    -> 'skip_delisted' (survivorship: no adjusted series)
    - neither                  -> 'skip_no_trade' (halt/suspension on an open day)
    """
    if _has_finmind_bar(conn, sid, day):
        return "pending_adj"
    if _has_delisted_bar(conn, sid, day):
        return "skip_delisted"
    return "skip_no_trade"


def _adj_entry(conn: sqlite3.Connection, sid: str, entry: str) -> tuple[float | None, str | None]:
    """Adjusted entry (T+1) open = raw_open * cum_factor.
    Returns (adj_open_entry, error_status). On success error_status is None.
    """
    row = conn.execute(
        """
        SELECT sdb.open AS raw_open, sca.cum_factor, sca.adj_close_v2
        FROM stock_daily_bars sdb
        JOIN stock_close_adjusted sca
          ON sca.stock_id = sdb.stock_id AND sca.trade_date = sdb.trade_date
        WHERE sdb.stock_id=? AND sdb.trade_date=? AND sdb.source='finmind'
        """,
        (sid, entry),
    ).fetchone()
    if row is None:
        return None, _no_data_status(conn, sid, entry)
    raw_open = row["raw_open"]
    cum = row["cum_factor"]
    if raw_open is not None and float(raw_open) > 0 and cum is not None:
        val = float(raw_open) * float(cum)
        if val > 0:
            return val, None
    # Rare: raw_open NULL/<=0 -> fall back to adjusted close basis for this row.
    if row["adj_close_v2"] is not None and float(row["adj_close_v2"]) > 0:
        return float(row["adj_close_v2"]), None
    return None, _no_data_status(conn, sid, entry)


def _adj_exit(conn: sqlite3.Connection, sid: str, exit_day: str) -> tuple[float | None, str | None]:
    """Adjusted exit (T+H) close = adj_close_v2."""
    row = conn.execute(
        "SELECT adj_close_v2 FROM stock_close_adjusted WHERE stock_id=? AND trade_date=?",
        (sid, exit_day),
    ).fetchone()
    if row is None or row["adj_close_v2"] is None or float(row["adj_close_v2"]) <= 0:
        return None, _no_data_status(conn, sid, exit_day)
    return float(row["adj_close_v2"]), None


# --------------------------------------------------------------------------- #
# Signal loaders
# --------------------------------------------------------------------------- #
def _load_rrg_signals(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """RRG close leading/improving picks, deduped to the LIVE-SHIPPED session.

    rrg_universe_scores is keyed (session_date, screen_kind, stock_id) but one
    data_baseline_date can carry multiple session snapshots (a real-time run
    plus weekend/backfill recomputes). The recomputes were never shipped to
    users, so grading them would inject look-ahead and make the ledger
    non-deterministic. Pick MIN(session_date) per baseline = the live run.
    T = data_baseline_date (always a real finmind trading day).
    """
    sql = """
        WITH shipped AS (
          SELECT data_baseline_date, MIN(session_date) AS ship_session
          FROM rrg_universe_scores
          WHERE screen_kind='close' AND data_baseline_date IS NOT NULL
          GROUP BY data_baseline_date
        )
        SELECT r.data_baseline_date AS t, r.stock_id, r.stock_name, r.quadrant,
               r.rs_ratio, COALESCE(r.tier2,0) AS tier2
        FROM rrg_universe_scores r
        JOIN shipped s
          ON s.data_baseline_date = r.data_baseline_date
         AND s.ship_session       = r.session_date
        WHERE r.screen_kind='close'
          AND r.quadrant IN ('leading','improving')
          AND r.data_baseline_date BETWEEN ? AND ?
    """
    out: list[dict] = []
    for row in conn.execute(sql, (start, end)).fetchall():
        out.append(
            {
                "run_id": "rrg_close",
                "t": str(row["t"]),
                "stock_id": str(row["stock_id"]),
                "stock_name": row["stock_name"],
                "entry_signal": f"quadrant={row['quadrant']}",
                "chip_tag": f"tier2={int(row['tier2'])}",
                "investment_score": row["rs_ratio"],
            }
        )
    return out


def _load_vcp_signals(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """VCP entry_ready picks. Source PK (stock_id, as_of_date, model_id) is
    already unique; run_id embeds model_id. T = as_of_date."""
    sql = """
        SELECT as_of_date AS t, stock_id, stock_name, model_id,
               composite_score, execution_state
        FROM vcp_screen_scores_v2
        WHERE entry_ready=1
          AND as_of_date BETWEEN ? AND ?
    """
    out: list[dict] = []
    for row in conn.execute(sql, (start, end)).fetchall():
        out.append(
            {
                "run_id": f"vcp_entryready:{row['model_id']}",
                "t": str(row["t"]),
                "stock_id": str(row["stock_id"]),
                "stock_name": row["stock_name"],
                "entry_signal": "entry_ready=1",
                "chip_tag": f"state={row['execution_state']}",
                "investment_score": row["composite_score"],
            }
        )
    return out


_LOADERS = {
    "rrg": _load_rrg_signals,
    "vcp": _load_vcp_signals,
}


# --------------------------------------------------------------------------- #
# Main engine
# --------------------------------------------------------------------------- #
def build_outcomes(
    conn: sqlite3.Connection,
    sources: list[str],
    date_start: str,
    date_end: str,
    *,
    horizons: tuple[int, ...] = HORIZONS,
) -> list[dict]:
    """Grade every signal from `sources` with T in [date_start, date_end].

    Returns a flat list of signal_outcomes row dicts (one per signal x horizon),
    ready for upsert_signal_outcomes. Never fabricates a return: rows whose T+H
    is not yet gradable are emitted with a non-complete status and NULL returns.
    """
    cal = _load_trade_calendar(conn)
    cal_index = {d: i for i, d in enumerate(cal)}

    # Cache benchmark legs per trading day (bench_open/bench_close are pure).
    @lru_cache(maxsize=None)
    def _b_open(day: str) -> float | None:
        return bench_open(conn, day)

    @lru_cache(maxsize=None)
    def _b_close(day: str) -> float | None:
        return bench_close(conn, day)

    signals: list[dict] = []
    for src in sources:
        loader = _LOADERS.get(src)
        if loader is None:
            raise ValueError(f"unknown signal source: {src!r} (known: {sorted(_LOADERS)})")
        signals.extend(loader(conn, date_start, date_end))

    rows: list[dict] = []
    for sig in signals:
        t = sig["t"]
        sid = sig["stock_id"]
        base = {
            "run_id": sig["run_id"],
            "as_of_date": t,
            "stock_id": sid,
            "stock_name": sig["stock_name"],
            "entry_signal": sig["entry_signal"],
            "chip_tag": sig["chip_tag"],
            "investment_score": sig["investment_score"],
        }
        for h in horizons:
            row = {**base, "horizon": h}
            off = _offset(cal, cal_index, t, h)
            if off is None:
                rows.append({**row, "status": "pending_future", "outcome_date": None,
                             "pm_bucket": None, "ret_pct": None, "bench_ret_pct": None,
                             "alpha_pct": None, "capm_alpha_pct": None, "beta": None})
                continue
            entry, exit_day = off
            row["pm_bucket"] = entry
            row["outcome_date"] = exit_day

            adj_open_entry, err_in = _adj_entry(conn, sid, entry)
            if err_in is not None:
                rows.append({**row, "status": err_in, "ret_pct": None,
                             "bench_ret_pct": None, "alpha_pct": None,
                             "capm_alpha_pct": None, "beta": None})
                continue
            adj_close_exit, err_out = _adj_exit(conn, sid, exit_day)
            if err_out is not None:
                rows.append({**row, "status": err_out, "ret_pct": None,
                             "bench_ret_pct": None, "alpha_pct": None,
                             "capm_alpha_pct": None, "beta": adj_open_entry})
                continue

            ix_open_entry = _b_open(entry)
            ix_close_exit = _b_close(exit_day)
            if ix_open_entry is None or ix_close_exit is None or ix_open_entry == 0:
                rows.append({**row, "status": "skip_no_bench", "ret_pct": None,
                             "bench_ret_pct": None, "alpha_pct": None,
                             "capm_alpha_pct": None, "beta": adj_open_entry})
                continue

            fwd_ret = adj_close_exit / adj_open_entry - 1.0
            ix_ret = ix_close_exit / ix_open_entry - 1.0
            excess = fwd_ret - ix_ret
            rows.append({
                **row,
                "status": "complete",
                "ret_pct": fwd_ret * 100.0,
                "bench_ret_pct": ix_ret * 100.0,
                "alpha_pct": excess * 100.0,
                "capm_alpha_pct": None,
                "beta": adj_open_entry,
            })
    return rows
