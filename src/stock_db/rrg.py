"""RRG narrow backtest bundle persistence."""
from __future__ import annotations

import sqlite3

from stock_db.util import utc_now_iso

def persist_rrg_narrow_backtest_bundle(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    run_row: dict,
    summaries: list[dict],
    periods: list[dict],
    regime_calendar: list[dict],
    year_stats: list[dict],
) -> str:
    """寫入 RRG 窄流極回測 run 與子表（同 run_id 先刪後插）。"""
    synced_at = utc_now_iso()
    for table in (
        "rrg_narrow_backtest_periods",
        "rrg_narrow_backtest_summary",
        "rrg_narrow_regime_calendar",
        "rrg_narrow_regime_year_stats",
        "rrg_narrow_backtest_runs",
    ):
        conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    defaults = {
        "regime_filter": "narrow_leadership_momentum",
        "factor_mode": "rolling",
        "top_n": 10,
        "min_vol": 3_000_000,
        "rrg_length": 20,
        "benchmark_code": "IX0001",
        "entry_price_mode": "open",
        "horizons_json": "[10,30,45]",
        "signal_dates_total": 0,
        "notes": None,
    }
    run_payload = {**defaults, **run_row, "run_id": run_id, "synced_at": synced_at}
    conn.execute(
        """
        INSERT INTO rrg_narrow_backtest_runs (
            run_id, label, regime_filter, year_start, year_end, factor_mode,
            top_n, min_vol, rrg_length, benchmark_code, entry_price_mode,
            horizons_json, signal_dates_total, notes, synced_at
        ) VALUES (
            :run_id, :label, :regime_filter, :year_start, :year_end, :factor_mode,
            :top_n, :min_vol, :rrg_length, :benchmark_code, :entry_price_mode,
            :horizons_json, :signal_dates_total, :notes, :synced_at
        )
        """,
        run_payload,
    )

    if summaries:
        conn.executemany(
            """
            INSERT INTO rrg_narrow_backtest_summary (
                run_id, strategy_id, strategy_label, hold_days, n_periods, n_skipped,
                mean_return_pct, mean_bench_pct, mean_excess_pct, total_excess_pct,
                win_rate_vs_bench_pct, win_rate_gross_pct, window_start, window_end,
                synced_at
            ) VALUES (
                :run_id, :strategy_id, :strategy_label, :hold_days, :n_periods, :n_skipped,
                :mean_return_pct, :mean_bench_pct, :mean_excess_pct, :total_excess_pct,
                :win_rate_vs_bench_pct, :win_rate_gross_pct, :window_start, :window_end,
                :synced_at
            )
            """,
            [{**r, "run_id": run_id, "synced_at": synced_at} for r in summaries],
        )
    if periods:
        conn.executemany(
            """
            INSERT INTO rrg_narrow_backtest_periods (
                run_id, strategy_id, hold_days, signal_date, entry_date, exit_date,
                n_stocks, picks_json, return_pct, bench_return_pct, excess_pct,
                beat_bench, gross_win, status, skip_reason, synced_at
            ) VALUES (
                :run_id, :strategy_id, :hold_days, :signal_date, :entry_date, :exit_date,
                :n_stocks, :picks_json, :return_pct, :bench_return_pct, :excess_pct,
                :beat_bench, :gross_win, :status, :skip_reason, :synced_at
            )
            """,
            [{**r, "run_id": run_id, "synced_at": synced_at} for r in periods],
        )
    if regime_calendar:
        conn.executemany(
            """
            INSERT INTO rrg_narrow_regime_calendar (
                run_id, eval_date, year, momentum_structure, dispersion_20d,
                rolling_m1_20d, top30_intra_std, realized_vol_20d, synced_at
            ) VALUES (
                :run_id, :eval_date, :year, :momentum_structure, :dispersion_20d,
                :rolling_m1_20d, :top30_intra_std, :realized_vol_20d, :synced_at
            )
            """,
            [{**r, "run_id": run_id, "synced_at": synced_at} for r in regime_calendar],
        )
    if year_stats:
        conn.executemany(
            """
            INSERT INTO rrg_narrow_regime_year_stats (
                run_id, year, narrow_extreme_days, narrow_moderate_days,
                total_trading_days, notes, synced_at
            ) VALUES (
                :run_id, :year, :narrow_extreme_days, :narrow_moderate_days,
                :total_trading_days, :notes, :synced_at
            )
            """,
            [{**r, "run_id": run_id, "synced_at": synced_at} for r in year_stats],
        )
    conn.commit()
    return run_id


def delete_rrg_universe_for_session(
    conn: sqlite3.Connection,
    session_date: str,
    screen_kind: str,
) -> int:
    cur = conn.execute(
        """
        DELETE FROM rrg_universe_scores
        WHERE session_date = ? AND screen_kind = ?
        """,
        (session_date, screen_kind),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def replace_rrg_universe_scores(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    screen_kind: str,
    rows: list[dict],
) -> int:
    """同 session_date + screen_kind 先刪後插。"""
    delete_rrg_universe_for_session(conn, session_date, screen_kind)
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO rrg_universe_scores (
            session_date, screen_kind, data_baseline_date, stock_id, stock_name,
            rs_ratio, rs_momentum, quadrant, quadrants_json, trend, disp, seg_last,
            segs_json, tier2, mono_tier2, mono_fresh, daily_pct, tick_ok, synced_at
        ) VALUES (
            :session_date, :screen_kind, :data_baseline_date, :stock_id, :stock_name,
            :rs_ratio, :rs_momentum, :quadrant, :quadrants_json, :trend, :disp, :seg_last,
            :segs_json, :tier2, :mono_tier2, :mono_fresh, :daily_pct, :tick_ok, :synced_at
        )
    """
    payload = []
    for r in rows:
        payload.append(
            {
                "session_date": session_date,
                "screen_kind": screen_kind,
                "data_baseline_date": r["data_baseline_date"],
                "stock_id": r["stock_id"],
                "stock_name": r.get("stock_name"),
                "rs_ratio": r.get("rs_ratio"),
                "rs_momentum": r.get("rs_momentum"),
                "quadrant": r.get("quadrant"),
                "quadrants_json": r.get("quadrants_json"),
                "trend": r.get("trend"),
                "disp": r.get("disp"),
                "seg_last": r.get("seg_last"),
                "segs_json": r.get("segs_json"),
                "tier2": int(r.get("tier2") or 0),
                "mono_tier2": int(r.get("mono_tier2") or 0),
                "mono_fresh": int(r.get("mono_fresh") or 0),
                "daily_pct": r.get("daily_pct"),
                "tick_ok": r.get("tick_ok"),
                "synced_at": r.get("synced_at") or synced_at,
            }
        )
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_rrg_universe_scores(
    conn: sqlite3.Connection,
    session_date: str,
    screen_kind: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM rrg_universe_scores
        WHERE session_date = ? AND screen_kind = ?
        ORDER BY stock_id ASC
        """,
        (session_date, screen_kind),
    ).fetchall()
