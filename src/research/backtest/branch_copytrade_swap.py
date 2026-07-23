"""Fixed-slot Copytrade with rank-based swap when slots are full.

Used as Phase-B overlay for broker-branch copytrade champions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from analytics.bench import bench_return_entry_to_exit
from copytrade.branch_signals import mean_net
from copytrade.signals import CopytradeSignal
from flow_returns import (
    exit_close_date_from_entry,
    return_pct,
    stock_close,
    stock_open,
)
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    compute_leg,
    count_hold_trading_days,
    leg_allocations_ntd,
    resolve_entry_date,
)


@dataclass
class _ActiveSlot:
    signal_date: str
    entry_date: str
    scheduled_exit: str
    mean_net: float
    signals: list[CopytradeSignal]
    allocs: list[float]
    deployed_ntd: float


@dataclass
class SwapSimResult:
    n_signals: int
    n_entries: int
    n_swaps: int
    n_skipped: int
    n_natural_exits: int
    recycled_total_pnl_ntd: float
    recycled_total_alpha_ntd: float
    recycled_locked_days: int
    signal_capture_pct: float | None
    total_capital_ntd: float
    period_return_pct: float | None
    alpha_per_locked_day: float | None
    trades: list[dict] = field(default_factory=list)


def _leg_pnl_custom_exit(
    conn: sqlite3.Connection,
    sig: CopytradeSignal,
    *,
    entry_date: str,
    exit_date: str,
    allocated_ntd: float,
    cost_bps: float,
    entry_price_mode: str,
    exit_price_mode: str,
) -> tuple[float, float, str]:
    """Return (pnl_ntd, return_pct, status)."""
    if entry_price_mode == "close":
        entry_px = stock_close(conn, sig.stock_id, entry_date)
    else:
        entry_px = stock_open(conn, sig.stock_id, entry_date)
    if exit_price_mode == "open":
        exit_px = stock_open(conn, sig.stock_id, exit_date)
    else:
        exit_px = stock_close(conn, sig.stock_id, exit_date)
    if entry_px is None or exit_px is None or entry_px <= 0:
        return 0.0, 0.0, "skip_no_prices"
    gross = return_pct(entry_px, exit_px)
    net = gross - cost_bps / 100.0
    return allocated_ntd * net / 100.0, net, "complete"


def _close_slot(
    conn: sqlite3.Connection,
    slot: _ActiveSlot,
    *,
    exit_date: str,
    cost_bps: float,
    entry_price_mode: str,
    exit_price_mode: str,
    reason: str,
) -> dict:
    pnl = 0.0
    deployed = 0.0
    for sig, alloc in zip(slot.signals, slot.allocs):
        leg_pnl, _, status = _leg_pnl_custom_exit(
            conn,
            sig,
            entry_date=slot.entry_date,
            exit_date=exit_date,
            allocated_ntd=alloc,
            cost_bps=cost_bps,
            entry_price_mode=entry_price_mode,
            exit_price_mode=exit_price_mode,
        )
        if status == "complete":
            pnl += leg_pnl
            deployed += alloc
    bench_ret = bench_return_entry_to_exit(
        conn,
        slot.entry_date,
        exit_date,
        entry_price_mode=entry_price_mode,
    )
    if bench_ret is None:
        bench_ret = 0.0
    # When exiting at open, approximate bench with open→open if available via same helper
    # (bench helper uses entry_price_mode for entry and close for exit — acceptable research approx).
    bench_pnl = deployed * bench_ret / 100.0
    return {
        "signal_date": slot.signal_date,
        "entry_date": slot.entry_date,
        "exit_date": exit_date,
        "deployed_ntd": deployed,
        "pnl_ntd": pnl,
        "alpha_ntd": pnl - bench_pnl,
        "mean_net": slot.mean_net,
        "exit_reason": reason,
        "status": "complete" if deployed > 0 else "skip_no_prices",
    }


def simulate_fixed_slots_with_swap(
    conn: sqlite3.Connection,
    grouped: dict[str, list[CopytradeSignal]],
    *,
    n_slots: int,
    entry_lag_days: int,
    hold_trading_days: int,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    cost_bps: float = 0.0,
    entry_price_mode: str = "open",
) -> SwapSimResult:
    """Slot-full days: swap weakest slot if new basket mean_net is strictly higher."""
    if n_slots < 1:
        raise ValueError("n_slots must be >= 1")
    total_capital = float(n_slots) * float(capital_ntd)
    active: list[_ActiveSlot] = []
    trades: list[dict] = []
    n_swaps = 0
    n_skipped = 0
    n_entries = 0
    n_natural = 0
    n_signals = 0

    for signal_date in sorted(grouped):
        legs = grouped[signal_date]
        if not legs:
            continue
        entry_date = resolve_entry_date(conn, signal_date, entry_lag_days)
        if entry_date is None:
            continue
        scheduled_exit = exit_close_date_from_entry(
            conn, entry_date, hold_trading_days
        )
        if scheduled_exit is None:
            continue
        n_signals += 1

        # Natural exits: free slots whose scheduled exit is before this entry day.
        remain: list[_ActiveSlot] = []
        for slot in active:
            if slot.scheduled_exit < entry_date:
                tr = _close_slot(
                    conn,
                    slot,
                    exit_date=slot.scheduled_exit,
                    cost_bps=cost_bps,
                    entry_price_mode=entry_price_mode,
                    exit_price_mode="close",
                    reason="hold_expiry",
                )
                if tr["status"] == "complete":
                    trades.append(tr)
                    n_natural += 1
            else:
                remain.append(slot)
        active = remain

        new_mean = mean_net(legs)
        allocs = leg_allocations_ntd(legs, capital_ntd, "equal")
        # Verify at least one priced leg at planned hold (probe).
        probe_ok = False
        for sig, alloc in zip(legs, allocs):
            lg = compute_leg(
                conn,
                sig,
                entry_date=entry_date,
                exit_date=scheduled_exit,
                allocated_ntd=alloc,
                cost_bps=cost_bps,
                entry_price_mode=entry_price_mode,
            )
            if lg.status == "complete":
                probe_ok = True
                break
        if not probe_ok:
            n_skipped += 1
            continue

        if len(active) < n_slots:
            active.append(
                _ActiveSlot(
                    signal_date=signal_date,
                    entry_date=entry_date,
                    scheduled_exit=scheduled_exit,
                    mean_net=new_mean,
                    signals=list(legs),
                    allocs=list(allocs),
                    deployed_ntd=float(capital_ntd),
                )
            )
            n_entries += 1
            continue

        # Slots full → maybe swap weakest.
        weakest_i = min(range(len(active)), key=lambda i: active[i].mean_net)
        weakest = active[weakest_i]
        if new_mean <= weakest.mean_net:
            n_skipped += 1
            continue

        # Early exit weakest at open of new entry day, then enter.
        tr = _close_slot(
            conn,
            weakest,
            exit_date=entry_date,
            cost_bps=cost_bps,
            entry_price_mode=entry_price_mode,
            exit_price_mode="open",
            reason="swap_out",
        )
        if tr["status"] == "complete":
            trades.append(tr)
        active.pop(weakest_i)
        active.append(
            _ActiveSlot(
                signal_date=signal_date,
                entry_date=entry_date,
                scheduled_exit=scheduled_exit,
                mean_net=new_mean,
                signals=list(legs),
                allocs=list(allocs),
                deployed_ntd=float(capital_ntd),
            )
        )
        n_swaps += 1
        n_entries += 1

    # Flush remaining at scheduled exit.
    for slot in active:
        tr = _close_slot(
            conn,
            slot,
            exit_date=slot.scheduled_exit,
            cost_bps=cost_bps,
            entry_price_mode=entry_price_mode,
            exit_price_mode="close",
            reason="hold_expiry_final",
        )
        if tr["status"] == "complete":
            trades.append(tr)
            n_natural += 1

    complete = [t for t in trades if t.get("status") == "complete"]
    total_pnl = sum(float(t["pnl_ntd"]) for t in complete)
    total_alpha = sum(float(t["alpha_ntd"]) for t in complete)
    locked = 0
    for t in complete:
        locked += count_hold_trading_days(
            conn, str(t["entry_date"]), str(t["exit_date"])
        )
    capture = round(100.0 * n_entries / n_signals, 2) if n_signals else None
    period_ret = (
        round(100.0 * total_pnl / total_capital, 4) if total_capital > 0 else None
    )
    return SwapSimResult(
        n_signals=n_signals,
        n_entries=n_entries,
        n_swaps=n_swaps,
        n_skipped=n_skipped,
        n_natural_exits=n_natural,
        recycled_total_pnl_ntd=round(total_pnl, 2),
        recycled_total_alpha_ntd=round(total_alpha, 2),
        recycled_locked_days=locked,
        signal_capture_pct=capture,
        total_capital_ntd=total_capital,
        period_return_pct=period_ret,
        alpha_per_locked_day=(
            round(total_alpha / locked, 4) if locked else None
        ),
        trades=complete,
    )


def swap_result_to_dict(result: SwapSimResult) -> dict:
    return {
        "n_signals": result.n_signals,
        "n_entries": result.n_entries,
        "n_swaps": result.n_swaps,
        "n_skipped": result.n_skipped,
        "n_natural_exits": result.n_natural_exits,
        "recycled_total_pnl_ntd": result.recycled_total_pnl_ntd,
        "recycled_total_alpha_ntd": result.recycled_total_alpha_ntd,
        "recycled_locked_days": result.recycled_locked_days,
        "signal_capture_pct": result.signal_capture_pct,
        "total_capital_ntd": result.total_capital_ntd,
        "period_return_pct": result.period_return_pct,
        "alpha_per_locked_day": result.alpha_per_locked_day,
    }
