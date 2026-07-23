"""元大-松江分點 Copytrade · L×H×槽×門檻×Top-N sweep + swap overlay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from copytrade.branch_signals import (
    DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    SHARES_PER_LOT,
    iter_branch_buy_signals,
)
from copytrade.signals import CopytradeSignal, group_signals_by_date
from research.backtest.branch_copytrade_swap import (
    simulate_fixed_slots_with_swap,
    swap_result_to_dict,
)
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    run_copytrade_backtest,
    select_executed_signal_days,
    simulate_fixed_slots,
)
from report_paths import REPORTS_RESEARCH

STRATEGY_TAG = "yuanta-songjiang"
ETF_CODE_PROXY = "YUANTA_SJ"  # run_copytrade_backtest requires etf_code label

DEFAULT_MIN_NETS = (50_000.0, 100_000.0, 200_000.0, 500_000.0)
DEFAULT_TOP_NS = (1, 3, 5)
DEFAULT_ENTRY_LAGS = (0, 1, 2)  # L1, L2, L3
DEFAULT_HOLDS = tuple(range(1, 21))
DEFAULT_SLOTS = (1, 3, 5, 9)
DEFAULT_WINDOW_START = "2024-07-01"


@dataclass(frozen=True)
class SweepCell:
    min_net_shares: float
    top_n: int
    entry_lag_days: int
    hold_trading_days: int
    n_slots: int
    slots_mode: str
    n_signals: int
    n_complete: int
    recycled_n_cycles: int
    recycled_total_pnl_ntd: float
    recycled_total_alpha_ntd: float
    recycled_locked_days: int
    signal_capture_pct: float | None
    alpha_per_locked_day: float | None
    total_capital_ntd: float
    period_return_pct: float | None
    unconstrained_total_pnl_ntd: float
    unconstrained_total_alpha_ntd: float


def day_results_to_dicts(result) -> list[dict]:
    out: list[dict] = []
    for d in result.signal_days:
        out.append(
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "n_legs": d.n_legs,
                "deployed_ntd": d.deployed_ntd,
                "pnl_ntd": d.pnl_ntd,
                "return_pct": d.return_pct,
                "bench_return_pct": d.bench_return_pct,
                "alpha_ntd": d.alpha_ntd,
                "status": d.status,
            }
        )
    return out


def _lag_label(entry_lag_days: int) -> str:
    return f"L{entry_lag_days + 1}"


def load_champion_trade_ledger(
    conn: sqlite3.Connection,
    champion: dict,
    *,
    trader_id: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
) -> dict:
    """Replay fixed-slot execution for one champion → leg-level trade rows."""
    tid = trader_id or DEFAULT_TRADER_ID_YUANTA_SONGJIANG
    w_start = window_start or DEFAULT_WINDOW_START
    w_end = window_end or date.today().isoformat()
    min_net = float(champion.get("min_net_shares") or 0)
    top_n = int(champion.get("top_n") or 1)
    lag = int(champion.get("entry_lag_days") or 0)
    hold = int(champion.get("hold_trading_days") or 1)
    n_slots = int(champion.get("n_slots") or 1)
    etf_proxy = str(champion.get("etf_code_proxy") or ETF_CODE_PROXY)

    grouped = build_signals(
        conn,
        trader_id=tid,
        min_net_shares=min_net,
        top_n=top_n,
        window_start=w_start,
        window_end=w_end,
        min_avg_volume_shares=min_avg_volume_shares,
    )
    result = run_copytrade_backtest(
        conn,
        etf_proxy,
        strategy_id=str(champion.get("strategy_id") or f"{_lag_label(lag)}H{hold}"),
        strategy_label="champion-ledger",
        entry_lag_days=lag,
        hold_trading_days=hold,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=0.0,
        window_start=w_start,
        window_end=w_end,
        grouped=grouped,
    )
    day_by_sig = {d.signal_date: d for d in result.signal_days}
    day_dicts = day_results_to_dicts(result)
    executed, meta = select_executed_signal_days(day_dicts, n_slots=n_slots)

    name_map = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            """
            SELECT stock_id, stock_name
            FROM etf_holdings
            WHERE stock_name IS NOT NULL AND TRIM(stock_name) != ''
            GROUP BY stock_id
            """
        ).fetchall()
    }

    trades: list[dict] = []
    for i, day in enumerate(executed, 1):
        sig_d = str(day["signal_date"])
        full = day_by_sig.get(sig_d)
        if full is None:
            continue
        complete_legs = [lg for lg in full.legs if lg.status == "complete"]
        for lg in complete_legs:
            sid = str(lg.stock_id)
            trades.append(
                {
                    "cycle": i,
                    "signal_date": lg.signal_date,
                    "stock_id": sid,
                    "stock_name": name_map.get(sid) or lg.stock_name or sid,
                    "net_shares": float(lg.share_delta),
                    "net_lots": round(float(lg.share_delta) / SHARES_PER_LOT, 2),
                    "entry_date": lg.entry_date,
                    "entry_px": lg.entry_px,
                    "entry_price_mode": "open",
                    "exit_date": lg.exit_date,
                    "exit_px": lg.exit_px,
                    "exit_price_mode": "close",
                    "allocated_ntd": round(float(lg.allocated_ntd), 2),
                    "return_pct": round(float(lg.return_pct), 4),
                    "pnl_ntd": round(float(lg.pnl_ntd), 2),
                    "basket_return_pct": round(float(full.return_pct), 4),
                    "basket_pnl_ntd": round(float(full.pnl_ntd), 2),
                    "basket_alpha_ntd": round(float(full.alpha_ntd), 2),
                    "bench_return_pct": round(float(full.bench_return_pct), 4),
                }
            )

    wins = sum(1 for t in trades if float(t["return_pct"]) > 0)
    return {
        "champion": {
            "strategy_id": champion.get("strategy_id"),
            "entry_row": champion.get("entry_row") or _lag_label(lag),
            "hold_trading_days": hold,
            "n_slots": n_slots,
            "min_net_lots": champion.get("min_net_lots")
            or (min_net / SHARES_PER_LOT),
            "top_n": top_n,
            "period_return_pct": champion.get("period_return_pct"),
        },
        "meta": {
            "n_signals": meta.get("n_signals"),
            "n_cycles": meta.get("recycled_n_cycles"),
            "n_skipped": meta.get("n_skipped"),
            "signal_capture_pct": meta.get("signal_capture_pct"),
            "n_trade_legs": len(trades),
            "leg_win_rate_pct": (
                round(100.0 * wins / len(trades), 2) if trades else None
            ),
            "sum_pnl_ntd": round(sum(float(t["pnl_ntd"]) for t in trades), 2),
            "window_start": w_start,
            "window_end": w_end,
            "trader_id": tid,
        },
        "trades": trades,
    }


def build_signals(
    conn: sqlite3.Connection,
    *,
    trader_id: str,
    min_net_shares: float,
    top_n: int,
    window_start: str | None,
    window_end: str | None,
    min_avg_volume_shares: float | None,
) -> dict[str, list[CopytradeSignal]]:
    signals = iter_branch_buy_signals(
        conn,
        trader_id,
        min_net_shares=min_net_shares,
        top_n=top_n,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=min_avg_volume_shares,
    )
    return group_signals_by_date(signals)


def evaluate_slots(
    conn: sqlite3.Connection,
    day_dicts: list[dict],
    *,
    n_slots: int,
    capital_ntd: float,
    slots_mode: str = "fixed",
    hold_trading_days: int | None = None,
) -> dict:
    if slots_mode == "match_horizon":
        if hold_trading_days is None:
            raise ValueError("hold_trading_days required for match_horizon")
        slots = int(hold_trading_days)
        total_capital = capital_ntd * slots
    else:
        slots = int(n_slots)
        total_capital = capital_ntd * slots
    sim = simulate_fixed_slots(
        conn, day_dicts, n_slots=slots, capital_ntd=capital_ntd
    )
    pnl = float(sim.get("recycled_total_pnl_ntd") or 0.0)
    period_ret = (
        round(100.0 * pnl / total_capital, 4) if total_capital > 0 else None
    )
    return {
        **sim,
        "n_slots": slots,
        "slots_mode": slots_mode,
        "total_capital_ntd": total_capital,
        "period_return_pct": period_ret,
    }


def run_phase_a_sweep(
    conn: sqlite3.Connection,
    *,
    trader_id: str = DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str | None = None,
    min_nets: tuple[float, ...] = DEFAULT_MIN_NETS,
    top_ns: tuple[int, ...] = DEFAULT_TOP_NS,
    entry_lags: tuple[int, ...] = DEFAULT_ENTRY_LAGS,
    holds: tuple[int, ...] = DEFAULT_HOLDS,
    slot_counts: tuple[int, ...] = DEFAULT_SLOTS,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
    include_match_horizon: bool = True,
    progress_every: int = 50,
    etf_code_proxy: str = ETF_CODE_PROXY,
    strategy_label_prefix: str = "元大松江",
) -> list[SweepCell]:
    w_end = window_end or date.today().isoformat()
    cells: list[SweepCell] = []
    n_done = 0
    for min_net in min_nets:
        for top_n in top_ns:
            grouped = build_signals(
                conn,
                trader_id=trader_id,
                min_net_shares=min_net,
                top_n=top_n,
                window_start=window_start,
                window_end=w_end,
                min_avg_volume_shares=min_avg_volume_shares,
            )
            if not grouped:
                continue
            for lag in entry_lags:
                for hold in holds:
                    sid = f"{_lag_label(lag)}H{hold}"
                    result = run_copytrade_backtest(
                        conn,
                        etf_code_proxy,
                        strategy_id=sid,
                        strategy_label=(
                            f"{strategy_label_prefix} {sid} "
                            f"net≥{min_net:.0f} top{top_n}"
                        ),
                        entry_lag_days=lag,
                        hold_trading_days=hold,
                        entry_price_mode="open",
                        capital_ntd=capital_ntd,
                        cost_bps=0.0,
                        window_start=window_start,
                        window_end=w_end,
                        grouped=grouped,
                    )
                    day_dicts = day_results_to_dicts(result)
                    slot_jobs: list[tuple[str, int]] = [
                        ("fixed", s) for s in slot_counts
                    ]
                    if include_match_horizon:
                        slot_jobs.append(("match_horizon", hold))
                    for mode, n_slots in slot_jobs:
                        sim = evaluate_slots(
                            conn,
                            day_dicts,
                            n_slots=n_slots,
                            capital_ntd=capital_ntd,
                            slots_mode=mode,
                            hold_trading_days=hold,
                        )
                        cells.append(
                            SweepCell(
                                min_net_shares=float(min_net),
                                top_n=int(top_n),
                                entry_lag_days=int(lag),
                                hold_trading_days=int(hold),
                                n_slots=int(sim["n_slots"]),
                                slots_mode=str(sim["slots_mode"]),
                                n_signals=int(sim.get("n_signals") or 0),
                                n_complete=int(result.n_complete_days),
                                recycled_n_cycles=int(
                                    sim.get("recycled_n_cycles") or 0
                                ),
                                recycled_total_pnl_ntd=float(
                                    sim.get("recycled_total_pnl_ntd") or 0
                                ),
                                recycled_total_alpha_ntd=float(
                                    sim.get("recycled_total_alpha_ntd") or 0
                                ),
                                recycled_locked_days=int(
                                    sim.get("recycled_locked_days") or 0
                                ),
                                signal_capture_pct=(
                                    float(sim["signal_capture_pct"])
                                    if sim.get("signal_capture_pct") is not None
                                    else None
                                ),
                                alpha_per_locked_day=(
                                    float(sim["alpha_per_locked_day"])
                                    if sim.get("alpha_per_locked_day") is not None
                                    else None
                                ),
                                total_capital_ntd=float(sim["total_capital_ntd"]),
                                period_return_pct=(
                                    float(sim["period_return_pct"])
                                    if sim.get("period_return_pct") is not None
                                    else None
                                ),
                                unconstrained_total_pnl_ntd=float(
                                    result.total_pnl_ntd
                                ),
                                unconstrained_total_alpha_ntd=float(
                                    result.total_alpha_ntd
                                ),
                            )
                        )
                        n_done += 1
                        if progress_every and n_done % progress_every == 0:
                            print(
                                f"  phaseA cells={n_done} "
                                f"last={_lag_label(lag)}H{hold} "
                                f"net={min_net:.0f} top={top_n} "
                                f"slots={sim['n_slots']}({mode})"
                            )
    return cells


def _sort_key_return(c: SweepCell) -> tuple:
    ret = c.period_return_pct if c.period_return_pct is not None else -1e18
    alpha = c.recycled_total_alpha_ntd
    apld = c.alpha_per_locked_day if c.alpha_per_locked_day is not None else -1e18
    # Prefer higher return/alpha/efficiency; then lower turnover.
    return (ret, alpha, apld, -c.recycled_n_cycles)


def select_champions(cells: list[SweepCell]) -> dict[str, SweepCell | None]:
    """Champion pool: fixed slots only, L≥1 (already), executable."""
    pool = [c for c in cells if c.slots_mode == "fixed"]
    if not pool:
        return {
            "by_period_return": None,
            "by_alpha": None,
            "by_alpha_per_locked_day": None,
        }
    by_ret = max(pool, key=_sort_key_return)
    by_alpha = max(
        pool,
        key=lambda c: (
            c.recycled_total_alpha_ntd,
            c.period_return_pct or -1e18,
            c.alpha_per_locked_day or -1e18,
            -c.recycled_n_cycles,
        ),
    )
    by_apld = max(
        pool,
        key=lambda c: (
            c.alpha_per_locked_day if c.alpha_per_locked_day is not None else -1e18,
            c.recycled_total_alpha_ntd,
            c.period_return_pct or -1e18,
            -c.recycled_n_cycles,
        ),
    )
    return {
        "by_period_return": by_ret,
        "by_alpha": by_alpha,
        "by_alpha_per_locked_day": by_apld,
    }


def cell_to_dict(c: SweepCell) -> dict:
    d = asdict(c)
    d["entry_row"] = _lag_label(c.entry_lag_days)
    d["strategy_id"] = f"{_lag_label(c.entry_lag_days)}H{c.hold_trading_days}"
    d["min_net_lots"] = c.min_net_shares / SHARES_PER_LOT
    return d


def run_phase_b_swap(
    conn: sqlite3.Connection,
    champion: SweepCell,
    *,
    trader_id: str = DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str | None = None,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
    etf_code_proxy: str = ETF_CODE_PROXY,
) -> dict:
    w_end = window_end or date.today().isoformat()
    grouped = build_signals(
        conn,
        trader_id=trader_id,
        min_net_shares=champion.min_net_shares,
        top_n=champion.top_n,
        window_start=window_start,
        window_end=w_end,
        min_avg_volume_shares=min_avg_volume_shares,
    )
    skip_sim = evaluate_slots(
        conn,
        day_results_to_dicts(
            run_copytrade_backtest(
                conn,
                etf_code_proxy,
                strategy_id=f"{_lag_label(champion.entry_lag_days)}"
                f"H{champion.hold_trading_days}",
                strategy_label="phaseB-skip-baseline",
                entry_lag_days=champion.entry_lag_days,
                hold_trading_days=champion.hold_trading_days,
                capital_ntd=capital_ntd,
                grouped=grouped,
                window_start=window_start,
                window_end=w_end,
            )
        ),
        n_slots=champion.n_slots,
        capital_ntd=capital_ntd,
        slots_mode="fixed",
    )
    swap = simulate_fixed_slots_with_swap(
        conn,
        grouped,
        n_slots=champion.n_slots,
        entry_lag_days=champion.entry_lag_days,
        hold_trading_days=champion.hold_trading_days,
        capital_ntd=capital_ntd,
    )
    swap_d = swap_result_to_dict(swap)
    skip_ret = skip_sim.get("period_return_pct")
    swap_ret = swap_d.get("period_return_pct")
    return {
        "champion": cell_to_dict(champion),
        "skip_when_full": {
            "recycled_total_pnl_ntd": skip_sim.get("recycled_total_pnl_ntd"),
            "recycled_total_alpha_ntd": skip_sim.get("recycled_total_alpha_ntd"),
            "signal_capture_pct": skip_sim.get("signal_capture_pct"),
            "period_return_pct": skip_ret,
            "recycled_n_cycles": skip_sim.get("recycled_n_cycles"),
            "alpha_per_locked_day": skip_sim.get("alpha_per_locked_day"),
        },
        "swap_when_full": swap_d,
        "delta": {
            "period_return_pct": (
                None
                if skip_ret is None or swap_ret is None
                else round(float(swap_ret) - float(skip_ret), 4)
            ),
            "recycled_total_pnl_ntd": round(
                float(swap_d["recycled_total_pnl_ntd"])
                - float(skip_sim.get("recycled_total_pnl_ntd") or 0),
                2,
            ),
            "recycled_total_alpha_ntd": round(
                float(swap_d["recycled_total_alpha_ntd"])
                - float(skip_sim.get("recycled_total_alpha_ntd") or 0),
                2,
            ),
            "n_swaps": swap_d["n_swaps"],
        },
        "swap_beats_skip": (
            swap_ret is not None
            and skip_ret is not None
            and float(swap_ret) > float(skip_ret)
        ),
    }


def format_report_md(
    *,
    window_start: str,
    window_end: str,
    trader_id: str,
    champions: dict[str, SweepCell | None],
    phase_b: dict | None,
    n_cells: int,
    branch_label: str = "元大-松江",
) -> str:
    lines = [
        f"# {branch_label} Copytrade Sweep",
        "",
        f"- window: `{window_start}` → `{window_end}`",
        f"- trader_id: `{trader_id}`",
        f"- branch: `{branch_label}`",
        f"- phase_a_cells: {n_cells}",
        f"- cost_bps: 0（未扣稅費／滑價）",
        "",
        "## Phase A champions（fixed slots）",
        "",
    ]
    for key, label in (
        ("by_period_return", "主冠軍 · max period return%"),
        ("by_alpha", "次優 · max recycled α"),
        ("by_alpha_per_locked_day", "次優 · max α / locked day"),
    ):
        c = champions.get(key)
        lines.append(f"### {label}")
        if c is None:
            lines.append("- (none)")
            lines.append("")
            continue
        lines.extend(
            [
                f"- strategy: `{_lag_label(c.entry_lag_days)}H{c.hold_trading_days}`",
                f"- min_net: {c.min_net_shares:.0f} 股 ({c.min_net_shares/SHARES_PER_LOT:.0f} 張)",
                f"- top_n: {c.top_n}",
                f"- slots: {c.n_slots} × {DEFAULT_SIGNAL_CAPITAL_NTD:.0f} = {c.total_capital_ntd:.0f}",
                f"- period_return%: {c.period_return_pct}",
                f"- recycled_pnl: {c.recycled_total_pnl_ntd}",
                f"- recycled_alpha: {c.recycled_total_alpha_ntd}",
                f"- capture%: {c.signal_capture_pct}",
                f"- cycles: {c.recycled_n_cycles}",
                f"- alpha_per_locked_day: {c.alpha_per_locked_day}",
                "",
            ]
        )
    lines.append("## Phase B · swap vs skip-when-full")
    lines.append("")
    if not phase_b:
        lines.append("- (not run)")
    else:
        d = phase_b.get("delta") or {}
        lines.extend(
            [
                f"- swap_beats_skip: **{phase_b.get('swap_beats_skip')}**",
                f"- Δ period_return_pp: {d.get('period_return_pct')}",
                f"- Δ recycled_pnl: {d.get('recycled_total_pnl_ntd')}",
                f"- Δ recycled_alpha: {d.get('recycled_total_alpha_ntd')}",
                f"- n_swaps: {d.get('n_swaps')}",
                "",
            ]
        )
    lines.append(
        "## How to follow (executable baseline from main champion)"
    )
    c = champions.get("by_period_return")
    if c:
        lines.extend(
            [
                "",
                f"1. After close on T, take {branch_label} nets ≥ "
                f"{c.min_net_shares/SHARES_PER_LOT:.0f} 張, Top-{c.top_n}.",
                f"2. Enter T+{c.entry_lag_days + 1} open (L{c.entry_lag_days + 1}).",
                f"3. Hold {c.hold_trading_days} trading days; exit at close.",
                f"4. Use {c.n_slots} slots × 10k; when full, "
                + (
                    "prefer swap (Phase B win)."
                    if phase_b and phase_b.get("swap_beats_skip")
                    else "skip new signals (Phase A / swap not better)."
                ),
            ]
        )
    return "\n".join(lines) + "\n"


def write_artifacts(
    *,
    cells: list[SweepCell],
    champions: dict[str, SweepCell | None],
    phase_b: dict | None,
    window_start: str,
    window_end: str,
    trader_id: str,
    out_dir: Path | None = None,
    stamp: str | None = None,
    strategy_tag: str = STRATEGY_TAG,
    branch_label: str = "元大-松江",
    file_stem: str = "yuanta_songjiang_copytrade_sweep",
    etf_code_proxy: str = ETF_CODE_PROXY,
) -> dict[str, Path]:
    out = out_dir or (REPORTS_RESEARCH / "yuanta-songjiang-copytrade")
    out.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    payload = {
        "strategy_tag": strategy_tag,
        "branch_label": branch_label,
        "etf_code_proxy": etf_code_proxy,
        "trader_id": trader_id,
        "window_start": window_start,
        "window_end": window_end,
        "n_cells": len(cells),
        "champions": {
            k: (cell_to_dict(v) if v is not None else None)
            for k, v in champions.items()
        },
        "phase_b": phase_b,
        "cells": [cell_to_dict(c) for c in cells],
    }
    json_path = out / f"{stamp}_{file_stem}.json"
    md_path = out / f"{stamp}_{file_stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(
        format_report_md(
            window_start=window_start,
            window_end=window_end,
            trader_id=trader_id,
            champions=champions,
            phase_b=phase_b,
            n_cells=len(cells),
            branch_label=branch_label,
        ),
        encoding="utf-8",
    )
    return {"json": json_path, "md": md_path}
