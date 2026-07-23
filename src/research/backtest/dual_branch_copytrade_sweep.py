"""雙分點 Copytrade · metric×mode×門檻×Top-N × L×H×槽位 sweep。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from copytrade.dual_branch_signals import (
    AMOUNT_FOCUS_MIN_NET_AMOUNTS,
    AMOUNT_FOCUS_MODES,
    DEFAULT_MIN_NET_AMOUNTS,
    DEFAULT_MIN_NET_SHARES,
    METRICS,
    MODES,
    DualBranchTapeCache,
    Metric,
    Mode,
    group_dual_branch_signals_by_date,
    iter_dual_branch_buy_signals,
    threshold_label,
)
from copytrade.branch_signals import SHARES_PER_LOT
from report_paths import REPORTS_RESEARCH
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    run_copytrade_backtest,
)
from research.backtest.yuanta_songjiang_copytrade_sweep import (
    day_results_to_dicts,
    evaluate_slots,
)

STRATEGY_TAG = "dual-branch"
ETF_CODE_PROXY = "DUAL_BR"
FILE_STEM = "dual_branch_copytrade_sweep"
DEFAULT_WINDOW_START = "2024-07-01"
DEFAULT_TOP_NS = (1, 3)
DEFAULT_ENTRY_LAGS = (0, 1)  # L1, L2
DEFAULT_HOLDS = (5, 9, 12, 18)
DEFAULT_SLOTS = (1, 9)
# Amount-focus: denser thr + L3; drop OR (already diluted).
AMOUNT_FOCUS_ENTRY_LAGS = (0, 1, 2)  # L1–L3
AMOUNT_FOCUS_FILE_STEM = "dual_branch_amount_focus_sweep"


@dataclass(frozen=True)
class DualSweepCell:
    mode: str
    metric: str
    min_threshold: float
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


def _lag_label(entry_lag_days: int) -> str:
    return f"L{entry_lag_days + 1}"


def build_dual_signals(
    conn: sqlite3.Connection,
    *,
    mode: Mode,
    metric: Metric,
    min_threshold: float,
    top_n: int,
    window_start: str,
    window_end: str,
    min_avg_volume_shares: float | None = 200_000.0,
    tape: DualBranchTapeCache | None = None,
) -> dict:
    signals = iter_dual_branch_buy_signals(
        conn,
        mode=mode,
        metric=metric,
        min_threshold=min_threshold,
        top_n=top_n,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=min_avg_volume_shares,
        tape=tape,
    )
    return group_dual_branch_signals_by_date(signals)


def run_dual_phase_a_sweep(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str | None = None,
    modes: tuple[Mode, ...] = MODES,
    metrics: tuple[Metric, ...] = METRICS,
    min_net_shares: tuple[float, ...] = DEFAULT_MIN_NET_SHARES,
    min_net_amounts: tuple[float, ...] = DEFAULT_MIN_NET_AMOUNTS,
    top_ns: tuple[int, ...] = DEFAULT_TOP_NS,
    entry_lags: tuple[int, ...] = DEFAULT_ENTRY_LAGS,
    holds: tuple[int, ...] = DEFAULT_HOLDS,
    slot_counts: tuple[int, ...] = DEFAULT_SLOTS,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
    progress_every: int = 40,
) -> list[DualSweepCell]:
    w_end = window_end or date.today().isoformat()
    cells: list[DualSweepCell] = []
    n_done = 0
    print("loading dual-branch tape cache …")
    tape = DualBranchTapeCache.load(
        conn,
        window_start=window_start,
        window_end=w_end,
        need_closes=("amount" in metrics),
    )
    print(
        f"  tape days sj={len(tape.sj)} xd={len(tape.xd)} "
        f"closes={len(tape.closes)}"
    )

    for metric in metrics:
        thresholds = min_net_shares if metric == "lots" else min_net_amounts
        for mode in modes:
            for thr in thresholds:
                for top_n in top_ns:
                    grouped = build_dual_signals(
                        conn,
                        mode=mode,
                        metric=metric,
                        min_threshold=float(thr),
                        top_n=int(top_n),
                        window_start=window_start,
                        window_end=w_end,
                        min_avg_volume_shares=min_avg_volume_shares,
                        tape=tape,
                    )
                    if not grouped:
                        continue
                    thr_lab = threshold_label(metric, float(thr))
                    for lag in entry_lags:
                        for hold in holds:
                            sid = f"{_lag_label(lag)}H{hold}"
                            result = run_copytrade_backtest(
                                conn,
                                ETF_CODE_PROXY,
                                strategy_id=sid,
                                strategy_label=(
                                    f"dual {mode}/{metric} ≥{thr_lab} "
                                    f"top{top_n} {sid}"
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
                                    DualSweepCell(
                                        mode=str(mode),
                                        metric=str(metric),
                                        min_threshold=float(thr),
                                        top_n=int(top_n),
                                        entry_lag_days=int(lag),
                                        hold_trading_days=int(hold),
                                        n_slots=int(sim["n_slots"]),
                                        slots_mode="fixed",
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
                                            if sim.get("signal_capture_pct")
                                            is not None
                                            else None
                                        ),
                                        alpha_per_locked_day=(
                                            float(sim["alpha_per_locked_day"])
                                            if sim.get("alpha_per_locked_day")
                                            is not None
                                            else None
                                        ),
                                        total_capital_ntd=float(
                                            sim["total_capital_ntd"]
                                        ),
                                        period_return_pct=(
                                            float(sim["period_return_pct"])
                                            if sim.get("period_return_pct")
                                            is not None
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
                                        f"  dual cells={n_done} "
                                        f"last={mode}/{metric}≥{thr_lab} "
                                        f"top{top_n} {sid} slots={n_slots}"
                                    )
    return cells


def _sort_key_return(c: DualSweepCell) -> tuple:
    ret = c.period_return_pct if c.period_return_pct is not None else -1e18
    alpha = c.recycled_total_alpha_ntd
    apld = c.alpha_per_locked_day if c.alpha_per_locked_day is not None else -1e18
    return (ret, alpha, apld, -c.recycled_n_cycles)


def select_champions(
    cells: list[DualSweepCell],
) -> dict[str, DualSweepCell | None]:
    pool = [c for c in cells if c.slots_mode == "fixed"]
    empty = {
        "by_period_return": None,
        "by_alpha": None,
        "by_alpha_per_locked_day": None,
    }
    if not pool:
        return empty
    return {
        "by_period_return": max(pool, key=_sort_key_return),
        "by_alpha": max(
            pool,
            key=lambda c: (
                c.recycled_total_alpha_ntd,
                c.period_return_pct or -1e18,
                c.alpha_per_locked_day or -1e18,
                -c.recycled_n_cycles,
            ),
        ),
        "by_alpha_per_locked_day": max(
            pool,
            key=lambda c: (
                c.alpha_per_locked_day
                if c.alpha_per_locked_day is not None
                else -1e18,
                c.recycled_total_alpha_ntd,
                c.period_return_pct or -1e18,
                -c.recycled_n_cycles,
            ),
        ),
    }


def best_by_mode_metric(
    cells: list[DualSweepCell],
    *,
    key: str = "period_return_pct",
) -> list[dict]:
    """Best fixed-slot cell per (mode, metric) for hypothesis tables."""
    best: dict[tuple[str, str], DualSweepCell] = {}
    for c in cells:
        if c.slots_mode != "fixed":
            continue
        k = (c.mode, c.metric)
        cur = best.get(k)
        if cur is None:
            best[k] = c
            continue
        if key == "alpha_per_locked_day":
            a = c.alpha_per_locked_day if c.alpha_per_locked_day is not None else -1e18
            b = (
                cur.alpha_per_locked_day
                if cur.alpha_per_locked_day is not None
                else -1e18
            )
            if a > b:
                best[k] = c
        elif key == "recycled_total_alpha_ntd":
            if c.recycled_total_alpha_ntd > cur.recycled_total_alpha_ntd:
                best[k] = c
        else:
            a = c.period_return_pct if c.period_return_pct is not None else -1e18
            b = cur.period_return_pct if cur.period_return_pct is not None else -1e18
            if a > b:
                best[k] = c
    return [cell_to_dict(best[k]) for k in sorted(best)]


def cell_to_dict(c: DualSweepCell) -> dict:
    d = asdict(c)
    d["entry_row"] = _lag_label(c.entry_lag_days)
    d["strategy_id"] = f"{_lag_label(c.entry_lag_days)}H{c.hold_trading_days}"
    d["threshold_label"] = threshold_label(
        c.metric,  # type: ignore[arg-type]
        c.min_threshold,
    )
    if c.metric == "lots":
        d["min_net_lots"] = c.min_threshold / SHARES_PER_LOT
        d["min_net_amount_ntd"] = None
    else:
        d["min_net_lots"] = None
        d["min_net_amount_ntd"] = c.min_threshold
    return d


def evaluate_hypotheses(cells: list[DualSweepCell]) -> dict:
    """H1–H3 from plan, computed on fixed-slot cells."""
    by_ret = best_by_mode_metric(cells, key="period_return_pct")
    by_apld = best_by_mode_metric(cells, key="alpha_per_locked_day")

    def _pick(rows: list[dict], mode: str, metric: str) -> dict | None:
        for r in rows:
            if r["mode"] == mode and r["metric"] == metric:
                return r
        return None

    # H1: amount vs lots within same mode (period_return or apld)
    h1_modes = []
    for mode in MODES:
        lots = _pick(by_ret, mode, "lots")
        amt = _pick(by_ret, mode, "amount")
        lots_a = _pick(by_apld, mode, "lots")
        amt_a = _pick(by_apld, mode, "amount")
        ret_win = None
        apld_win = None
        if lots and amt:
            lr = lots.get("period_return_pct") or -1e18
            ar = amt.get("period_return_pct") or -1e18
            ret_win = "amount" if ar > lr else "lots"
        if lots_a and amt_a:
            la = lots_a.get("alpha_per_locked_day") or -1e18
            aa = amt_a.get("alpha_per_locked_day") or -1e18
            apld_win = "amount" if aa > la else "lots"
        h1_modes.append(
            {
                "mode": mode,
                "ret_winner": ret_win,
                "apld_winner": apld_win,
                "lots_ret": lots.get("period_return_pct") if lots else None,
                "amount_ret": amt.get("period_return_pct") if amt else None,
                "lots_apld": lots_a.get("alpha_per_locked_day") if lots_a else None,
                "amount_apld": amt_a.get("alpha_per_locked_day") if amt_a else None,
            }
        )
    amount_ret_wins = sum(1 for x in h1_modes if x["ret_winner"] == "amount")
    amount_apld_wins = sum(1 for x in h1_modes if x["apld_winner"] == "amount")
    h1_pass = amount_ret_wins >= 3 or amount_apld_wins >= 3

    # H2: and/sum apld vs best single-branch sleeve
    singles = [
        r
        for r in by_apld
        if r["mode"] in ("sj", "xd") and r.get("alpha_per_locked_day") is not None
    ]
    best_single = max(
        singles,
        key=lambda r: float(r["alpha_per_locked_day"]),
        default=None,
    )
    combo_apld = [
        r
        for r in by_apld
        if r["mode"] in ("and", "sum") and r.get("alpha_per_locked_day") is not None
    ]
    best_combo = max(
        combo_apld,
        key=lambda r: float(r["alpha_per_locked_day"]),
        default=None,
    )
    h2_pass = bool(
        best_combo
        and best_single
        and float(best_combo["alpha_per_locked_day"])
        > float(best_single["alpha_per_locked_day"])
    )

    # H3: or dilution — higher capture / pnl but lower apld vs best single
    or_rows = [r for r in by_apld if r["mode"] == "or"]
    or_best = max(
        or_rows,
        key=lambda r: float(r.get("alpha_per_locked_day") or -1e18),
        default=None,
    )
    or_ret = _pick(by_ret, "or", "lots") or _pick(by_ret, "or", "amount")
    diluted = False
    if or_best and best_single:
        diluted = float(or_best.get("alpha_per_locked_day") or -1e18) < float(
            best_single["alpha_per_locked_day"]
        )
    # H3 passes when dilution is confirmed → do not adopt OR as primary sleeve.
    h3_dilution_confirmed = diluted

    return {
        "H1_amount_beats_lots": {
            "status": "passed" if h1_pass else "rejected",
            "amount_ret_mode_wins": amount_ret_wins,
            "amount_apld_mode_wins": amount_apld_wins,
            "by_mode": h1_modes,
        },
        "H2_and_or_sum_beats_single_apld": {
            "status": "passed" if h2_pass else "rejected",
            "best_single": best_single,
            "best_combo": best_combo,
        },
        "H3_or_dilution": {
            "status": "passed" if h3_dilution_confirmed else "rejected",
            "note": (
                "OR α/日劣於最佳單一分點 → 視為稀釋、不採納為主袖"
                if h3_dilution_confirmed
                else "OR α/日未劣於最佳單一分點（本次未確認稀釋）"
            ),
            "adopt_or_as_primary": not h3_dilution_confirmed,
            "or_best_apld": or_best,
            "or_best_ret_cell": or_ret,
            "best_single": best_single,
        },
        "best_per_mode_metric_by_return": by_ret,
        "best_per_mode_metric_by_apld": by_apld,
    }


def evaluate_amount_focus(cells: list[DualSweepCell]) -> dict:
    """Amount-primary follow-up: density-matched lots vs amount + amount champions."""
    fixed = [c for c in cells if c.slots_mode == "fixed"]
    amount_cells = [c for c in fixed if c.metric == "amount"]
    lots_cells = [c for c in fixed if c.metric == "lots"]

    def _best(pool: list[DualSweepCell], key: str) -> DualSweepCell | None:
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

    amt_ret = _best(amount_cells, "period_return_pct")
    amt_apld = _best(amount_cells, "alpha_per_locked_day")
    lots_ret = _best(lots_cells, "period_return_pct")
    lots_apld = _best(lots_cells, "alpha_per_locked_day")

    # Density-matched: for each mode, for each amount cell, find lots cell with
    # closest n_signals (same mode, top_n, L, H, slots).
    fair_rows: list[dict] = []
    for mode in AMOUNT_FOCUS_MODES:
        amt_mode = [c for c in amount_cells if c.mode == mode]
        lots_mode = [c for c in lots_cells if c.mode == mode]
        if not amt_mode or not lots_mode:
            continue
        # Best amount by return in mode, then match density
        best_amt = _best(amt_mode, "period_return_pct")
        assert best_amt is not None
        twin = min(
            lots_mode,
            key=lambda c: (
                abs(c.n_signals - best_amt.n_signals),
                -(c.period_return_pct or -1e18),
            ),
        )
        fair_rows.append(
            {
                "mode": mode,
                "amount": cell_to_dict(best_amt),
                "lots_density_match": cell_to_dict(twin),
                "n_signals_delta": twin.n_signals - best_amt.n_signals,
                "ret_winner": (
                    "amount"
                    if (best_amt.period_return_pct or -1e18)
                    > (twin.period_return_pct or -1e18)
                    else "lots"
                ),
                "apld_winner": (
                    "amount"
                    if (best_amt.alpha_per_locked_day or -1e18)
                    > (twin.alpha_per_locked_day or -1e18)
                    else "lots"
                ),
            }
        )

    amt_ret_fair_wins = sum(1 for r in fair_rows if r["ret_winner"] == "amount")
    amt_apld_fair_wins = sum(1 for r in fair_rows if r["apld_winner"] == "amount")
    # Also: unrestricted amount champion vs lots champion
    beat_lots_ret = bool(
        amt_ret
        and lots_ret
        and (amt_ret.period_return_pct or -1e18)
        > (lots_ret.period_return_pct or -1e18)
    )
    beat_lots_apld = bool(
        amt_apld
        and lots_apld
        and (amt_apld.alpha_per_locked_day or -1e18)
        > (lots_apld.alpha_per_locked_day or -1e18)
    )

    # Recommend executable amount sleeve: prefer xd or and, 1-slot, top1/3
    recommend_pool = [
        c
        for c in amount_cells
        if c.mode in ("xd", "and", "sj", "sum") and c.n_slots == 1
    ]
    recommend = _best(recommend_pool, "period_return_pct")
    recommend_apld = _best(recommend_pool, "alpha_per_locked_day")

    return {
        "H_AMOUNT_FOCUS": {
            "status": (
                "passed"
                if (amt_ret_fair_wins >= 2 or beat_lots_ret or beat_lots_apld)
                else "rejected"
            ),
            "note": (
                "Amount beats density-matched lots on ≥2 modes, "
                "or beats global lots champion on ret/apld"
            ),
            "fair_ret_wins": amt_ret_fair_wins,
            "fair_apld_wins": amt_apld_fair_wins,
            "beat_lots_global_ret": beat_lots_ret,
            "beat_lots_global_apld": beat_lots_apld,
            "fair_by_mode": fair_rows,
        },
        "amount_champion_by_return": (
            cell_to_dict(amt_ret) if amt_ret else None
        ),
        "amount_champion_by_apld": (
            cell_to_dict(amt_apld) if amt_apld else None
        ),
        "lots_champion_by_return": (
            cell_to_dict(lots_ret) if lots_ret else None
        ),
        "lots_champion_by_apld": (
            cell_to_dict(lots_apld) if lots_apld else None
        ),
        "recommended_amount_sleeve": (
            cell_to_dict(recommend) if recommend else None
        ),
        "recommended_amount_apld_sleeve": (
            cell_to_dict(recommend_apld) if recommend_apld else None
        ),
        "best_per_mode_metric_by_return": best_by_mode_metric(
            cells, key="period_return_pct"
        ),
        "best_per_mode_metric_by_apld": best_by_mode_metric(
            cells, key="alpha_per_locked_day"
        ),
    }


def format_report_md(
    *,
    window_start: str,
    window_end: str,
    champions: dict[str, DualSweepCell | None],
    hypotheses: dict,
    n_cells: int,
) -> str:
    lines = [
        "# 雙分點 Copytrade Sweep（松江 × 新店）",
        "",
        f"- window: `{window_start}` → `{window_end}`",
        f"- traders: `9801` 元大-松江 · `9661` 富邦-新店",
        f"- phase_a_cells: {n_cells}",
        f"- cost_bps: 0（未扣稅費／滑價）",
        f"- amount proxy: net_shares × signal-day close（PIT）",
        "",
        "## Hypotheses",
        "",
    ]
    haf = hypotheses.get("H_AMOUNT_FOCUS")
    if haf:
        rec = hypotheses.get("recommended_amount_sleeve") or {}
        lines.extend(
            [
                f"- **H-AMOUNT-FOCUS** density-matched: `{haf.get('status')}` "
                f"(fair ret wins {haf.get('fair_ret_wins')} · "
                f"fair apld wins {haf.get('fair_apld_wins')} · "
                f"beat lots global ret={haf.get('beat_lots_global_ret')} "
                f"apld={haf.get('beat_lots_global_apld')})",
                f"- recommended amount sleeve: "
                f"`{rec.get('mode')}/{rec.get('metric')}` ≥{rec.get('threshold_label')} "
                f"{rec.get('strategy_id')} top{rec.get('top_n')} "
                f"slots={rec.get('n_slots')} ret%={rec.get('period_return_pct')}",
                "",
                "## Global champions（fixed slots）",
                "",
            ]
        )
    else:
        h1 = hypotheses.get("H1_amount_beats_lots") or {}
        h2 = hypotheses.get("H2_and_or_sum_beats_single_apld") or {}
        h3 = hypotheses.get("H3_or_dilution") or {}
        lines.extend(
            [
                f"- **H1** amount vs lots: `{h1.get('status')}` "
                f"(ret wins {h1.get('amount_ret_mode_wins')}/5 · "
                f"apld wins {h1.get('amount_apld_mode_wins')}/5)",
                f"- **H2** and/sum α/日 vs best single: `{h2.get('status')}`",
                f"- **H3** OR dilution check: `{h3.get('status')}` — {h3.get('note')}",
                "",
                "## Global champions（fixed slots）",
                "",
            ]
        )
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
                f"- mode/metric: `{c.mode}` / `{c.metric}`",
                f"- threshold: {threshold_label(c.metric, c.min_threshold)}",  # type: ignore[arg-type]
                f"- strategy: `{_lag_label(c.entry_lag_days)}H{c.hold_trading_days}`",
                f"- top_n: {c.top_n}",
                f"- slots: {c.n_slots}",
                f"- period_return%: {c.period_return_pct}",
                f"- recycled_alpha: {c.recycled_total_alpha_ntd}",
                f"- capture%: {c.signal_capture_pct}",
                f"- alpha_per_locked_day: {c.alpha_per_locked_day}",
                "",
            ]
        )
    lines.append("## Best per mode×metric（by period return）")
    lines.append("")
    for r in hypotheses.get("best_per_mode_metric_by_return") or []:
        lines.append(
            f"- `{r['mode']}/{r['metric']}` ≥{r['threshold_label']} "
            f"top{r['top_n']} {r['strategy_id']} slots={r['n_slots']} "
            f"ret%={r['period_return_pct']} α/日={r['alpha_per_locked_day']}"
        )
    lines.append("")
    lines.append("## How to follow")
    c = champions.get("by_period_return")
    if c:
        lines.extend(
            [
                "",
                f"1. Mode `{c.mode}` · metric `{c.metric}` · "
                f"threshold ≥ {threshold_label(c.metric, c.min_threshold)} · "  # type: ignore[arg-type]
                f"Top-{c.top_n}.",
                f"2. Enter T+{c.entry_lag_days + 1} open.",
                f"3. Hold {c.hold_trading_days} trading days; exit at close.",
                f"4. {c.n_slots} slots × 10k; skip when full.",
            ]
        )
    return "\n".join(lines) + "\n"


def write_artifacts(
    *,
    cells: list[DualSweepCell],
    champions: dict[str, DualSweepCell | None],
    hypotheses: dict,
    window_start: str,
    window_end: str,
    out_dir: Path | None = None,
    stamp: str | None = None,
    file_stem: str = FILE_STEM,
    strategy_tag: str = STRATEGY_TAG,
    branch_label: str = "雙分點（元大-松江 × 富邦-新店）",
    grid_meta: dict | None = None,
) -> dict[str, Path]:
    out = out_dir or (REPORTS_RESEARCH / "dual-branch-copytrade")
    out.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    payload = {
        "strategy_tag": strategy_tag,
        "branch_label": branch_label,
        "etf_code_proxy": ETF_CODE_PROXY,
        "trader_ids": {"sj": "9801", "xd": "9661"},
        "window_start": window_start,
        "window_end": window_end,
        "n_cells": len(cells),
        "grid": grid_meta
        or {
            "modes": list(MODES),
            "metrics": list(METRICS),
            "min_net_shares": list(DEFAULT_MIN_NET_SHARES),
            "min_net_amounts": list(DEFAULT_MIN_NET_AMOUNTS),
            "top_ns": list(DEFAULT_TOP_NS),
            "entry_lags": list(DEFAULT_ENTRY_LAGS),
            "holds": list(DEFAULT_HOLDS),
            "slots": list(DEFAULT_SLOTS),
        },
        "champions": {
            k: (cell_to_dict(v) if v is not None else None)
            for k, v in champions.items()
        },
        "hypotheses": hypotheses,
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
            champions=champions,
            hypotheses=hypotheses,
            n_cells=len(cells),
        ),
        encoding="utf-8",
    )
    return {"json": json_path, "md": md_path}


def load_champion_trade_ledger(
    conn: sqlite3.Connection,
    champion: dict,
    *,
    window_start: str,
    window_end: str,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    min_avg_volume_shares: float | None = 200_000.0,
) -> dict:
    """Replay fixed-slot execution for one dual-branch champion."""
    from research.backtest.copytrade_backtest import select_executed_signal_days

    mode = str(champion["mode"])
    metric = str(champion["metric"])
    thr = float(champion["min_threshold"])
    top_n = int(champion["top_n"])
    lag = int(champion["entry_lag_days"])
    hold = int(champion["hold_trading_days"])
    n_slots = int(champion["n_slots"])

    grouped = build_dual_signals(
        conn,
        mode=mode,  # type: ignore[arg-type]
        metric=metric,  # type: ignore[arg-type]
        min_threshold=thr,
        top_n=top_n,
        window_start=window_start,
        window_end=window_end,
        min_avg_volume_shares=min_avg_volume_shares,
    )
    result = run_copytrade_backtest(
        conn,
        ETF_CODE_PROXY,
        strategy_id=str(champion.get("strategy_id") or f"{_lag_label(lag)}H{hold}"),
        strategy_label="dual-champion-ledger",
        entry_lag_days=lag,
        hold_trading_days=hold,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=0.0,
        window_start=window_start,
        window_end=window_end,
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
        full = day_by_sig.get(str(day["signal_date"]))
        if full is None:
            continue
        for lg in full.legs:
            if lg.status != "complete":
                continue
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
            "mode": mode,
            "metric": metric,
            "threshold_label": champion.get("threshold_label"),
            "top_n": top_n,
            "n_slots": n_slots,
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
            "window_start": window_start,
            "window_end": window_end,
        },
        "trades": trades,
    }
