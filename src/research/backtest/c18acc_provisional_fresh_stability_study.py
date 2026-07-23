"""C18acc · provisional fresh (第四日 mono fresh) intraday stability.

PIT framing (aligned with live buy radar):
- Order criterion = mono fresh（LOOKBACK=4 收斂日上，昨非 mono、今為 mono_tier2）。
- Each clock rebuilds provisional RRG with 5m kbar prices (no EOD peek).
- Anchor = near-close SNAP@13:20（近似收盤可交易名單）.

Questions:
1. Of morning (09:30) provisional fresh, what fraction remain at 13:20?
2. How many morning signals are later eliminated (churn)?
3. Do multi-clock confirms lift precision toward the 13:20 set?

Not a slot PnL study — membership stability only.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from report_paths import RESEARCH_RRG
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_mono_daily_brief import LOOKBACK, LENGTH, _feat, _fresh_mono
from rrg_rotation import compute_rrg_panel
from stock_db.etf import load_etf_constituent_watchlist
from stock_db.kbar import price_at_or_before_minute

BENCH_ID = "IX0001"
# morning → close clocks · 5m aligned
DEFAULT_CLOCKS = ("09:30", "10:00", "11:00", "12:00", "13:00", "13:20")
ANCHOR = "13:20"
RRG_WINDOW = 80  # days of history for truncated provisional RRG

# Earlier = stricter · later = looser (standards decay toward close)
SEG_FLOOR_STEEP: dict[str, float] = {
    "09:30": 1.20,
    "10:00": 1.00,
    "11:00": 0.85,
    "12:00": 0.70,
    "13:00": 0.50,
    "13:20": 0.00,
}
SEG_FLOOR_MILD: dict[str, float] = {
    "09:30": 0.90,
    "10:00": 0.75,
    "11:00": 0.60,
    "12:00": 0.45,
    "13:00": 0.30,
    "13:20": 0.00,
}
TOPN_DECAY: dict[str, int] = {
    "09:30": 1,
    "10:00": 1,
    "11:00": 2,
    "12:00": 2,
    "13:00": 3,
    "13:20": 99,
}
# Consecutive provisional-fresh polls required (incl. current) before accept
CONFIRM_NEED_DECAY: dict[str, int] = {
    "09:30": 3,
    "10:00": 3,
    "11:00": 2,
    "12:00": 2,
    "13:00": 1,
    "13:20": 1,
}


def _load_day_5m_closes_batch(
    conn: sqlite3.Connection,
    trade_date: str,
    stock_ids: list[str],
) -> dict[str, tuple[tuple[str, float], ...]]:
    """Batch-load 5m closes for many stocks on one day (finmind preferred)."""
    if not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, minute, close, source
        FROM stock_kbar_5m
        WHERE trade_date = ?
          AND stock_id IN ({placeholders})
        ORDER BY stock_id, source, minute
        """,
        (trade_date, *stock_ids),
    ).fetchall()
    src_rank = {"finmind": 0, "yahoo": 1}
    by_src: dict[tuple[str, str], tuple[int, float]] = {}
    for r in rows:
        sid = str(r["stock_id"])
        minute = str(r["minute"])[:5]
        src = str(r["source"] or "")
        try:
            px = float(r["close"])
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        rank = src_rank.get(src, 9)
        cur = by_src.get((sid, minute))
        if cur is None or rank < cur[0]:
            by_src[(sid, minute)] = (rank, px)
    best: dict[str, dict[str, float]] = defaultdict(dict)
    for (sid, minute), (_, px) in by_src.items():
        best[sid][minute] = px
    return {
        sid: tuple((m, best[sid][m]) for m in sorted(best[sid]))
        for sid in stock_ids
        if sid in best and best[sid]
    }


def _prices_at_minute(
    day_bars: dict[str, tuple[tuple[str, float], ...]],
    minute: str,
    stock_ids: list[str],
) -> dict[str, float]:
    px: dict[str, float] = {}
    for sid in stock_ids:
        bars = day_bars.get(sid) or ()
        if not bars:
            continue
        v = price_at_or_before_minute(bars, minute)
        if v is not None and float(v) > 0:
            px[sid] = float(v)
    return px


def _fresh_scores_from_panels(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    universe: list[str],
) -> dict[str, float]:
    """Return {stock_id: seg_last} for provisional mono fresh names."""
    if as_of not in full_dates:
        return {}
    si = full_dates.index(as_of)
    if si < LOOKBACK:
        return {}
    out: dict[str, float] = {}
    for sid in universe:
        if sid not in rs_ratio.columns:
            continue
        if not _fresh_mono(rs_ratio, rs_mom, full_dates, si, sid):
            continue
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
        if f is None:
            continue
        out[sid] = float(f["seg_last"])
    return out


def _fresh_ids_from_panels(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    universe: list[str],
) -> set[str]:
    return set(_fresh_scores_from_panels(rs_ratio, rs_mom, full_dates, as_of, universe))


def _provisional_fresh_scores_at(
    close_window: pd.DataFrame,
    bench_window: pd.Series,
    *,
    as_of: str,
    prices: dict[str, float],
    bench_px: float | None,
    universe: list[str],
) -> dict[str, float]:
    """Mutate as_of row with provisional prices, compute truncated RRG, restore."""
    if as_of not in close_window.index:
        return {}
    old_close = close_window.loc[as_of].copy()
    old_bench = float(bench_window.at[as_of]) if as_of in bench_window.index else None
    try:
        for sid, px in prices.items():
            if sid in close_window.columns:
                close_window.at[as_of, sid] = px
        if bench_px is not None and bench_px > 0:
            bench_window.at[as_of] = float(bench_px)
        rs_r, rs_m, _ = compute_rrg_panel(close_window, bench_window, length=LENGTH)
        dates = close_window.index.astype(str).tolist()
        return _fresh_scores_from_panels(rs_r, rs_m, dates, as_of, universe)
    finally:
        close_window.loc[as_of] = old_close
        if old_bench is not None:
            bench_window.at[as_of] = old_bench


def _provisional_fresh_at(
    close_window: pd.DataFrame,
    bench_window: pd.Series,
    *,
    as_of: str,
    prices: dict[str, float],
    bench_px: float | None,
    universe: list[str],
) -> set[str]:
    return set(
        _provisional_fresh_scores_at(
            close_window,
            bench_window,
            as_of=as_of,
            prices=prices,
            bench_px=bench_px,
            universe=universe,
        )
    )

def _window_slice(
    close: pd.DataFrame,
    bench: pd.Series,
    as_of: str,
    *,
    window: int = RRG_WINDOW,
) -> tuple[pd.DataFrame, pd.Series] | None:
    if as_of not in close.index:
        return None
    loc = close.index.get_loc(as_of)
    if isinstance(loc, slice):
        return None
    i = int(loc)
    i0 = max(0, i - window + 1)
    cw = close.iloc[i0 : i + 1].copy()
    bw = bench.reindex(cw.index).astype(float).ffill().copy()
    return cw, bw


def _rate(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return round(100.0 * float(num) / float(den), 2)


def _confirm_rules(clocks: tuple[str, ...], anchor: str) -> list[dict[str, Any]]:
    """Precision of signal set toward anchor membership."""
    pre = [c for c in clocks if c < anchor]
    rules: list[dict[str, Any]] = []
    if "09:30" in clocks:
        rules.append(
            {
                "id": "ONCE_0930",
                "label": "09:30 首次出現（單次）",
                "need_clocks": ["09:30"],
            }
        )
    if "09:30" in clocks and "11:00" in clocks:
        rules.append(
            {
                "id": "AND_0930_1100",
                "label": "09:30 ∩ 11:00（兩次確認）",
                "need_clocks": ["09:30", "11:00"],
            }
        )
    if "09:30" in clocks and "12:00" in clocks:
        rules.append(
            {
                "id": "AND_0930_1200",
                "label": "09:30 ∩ 12:00（早+午）",
                "need_clocks": ["09:30", "12:00"],
            }
        )
    if "09:30" in clocks and "11:00" in clocks and "12:00" in clocks:
        rules.append(
            {
                "id": "AND_0930_1100_1200",
                "label": "09:30 ∩ 11:00 ∩ 12:00（三次確認）",
                "need_clocks": ["09:30", "11:00", "12:00"],
            }
        )
    if "12:00" in clocks and "13:00" in clocks:
        rules.append(
            {
                "id": "AND_1200_1300",
                "label": "12:00 ∩ 13:00（午後穩定）",
                "need_clocks": ["12:00", "13:00"],
            }
        )
    if len(pre) >= 2:
        rules.append(
            {
                "id": "ANY2_PRE_ANCHOR",
                "label": f"收盤前任意 ≥2 個時點出現（{', '.join(pre)}）",
                "need_any_n": 2,
                "pool_clocks": pre,
            }
        )
    if len(pre) >= 3:
        rules.append(
            {
                "id": "ANY3_PRE_ANCHOR",
                "label": f"收盤前任意 ≥3 個時點出現（{', '.join(pre)}）",
                "need_any_n": 3,
                "pool_clocks": pre,
            }
        )
    return rules


def _apply_rule(
    rule: dict[str, Any],
    by_clock: dict[str, set[str]],
) -> set[str]:
    if "need_clocks" in rule:
        sets = [by_clock.get(c, set()) for c in rule["need_clocks"]]
        if not sets:
            return set()
        out = set(sets[0])
        for s in sets[1:]:
            out &= s
        return out
    pool_clocks = rule.get("pool_clocks") or []
    need = int(rule.get("need_any_n") or 0)
    counts: dict[str, int] = defaultdict(int)
    for c in pool_clocks:
        for sid in by_clock.get(c, set()):
            counts[sid] += 1
    return {sid for sid, n in counts.items() if n >= need}


def run_provisional_fresh_stability_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    clocks: tuple[str, ...] = DEFAULT_CLOCKS,
    anchor: str = ANCHOR,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    if anchor not in clocks:
        clocks = tuple(list(clocks) + [anchor])

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    universe = [str(w["stock_id"]) for w in watch]
    universe_plus_bench = list(dict.fromkeys([*universe, BENCH_ID]))

    # EOD fresh as optional reference (truth at close of T — not used for early signals)
    eod_rs, eod_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    eod_fresh_by_date = {
        d: _fresh_ids_from_panels(eod_rs, eod_mom, full_dates, d, universe) for d in trade_dates
    }

    day_records: list[dict[str, Any]] = []
    # aggregate counters
    morn = clocks[0]
    tot_morn = tot_ret = tot_churn = tot_anchor = 0
    rule_stats: dict[str, dict[str, int]] = {
        r["id"]: {"signal_n": 0, "hit_n": 0, "days": 0} for r in _confirm_rules(clocks, anchor)
    }

    for i, as_of in enumerate(trade_dates):
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  day {i + 1}/{len(trade_dates)} {as_of} …", flush=True)

        win = _window_slice(close, bench, as_of)
        if win is None:
            continue
        cw, bw = win
        day_bars = _load_day_5m_closes_batch(conn, as_of, universe_plus_bench)
        if BENCH_ID not in day_bars and not day_bars:
            continue

        by_clock: dict[str, set[str]] = {}
        coverage: dict[str, int] = {}
        for minute in clocks:
            prices = _prices_at_minute(day_bars, minute, universe)
            coverage[minute] = len(prices)
            if len(prices) < max(30, len(universe) // 4):
                by_clock[minute] = set()
                continue
            bench_px = None
            bb = day_bars.get(BENCH_ID) or ()
            if bb:
                bench_px = price_at_or_before_minute(bb, minute)
            by_clock[minute] = _provisional_fresh_at(
                cw,
                bw,
                as_of=as_of,
                prices=prices,
                bench_px=float(bench_px) if bench_px else None,
                universe=universe,
            )

        morning = by_clock.get(morn, set())
        at_anchor = by_clock.get(anchor, set())
        retained = morning & at_anchor
        churned = morning - at_anchor
        late = at_anchor - morning

        tot_morn += len(morning)
        tot_ret += len(retained)
        tot_churn += len(churned)
        tot_anchor += len(at_anchor)

        eod = eod_fresh_by_date.get(as_of, set())
        anchor_vs_eod = at_anchor & eod

        for rule in _confirm_rules(clocks, anchor):
            sig = _apply_rule(rule, by_clock)
            hit = sig & at_anchor
            st = rule_stats[rule["id"]]
            st["signal_n"] += len(sig)
            st["hit_n"] += len(hit)
            if sig or at_anchor or morning:
                st["days"] += 1

        day_records.append(
            {
                "date": as_of,
                "n_morning": len(morning),
                "n_anchor": len(at_anchor),
                "n_retained": len(retained),
                "n_churned": len(churned),
                "n_late": len(late),
                "retention_pct": _rate(len(retained), len(morning)),
                "churn_pct": _rate(len(churned), len(morning)),
                "n_eod": len(eod),
                "anchor_in_eod_pct": _rate(len(anchor_vs_eod), len(at_anchor)),
                "coverage": coverage,
                "morning_ids": sorted(morning),
                "anchor_ids": sorted(at_anchor),
                "churned_ids": sorted(churned),
            }
        )

    days_with_morning = sum(1 for r in day_records if r["n_morning"] > 0)
    days_with_anchor = sum(1 for r in day_records if r["n_anchor"] > 0)

    confirm_table = []
    for rule in _confirm_rules(clocks, anchor):
        st = rule_stats[rule["id"]]
        confirm_table.append(
            {
                **rule,
                "signal_n": st["signal_n"],
                "hit_n": st["hit_n"],
                "precision_pct": _rate(st["hit_n"], st["signal_n"]),
                "lift_vs_once": None,
            }
        )
    once = next((r for r in confirm_table if r["id"] == "ONCE_0930"), None)
    base_p = once.get("precision_pct") if once else None
    if base_p is not None:
        for r in confirm_table:
            p = r.get("precision_pct")
            if p is not None and r["id"] != "ONCE_0930":
                r["lift_vs_once_pp"] = round(float(p) - float(base_p), 2)

    # recommend confirm: best precision with signal_n >= 20
    eligible = [r for r in confirm_table if int(r["signal_n"]) >= 20 and r.get("precision_pct") is not None]
    best = max(eligible, key=lambda r: float(r["precision_pct"])) if eligible else None

    summary = {
        "n_days": len(day_records),
        "days_with_morning_signal": days_with_morning,
        "days_with_anchor_signal": days_with_anchor,
        "morning_clock": morn,
        "anchor_clock": anchor,
        "morning_signal_n": tot_morn,
        "retained_n": tot_ret,
        "churned_n": tot_churn,
        "anchor_signal_n": tot_anchor,
        "retention_pct": _rate(tot_ret, tot_morn),
        "churn_pct": _rate(tot_churn, tot_morn),
        "morning_recall_of_anchor_pct": _rate(tot_ret, tot_anchor),
        "mean_morning_per_day": round(tot_morn / max(1, len(day_records)), 3),
        "mean_anchor_per_day": round(tot_anchor / max(1, len(day_records)), 3),
        "mean_daily_retention_pct": _finite_mean(
            [r["retention_pct"] for r in day_records if r["retention_pct"] is not None]
        ),
    }

    return {
        "study_id": "c18acc_provisional_fresh_stability",
        "schema": "c18acc_provisional_fresh_stability-v1",
        "pit": {
            "criterion": "mono_fresh (LOOKBACK=4 · 昨非 mono / 今 mono_tier2)",
            "price": "5m kbar provisional close at clock",
            "anchor": f"SNAP@{anchor} provisional fresh ≈ near-close tradeable list",
            "no_eod_peek_for_early_clocks": True,
        },
        "window": {"date_start": date_start, "date_end": end, "n_stocks": len(universe)},
        "clocks": list(clocks),
        "summary": summary,
        "confirm_rules": confirm_table,
        "recommendation": {
            "best_confirm_rule": best,
            "note": (
                "多次確認提高的是「仍出現在 13:20 暫定 fresh」的精確率（precision），"
                "不是帳面超額報酬。命中率↑通常伴隨訊號數↓。"
            ),
        },
        "days": day_records,
    }


def _finite_mean(vals: list[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 2)


def _filter_seg_floor(scores: dict[str, float], floor: float) -> set[str]:
    return {sid for sid, seg in scores.items() if float(seg) >= float(floor)}


def _filter_top_n(scores: dict[str, float], top_n: int) -> set[str]:
    if top_n <= 0:
        return set()
    ranked = sorted(scores.items(), key=lambda kv: (-float(kv[1]), kv[0]))
    return {sid for sid, _ in ranked[: int(top_n)]}


def _filter_confirm_need(
    scores_by_clock: dict[str, dict[str, float]],
    clocks: tuple[str, ...],
    minute: str,
    need: int,
) -> set[str]:
    """Accept sid at ``minute`` only if fresh in last ``need`` clocks ending at minute."""
    if need <= 1:
        return set(scores_by_clock.get(minute) or {})
    if minute not in clocks:
        return set()
    idx = clocks.index(minute)
    if idx + 1 < need:
        return set()  # not enough prior clocks — early open cannot meet high confirm
    window = clocks[idx + 1 - need : idx + 1]
    sets = [set(scores_by_clock.get(c) or {}) for c in window]
    out = sets[0]
    for s in sets[1:]:
        out &= s
    return out


def _decaying_schedules() -> list[dict[str, Any]]:
    return [
        {
            "id": "FLAT_FRESH",
            "label": "對照 · 僅 mono fresh（無遞增門檻）",
            "kind": "flat",
        },
        {
            "id": "SEG_STEEP",
            "label": "seg_last 陡降 · 早 1.20 → 近收 0",
            "kind": "seg_floor",
            "floors": dict(SEG_FLOOR_STEEP),
        },
        {
            "id": "SEG_MILD",
            "label": "seg_last 緩降 · 早 0.90 → 近收 0",
            "kind": "seg_floor",
            "floors": dict(SEG_FLOOR_MILD),
        },
        {
            "id": "TOPN_DECAY",
            "label": "排名門檻遞減 · 早 top1 → 近收全取",
            "kind": "top_n",
            "top_n": dict(TOPN_DECAY),
        },
        {
            "id": "CONFIRM_DECAY",
            "label": "confirm 遞減 · 早需連續 3 拍 → 近收 1 拍",
            "kind": "confirm_need",
            "need": dict(CONFIRM_NEED_DECAY),
        },
        {
            "id": "HYBRID_SEG_CONFIRM",
            "label": "混合 · 陡降 seg + confirm 遞減",
            "kind": "hybrid_seg_confirm",
            "floors": dict(SEG_FLOOR_STEEP),
            "need": dict(CONFIRM_NEED_DECAY),
        },
    ]


def _accept_at_clock(
    schedule: dict[str, Any],
    *,
    minute: str,
    clocks: tuple[str, ...],
    scores_by_clock: dict[str, dict[str, float]],
) -> set[str]:
    scores = scores_by_clock.get(minute) or {}
    kind = schedule["kind"]
    if kind == "flat":
        return set(scores)
    if kind == "seg_floor":
        floor = float((schedule.get("floors") or {}).get(minute, 0.0))
        return _filter_seg_floor(scores, floor)
    if kind == "top_n":
        n = int((schedule.get("top_n") or {}).get(minute, 99))
        return _filter_top_n(scores, n)
    if kind == "confirm_need":
        need = int((schedule.get("need") or {}).get(minute, 1))
        return _filter_confirm_need(scores_by_clock, clocks, minute, need)
    if kind == "hybrid_seg_confirm":
        floor = float((schedule.get("floors") or {}).get(minute, 0.0))
        need = int((schedule.get("need") or {}).get(minute, 1))
        seg_ok = _filter_seg_floor(scores, floor)
        conf_ok = _filter_confirm_need(scores_by_clock, clocks, minute, need)
        return seg_ok & conf_ok
    return set()


def run_decaying_standard_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    clocks: tuple[str, ...] = DEFAULT_CLOCKS,
    anchor: str = ANCHOR,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    """Time-decaying entry standards on provisional fresh · precision vs anchor."""
    if anchor not in clocks:
        clocks = tuple(list(clocks) + [anchor])

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    universe = [str(w["stock_id"]) for w in watch]
    universe_plus_bench = list(dict.fromkeys([*universe, BENCH_ID]))

    schedules = _decaying_schedules()
    # per schedule: overall first-hit + per-clock buckets
    overall: dict[str, dict[str, int]] = {
        s["id"]: {"signal_n": 0, "hit_n": 0} for s in schedules
    }
    by_clock_stats: dict[str, dict[str, dict[str, int]]] = {
        s["id"]: {c: {"signal_n": 0, "hit_n": 0} for c in clocks} for s in schedules
    }

    for i, as_of in enumerate(trade_dates):
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  day {i + 1}/{len(trade_dates)} {as_of} …", flush=True)
        win = _window_slice(close, bench, as_of)
        if win is None:
            continue
        cw, bw = win
        day_bars = _load_day_5m_closes_batch(conn, as_of, universe_plus_bench)
        if not day_bars:
            continue

        scores_by_clock: dict[str, dict[str, float]] = {}
        for minute in clocks:
            prices = _prices_at_minute(day_bars, minute, universe)
            if len(prices) < max(30, len(universe) // 4):
                scores_by_clock[minute] = {}
                continue
            bench_px = None
            bb = day_bars.get(BENCH_ID) or ()
            if bb:
                bench_px = price_at_or_before_minute(bb, minute)
            scores_by_clock[minute] = _provisional_fresh_scores_at(
                cw,
                bw,
                as_of=as_of,
                prices=prices,
                bench_px=float(bench_px) if bench_px else None,
                universe=universe,
            )

        anchor_set = set(scores_by_clock.get(anchor) or {})

        for sched in schedules:
            # first clock in day where sid is accepted (time-decay permits earlier only if stronger)
            first_hit: dict[str, str] = {}
            for minute in clocks:
                accepted = _accept_at_clock(
                    sched,
                    minute=minute,
                    clocks=clocks,
                    scores_by_clock=scores_by_clock,
                )
                st = by_clock_stats[sched["id"]][minute]
                st["signal_n"] += len(accepted)
                st["hit_n"] += len(accepted & anchor_set)
                for sid in accepted:
                    if sid not in first_hit:
                        first_hit[sid] = minute
            # overall = unique first acceptances that day (one signal per stock-day)
            ov = overall[sched["id"]]
            ov["signal_n"] += len(first_hit)
            ov["hit_n"] += sum(1 for sid in first_hit if sid in anchor_set)

    base = overall.get("FLAT_FRESH") or {}
    base_p = _rate(int(base.get("hit_n") or 0), int(base.get("signal_n") or 0))

    schedule_rows: list[dict[str, Any]] = []
    for sched in schedules:
        ov = overall[sched["id"]]
        prec = _rate(ov["hit_n"], ov["signal_n"])
        clock_rows = []
        for c in clocks:
            st = by_clock_stats[sched["id"]][c]
            clock_rows.append(
                {
                    "clock": c,
                    "signal_n": st["signal_n"],
                    "hit_n": st["hit_n"],
                    "precision_pct": _rate(st["hit_n"], st["signal_n"]),
                }
            )
        row = {
            "id": sched["id"],
            "label": sched["label"],
            "kind": sched["kind"],
            "signal_n": ov["signal_n"],
            "hit_n": ov["hit_n"],
            "precision_pct": prec,
            "lift_vs_flat_pp": (
                round(float(prec) - float(base_p), 2)
                if prec is not None and base_p is not None and sched["id"] != "FLAT_FRESH"
                else None
            ),
            "by_clock": clock_rows,
            "params": {
                k: sched[k]
                for k in ("floors", "top_n", "need")
                if k in sched
            },
        }
        schedule_rows.append(row)

    eligible = [
        r
        for r in schedule_rows
        if r["id"] != "FLAT_FRESH"
        and int(r["signal_n"]) >= 20
        and r.get("precision_pct") is not None
    ]
    best = max(eligible, key=lambda r: float(r["precision_pct"])) if eligible else None

    return {
        "study_id": "c18acc_provisional_fresh_decaying_standard",
        "schema": "c18acc_provisional_fresh_decaying_standard-v1",
        "pit": {
            "criterion": "mono_fresh + time-decaying extra gate",
            "idea": "earlier clocks require stricter std; standards decay toward close",
            "anchor": f"SNAP@{anchor} provisional fresh",
            "no_eod_peek_for_early_clocks": True,
        },
        "window": {"date_start": date_start, "date_end": end, "n_stocks": len(universe)},
        "clocks": list(clocks),
        "schedules": schedule_rows,
        "recommendation": {
            "best_schedule": best,
            "note": (
                "precision = 首次被該排程放行的股日中，仍出現在 13:20 暫定 fresh 的比例。"
                "門檻越高／越早 → 通常 precision↑、訊號↓。非槽位報酬。"
            ),
        },
    }


def render_decaying_standard_md(payload: dict[str, Any]) -> str:
    w = payload.get("window") or {}
    lines = [
        "# C18acc · 暫定 fresh · 越早門檻越高（標準隨時間遞減）",
        "",
        f"窗口：**{w.get('date_start')}** .. **{w.get('date_end')}** · "
        f"watchlist_n=**{w.get('n_stocks')}** · clocks=`{', '.join(payload.get('clocks') or [])}`",
        "",
        "## 語意",
        "",
        "- 每個時點重算暫定 RRG mono fresh（PIT）。",
        "- **提早放行必須通過更高標準**；越接近 13:20 標準越鬆。",
        "- 股日以「當天首次被放行」計一次訊號；命中 = 仍在 13:20 暫定 fresh。",
        "",
        "## 排程比較（全日首次放行）",
        "",
        "| schedule | 訊號數 | 命中 13:20 | precision% | vs 無門檻 |",
        "|----------|-------:|----------:|-------------:|----------:|",
    ]
    for r in payload.get("schedules") or []:
        lift = r.get("lift_vs_flat_pp")
        lift_s = f"{lift:+.2f}pp" if lift is not None else "—"
        lines.append(
            f"| {r.get('label')} | {r.get('signal_n')} | {r.get('hit_n')} | "
            f"{r.get('precision_pct')} | {lift_s} |"
        )
    rec = (payload.get("recommendation") or {}).get("best_schedule") or {}
    lines.append("")
    if rec:
        lines.append(
            f"**最佳（樣本≥20）**：`{rec.get('id')}` — {rec.get('label')} · "
            f"precision **{rec.get('precision_pct')}%**（訊號 {rec.get('signal_n')}）"
        )
    note = (payload.get("recommendation") or {}).get("note")
    if note:
        lines.extend(["", f"> {note}", ""])

    lines.append("## 各時點細節（該刻放行 ∩ 錨點）")
    lines.append("")
    for r in payload.get("schedules") or []:
        if r.get("id") == "FLAT_FRESH":
            continue
        lines.append(f"### {r.get('label')}")
        lines.append("")
        lines.append("| clock | 訊號 | 命中 | precision% |")
        lines.append("|------|-----:|-----:|-----------:|")
        for c in r.get("by_clock") or []:
            lines.append(
                f"| {c.get('clock')} | {c.get('signal_n')} | {c.get('hit_n')} | "
                f"{c.get('precision_pct')} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 怎麼用",
            "",
            "1. **早盤想下單**：門檻要明顯高於近收（更高 `seg_last` / 只取 top1 / 要連續多拍）。",
            "2. **午後靠近 13:20**：可逐步放寬，最後回到「只要 fresh」。",
            "3. 與「多時點 AND 確認」互補：一個管**強度**，一個管**時間穩定**。",
            "",
            "模組：`src/research/backtest/c18acc_provisional_fresh_stability_study.py` · `--mode decaying`",
        ]
    )
    return "\n".join(lines) + "\n"


def render_provisional_fresh_stability_md(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or {}
    w = payload.get("window") or {}
    lines = [
        "# C18acc · 暫定 RRG mono fresh 穩定度（早→近收）",
        "",
        f"窗口：**{w.get('date_start')}** .. **{w.get('date_end')}** · "
        f"watchlist_n=**{w.get('n_stocks')}** · clocks=`{', '.join(payload.get('clocks') or [])}`",
        "",
        "## 問題與語意（PIT）",
        "",
        "- **下單標準**：mono fresh（LOOKBACK=4 收斂日／俗稱第四日 · 暫定 RRG 重算）。",
        "- 每個時點用當下 5m 價重算暫定 RRG，**不偷看收盤**。",
        f"- **錨點**：`{s.get('anchor_clock')}` 暫定 fresh ≈ 近收盤仍可交易名單。",
        "- 本報告只量「名單穩定／淘汰」，**不做槽位報酬回測**。",
        "",
        "## 主結論",
        "",
    ]
    ret = s.get("retention_pct")
    churn = s.get("churn_pct")
    if ret is not None:
        lines.append(
            f"- 早上 `{s.get('morning_clock')}` 出現的暫定 fresh 中，"
            f"**{ret}%** 在 `{s.get('anchor_clock')}` 仍選中；"
            f"**{churn}%** 被淘汰（不穩定訊號）。"
        )
    lines.append(
        f"- 股日樣本：morning_n={s.get('morning_signal_n')} · "
        f"retained={s.get('retained_n')} · churned={s.get('churned_n')} · "
        f"anchor_n={s.get('anchor_signal_n')} · days={s.get('n_days')}"
    )
    lines.append(
        f"- 早上對錨點的召回（早上就能抓到多少終局）："
        f"**{s.get('morning_recall_of_anchor_pct')}%**"
    )
    lines.append("")
    lines.append("## 多次確認 → 精確率（命中錨點）")
    lines.append("")
    lines.append("| rule | 訊號數 | 命中錨點 | precision% | vs 單次 09:30 |")
    lines.append("|------|-------:|---------:|-------------:|--------------:|")
    for r in payload.get("confirm_rules") or []:
        lift = r.get("lift_vs_once_pp")
        lift_s = f"{lift:+.2f}pp" if lift is not None else "—"
        lines.append(
            f"| {r.get('label')} | {r.get('signal_n')} | {r.get('hit_n')} | "
            f"{r.get('precision_pct')} | {lift_s} |"
        )
    rec = (payload.get("recommendation") or {}).get("best_confirm_rule") or {}
    lines.append("")
    if rec:
        lines.append(
            f"**建議確認規則（樣本≥20 且 precision 最高）**：`{rec.get('id')}` — "
            f"{rec.get('label')} · precision **{rec.get('precision_pct')}%** "
            f"（訊號 {rec.get('signal_n')}）"
        )
    note = (payload.get("recommendation") or {}).get("note")
    if note:
        lines.extend(["", f"> {note}", ""])
    lines.extend(
        [
            "## 怎麼用在交易上",
            "",
            "1. **不要**把早上一次暫定 fresh 當成收盤真相；預期約有一半級（見上）會翻車。",
            "2. **可以提高命中率**：要求同一票在多個時點重算仍符合（上表 AND / ≥N 時點）。",
            "3. **代價**：訊號變少；越靠近 12:00–13:00 的交集通常越穩、也越接近現行近收開窗。",
            "4. live 若要「全天都有機會」，應採 **poll 重算 + 多次 confirm**，而非鎖死 EOD 名單提早買。",
            "",
            "模組：`src/research/backtest/c18acc_provisional_fresh_stability_study.py`",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    from stock_db import DEFAULT_DB_PATH, connect

    ap = argparse.ArgumentParser(description="Provisional mono-fresh stability / decaying standards")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument(
        "--clocks",
        default=",".join(DEFAULT_CLOCKS),
        help="comma minutes, must include 13:20 anchor",
    )
    ap.add_argument(
        "--mode",
        choices=("stability", "decaying"),
        default="stability",
        help="stability=早→近收保留率；decaying=越早門檻越高",
    )
    args = ap.parse_args(argv)
    clocks = tuple(c.strip() for c in str(args.clocks).split(",") if c.strip())

    conn = connect(args.db)
    try:
        if args.mode == "decaying":
            payload = run_decaying_standard_study(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                clocks=clocks,
            )
        else:
            payload = run_provisional_fresh_stability_study(
                conn,
                date_start=args.date_start,
                date_end=args.date_end,
                clocks=clocks,
            )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    if args.mode == "decaying":
        out_json = RESEARCH_RRG / f"{stamp}_c18acc_provisional_fresh_decaying_standard.json"
        out_md = out_json.with_suffix(".md")
        body = render_decaying_standard_md(payload)
    else:
        out_json = RESEARCH_RRG / f"{stamp}_c18acc_provisional_fresh_stability.json"
        out_md = out_json.with_suffix(".md")
        body = render_provisional_fresh_stability_md(payload)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(body, encoding="utf-8")

    if args.mode == "decaying":
        rec = (payload.get("recommendation") or {}).get("best_schedule") or {}
        print(
            f"best={rec.get('id')} precision={rec.get('precision_pct')}% "
            f"signals={rec.get('signal_n')}"
        )
        for r in payload.get("schedules") or []:
            print(
                f"  {r['id']}: n={r['signal_n']} hit={r['hit_n']} "
                f"prec={r['precision_pct']} lift={r.get('lift_vs_flat_pp')}"
            )
    else:
        s = payload.get("summary") or {}
        print(
            f"retention={s.get('retention_pct')}% churn={s.get('churn_pct')}% "
            f"morning_n={s.get('morning_signal_n')} retained={s.get('retained_n')}"
        )
        rec = (payload.get("recommendation") or {}).get("best_confirm_rule")
        if rec:
            print(f"best_confirm={rec.get('id')} precision={rec.get('precision_pct')}%")
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
