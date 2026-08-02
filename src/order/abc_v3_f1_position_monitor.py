"""ABC v3+F1 open-position monitor · champion TP exit + underwater_rebound pyramid add."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from order.abc_v3_f1_lifecycle import LIFECYCLE_FILLED, LIFECYCLE_PARTIAL, entry_blocks_retry
from order.abc_v3_f1_monitor_config import (
    ChampionTpMonitorConfig,
    PyramidAddMonitorConfig,
    load_champion_tp_monitor_config,
    load_pyramid_add_monitor_config,
)
from order.abc_v3_f1_reentry import AbcV3F1Ledger, load_ledger, trading_days_between


@dataclass(frozen=True)
class OpenAbcPosition:
    symbol: str
    entry_session_date: str
    entry_poll_minute: str
    entry_price: float
    quantity_shares: int
    client_intent_id: str
    strategy_id: str
    stock_name: str = ""


@dataclass
class PositionMonitorHit:
    kind: str  # champion_tp_sell | hold_cap_sell | pyramid_add_buy
    position: OpenAbcPosition
    poll_minute: str
    reason: str
    timeline_len: int = 0
    ret_from_entry_pct: float | None = None


@dataclass
class PositionMonitorResult:
    session_date: str
    poll_minute: str
    open_positions: list[OpenAbcPosition] = field(default_factory=list)
    hits: list[PositionMonitorHit] = field(default_factory=list)
    skipped_reason: str | None = None


def _strategy_id(entry: dict[str, Any]) -> str:
    return str(entry.get("strategy_id") or entry.get("user_def") or "abc-v3-f1-pullback")


def _filled_qty(entry: dict[str, Any]) -> int:
    filled = int(entry.get("filled_qty") or 0)
    if filled > 0:
        return filled
    return int(entry.get("quantity_shares") or 0)


def list_open_abc_positions(
    ledger: AbcV3F1Ledger,
    *,
    session_date: str,
    trading_dates: list[str],
    hold_days: int,
    strategy_ids: tuple[str, ...] | None = None,
) -> list[OpenAbcPosition]:
    allowed = set(strategy_ids or ("abc-v3-f1-pullback", "abc-v3-f1-champion-tp"))
    out: list[OpenAbcPosition] = []
    seen: set[str] = set()
    for entry in ledger.entries:
        if not entry_blocks_retry(entry):
            continue
        status = str(entry.get("lifecycle_status") or "")
        if status not in (LIFECYCLE_FILLED, LIFECYCLE_PARTIAL):
            continue
        qty = _filled_qty(entry)
        if qty <= 0:
            continue
        sid = _strategy_id(entry)
        if sid not in allowed:
            continue
        entry_date = str(entry.get("session_date") or "")
        if not entry_date or entry_date > session_date:
            continue
        held = trading_days_between(trading_dates, entry_date, session_date)
        if held < 0:
            continue
        # Keep overdue positions so hold_cap_sell can still fire (research: else hold to cap).
        if held > hold_days + 2:
            continue
        cid = str(entry.get("client_intent_id") or "")
        if cid in seen:
            continue
        seen.add(cid)
        px = float(entry.get("avg_fill_price") or entry.get("ask_price") or 0.0)
        if px <= 0:
            continue
        out.append(
            OpenAbcPosition(
                symbol=str(entry.get("symbol") or "").strip(),
                entry_session_date=entry_date,
                entry_poll_minute=str(entry.get("poll_minute") or "09:15"),
                entry_price=px,
                quantity_shares=qty,
                client_intent_id=cid,
                strategy_id=sid,
                stock_name=str(entry.get("stock_name") or ""),
            )
        )
    out.sort(key=lambda p: (p.entry_session_date, p.symbol))
    return out


def _pyramid_already_done(ledger: AbcV3F1Ledger, parent_cid: str) -> bool:
    for e in ledger.entries:
        meta = e.get("pyramid_parent_cid") or (e.get("metadata") or {}).get("pyramid_parent_cid")
        if str(meta or "") == parent_cid and entry_blocks_retry(e):
            return True
    return False


def champion_tp_combo_from_config(cfg: ChampionTpMonitorConfig) -> Any:
    from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import (
        NEVER_SL,
        TakeProfitParams,
        TpSlCombo,
    )

    tp = TakeProfitParams(
        gw5_min=cfg.gw5_min,
        gw3_min=cfg.gw3_min,
        rv_leg=cfg.rv_leg,  # type: ignore[arg-type]
        rv_mode=cfg.rv_mode,  # type: ignore[arg-type]
        drv_min=cfg.drv_min,
        min_ret_pct=cfg.min_ret_pct,
    )
    return TpSlCombo(tp=tp, sl=NEVER_SL)


def evaluate_champion_tp_on_timeline(
    timeline: list[dict[str, Any]],
    *,
    combo: Any | None = None,
    cfg: ChampionTpMonitorConfig | None = None,
) -> tuple[bool, int | None]:
    """Return (fired, exit_snap_index) on a partial timeline ending at current poll."""
    if len(timeline) < 2:
        return False, None
    from research.backtest.archive.abc_v3_f1_kinematic_tp_sl_study import _tp_snap_ok

    if combo is None:
        combo = champion_tp_combo_from_config(cfg or load_champion_tp_monitor_config())
    tp = combo.tp
    entry = timeline[0]
    running_peak = float(entry.get("ret_from_entry_pct") or 0)
    tp_streak = 0
    for i in range(1, len(timeline)):
        prev = timeline[i - 1]
        snap = timeline[i]
        ret = float(snap.get("ret_from_entry_pct") or 0)
        running_peak = max(running_peak, ret)
        if _tp_snap_ok(snap, prev, entry, tp, running_peak_ret=running_peak):
            tp_streak += 1
            if tp_streak >= tp.consecutive:
                return True, i
        else:
            tp_streak = 0
    return False, None


def evaluate_pyramid_rebound_on_timeline(
    timeline: list[dict[str, Any]],
    *,
    rebound_min: float = 0.3,
) -> tuple[bool, int | None]:
    """True when the *last* snap in timeline is the first underwater_rebound add point."""
    if len(timeline) < 3:
        return False, None
    from research.backtest.abc_v3_f1_structural_pyramid_study import (
        default_pyramid_conditions,
        find_add_idx,
    )

    cond = next(c for c in default_pyramid_conditions() if c.id == "underwater_rebound")
    # Backtest legs end with an exit snap; find_add_idx excludes the last index.
    # Live polls end at the current minute — append a flat sentinel so the real
    # last snap can be evaluated as i == len(timeline) - 1.
    last = timeline[-1]
    sentinel = {**last, "ret_from_entry_pct": max(float(last.get("ret_from_entry_pct") or 0), 0.0)}
    work = [*timeline, sentinel]
    leg = {"timeline": work, "entry_t0": timeline[0]}
    idx = find_add_idx(leg, cond)
    if idx is None:
        return False, None
    return idx == len(timeline) - 1, idx


def build_position_timeline_to_poll(
    conn: sqlite3.Connection,
    position: OpenAbcPosition,
    *,
    session_date: str,
    poll_minute: str,
) -> list[dict[str, Any]] | None:
    """Build kinematic timeline from entry through current poll (PIT-safe)."""
    from market_benchmark import load_benchmark_close
    from research.backtest.archive.c18acc_kinematic_timeline_cache import _build_kinematic_timeline
    from research.backtest.archive.c18acc_triple_wma_extreme_profile import _frame_idx_for_minute
    from research.backtest.dual_wma_signal_backtest import _build_stock_frame_maps, _price_at
    from price_panels import load_price_panels
    from rrg_universe_intraday_panel import (
        WMA_MICRO_LENGTH,
        WMA_ULTRA_LENGTH,
        build_dual_wma_intraday_trajectories,
        intraday_poll_minutes,
    )

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    dates = [d for d in full_dates if position.entry_session_date <= d <= session_date]
    if not dates:
        return None
    frames, trajectories, _ = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        stock_ids=[position.symbol],
        poll_minutes=intraday_poll_minutes(),
        micro_lengths=(WMA_ULTRA_LENGTH, WMA_MICRO_LENGTH),
        kbar_bar_minutes=5,
    )
    stock_maps = _build_stock_frame_maps(trajectories, frames)
    points = stock_maps.get(position.symbol)
    if not points:
        return None
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    entry_i = _frame_idx_for_minute(frames, position.entry_session_date, position.entry_poll_minute)
    if entry_i < 0:
        return None
    exit_i = _frame_idx_for_minute(frames, session_date, poll_minute)
    if exit_i < entry_i:
        return None
    entry_px, _ = _price_at(
        conn,
        bench,
        position.symbol,
        position.entry_session_date,
        position.entry_poll_minute,
    )
    px = float(entry_px or position.entry_price)
    if px <= 0:
        return None
    snaps = _build_kinematic_timeline(
        conn,
        bench,
        stock_id=position.symbol,
        entry_i=entry_i,
        exit_i=exit_i,
        frames=frames,
        points_by_idx=points,
        entry_px=px,
    )
    return [s.to_dict() for s in snaps]


def scan_abc_v3_f1_positions(
    conn: sqlite3.Connection | None,
    *,
    session_date: str,
    poll_minute: str,
    trading_dates: list[str],
    ledger: AbcV3F1Ledger | None = None,
    champion_cfg: ChampionTpMonitorConfig | None = None,
    pyramid_cfg: PyramidAddMonitorConfig | None = None,
    timelines: dict[str, list[dict[str, Any]]] | None = None,
) -> PositionMonitorResult:
    """Scan open ABC ledger positions for TP sell / pyramid add at ``poll_minute``."""
    champion_cfg = champion_cfg or load_champion_tp_monitor_config()
    pyramid_cfg = pyramid_cfg or load_pyramid_add_monitor_config()
    ledger = ledger or load_ledger()

    if not champion_cfg.enabled and not pyramid_cfg.enabled:
        return PositionMonitorResult(
            session_date=session_date,
            poll_minute=poll_minute,
            skipped_reason="monitor_disabled",
        )

    hold_days = champion_cfg.hold_days_cap if champion_cfg.enabled else 3
    positions = list_open_abc_positions(
        ledger,
        session_date=session_date,
        trading_dates=trading_dates,
        hold_days=hold_days,
        strategy_ids=pyramid_cfg.applies_to,
    )
    result = PositionMonitorResult(
        session_date=session_date,
        poll_minute=poll_minute,
        open_positions=positions,
    )
    if not positions:
        return result

    combo = champion_tp_combo_from_config(champion_cfg) if champion_cfg.enabled else None
    tl_cache = timelines or {}

    for pos in positions:
        timeline = tl_cache.get(pos.client_intent_id)
        if timeline is None and conn is not None:
            timeline = build_position_timeline_to_poll(
                conn, pos, session_date=session_date, poll_minute=poll_minute
            )
        held = trading_days_between(
            trading_dates, pos.entry_session_date, session_date
        )
        last = timeline[-1] if timeline else {}
        ret = float(last.get("ret_from_entry_pct") or 0) if last else 0.0

        if champion_cfg.enabled and combo is not None and timeline and len(timeline) >= 2:
            fired, _ = evaluate_champion_tp_on_timeline(timeline, combo=combo)
            if fired:
                result.hits.append(
                    PositionMonitorHit(
                        kind="champion_tp_sell",
                        position=pos,
                        poll_minute=poll_minute,
                        reason=f"champion_tp {champion_cfg.variant_id}",
                        timeline_len=len(timeline),
                        ret_from_entry_pct=ret,
                    )
                )
                continue

        if champion_cfg.enabled and held > champion_cfg.hold_days_cap:
            # Policy B: Day=cap still waits full session for TP; force next session open+.
            result.hits.append(
                PositionMonitorHit(
                    kind="hold_cap_sell",
                    position=pos,
                    poll_minute=poll_minute,
                    reason=(
                        f"hold_cap>{champion_cfg.hold_days_cap}d held={held} "
                        f"(next-open force after full cap day)"
                    ),
                    timeline_len=len(timeline or []),
                    ret_from_entry_pct=ret,
                )
            )
            continue

        if not timeline or len(timeline) < 2:
            continue

        if (
            pyramid_cfg.enabled
            and pos.strategy_id in pyramid_cfg.applies_to
            and not _pyramid_already_done(ledger, pos.client_intent_id)
        ):
            fired, _ = evaluate_pyramid_rebound_on_timeline(
                timeline, rebound_min=pyramid_cfg.rebound_min
            )
            if fired:
                result.hits.append(
                    PositionMonitorHit(
                        kind="pyramid_add_buy",
                        position=pos,
                        poll_minute=poll_minute,
                        reason=f"underwater_rebound≥{pyramid_cfg.rebound_min}",
                        timeline_len=len(timeline),
                        ret_from_entry_pct=ret,
                    )
                )

    return result


def process_abc_v3_f1_position_monitor(
    *,
    session_date: str,
    poll_minute: str,
    conn: sqlite3.Connection | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """High-level hook for radar / CLI. Default dry_run=True (no broker submit)."""
    from order.abc_v3_f1_reentry import load_tw_trading_dates

    own_conn = False
    if conn is None:
        from stock_db import DEFAULT_DB_PATH, connect

        conn = connect(DEFAULT_DB_PATH)
        own_conn = True
    try:
        trading_dates = load_tw_trading_dates(conn)
        scan = scan_abc_v3_f1_positions(
            conn,
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
                "client_intent_id": hit.position.client_intent_id,
                "reason": hit.reason,
                "ret_from_entry_pct": hit.ret_from_entry_pct,
                "dry_run": dry_run,
                "submitted": False,
            }
        )

    return {
        "session_date": session_date,
        "poll_minute": poll_minute,
        "open_n": len(scan.open_positions),
        "hits_n": len(scan.hits),
        "skipped_reason": scan.skipped_reason,
        "actions": actions,
    }
