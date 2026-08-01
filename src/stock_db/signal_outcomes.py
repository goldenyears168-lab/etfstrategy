"""signal_outcomes — forward-outcome ledger writer (labeling layer).

Grades each daily *signal* (RRG leading/improving close pick, VCP entry_ready)
at date T with its realized forward return and EXCESS vs IX0001 over three
trading-day horizons (H5/H10/H20). One row per (run_id, as_of_date, stock_id,
horizon), LONG format. Writes ONLY the signal_outcomes table (idempotent upsert).

COLUMN REMAP (the stub schema is reused verbatim — see _schema.py:442-461):
  as_of_date       = T  (the signal date; RRG data_baseline_date / VCP as_of_date)
  pm_bucket        = entry date  = T+1 (first trading day strictly after T)
  outcome_date     = exit  date  = T+H (H-th trading day after T)
  beta             = entry price = adj_open_entry (raw_open * cum_factor at T+1)
  ret_pct          = fwd_ret_H  * 100   (stock leg, ADJUSTED: adj_close_exit/adj_open_entry - 1)
  bench_ret_pct    = ix_ret_H   * 100   (IX0001 leg, house convention via analytics.bench)
  alpha_pct        = excess_H   * 100   (fwd_ret_H - ix_ret_H)
  capm_alpha_pct   = NULL  (no beta-adjusted alpha computed in this ledger)
  entry_signal     = the trigger rule as shipped (e.g. 'quadrant=leading', 'entry_ready=1')
  chip_tag         = extra classifier ('tier2=1', 'state=...')
  investment_score = the source's own score (RRG rs_ratio / VCP composite_score)
  status           = complete | pending_future | pending_adj | skip_no_trade
                     | skip_delisted | skip_no_bench   (see build_signal_outcomes)

Only status='complete' rows carry non-NULL return columns; every other status
is still upserted so it can be idempotently *promoted* on a later re-run.
"""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso


def _f(v: object) -> float | None:
    """Coerce to float, mapping None / empty / non-numeric to None."""
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def upsert_signal_outcomes(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Idempotent upsert into signal_outcomes.

    Keyed on (run_id, as_of_date, stock_id, horizon). Re-running promotes
    pending_* rows to complete without duplicating. Returns rows written.
    """
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO signal_outcomes (
            run_id, as_of_date, stock_id, horizon, stock_name,
            outcome_date, pm_bucket, entry_signal, chip_tag, investment_score,
            ret_pct, bench_ret_pct, alpha_pct, capm_alpha_pct, beta,
            status, synced_at
        ) VALUES (
            :run_id, :as_of_date, :stock_id, :horizon, :stock_name,
            :outcome_date, :pm_bucket, :entry_signal, :chip_tag, :investment_score,
            :ret_pct, :bench_ret_pct, :alpha_pct, :capm_alpha_pct, :beta,
            :status, :synced_at
        )
        ON CONFLICT(run_id, as_of_date, stock_id, horizon) DO UPDATE SET
            stock_name=excluded.stock_name,
            outcome_date=excluded.outcome_date,
            pm_bucket=excluded.pm_bucket,
            entry_signal=excluded.entry_signal,
            chip_tag=excluded.chip_tag,
            investment_score=excluded.investment_score,
            ret_pct=excluded.ret_pct,
            bench_ret_pct=excluded.bench_ret_pct,
            alpha_pct=excluded.alpha_pct,
            capm_alpha_pct=excluded.capm_alpha_pct,
            beta=excluded.beta,
            status=excluded.status,
            synced_at=excluded.synced_at
    """
    payload = []
    for r in rows:
        payload.append(
            {
                "run_id": r["run_id"],
                "as_of_date": r["as_of_date"],
                "stock_id": r["stock_id"],
                "horizon": int(r["horizon"]),
                "stock_name": r.get("stock_name"),
                "outcome_date": r.get("outcome_date"),
                "pm_bucket": r.get("pm_bucket"),
                "entry_signal": r.get("entry_signal"),
                "chip_tag": r.get("chip_tag"),
                "investment_score": _f(r.get("investment_score")),
                "ret_pct": _f(r.get("ret_pct")),
                "bench_ret_pct": _f(r.get("bench_ret_pct")),
                "alpha_pct": _f(r.get("alpha_pct")),
                "capm_alpha_pct": _f(r.get("capm_alpha_pct")),
                "beta": _f(r.get("beta")),
                "status": r.get("status") or "complete",
                "synced_at": r.get("synced_at") or synced_at,
            }
        )
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def delete_signal_outcomes_for_run(conn: sqlite3.Connection, run_id: str) -> int:
    cur = conn.execute("DELETE FROM signal_outcomes WHERE run_id = ?", (run_id,))
    conn.commit()
    return int(cur.rowcount or 0)


def load_signal_outcomes_for_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM signal_outcomes WHERE run_id = ?"
    params: list[object] = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY as_of_date ASC, stock_id ASC, horizon ASC"
    return conn.execute(sql, params).fetchall()
