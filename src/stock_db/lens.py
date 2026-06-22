"""stock_daily_lens · lens_daily_alert SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from stock_db.util import utc_now_iso


def delete_stock_daily_lens_for_date(conn: sqlite3.Connection, trade_date: str) -> int:
    cur = conn.execute(
        "DELETE FROM stock_daily_lens WHERE trade_date = ?",
        (trade_date,),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def upsert_stock_daily_lens_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_daily_lens (
            trade_date, stock_id, stock_name,
            etf_add_count, etf_reduce_count, etf_add_codes_json,
            etf_flow_ntd, share_delta_total, growth_pct,
            consensus_add, consensus_streak_days,
            breadth_zone_200, trend_posture, regime_aligned,
            rrg_quadrant, rrg_quadrant_prev, rrg_mono_fresh, rrg_tier2,
            vcp_composite, vcp_execution_state, vcp_distance_pivot_pct,
            copytrade_l1h9_signal,
            delta_new_to_watchlist, delta_rrg_quadrant_change, delta_consensus_new_today,
            delta_score_change, delta_any_signal,
            signal_convergence, lens_score, narrative_zh, highlight_tier,
            holdings_aligned, data_baseline_date, sources_json, computed_at
        ) VALUES (
            :trade_date, :stock_id, :stock_name,
            :etf_add_count, :etf_reduce_count, :etf_add_codes_json,
            :etf_flow_ntd, :share_delta_total, :growth_pct,
            :consensus_add, :consensus_streak_days,
            :breadth_zone_200, :trend_posture, :regime_aligned,
            :rrg_quadrant, :rrg_quadrant_prev, :rrg_mono_fresh, :rrg_tier2,
            :vcp_composite, :vcp_execution_state, :vcp_distance_pivot_pct,
            :copytrade_l1h9_signal,
            :delta_new_to_watchlist, :delta_rrg_quadrant_change, :delta_consensus_new_today,
            :delta_score_change, :delta_any_signal,
            :signal_convergence, :lens_score, :narrative_zh, :highlight_tier,
            :holdings_aligned, :data_baseline_date, :sources_json, :computed_at
        )
        ON CONFLICT(trade_date, stock_id) DO UPDATE SET
            stock_name=excluded.stock_name,
            etf_add_count=excluded.etf_add_count,
            etf_reduce_count=excluded.etf_reduce_count,
            etf_add_codes_json=excluded.etf_add_codes_json,
            etf_flow_ntd=excluded.etf_flow_ntd,
            share_delta_total=excluded.share_delta_total,
            growth_pct=excluded.growth_pct,
            consensus_add=excluded.consensus_add,
            consensus_streak_days=excluded.consensus_streak_days,
            breadth_zone_200=excluded.breadth_zone_200,
            trend_posture=excluded.trend_posture,
            regime_aligned=excluded.regime_aligned,
            rrg_quadrant=excluded.rrg_quadrant,
            rrg_quadrant_prev=excluded.rrg_quadrant_prev,
            rrg_mono_fresh=excluded.rrg_mono_fresh,
            rrg_tier2=excluded.rrg_tier2,
            vcp_composite=excluded.vcp_composite,
            vcp_execution_state=excluded.vcp_execution_state,
            vcp_distance_pivot_pct=excluded.vcp_distance_pivot_pct,
            copytrade_l1h9_signal=excluded.copytrade_l1h9_signal,
            delta_new_to_watchlist=excluded.delta_new_to_watchlist,
            delta_rrg_quadrant_change=excluded.delta_rrg_quadrant_change,
            delta_consensus_new_today=excluded.delta_consensus_new_today,
            delta_score_change=excluded.delta_score_change,
            delta_any_signal=excluded.delta_any_signal,
            signal_convergence=excluded.signal_convergence,
            lens_score=excluded.lens_score,
            narrative_zh=excluded.narrative_zh,
            highlight_tier=excluded.highlight_tier,
            holdings_aligned=excluded.holdings_aligned,
            data_baseline_date=excluded.data_baseline_date,
            sources_json=excluded.sources_json,
            computed_at=excluded.computed_at
    """
    payload: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        codes = item.pop("etf_add_codes", None)
        if codes is None:
            item["etf_add_codes_json"] = item.get("etf_add_codes_json") or "[]"
        elif isinstance(codes, str):
            item["etf_add_codes_json"] = codes
        else:
            item["etf_add_codes_json"] = json.dumps(list(codes), ensure_ascii=False)
        src = item.get("sources_json")
        if isinstance(src, dict):
            item["sources_json"] = json.dumps(src, ensure_ascii=False)
        for flag in (
            "consensus_add",
            "regime_aligned",
            "rrg_mono_fresh",
            "rrg_tier2",
            "copytrade_l1h9_signal",
            "delta_new_to_watchlist",
            "delta_consensus_new_today",
            "delta_any_signal",
            "holdings_aligned",
        ):
            if flag in item:
                item[flag] = 1 if item[flag] else 0
        for opt in (
            "etf_flow_ntd",
            "share_delta_total",
            "growth_pct",
            "breadth_zone_200",
            "trend_posture",
            "rrg_quadrant",
            "rrg_quadrant_prev",
            "vcp_composite",
            "vcp_execution_state",
            "vcp_distance_pivot_pct",
            "delta_rrg_quadrant_change",
            "delta_score_change",
        ):
            item.setdefault(opt, None)
        item["computed_at"] = item.get("computed_at") or synced_at
        payload.append(item)
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_stock_daily_lens_for_date(
    conn: sqlite3.Connection,
    trade_date: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM stock_daily_lens
        WHERE trade_date = ?
        ORDER BY delta_any_signal DESC, signal_convergence DESC, lens_score DESC
        """,
        (trade_date,),
    ).fetchall()


def load_stock_daily_lens_row(
    conn: sqlite3.Connection,
    trade_date: str,
    stock_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM stock_daily_lens
        WHERE trade_date = ? AND stock_id = ?
        """,
        (trade_date, stock_id),
    ).fetchone()


def upsert_lens_daily_alert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    synced_at = utc_now_iso()
    items = row.get("items_json")
    if isinstance(items, (list, dict)):
        items_json = json.dumps(items, ensure_ascii=False)
    else:
        items_json = str(items or "[]")
    conn.execute(
        """
        INSERT INTO lens_daily_alert (
            trade_date, fire_count, delta_new_count, headline_zh, items_json, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET
            fire_count=excluded.fire_count,
            delta_new_count=excluded.delta_new_count,
            headline_zh=excluded.headline_zh,
            items_json=excluded.items_json,
            computed_at=excluded.computed_at
        """,
        (
            row["trade_date"],
            int(row.get("fire_count") or 0),
            int(row.get("delta_new_count") or 0),
            row["headline_zh"],
            items_json,
            row.get("computed_at") or synced_at,
        ),
    )
    conn.commit()


def load_lens_daily_alert(
    conn: sqlite3.Connection,
    trade_date: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM lens_daily_alert WHERE trade_date = ?",
        (trade_date,),
    ).fetchone()
