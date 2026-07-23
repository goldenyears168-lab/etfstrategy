"""C18acc open-slot monitor · underwater_rebound pyramid add (O3-7)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from order.abc_v3_f1_position_monitor import (
    OpenAbcPosition,
    build_position_timeline_to_poll,
    evaluate_pyramid_rebound_on_timeline,
)
from order.abc_v3_f1_reentry import trading_days_between
from order.c18acc_monitor_config import (
    C18accPyramidAddMonitorConfig,
    STRATEGY_ID,
    load_c18acc_pyramid_add_monitor_config,
)
from order.c18acc_pyramid_ledger import C18accPyramidLedger, load_c18acc_pyramid_ledger

DEFAULT_ENTRY_POLL_MINUTE = "13:00"


@dataclass(frozen=True)
class OpenC18Position:
    slot: int
    symbol: str
    stock_name: str
    entry_session_date: str
    entry_poll_minute: str
    entry_price: float
    parent_cid: str


@dataclass
class C18PositionMonitorHit:
    kind: str  # pyramid_add_buy
    position: OpenC18Position
    poll_minute: str
    reason: str
    timeline_len: int = 0
    ret_from_entry_pct: float | None = None


@dataclass
class C18PositionMonitorResult:
    session_date: str
    poll_minute: str
    open_positions: list[OpenC18Position] = field(default_factory=list)
    hits: list[C18PositionMonitorHit] = field(default_factory=list)
    skipped_reason: str | None = None


def slot_parent_cid(pos: dict[str, Any]) -> str:
    slot = int(pos.get("slot") or 0)
    sid = str(pos.get("stock_id") or "").strip()
    entry = str(pos.get("entry_date") or "")
    return f"{STRATEGY_ID}_{slot}_{entry}_{sid}"


def is_kinematic_poll(poll_minute: str, *, interval_min: int = 30) -> bool:
    parts = str(poll_minute or "00:00").split(":")
    if len(parts) < 2:
        return False
    return int(parts[1]) % interval_min == 0


def list_open_c18_positions(
    state: dict[str, Any],
    *,
    session_date: str,
    trading_dates: list[str],
    max_hold_days: int = 10,
) -> list[OpenC18Position]:
    out: list[OpenC18Position] = []
    for pos in state.get("slots") or []:
        if not isinstance(pos, dict):
            continue
        entry_date = str(pos.get("entry_date") or "")
        if not entry_date or entry_date > session_date:
            continue
        held = trading_days_between(trading_dates, entry_date, session_date)
        if held < 0 or held >= max_hold_days:
            continue
        px = float(pos.get("entry_px") or 0.0)
        if px <= 0:
            continue
        sid = str(pos.get("stock_id") or "").strip()
        if not sid:
            continue
        parent = slot_parent_cid(pos)
        out.append(
            OpenC18Position(
                slot=int(pos.get("slot") or 0),
                symbol=sid,
                stock_name=str(pos.get("stock_name") or ""),
                entry_session_date=entry_date,
                entry_poll_minute=str(pos.get("entry_poll_minute") or DEFAULT_ENTRY_POLL_MINUTE),
                entry_price=px,
                parent_cid=parent,
            )
        )
    out.sort(key=lambda p: (p.entry_session_date, p.slot, p.symbol))
    return out


def _pyramid_already_done(ledger: C18accPyramidLedger, parent_cid: str) -> bool:
    return ledger.has_parent(parent_cid)


def _as_abc_position(pos: OpenC18Position) -> OpenAbcPosition:
    return OpenAbcPosition(
        symbol=pos.symbol,
        entry_session_date=pos.entry_session_date,
        entry_poll_minute=pos.entry_poll_minute,
        entry_price=pos.entry_price,
        quantity_shares=0,
        client_intent_id=pos.parent_cid,
        strategy_id=STRATEGY_ID,
        stock_name=pos.stock_name,
    )


def scan_c18acc_pyramid_adds(
    conn: sqlite3.Connection | None,
    *,
    state: dict[str, Any],
    session_date: str,
    poll_minute: str,
    trading_dates: list[str],
    pyramid_cfg: C18accPyramidAddMonitorConfig | None = None,
    ledger: C18accPyramidLedger | None = None,
    timelines: dict[str, list[dict[str, Any]]] | None = None,
) -> C18PositionMonitorResult:
    """Scan open C18acc slots for underwater_rebound pyramid add at ``poll_minute``."""
    pyramid_cfg = pyramid_cfg or load_c18acc_pyramid_add_monitor_config()
    ledger = ledger or load_c18acc_pyramid_ledger()

    if not pyramid_cfg.enabled:
        return C18PositionMonitorResult(
            session_date=session_date,
            poll_minute=poll_minute,
            skipped_reason="monitor_disabled",
        )
    if not is_kinematic_poll(poll_minute, interval_min=pyramid_cfg.poll_interval_minutes):
        return C18PositionMonitorResult(
            session_date=session_date,
            poll_minute=poll_minute,
            skipped_reason="not_kinematic_poll",
        )

    positions = list_open_c18_positions(
        state,
        session_date=session_date,
        trading_dates=trading_dates,
        max_hold_days=pyramid_cfg.max_hold_days,
    )
    result = C18PositionMonitorResult(
        session_date=session_date,
        poll_minute=poll_minute,
        open_positions=positions,
    )
    if not positions:
        return result

    tl_cache = timelines or {}
    for pos in positions:
        if _pyramid_already_done(ledger, pos.parent_cid):
            continue
        timeline = tl_cache.get(pos.parent_cid)
        if timeline is None and conn is not None:
            timeline = build_position_timeline_to_poll(
                conn,
                _as_abc_position(pos),
                session_date=session_date,
                poll_minute=poll_minute,
            )
        if not timeline or len(timeline) < 2:
            continue
        fired, _ = evaluate_pyramid_rebound_on_timeline(
            timeline, rebound_min=pyramid_cfg.rebound_min
        )
        if not fired:
            continue
        last = timeline[-1]
        result.hits.append(
            C18PositionMonitorHit(
                kind="pyramid_add_buy",
                position=pos,
                poll_minute=poll_minute,
                reason=f"underwater_rebound≥{pyramid_cfg.rebound_min}",
                timeline_len=len(timeline),
                ret_from_entry_pct=float(last.get("ret_from_entry_pct") or 0),
            )
        )
    return result


def process_c18acc_position_monitor(
    *,
    state: dict[str, Any],
    session_date: str,
    poll_minute: str,
    conn: sqlite3.Connection | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """High-level hook for screen / CLI. Default dry_run=True (no ledger write)."""
    from order.abc_v3_f1_reentry import load_tw_trading_dates

    own_conn = False
    if conn is None:
        from stock_db import DEFAULT_DB_PATH, connect

        conn = connect(DEFAULT_DB_PATH)
        own_conn = True
    try:
        trading_dates = load_tw_trading_dates(conn)
        scan = scan_c18acc_pyramid_adds(
            conn,
            state=state,
            session_date=session_date,
            poll_minute=poll_minute,
            trading_dates=trading_dates,
        )
    finally:
        if own_conn and conn is not None:
            conn.close()

    actions: list[dict[str, Any]] = []
    for hit in scan.hits:
        actions.append(
            {
                "kind": hit.kind,
                "symbol": hit.position.symbol,
                "slot": hit.position.slot,
                "pyramid_parent_cid": hit.position.parent_cid,
                "reason": hit.reason,
                "ret_from_entry_pct": hit.ret_from_entry_pct,
                "dry_run": dry_run,
                "recorded": False,
            }
        )

    return {
        "session_date": session_date,
        "poll_minute": poll_minute,
        "strategy_id": STRATEGY_ID,
        "open_n": len(scan.open_positions),
        "hits_n": len(scan.hits),
        "skipped_reason": scan.skipped_reason,
        "actions": actions,
    }
