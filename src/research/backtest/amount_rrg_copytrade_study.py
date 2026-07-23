"""金額門檻 Copytrade × RRG／自適應門檻研究。

PIT：訊號日收盤後同時可知分點 tape 與 screen_kind=close 的 RRG；
進場仍為 L1+ 開盤。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from copytrade.branch_signals import BRANCH_BUY_ACTION
from copytrade.dual_branch_signals import (
    DualBranchTapeCache,
    Mode,
    group_dual_branch_signals_by_date,
    iter_dual_branch_buy_signals,
    threshold_label,
)
from copytrade.signals import CopytradeSignal
from report_paths import REPORTS_RESEARCH
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    run_copytrade_backtest,
)
from research.backtest.dual_branch_copytrade_sweep import (
    _lag_label,
    day_results_to_dicts,
    evaluate_slots,
)

ETF_CODE_PROXY = "AMT_RRG"
STRATEGY_TAG = "amount-rrg-copytrade"
FILE_STEM = "amount_rrg_copytrade_sweep"
DEFAULT_WINDOW_START = "2024-07-01"

RrgGate = Literal[
    "none",
    "leading",
    "lead_improve",
    "not_lagging",
    "rs_mom_gt100",
    "rs_ratio_gt100",
    "mono_fresh",
]

RRG_GATES: tuple[RrgGate, ...] = (
    "none",
    "leading",
    "lead_improve",
    "not_lagging",
    "rs_mom_gt100",
    "rs_ratio_gt100",
    "mono_fresh",
)

ThrKind = Literal["fixed", "adapt_p90_60"]


@dataclass(frozen=True)
class AmtRrgCell:
    mode: str
    thr_kind: str
    min_threshold: float  # floor for adapt; fixed thr otherwise
    rrg_gate: str
    top_n: int
    entry_lag_days: int
    hold_trading_days: int
    n_slots: int
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
    n_gate_pass_days: int
    n_gate_drop_legs: int


@dataclass(frozen=True)
class RrgSnap:
    quadrant: str | None
    rs_ratio: float | None
    rs_momentum: float | None
    mono_fresh: int


def load_rrg_close_index(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
) -> dict[tuple[str, str], RrgSnap]:
    """(stock_id, session_date) → RRG close snapshot."""
    rows = conn.execute(
        """
        SELECT stock_id, session_date, quadrant, rs_ratio, rs_momentum, mono_fresh
        FROM rrg_universe_scores
        WHERE screen_kind = 'close'
          AND session_date >= ? AND session_date <= ?
        """,
        (window_start, window_end),
    ).fetchall()
    out: dict[tuple[str, str], RrgSnap] = {}
    for r in rows:
        out[(str(r[0]), str(r[1]))] = RrgSnap(
            quadrant=(str(r[2]) if r[2] else None),
            rs_ratio=(float(r[3]) if r[3] is not None else None),
            rs_momentum=(float(r[4]) if r[4] is not None else None),
            mono_fresh=int(r[5] or 0),
        )
    return out


def _rrg_asof(
    index: dict[tuple[str, str], RrgSnap],
    stock_id: str,
    signal_date: str,
    *,
    dates_desc_cache: dict[str, list[str]] | None = None,
) -> RrgSnap | None:
    """Exact session_date match first; else nearest prior date with any RRG row.

    For speed we only do exact match in the hot path; caller may pre-fill
    forward-fill map. Here: exact only, then one SQL-free fallback via
    scanning known dates for this stock is too slow — use exact + None.
    """
    hit = index.get((stock_id, signal_date))
    if hit is not None:
        return hit
    return None


def build_rrg_ffill_index(
    index: dict[tuple[str, str], RrgSnap],
    calendar: list[str],
) -> dict[tuple[str, str], RrgSnap]:
    """Forward-fill last known RRG per stock across trading calendar (PIT)."""
    by_stock: dict[str, list[tuple[str, RrgSnap]]] = {}
    for (sid, d), snap in index.items():
        by_stock.setdefault(sid, []).append((d, snap))
    for sid in by_stock:
        by_stock[sid].sort(key=lambda x: x[0])

    out: dict[tuple[str, str], RrgSnap] = {}
    for sid, series in by_stock.items():
        j = 0
        last: RrgSnap | None = None
        for d in calendar:
            while j < len(series) and series[j][0] <= d:
                last = series[j][1]
                j += 1
            if last is not None:
                out[(sid, d)] = last
    return out


def gate_passes(snap: RrgSnap | None, gate: RrgGate) -> bool:
    if gate == "none":
        return True
    if snap is None:
        return False
    q = (snap.quadrant or "").lower()
    if gate == "leading":
        return q == "leading"
    if gate == "lead_improve":
        return q in ("leading", "improving")
    if gate == "not_lagging":
        return q in ("leading", "improving", "weakening") and q != "lagging"
    if gate == "rs_mom_gt100":
        return snap.rs_momentum is not None and snap.rs_momentum > 100.0
    if gate == "rs_ratio_gt100":
        return snap.rs_ratio is not None and snap.rs_ratio > 100.0
    if gate == "mono_fresh":
        return int(snap.mono_fresh) == 1
    return False


def apply_rrg_gate(
    grouped: dict[str, list[CopytradeSignal]],
    rrg_ffill: dict[tuple[str, str], RrgSnap],
    gate: RrgGate,
) -> tuple[dict[str, list[CopytradeSignal]], int, int]:
    """Filter legs by RRG gate. Returns (grouped, n_pass_days, n_drop_legs)."""
    if gate == "none":
        return grouped, len(grouped), 0
    out: dict[str, list[CopytradeSignal]] = {}
    drops = 0
    for day, legs in grouped.items():
        kept: list[CopytradeSignal] = []
        for leg in legs:
            snap = rrg_ffill.get((leg.stock_id, day))
            if gate_passes(snap, gate):
                kept.append(leg)
            else:
                drops += 1
        if kept:
            out[day] = kept
    return out, len(out), drops


def xd_top1_amount_series(
    tape: DualBranchTapeCache,
) -> list[tuple[str, float]]:
    """Sorted (date, top1 amount NTD) for 新店."""
    series: list[tuple[str, float]] = []
    for day in sorted(tape.xd):
        best = 0.0
        for sid, net in tape.xd[day].items():
            close = tape.closes.get((sid, day))
            if close is None or close <= 0 or net <= 0:
                continue
            best = max(best, net * close)
        if best > 0:
            series.append((day, best))
    return series


def adaptive_threshold_map(
    top1_series: list[tuple[str, float]],
    *,
    lookback: int = 60,
    pct: float = 0.90,
    floor: float = 50_000_000.0,
) -> dict[str, float]:
    """For each day, thr = max(floor, percentile of prior lookback top1 amounts)."""
    out: dict[str, float] = {}
    amts: list[float] = []
    for i, (day, amt) in enumerate(top1_series):
        hist = amts[-lookback:] if amts else []
        if len(hist) >= max(20, lookback // 3):
            s = sorted(hist)
            idx = min(len(s) - 1, max(0, int(len(s) * pct)))
            thr = max(floor, float(s[idx]))
        else:
            thr = floor
        out[day] = thr
        amts.append(amt)
    return out


def build_signals_fixed(
    conn: sqlite3.Connection,
    *,
    mode: Mode,
    min_threshold: float,
    top_n: int,
    window_start: str,
    window_end: str,
    tape: DualBranchTapeCache,
    min_avg_volume_shares: float | None,
) -> dict[str, list[CopytradeSignal]]:
    sigs = iter_dual_branch_buy_signals(
        conn,
        mode=mode,
        metric="amount",
        min_threshold=min_threshold,
        top_n=top_n,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=min_avg_volume_shares,
        tape=tape,
    )
    return group_dual_branch_signals_by_date(sigs)


def build_signals_adaptive(
    conn: sqlite3.Connection,
    *,
    mode: Mode,
    thr_by_day: dict[str, float],
    top_n: int,
    window_start: str,
    window_end: str,
    tape: DualBranchTapeCache,
    min_avg_volume_shares: float | None,
) -> dict[str, list[CopytradeSignal]]:
    """Per-day threshold: keep stock if score >= that day's adaptive thr, then Top-N."""
    # Build from floor=0 then filter — reuse iter with min 0 is heavy; do manually.
    from copytrade.dual_branch_signals import (
        _is_listed_equity_id,
        avg_volume_shares,
    )
    from stock_db import normalize_stock_name
    from collections import defaultdict

    sj, xd = tape.sj, tape.xd
    closes = tape.closes
    by_day_scores: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    # (sid, score, net_shares)
    all_dates = sorted(set(sj) | set(xd))
    for day in all_dates:
        if day < window_start or day > window_end:
            continue
        thr = thr_by_day.get(day)
        if thr is None:
            continue
        stock_ids = set(sj.get(day, {})) | set(xd.get(day, {}))
        day_rows: list[tuple[str, float, float]] = []
        for sid in stock_ids:
            if not _is_listed_equity_id(sid):
                continue
            sj_net = float((sj.get(day) or {}).get(sid) or 0.0)
            xd_net = float((xd.get(day) or {}).get(sid) or 0.0)
            close = closes.get((sid, day))
            if close is None or close <= 0:
                continue
            sj_sc = sj_net * close if sj_net > 0 else None
            xd_sc = xd_net * close if xd_net > 0 else None
            score = None
            if mode == "xd":
                if xd_sc is not None and xd_sc >= thr:
                    score = xd_sc
            elif mode == "sj":
                if sj_sc is not None and sj_sc >= thr:
                    score = sj_sc
            elif mode == "and":
                if (
                    sj_sc is not None
                    and xd_sc is not None
                    and sj_sc >= thr
                    and xd_sc >= thr
                ):
                    score = sj_sc + xd_sc
            elif mode == "sum":
                parts = [s for s in (sj_sc, xd_sc) if s is not None]
                if parts and sum(parts) >= thr:
                    score = sum(parts)
            elif mode == "or":
                cands = [s for s in (sj_sc, xd_sc) if s is not None and s >= thr]
                if cands:
                    score = max(cands)
            if score is None:
                continue
            day_rows.append((sid, score, max(sj_net, xd_net)))
        day_rows.sort(key=lambda x: (-x[1], x[0]))
        by_day_scores[day] = day_rows

    vol_cache: dict[tuple[str, str], float | None] = {}
    out: dict[str, list[CopytradeSignal]] = {}
    for day, rows in by_day_scores.items():
        kept: list[CopytradeSignal] = []
        for sid, _sc, net in rows:
            if len(kept) >= top_n:
                break
            if min_avg_volume_shares is not None:
                key = (sid, day)
                if key not in vol_cache:
                    vol_cache[key] = avg_volume_shares(conn, sid, day)
                avg_vol = vol_cache[key]
                if avg_vol is None or avg_vol < min_avg_volume_shares:
                    continue
            kept.append(
                CopytradeSignal(
                    signal_date=day,
                    stock_id=sid,
                    stock_name=normalize_stock_name(sid),
                    action=BRANCH_BUY_ACTION,
                    share_delta=net,
                    weight_delta=None,
                    weight_pct_curr=None,
                )
            )
        if kept:
            out[day] = kept
    return out


def cell_to_dict(c: AmtRrgCell) -> dict:
    d = asdict(c)
    d["entry_row"] = _lag_label(c.entry_lag_days)
    d["strategy_id"] = f"{_lag_label(c.entry_lag_days)}H{c.hold_trading_days}"
    d["metric"] = "amount"
    d["threshold_label"] = (
        f"adaptP90≥{c.min_threshold/1e6:.0f}M"
        if c.thr_kind.startswith("adapt")
        else threshold_label("amount", c.min_threshold)
    )
    d["min_net_amount_ntd"] = c.min_threshold
    d["min_net_lots"] = None
    return d


def run_amount_rrg_sweep(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str | None = None,
    modes: tuple[Mode, ...] = ("xd", "and", "sum"),
    fixed_amounts: tuple[float, ...] = (
        50_000_000.0,
        80_000_000.0,
        100_000_000.0,
        150_000_000.0,
    ),
    rrg_gates: tuple[RrgGate, ...] = RRG_GATES,
    include_adaptive: bool = True,
    adapt_floor: float = 50_000_000.0,
    top_ns: tuple[int, ...] = (1,),
    entry_lags: tuple[int, ...] = (0, 1),
    holds: tuple[int, ...] = (5, 9, 12, 18),
    slot_counts: tuple[int, ...] = (1, 9),
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
    progress_every: int = 40,
) -> list[AmtRrgCell]:
    w_end = window_end or date.today().isoformat()
    print("loading tape + RRG …", flush=True)
    tape = DualBranchTapeCache.load(
        conn, window_start=window_start, window_end=w_end, need_closes=True
    )
    raw_rrg = load_rrg_close_index(
        conn, window_start=window_start, window_end=w_end
    )
    calendar = sorted(set(tape.sj) | set(tape.xd))
    rrg_ffill = build_rrg_ffill_index(raw_rrg, calendar)
    print(
        f"  tape days={len(calendar)} closes={len(tape.closes)} "
        f"rrg_raw={len(raw_rrg)} rrg_ffill={len(rrg_ffill)}",
        flush=True,
    )

    top1 = xd_top1_amount_series(tape)
    thr_adapt = adaptive_threshold_map(top1, floor=adapt_floor)

    jobs: list[tuple[str, str, float, Mode, int]] = []
    # thr_kind, label_thr, thr_value_or_floor, mode, top_n
    for mode in modes:
        for thr in fixed_amounts:
            for top_n in top_ns:
                jobs.append(("fixed", f"{thr/1e6:.0f}M", thr, mode, top_n))
        if include_adaptive:
            for top_n in top_ns:
                jobs.append(
                    ("adapt_p90_60", f"adapt≥{adapt_floor/1e6:.0f}M", adapt_floor, mode, top_n)
                )

    cells: list[AmtRrgCell] = []
    n_done = 0
    signal_cache: dict[tuple[str, str, float, int], dict[str, list[CopytradeSignal]]] = {}

    for thr_kind, thr_lab, thr_val, mode, top_n in jobs:
        cache_key = (thr_kind, mode, thr_val, top_n)
        if cache_key not in signal_cache:
            if thr_kind == "fixed":
                signal_cache[cache_key] = build_signals_fixed(
                    conn,
                    mode=mode,
                    min_threshold=thr_val,
                    top_n=top_n,
                    window_start=window_start,
                    window_end=w_end,
                    tape=tape,
                    min_avg_volume_shares=min_avg_volume_shares,
                )
            else:
                signal_cache[cache_key] = build_signals_adaptive(
                    conn,
                    mode=mode,
                    thr_by_day=thr_adapt,
                    top_n=top_n,
                    window_start=window_start,
                    window_end=w_end,
                    tape=tape,
                    min_avg_volume_shares=min_avg_volume_shares,
                )
        base_grouped = signal_cache[cache_key]
        if not base_grouped:
            continue

        for gate in rrg_gates:
            grouped, n_pass_days, n_drops = apply_rrg_gate(
                base_grouped, rrg_ffill, gate
            )
            if not grouped:
                continue
            for lag in entry_lags:
                for hold in holds:
                    sid = f"{_lag_label(lag)}H{hold}"
                    result = run_copytrade_backtest(
                        conn,
                        ETF_CODE_PROXY,
                        strategy_id=sid,
                        strategy_label=(
                            f"{mode}/amt≥{thr_lab}/{gate} top{top_n} {sid}"
                        ),
                        entry_lag_days=int(lag),
                        hold_trading_days=int(hold),
                        entry_price_mode="open",
                        capital_ntd=capital_ntd,
                        cost_bps=0.0,
                        window_start=window_start,
                        window_end=w_end,
                        grouped=grouped,
                    )
                    day_dicts = day_results_to_dicts(result)
                    for n_slots in slot_counts:
                        sim = evaluate_slots(
                            conn,
                            day_dicts,
                            n_slots=int(n_slots),
                            capital_ntd=capital_ntd,
                            slots_mode="fixed",
                        )
                        cells.append(
                            AmtRrgCell(
                                mode=str(mode),
                                thr_kind=thr_kind,
                                min_threshold=float(thr_val),
                                rrg_gate=str(gate),
                                top_n=int(top_n),
                                entry_lag_days=int(lag),
                                hold_trading_days=int(hold),
                                n_slots=int(sim["n_slots"]),
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
                                n_gate_pass_days=int(n_pass_days),
                                n_gate_drop_legs=int(n_drops),
                            )
                        )
                        n_done += 1
                        if progress_every and n_done % progress_every == 0:
                            print(
                                f"  amt-rrg cells={n_done} "
                                f"last={mode}≥{thr_lab}/{gate} {sid} "
                                f"slots={n_slots}",
                                flush=True,
                            )
    return cells


def select_amt_rrg_champions(
    cells: list[AmtRrgCell],
) -> dict[str, AmtRrgCell | None]:
    if not cells:
        return {
            "by_period_return": None,
            "by_alpha": None,
            "by_alpha_per_locked_day": None,
        }
    by_ret = max(
        cells,
        key=lambda c: (
            c.period_return_pct if c.period_return_pct is not None else -1e18,
            c.recycled_total_alpha_ntd,
            c.alpha_per_locked_day or -1e18,
            -c.recycled_n_cycles,
        ),
    )
    by_alpha = max(
        cells,
        key=lambda c: (
            c.recycled_total_alpha_ntd,
            c.period_return_pct or -1e18,
            c.alpha_per_locked_day or -1e18,
            -c.recycled_n_cycles,
        ),
    )
    by_apld = max(
        cells,
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


def evaluate_amount_rrg_hypotheses(cells: list[AmtRrgCell]) -> dict:
    """H-RRG-* and adaptive vs fixed."""

    def best(
        pool: list[AmtRrgCell], *, key: str = "period_return_pct"
    ) -> AmtRrgCell | None:
        if not pool:
            return None
        if key == "alpha_per_locked_day":
            return max(
                pool,
                key=lambda c: (
                    c.alpha_per_locked_day
                    if c.alpha_per_locked_day is not None
                    else -1e18,
                    c.period_return_pct or -1e18,
                ),
            )
        return max(
            pool,
            key=lambda c: (
                c.period_return_pct if c.period_return_pct is not None else -1e18,
                c.alpha_per_locked_day or -1e18,
            ),
        )

    base = [
        c
        for c in cells
        if c.rrg_gate == "none" and c.thr_kind == "fixed" and c.mode == "xd"
    ]
    base_champ = best(base)
    base_apld = best(base, key="alpha_per_locked_day")

    def gate_vs_none(gate: RrgGate) -> dict:
        g_pool = [
            c
            for c in cells
            if c.rrg_gate == gate and c.thr_kind == "fixed" and c.mode == "xd"
        ]
        g_ret = best(g_pool)
        g_apld = best(g_pool, key="alpha_per_locked_day")
        ret_beat = bool(
            g_ret
            and base_champ
            and (g_ret.period_return_pct or -1e18)
            > (base_champ.period_return_pct or -1e18)
        )
        apld_beat = bool(
            g_apld
            and base_apld
            and (g_apld.alpha_per_locked_day or -1e18)
            > (base_apld.alpha_per_locked_day or -1e18)
        )
        return {
            "gate": gate,
            "ret_beats_none": ret_beat,
            "apld_beats_none": apld_beat,
            "best_ret": cell_to_dict(g_ret) if g_ret else None,
            "best_apld": cell_to_dict(g_apld) if g_apld else None,
            "base_ret": cell_to_dict(base_champ) if base_champ else None,
            "base_apld": cell_to_dict(base_apld) if base_apld else None,
        }

    gate_tests = {
        g: gate_vs_none(g) for g in RRG_GATES if g != "none"
    }
    any_rrg_apld = any(v["apld_beats_none"] for v in gate_tests.values())
    any_rrg_ret = any(v["ret_beats_none"] for v in gate_tests.values())

    adapt = [c for c in cells if c.thr_kind.startswith("adapt") and c.mode == "xd"]
    adapt_none = [c for c in adapt if c.rrg_gate == "none"]
    adapt_champ = best(adapt_none)
    adapt_beats = bool(
        adapt_champ
        and base_champ
        and (
            (adapt_champ.period_return_pct or -1e18)
            > (base_champ.period_return_pct or -1e18)
            or (adapt_champ.alpha_per_locked_day or -1e18)
            > (base_apld.alpha_per_locked_day or -1e18 if base_apld else -1e18)
        )
    )

    # Best overall
    overall_ret = best(cells)
    overall_apld = best(cells, key="alpha_per_locked_day")
    # Prefer 1-slot recommendations
    sleeve_pool = [c for c in cells if c.n_slots == 1 and c.mode in ("xd", "and")]
    recommend = best(sleeve_pool)

    return {
        "H_RRG_IMPROVES_APLD": {
            "status": "passed" if any_rrg_apld else "rejected",
            "note": "Some RRG gate beats amount-only (xd fixed) on α/日",
            "by_gate": gate_tests,
        },
        "H_RRG_IMPROVES_RET": {
            "status": "passed" if any_rrg_ret else "rejected",
            "note": "Some RRG gate beats amount-only on period return",
            "by_gate": gate_tests,
        },
        "H_ADAPT_P90": {
            "status": "passed" if adapt_beats else "rejected",
            "note": "Adaptive P90-60 floor beats best fixed xd amount-only",
            "adapt_best": cell_to_dict(adapt_champ) if adapt_champ else None,
            "fixed_best": cell_to_dict(base_champ) if base_champ else None,
        },
        "overall_by_return": cell_to_dict(overall_ret) if overall_ret else None,
        "overall_by_apld": cell_to_dict(overall_apld) if overall_apld else None,
        "recommended_sleeve": cell_to_dict(recommend) if recommend else None,
        "baseline_xd_amount_none": cell_to_dict(base_champ) if base_champ else None,
    }


def format_report_md(
    *,
    window_start: str,
    window_end: str,
    champions: dict[str, AmtRrgCell | None],
    hypotheses: dict,
    n_cells: int,
) -> str:
    lines = [
        "# 金額門檻 × RRG Copytrade Sweep",
        "",
        f"- window: `{window_start}` → `{window_end}`",
        f"- cells: {n_cells}",
        f"- amount proxy: net×close PIT · RRG: screen_kind=close ffill",
        "",
        "## Hypotheses",
        "",
    ]
    for hid in ("H_RRG_IMPROVES_APLD", "H_RRG_IMPROVES_RET", "H_ADAPT_P90"):
        h = hypotheses.get(hid) or {}
        lines.append(f"- **{hid}**: `{h.get('status')}` — {h.get('note')}")
    lines.extend(["", "## Champions", ""])
    for key, label in (
        ("by_period_return", "主冠軍 · period return"),
        ("by_alpha", "次優 · α"),
        ("by_alpha_per_locked_day", "次優 · α/日"),
    ):
        c = champions.get(key)
        lines.append(f"### {label}")
        if not c:
            lines.append("- (none)\n")
            continue
        d = cell_to_dict(c)
        lines.extend(
            [
                f"- `{d['mode']}` ≥{d['threshold_label']} · gate=`{c.rrg_gate}` · "
                f"{d['strategy_id']} top{c.top_n} slots={c.n_slots}",
                f"- ret%={c.period_return_pct} α={c.recycled_total_alpha_ntd} "
                f"α/日={c.alpha_per_locked_day} capture%={c.signal_capture_pct}",
                f"- gate_pass_days={c.n_gate_pass_days} drops={c.n_gate_drop_legs}",
                "",
            ]
        )
    rec = hypotheses.get("recommended_sleeve") or {}
    lines.extend(
        [
            "## Recommended",
            "",
            f"- `{rec.get('mode')}` ≥{rec.get('threshold_label')} · "
            f"gate=`{rec.get('rrg_gate')}` · {rec.get('strategy_id')} · "
            f"slots={rec.get('n_slots')} ret%={rec.get('period_return_pct')}",
            "",
        ]
    )
    # Per-gate summary
    lines.append("## RRG gate vs amount-only (xd fixed)")
    lines.append("")
    by_gate = (hypotheses.get("H_RRG_IMPROVES_APLD") or {}).get("by_gate") or {}
    for g, row in by_gate.items():
        br = row.get("best_ret") or {}
        lines.append(
            f"- `{g}`: ret_beat={row.get('ret_beats_none')} "
            f"apld_beat={row.get('apld_beats_none')} "
            f"best={br.get('threshold_label')} {br.get('strategy_id')} "
            f"ret%={br.get('period_return_pct')} α/日={br.get('alpha_per_locked_day')}"
        )
    return "\n".join(lines) + "\n"


def write_artifacts(
    *,
    cells: list[AmtRrgCell],
    champions: dict[str, AmtRrgCell | None],
    hypotheses: dict,
    window_start: str,
    window_end: str,
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    out = out_dir or (REPORTS_RESEARCH / "dual-branch-copytrade")
    out.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    payload = {
        "strategy_tag": STRATEGY_TAG,
        "branch_label": "金額門檻 × RRG（富邦新店主軸）",
        "etf_code_proxy": ETF_CODE_PROXY,
        "window_start": window_start,
        "window_end": window_end,
        "n_cells": len(cells),
        "champions": {
            k: (cell_to_dict(v) if v else None) for k, v in champions.items()
        },
        "hypotheses": hypotheses,
        "cells": [cell_to_dict(c) for c in cells],
    }
    json_path = out / f"{stamp}_{FILE_STEM}.json"
    md_path = out / f"{stamp}_{FILE_STEM}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(
        format_report_md(
            window_start=window_start,
            window_end=window_end,
            champions=champions,
            hypotheses=hypotheses,
            n_cells=len(cells),
        ),
        encoding="utf-8",
    )
    return {"json": json_path, "md": md_path}
