"""C18acc · open-window timing study · NTB_0930 / NTB_0910 / PRIOR_1300.

Frozen baseline: fresh · confirm_bars=2 · avoid_spread_mixed · G_R5_12 · S2 · n_slots=3.

- NTB_0930: no_trade_before=09:30 (live default) · full slot re-sim
- NTB_0910: no_trade_before=09:10 · full slot re-sim · spread gate anchor 09:10
- PRIOR_1300: post-hoc fill shift on NTB_0930 legs → prior trading day @ 13:00
  (selection / exits unchanged)
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from analytics.bench import bench_return_entry_to_exit
from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (
    build_avoid_mixed_gate_lookup_live,
    load_gate_cache,
    save_gate_cache,
)
from research.backtest.archive.c18acc_hold3d_event_study import (
    _prepare_c18acc_context,
    c18acc_live_s2_config,
)
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_lens_score_swap import _prior_trading_date
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _poll5m_needs_spread_rs_panel,
    _trading_days_between,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

ADOPT_OOS_MIN_N = 8
ADOPT_OOS_MIN_DELTA_PP = 0.5
ADOPT_IS_MIN_DELTA_PP = -0.3
PRIOR_FILL_MINUTE = "13:00"
DEFAULT_GATE_CACHE = (
    "reports/research/rrg/20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)


def _entry_c_config(confirm_bars: int = 2):
    base = next(c for c in DEFAULT_C_SWEEP if c.variant_id == ("C0" if confirm_bars <= 1 else "C3"))
    if int(base.confirm_bars) == int(confirm_bars):
        return base
    return replace(base, confirm_bars=int(confirm_bars))


def _slice_periods(periods: list[dict[str, Any]], *, start: str, end: str) -> list[dict[str, Any]]:
    return [p for p in periods if start <= str(p.get("entry_date") or "") <= end]


def _period_stats(periods: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> dict[str, Any]:
    s = summarize_periods(periods)
    s["n_legs"] = len(periods)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    if mr is not None and mb is not None:
        s["mean_excess_pct"] = round(float(mr) - float(mb), 4)
    elif summary and summary.get("mean_excess_pct") is not None:
        s["mean_excess_pct"] = summary.get("mean_excess_pct")
    else:
        s["mean_excess_pct"] = None
    if summary:
        s["swaps"] = summary.get("swaps_total")
        s["force_exits"] = summary.get("force_exits")
    return s


def _entry_minute_hist(periods: list[dict[str, Any]]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for p in periods:
        m = str(p.get("entry_minute") or "—")
        c[m] += 1
    return dict(sorted(c.items()))


def _delta_pp(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def prior_day_1300_px(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    prior_date: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> float | None:
    """Kbar close at or before 13:00 on prior_date · None if missing."""
    cache = kbar_cache if kbar_cache is not None else {}
    key = (stock_id, prior_date)
    if key not in cache:
        cache[key] = load_kbar_day_closes(conn, stock_id, prior_date)
    px = price_at_or_before_minute(cache[key], PRIOR_FILL_MINUTE)
    if px is None or px <= 0:
        return None
    return float(px)


def prior_day_1300_fill(
    conn: sqlite3.Connection,
    leg: dict[str, Any],
    *,
    full_dates: list[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> dict[str, Any] | None:
    """Shift baseline leg entry to prior trading day @ 13:00 · keep exit fixed.

    Returns None when prior day / price unavailable or entry would not precede exit.
    """
    entry = str(leg.get("entry_date") or "")
    exit_d = str(leg.get("exit_date") or "")
    sid = str(leg.get("stock_id") or "")
    if not entry or not exit_d or not sid:
        return None
    prior = _prior_trading_date(full_dates, entry)
    if not prior:
        return None
    if prior >= exit_d:
        return None
    exit_px = leg.get("exit_px")
    if exit_px is None:
        return None
    try:
        exit_px_f = float(exit_px)
    except (TypeError, ValueError):
        return None
    if exit_px_f <= 0:
        return None
    entry_px = prior_day_1300_px(conn, stock_id=sid, prior_date=prior, kbar_cache=kbar_cache)
    if entry_px is None:
        return None
    ret = (exit_px_f / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, prior, exit_d, entry_price_mode="close")
    if bench is None:
        return None
    out = dict(leg)
    out["baseline_entry_date"] = entry
    out["baseline_entry_minute"] = leg.get("entry_minute")
    out["baseline_entry_px"] = leg.get("entry_px")
    out["baseline_return_pct"] = leg.get("return_pct")
    out["baseline_excess_pct"] = leg.get("excess_pct")
    out["entry_date"] = prior
    out["entry_minute"] = PRIOR_FILL_MINUTE
    out["entry_px"] = round(entry_px, 4)
    out["hold_days"] = _trading_days_between(full_dates, prior, exit_d)
    out["return_pct"] = round(ret, 4)
    out["bench_return_pct"] = round(float(bench), 4)
    out["excess_pct"] = round(ret - float(bench), 4)
    out["beat_bench"] = ret > float(bench)
    out["gross_win"] = ret > 0
    out["variant_id"] = "PRIOR_1300"
    return out


def same_day_1300_fill(
    conn: sqlite3.Connection,
    leg: dict[str, Any],
    *,
    full_dates: list[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> dict[str, Any] | None:
    """Keep entry_date · move fill to 13:00 same day (still look-ahead vs EOD signal)."""
    entry = str(leg.get("entry_date") or "")
    exit_d = str(leg.get("exit_date") or "")
    sid = str(leg.get("stock_id") or "")
    if not entry or not exit_d or not sid:
        return None
    if entry >= exit_d and entry != exit_d:
        # allow same-day exit edge cases via price only; hold_days handles
        pass
    exit_px = leg.get("exit_px")
    if exit_px is None:
        return None
    try:
        exit_px_f = float(exit_px)
    except (TypeError, ValueError):
        return None
    if exit_px_f <= 0:
        return None
    entry_px = prior_day_1300_px(conn, stock_id=sid, prior_date=entry, kbar_cache=kbar_cache)
    if entry_px is None:
        return None
    ret = (exit_px_f / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode="close")
    if bench is None:
        return None
    out = dict(leg)
    out["baseline_entry_date"] = entry
    out["baseline_entry_minute"] = leg.get("entry_minute")
    out["baseline_entry_px"] = leg.get("entry_px")
    out["baseline_return_pct"] = leg.get("return_pct")
    out["baseline_excess_pct"] = leg.get("excess_pct")
    out["entry_date"] = entry
    out["entry_minute"] = PRIOR_FILL_MINUTE
    out["entry_px"] = round(entry_px, 4)
    out["hold_days"] = _trading_days_between(full_dates, entry, exit_d)
    out["return_pct"] = round(ret, 4)
    out["bench_return_pct"] = round(float(bench), 4)
    out["excess_pct"] = round(ret - float(bench), 4)
    out["beat_bench"] = ret > float(bench)
    out["gross_win"] = ret > 0
    out["variant_id"] = "SAME_DAY_1300"
    return out


def apply_same_day_1300_fills(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    full_dates: list[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = kbar_cache if kbar_cache is not None else {}
    out: list[dict[str, Any]] = []
    n_skip_no_px = 0
    for leg in periods:
        filled = same_day_1300_fill(conn, leg, full_dates=full_dates, kbar_cache=cache)
        if filled is None:
            n_skip_no_px += 1
            continue
        out.append(filled)
    return out, {
        "n_baseline": len(periods),
        "n_filled": len(out),
        "n_skipped_no_px": n_skip_no_px,
        "fill_minute": PRIOR_FILL_MINUTE,
    }


def lag_rows_by_prior_day(
    by_date: dict[str, list],
    *,
    full_dates: list[str],
    trade_dates: list[str],
) -> dict[str, list]:
    """Live-aligned pool: as_of uses prior trading day's rows."""
    out: dict[str, list] = {}
    for d in trade_dates:
        prior = _prior_trading_date(full_dates, d)
        out[d] = list(by_date.get(prior, [])) if prior else []
    return out


def apply_prior_day_1300_fills(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    full_dates: list[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = kbar_cache if kbar_cache is not None else {}
    out: list[dict[str, Any]] = []
    n_skip_no_prior = 0
    n_skip_no_px = 0
    n_skip_bad_window = 0
    for leg in periods:
        entry = str(leg.get("entry_date") or "")
        prior = _prior_trading_date(full_dates, entry) if entry else None
        if not prior:
            n_skip_no_prior += 1
            continue
        exit_d = str(leg.get("exit_date") or "")
        if prior >= exit_d:
            n_skip_bad_window += 1
            continue
        filled = prior_day_1300_fill(conn, leg, full_dates=full_dates, kbar_cache=cache)
        if filled is None:
            n_skip_no_px += 1
            continue
        out.append(filled)
    meta = {
        "n_baseline": len(periods),
        "n_filled": len(out),
        "n_skipped_no_prior": n_skip_no_prior,
        "n_skipped_no_px": n_skip_no_px,
        "n_skipped_bad_window": n_skip_bad_window,
        "fill_minute": PRIOR_FILL_MINUTE,
    }
    return out, meta


def _ensure_gate_at_anchor(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    gate_cache_path: str | Path | None,
    gate_anchor_minute: str,
    n_slots: int = 3,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Load or build avoid_mixed gate at the given anchor minute."""
    if gate_anchor_minute == "09:30" and gate_cache_path:
        gate, meta = _ensure_avoid_mixed_gate(
            conn,
            trade_dates,
            gate_cache_path=gate_cache_path,
            live_aligned=True,
            n_slots=n_slots,
        )
        if gate is None:
            raise ValueError("avoid_mixed gate required for NTB_0930")
        return gate, meta

    path = Path(gate_cache_path) if gate_cache_path else None
    if path and path.is_file():
        gate, meta, c0, c1 = load_gate_cache(str(path))
        if c0 != trade_dates[0] or c1 != trade_dates[-1]:
            raise ValueError(f"gate cache {c0}..{c1} != {trade_dates[0]}..{trade_dates[-1]}")
        cached_anchor = str((meta or {}).get("gate_anchor_minute") or "")
        if cached_anchor and cached_anchor != gate_anchor_minute:
            raise ValueError(
                f"gate cache anchor {cached_anchor!r} != requested {gate_anchor_minute!r}"
            )
        return gate, meta

    print(
        f"building avoid_mixed gate @ {gate_anchor_minute} · "
        f"{trade_dates[0]}→{trade_dates[-1]} ({len(trade_dates)} td) …",
        flush=True,
    )
    ctx = _prepare_c18acc_context(conn, trade_dates)
    cfg = replace(
        c18acc_live_s2_config(n_slots=n_slots),
        no_trade_before=gate_anchor_minute,
    )
    gate, meta = build_avoid_mixed_gate_lookup_live(
        conn,
        trade_dates=trade_dates,
        ctx=ctx,
        cfg=cfg,
        no_trade_before=gate_anchor_minute,
        gate_anchor_minute=gate_anchor_minute,
        passthrough_pool=True,
    )
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_gate_cache(
            str(path),
            gate=gate,
            meta=meta,
            date_start=trade_dates[0],
            date_end=trade_dates[-1],
        )
        print(f"saved gate → {path}", flush=True)
    return gate, meta


def _run_sim(
    conn: sqlite3.Connection,
    *,
    ctx: dict[str, Any],
    trade_dates: list[str],
    cfg: ScoreSwapCConfig,
    confirm_bars: int,
    gate: dict[str, set[str]] | None,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    print(f"  sim {label} …", flush=True)
    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=ctx["full_dates"],
        close=ctx["close"],
        bench=ctx["bench"],
        fresh_by_date=ctx["fresh_by_date"],
        zone_by_date=ctx["zone_by_date"],
        config=cfg,
        mono_by_date=ctx["mono_by_date"],
        kbar_cache=ctx["kbar_cache"],
        rs_mom=ctx["rs_mom"],
        rs_ratio=ctx["rs_ratio"],
        spread_rs_panel=ctx["spread_rs_panel"],
        entry_c_config=_entry_c_config(confirm_bars),
        entry_gate_by_date=gate,
        swap_gate_by_date=gate,
    )
    stats = _period_stats(periods, summary)
    print(
        f"    n={stats['n_legs']} excess={stats.get('mean_excess_pct')}% "
        f"win={stats.get('win_rate_gross_pct')}",
        flush=True,
    )
    return periods, summary, stats


def _variant_row(
    *,
    variant_id: str,
    label: str,
    periods: list[dict[str, Any]],
    summary: dict[str, Any] | None,
    date_start: str,
    is_end: str,
    oos_start: str,
    end: str,
    base_full: dict[str, Any] | None = None,
    base_is: dict[str, Any] | None = None,
    base_oos: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full = _period_stats(periods, summary)
    is_s = _period_stats(_slice_periods(periods, start=date_start, end=is_end))
    oos = _period_stats(_slice_periods(periods, start=oos_start, end=end))
    row: dict[str, Any] = {
        "variant_id": variant_id,
        "label": label,
        "slices": {"full": full, "is": is_s, "oos": oos},
        "entry_minute_hist": _entry_minute_hist(periods),
        "delta_full_excess_pp": _delta_pp(
            full.get("mean_excess_pct"), (base_full or {}).get("mean_excess_pct")
        )
        if base_full
        else None,
        "delta_is_excess_pp": _delta_pp(
            is_s.get("mean_excess_pct"), (base_is or {}).get("mean_excess_pct")
        )
        if base_is
        else None,
        "delta_oos_excess_pp": _delta_pp(
            oos.get("mean_excess_pct"), (base_oos or {}).get("mean_excess_pct")
        )
        if base_oos
        else None,
    }
    if extra:
        row.update(extra)
    return row


def _verdict(variants: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {v["variant_id"]: v for v in variants}
    base = by_id.get("NTB_0930") or {}
    notes: list[str] = []
    adopt: dict[str, str] = {}
    for vid in ("NTB_0910", "PRIOR_1300"):
        v = by_id.get(vid) or {}
        oos = (v.get("slices") or {}).get("oos") or {}
        is_s = (v.get("slices") or {}).get("is") or {}
        d_oos = v.get("delta_oos_excess_pp")
        d_is = v.get("delta_is_excess_pp")
        n_oos = int(oos.get("n_legs") or 0)
        ok = (
            n_oos >= ADOPT_OOS_MIN_N
            and d_oos is not None
            and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
            and d_is is not None
            and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
        )
        adopt[vid] = "GO" if ok else "NO_GO"
        notes.append(
            f"{vid}: OOS Δexcess={d_oos}pp n={n_oos} · IS Δ={d_is}pp → {adopt[vid]}"
        )
    return {
        "baseline": "NTB_0930",
        "adopt": adopt,
        "thresholds": {
            "oos_min_n": ADOPT_OOS_MIN_N,
            "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
            "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
        },
        "baseline_oos_excess": ((base.get("slices") or {}).get("oos") or {}).get(
            "mean_excess_pct"
        ),
        "summary": " · ".join(notes),
    }


def run_open_timing_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    gate_cache_0910_path: str | Path | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    # Align to default 09:30 gate cache window when present
    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
    )
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench) if _poll5m_needs_spread_rs_panel(champ) else None
    )
    ctx: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": {},
        "spread_rs_panel": spread_rs_panel,
    }

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS dates after is_end={is_end}")

    gate_0930, meta_0930 = _ensure_gate_at_anchor(
        conn,
        trade_dates,
        gate_cache_path=gate_path,
        gate_anchor_minute="09:30",
        n_slots=n_slots,
    )

    path_0910 = Path(gate_cache_0910_path) if gate_cache_0910_path else Path(
        f"reports/research/rrg/"
        f"c18acc_avoid_mixed_gate_live_poll5m_0910_{trade_dates[0]}_{trade_dates[-1]}.json"
    )

    gate_0910, meta_0910 = _ensure_gate_at_anchor(
        conn,
        trade_dates,
        gate_cache_path=path_0910,
        gate_anchor_minute="09:10",
        n_slots=n_slots,
    )

    cfg_0930 = replace(
        champ,
        variant_id="NTB_0930",
        label="no_trade_before=09:30",
        no_trade_before="09:30",
    )
    cfg_0910 = replace(
        champ,
        variant_id="NTB_0910",
        label="no_trade_before=09:10",
        no_trade_before="09:10",
    )

    p0930, s0930, _ = _run_sim(
        conn,
        ctx=ctx,
        trade_dates=trade_dates,
        cfg=cfg_0930,
        confirm_bars=confirm_bars,
        gate=gate_0930,
        label="NTB_0930 · ≥09:30",
    )
    p0910, s0910, _ = _run_sim(
        conn,
        ctx=ctx,
        trade_dates=trade_dates,
        cfg=cfg_0910,
        confirm_bars=confirm_bars,
        gate=gate_0910,
        label="NTB_0910 · ≥09:10",
    )

    print("  post-hoc PRIOR_1300 …", flush=True)
    prior_periods, prior_meta = apply_prior_day_1300_fills(
        conn,
        p0930,
        full_dates=full_dates,
        kbar_cache=ctx["kbar_cache"],
    )
    print(
        f"    filled={prior_meta['n_filled']} / {prior_meta['n_baseline']} "
        f"skip_no_px={prior_meta['n_skipped_no_px']}",
        flush=True,
    )

    base_row = _variant_row(
        variant_id="NTB_0930",
        label="現行 · ≥09:30 開窗",
        periods=p0930,
        summary=s0930,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
    )
    base_full = base_row["slices"]["full"]
    base_is = base_row["slices"]["is"]
    base_oos = base_row["slices"]["oos"]

    variants = [
        base_row,
        _variant_row(
            variant_id="NTB_0910",
            label="提前開窗 · ≥09:10",
            periods=p0910,
            summary=s0910,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
        ),
        _variant_row(
            variant_id="PRIOR_1300",
            label="隔夜提前 · 前一交易日 13:00 成交（post-hoc）",
            periods=prior_periods,
            summary=None,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
            extra={"prior_fill_meta": prior_meta},
        ),
    ]

    verdict = _verdict(variants)
    return {
        "schema": "c18acc_open_timing_study-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "candidate_pool": "fresh",
        "frozen": {
            "avoid_spread_mixed": True,
            "entry_prior_ret_max": champ.entry_prior_ret_max,
            "entry_prior_ret_days": champ.entry_prior_ret_days,
            "exit": "S2",
        },
        "gate_meta_0930": meta_0930,
        "gate_meta_0910": meta_0910,
        "gate_cache_0930": str(gate_path) if gate_path else None,
        "gate_cache_0910": str(path_0910) if path_0910 else None,
        "variants": variants,
        "verdict": verdict,
    }


def render_open_timing_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · 開窗時點三變體",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        "Frozen：`fresh` · `confirm_bars=2` · `avoid_spread_mixed` · `G_R5_12` · S2 · 3 槽",
        "",
        "## 變體定義",
        "",
        "| variant | 意思 |",
        "|---------|------|",
        "| NTB_0930 | 現行 · ≥09:30 可進／換倉買（slot re-sim） |",
        "| NTB_0910 | ≥09:10 開窗（slot re-sim · spread gate 錨點 09:10） |",
        "| PRIOR_1300 | 隔夜提前：NTB_0930 腿改在**前一交易日 13:00**成交（post-hoc · 出場不變） |",
        "",
        "> PRIOR_1300 對齊 live「昨收訊號 → 隔日早上進場」的反事實："
        "同一批選中標的，改在前一交易日下午成交；**不做** slot 重跑。",
        "",
        "## 結果 × 窗口",
        "",
        "| variant | window | n | mean_ret% | mean_excess% | win% | Δ vs NTB_0930 excess |",
        "|---------|--------|--:|----------:|-------------:|-----:|---------------------:|",
    ]
    for v in payload.get("variants") or []:
        vid = v.get("variant_id")
        for win, key in (("FULL", "full"), ("IS", "is"), ("OOS", "oos")):
            s = (v.get("slices") or {}).get(key) or {}
            if win == "FULL":
                d = v.get("delta_full_excess_pp")
            elif win == "IS":
                d = v.get("delta_is_excess_pp")
            else:
                d = v.get("delta_oos_excess_pp")
            d_s = "—" if d is None else f"{d:+.4f}pp"
            lines.append(
                f"| {vid} | {win} | {s.get('n_legs')} | {s.get('mean_return_pct')} | "
                f"{s.get('mean_excess_pct')} | {s.get('win_rate_gross_pct')} | {d_s} |"
            )

    lines.extend(["", "## Entry minute 分布", ""])
    for v in payload.get("variants") or []:
        hist = v.get("entry_minute_hist") or {}
        top = ", ".join(f"{m}:{n}" for m, n in list(hist.items())[:8]) or "—"
        lines.append(f"- **{v.get('variant_id')}**：{top}")

    prior = next(
        (v for v in (payload.get("variants") or []) if v.get("variant_id") == "PRIOR_1300"),
        None,
    )
    if prior and prior.get("prior_fill_meta"):
        m = prior["prior_fill_meta"]
        lines.extend(
            [
                "",
                "## PRIOR_1300 fill meta",
                "",
                f"- baseline legs：**{m.get('n_baseline')}** · filled：**{m.get('n_filled')}**",
                f"- skip no_px：**{m.get('n_skipped_no_px')}** · "
                f"no_prior：**{m.get('n_skipped_no_prior')}** · "
                f"bad_window：**{m.get('n_skipped_bad_window')}**",
            ]
        )

    v = payload.get("verdict") or {}
    lines.extend(
        [
            "",
            "## 採納建議（研究門檻 · 非自動寫入 order）",
            "",
            f"- 門檻：OOS n≥{ADOPT_OOS_MIN_N} · Δexcess≥{ADOPT_OOS_MIN_DELTA_PP}pp · "
            f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp",
            f"- **{v.get('summary')}**",
            "",
            "模組：`src/research/backtest/c18acc_open_timing_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
