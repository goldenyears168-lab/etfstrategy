"""持倉盤中出場 · intraday-exit-playbook v2.1 · S0/S1a/S1b/S2/S2-lite."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_db.kbar import kbar_sql_minute
from stock_db.order_holdings import OrderHoldingSnapshotRow, load_order_holdings_snapshot_rows

if TYPE_CHECKING:
    from strategy_signal_radar import SellSignal

_STRUCTURAL_MIN_MINUTE = "09:05"
_S1B_QUADRANTS = frozenset({"weakening"})
_S0_PCT = 0.97
_S0B_FROM_HIGH_PCT = 0.97
_S0B_MIN_RUNUP_PCT = 0.01
_HOLDINGS_STRESS_PCT = 0.98
_HOLDINGS_STRESS_MIN = 3
_S2_LITE_PCT = 0.97


@dataclass(frozen=True)
class StructuralExitContext:
    portfolio_exit_mode: bool | None
    gate_ran: bool
    snapshot_date: str | None
    holdings_stress: bool = False
    holdings_stress_count: int = 0


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip() not in ("0", "false", "False")


def _minute_ge(a: str, b: str) -> bool:
    am = a[:5] if len(a) >= 5 else a
    bm = b[:5] if len(b) >= 5 else b
    return am >= bm


def structural_exit_allowed(poll_minute: str) -> bool:
    return _minute_ge(poll_minute, _STRUCTURAL_MIN_MINUTE)


def load_portfolio_exit_mode(conn: sqlite3.Connection, trade_date: str) -> tuple[bool | None, bool]:
    """Return (portfolio_exit_mode, gate_ran). mode is None when gate skipped or not run."""
    row = conn.execute(
        """
        SELECT event FROM order_intraday_exit_log
        WHERE trade_date = ? AND phase = 'gate'
          AND event IN ('gate_on', 'gate_off', 'gate_skip')
        ORDER BY checked_at DESC LIMIT 1
        """,
        (trade_date,),
    ).fetchone()
    if not row:
        return None, False
    if row[0] == "gate_skip":
        return None, True
    return row[0] == "gate_on", True


def _resolve_snapshot_date(conn: sqlite3.Connection, session_date: str) -> str | None:
    row = conn.execute(
        "SELECT 1 FROM order_holdings_snapshot WHERE snapshot_date = ? LIMIT 1",
        (session_date,),
    ).fetchone()
    if row:
        return session_date
    row = conn.execute(
        """
        SELECT snapshot_date FROM order_holdings_snapshot
        WHERE snapshot_date <= ?
        ORDER BY snapshot_date DESC LIMIT 1
        """,
        (session_date,),
    ).fetchone()
    return str(row[0]) if row else None


def _price_at_minute(
    conn: sqlite3.Connection,
    stock_id: str,
    session: str,
    minute: str,
) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_kbar_5m
        WHERE stock_id = ? AND trade_date = ? AND minute <= ?
        ORDER BY minute DESC LIMIT 1
        """,
        (stock_id, session, kbar_sql_minute(minute)),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def _day_high_at_minute(
    conn: sqlite3.Connection,
    stock_id: str,
    session: str,
    minute: str,
) -> float | None:
    row = conn.execute(
        """
        SELECT MAX(close) FROM stock_kbar_5m
        WHERE stock_id = ? AND trade_date = ? AND minute <= ?
        """,
        (stock_id, session, kbar_sql_minute(minute)),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def _vcp_stop_loss(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    as_of_session: str,
) -> float | None:
    row = conn.execute(
        """
        SELECT stop_loss FROM vcp_screen_scores_v2
        WHERE stock_id = ? AND as_of_date < ? AND stop_loss IS NOT NULL AND stop_loss > 0
        ORDER BY as_of_date DESC LIMIT 1
        """,
        (stock_id, as_of_session),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def _s1b_threshold(row: OrderHoldingSnapshotRow) -> float | None:
    if row.rrg_quadrant not in _S1B_QUADRANTS:
        return None
    return row.trigger_s1b


def _s2_threshold(row: OrderHoldingSnapshotRow) -> float | None:
    if row.structure_tier != "weak":
        return None
    if row.trigger_s1b is not None:
        return row.trigger_s1b
    if row.prev_close is not None:
        return round(row.prev_close * _S2_LITE_PCT, 2)
    return None


def _s0_threshold(row: OrderHoldingSnapshotRow) -> float | None:
    if row.prev_close is None:
        return None
    return round(row.prev_close * _S0_PCT, 2)


def _s2_lite_threshold(row: OrderHoldingSnapshotRow) -> float | None:
    if row.structure_tier == "weak":
        return None
    return _s0_threshold(row)


def evaluate_holdings_stress(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
    snapshot_rows: list[OrderHoldingSnapshotRow],
    holdings: dict[str, int],
    min_count: int = _HOLDINGS_STRESS_MIN,
) -> tuple[bool, int]:
    count = 0
    for row in snapshot_rows:
        qty = int(holdings.get(row.stock_id, 0))
        if qty <= 0 or row.prev_close is None:
            continue
        px = _price_at_minute(conn, row.stock_id, session_date, poll_minute)
        if px is None:
            continue
        if px <= round(row.prev_close * _HOLDINGS_STRESS_PCT, 2):
            count += 1
    return count >= min_count, count


def scan_holdings_exit_signals(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
    holdings: dict[str, int],
) -> tuple[list[Any], StructuralExitContext]:
    from strategy_signal_radar import SellSignal

    exit_mode, gate_ran = load_portfolio_exit_mode(conn, session_date)
    snap_date = _resolve_snapshot_date(conn, session_date)
    stress_on, stress_n = False, 0
    if snap_date and holdings:
        stress_on, stress_n = evaluate_holdings_stress(
            conn,
            session_date=session_date,
            poll_minute=poll_minute,
            snapshot_rows=load_order_holdings_snapshot_rows(conn, snap_date),
            holdings=holdings,
        )
    ctx = StructuralExitContext(
        portfolio_exit_mode=exit_mode,
        gate_ran=gate_ran,
        snapshot_date=snap_date,
        holdings_stress=stress_on,
        holdings_stress_count=stress_n,
    )
    if not structural_exit_allowed(poll_minute):
        return [], ctx
    if not snap_date or not holdings:
        return [], ctx

    s0_on = _env_flag("SIGNAL_RADAR_S0_WATCH", "1")
    s0b_on = _env_flag("SIGNAL_RADAR_S0B_WATCH", "1")
    s2lite_on = _env_flag("SIGNAL_RADAR_HOLDINGS_STRESS", "1")
    s1a_on = _env_flag("SIGNAL_RADAR_S1A_VCP", "1")

    snapshot_rows = load_order_holdings_snapshot_rows(conn, snap_date)
    signals: list[SellSignal] = []

    for row in snapshot_rows:
        qty = int(holdings.get(row.stock_id, 0))
        if qty <= 0:
            continue
        px = _price_at_minute(conn, row.stock_id, session_date, poll_minute)
        if px is None or px <= 0:
            continue

        if s1a_on:
            vcp_stop = _vcp_stop_loss(conn, row.stock_id, as_of_session=session_date)
            if vcp_stop is not None and px <= vcp_stop:
                signals.append(
                    SellSignal(
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        action="sell",
                        reason=f"S1a VCP stop · px {px:.2f} ≤ stop {vcp_stop:.2f}",
                        price=px,
                        poll_minute=poll_minute,
                        exit_mode="trigger_s1a",
                        in_holdings=True,
                        quantity=qty,
                    )
                )
                continue

        s1b_th = _s1b_threshold(row)
        if s1b_th is not None and px <= s1b_th:
            signals.append(
                SellSignal(
                    stock_id=row.stock_id,
                    stock_name=row.stock_name,
                    action="sell",
                    reason=(
                        f"S1b RRG weakening · px {px:.2f} ≤ trigger {s1b_th:.2f}"
                        + (f" (prev {row.prev_close:.2f})" if row.prev_close else "")
                    ),
                    price=px,
                    poll_minute=poll_minute,
                    exit_mode="trigger_s1b",
                    in_holdings=True,
                    quantity=qty,
                )
            )
            continue

        if exit_mode is True:
            s2_th = _s2_threshold(row)
            if s2_th is not None and px <= s2_th:
                signals.append(
                    SellSignal(
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        action="sell",
                        reason=f"S2 weak -3% · MODE=ON · px {px:.2f} ≤ trigger {s2_th:.2f}",
                        price=px,
                        poll_minute=poll_minute,
                        exit_mode="trigger_s2",
                        in_holdings=True,
                        quantity=qty,
                    )
                )
                continue

        if s2lite_on and stress_on:
            s2l_th = _s2_lite_threshold(row)
            if s2l_th is not None and px <= s2l_th:
                signals.append(
                    SellSignal(
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        action="sell",
                        reason=(
                            f"S2-lite holdings_stress · {stress_n}檔≤-2% · "
                            f"px {px:.2f} ≤ trigger {s2l_th:.2f}"
                        ),
                        price=px,
                        poll_minute=poll_minute,
                        exit_mode="trigger_s2_lite",
                        in_holdings=True,
                        quantity=qty,
                    )
                )
                continue

        if s0_on:
            s0_th = _s0_threshold(row)
            if s0_th is not None and px <= s0_th:
                signals.append(
                    SellSignal(
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        action="sell",
                        reason=(
                            f"S0 watch · px {px:.2f} ≤ {s0_th:.2f} (-3%) · "
                            f"tier={row.structure_tier or '—'} · advisory · 不送單"
                        ),
                        price=px,
                        poll_minute=poll_minute,
                        exit_mode="watch_s0",
                        in_holdings=True,
                        quantity=qty,
                    )
                )
                continue

        if s0b_on and row.prev_close:
            day_hi = _day_high_at_minute(conn, row.stock_id, session_date, poll_minute)
            if day_hi and day_hi >= row.prev_close * (1 + _S0B_MIN_RUNUP_PCT):
                s0b_th = round(day_hi * _S0B_FROM_HIGH_PCT, 2)
                if px <= s0b_th:
                    signals.append(
                        SellSignal(
                            stock_id=row.stock_id,
                            stock_name=row.stock_name,
                            action="sell",
                            reason=(
                                f"S0b watch · px {px:.2f} ≤ high×0.97 {s0b_th:.2f} "
                                f"(high {day_hi:.2f}) · advisory · 不送單"
                            ),
                            price=px,
                            poll_minute=poll_minute,
                            exit_mode="watch_s0b",
                            in_holdings=True,
                            quantity=qty,
                        )
                    )

    return signals, ctx


def scan_structural_sell_signals(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
    holdings: dict[str, int],
) -> tuple[list[Any], StructuralExitContext]:
    """Backward-compatible alias for scan_holdings_exit_signals."""
    return scan_holdings_exit_signals(
        conn,
        session_date=session_date,
        poll_minute=poll_minute,
        holdings=holdings,
    )


def persist_structural_triggers(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    checked_at: str,
    signals: list[Any],
    dry_run: bool = True,
) -> None:
    from stock_db.order_holdings import append_intraday_exit_log

    for sig in signals:
        append_intraday_exit_log(
            conn,
            trade_date=session_date,
            checked_at=checked_at,
            phase="structural",
            stock_id=sig.stock_id,
            event=sig.exit_mode or "structural_trigger",
            detail={
                "price": sig.price,
                "poll_minute": sig.poll_minute,
                "reason": sig.reason,
                "quantity": sig.quantity,
            },
            dry_run=dry_run,
        )
