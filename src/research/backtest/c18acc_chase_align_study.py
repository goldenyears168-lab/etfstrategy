"""C18acc · chase/extension entry gate + confirm/S2 alignment slot re-sim.

Frozen baseline: fresh · confirm_bars=2 · avoid_spread_mixed · S2 · n_slots=3.

Research questions (backtest only · not Fubon holdings):
  H-CHASE-*: block high W20 RV / prior 3–5d rally / open gap-down before entry.
  H-ALIGN-C: confirm=1 vs confirm=2 on the same live-aligned stack.
  H-ALIGN-S: hard stop / loss-waiver / S2 without min_hold — can deep losers exit earlier?
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel

ADOPT_OOS_MIN_N = 8
ADOPT_OOS_MIN_DELTA_PP = 0.5
ADOPT_IS_MIN_DELTA_PP = -0.3
DEFAULT_GATE_CACHE = (
    "reports/research/rrg/20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)


@dataclass(frozen=True)
class ChaseGateSpec:
    variant_id: str
    label: str
    pred: Callable[[dict[str, float | None]], bool]  # True → block (chase)


def _entry_c_config(confirm_bars: int):
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


def _prior_return(close: pd.DataFrame, sid: str, dates: list[str], i: int, n: int) -> float | None:
    """Return over n trading days ending at dates[i-1] (PIT before entry day dates[i])."""
    if i < n + 1 or sid not in close.columns:
        return None
    end_d = dates[i - 1]
    start_d = dates[i - 1 - n]
    try:
        c1 = float(close.at[end_d, sid])
        c0 = float(close.at[start_d, sid])
    except (KeyError, TypeError, ValueError):
        return None
    if not (c0 > 0 and c1 == c1 and c0 == c0):
        return None
    return c1 / c0 - 1.0


def _w20_rv(rs_ratio: pd.DataFrame, sid: str, signal_date: str) -> float | None:
    if sid not in rs_ratio.columns or signal_date not in rs_ratio.index:
        return None
    v = rs_ratio.at[signal_date, sid]
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _open_gap(open_px: pd.DataFrame, close: pd.DataFrame, sid: str, dates: list[str], i: int) -> float | None:
    if i < 1 or sid not in open_px.columns or sid not in close.columns:
        return None
    d, prev = dates[i], dates[i - 1]
    try:
        o = float(open_px.at[d, sid])
        c = float(close.at[prev, sid])
    except (KeyError, TypeError, ValueError):
        return None
    if not (c > 0 and o == o and c == c):
        return None
    return o / c - 1.0


def _load_open_panel(conn: sqlite3.Connection, close: pd.DataFrame) -> pd.DataFrame:
    """Daily open aligned to close panel index/columns (best-effort)."""
    dates = close.index.astype(str).tolist()
    sids = [str(c) for c in close.columns]
    q = """
      SELECT stock_id, trade_date, open FROM stock_daily_bars
      WHERE trade_date >= ? AND trade_date <= ?
    """
    rows = conn.execute(q, (dates[0], dates[-1])).fetchall()
    if not rows:
        return pd.DataFrame(index=close.index, columns=close.columns, dtype=float)
    by: dict[str, dict[str, float]] = {}
    for sid, d, o in rows:
        if o is None:
            continue
        by.setdefault(str(sid), {})[str(d)] = float(o)
    data = {sid: [by.get(sid, {}).get(d) for d in dates] for sid in sids}
    return pd.DataFrame(data, index=close.index)


def chase_gate_specs() -> list[ChaseGateSpec]:
    return [
        ChaseGateSpec("G_RV105", "block W20 RV>105", lambda f: (f.get("w20_rv") or 0) > 105.0),
        ChaseGateSpec("G_RV108", "block W20 RV>108", lambda f: (f.get("w20_rv") or 0) > 108.0),
        ChaseGateSpec("G_R3_08", "block prior3d >8%", lambda f: (f.get("ret3") or 0) > 0.08),
        ChaseGateSpec("G_R3_10", "block prior3d >10%", lambda f: (f.get("ret3") or 0) > 0.10),
        ChaseGateSpec("G_R5_12", "block prior5d >12%", lambda f: (f.get("ret5") or 0) > 0.12),
        ChaseGateSpec("G_R5_15", "block prior5d >15%", lambda f: (f.get("ret5") or 0) > 0.15),
        ChaseGateSpec("G_GAP3", "block open gap ≤−3%", lambda f: (f.get("gap") or 0) <= -0.03),
        ChaseGateSpec("G_GAP5", "block open gap ≤−5%", lambda f: (f.get("gap") or 0) <= -0.05),
        ChaseGateSpec(
            "G_COMBO",
            "block RV>105 ∨ R5>12% ∨ gap≤−3%",
            lambda f: (f.get("w20_rv") or 0) > 105.0
            or (f.get("ret5") or 0) > 0.12
            or (f.get("gap") or 0) <= -0.03,
        ),
        ChaseGateSpec(
            "G_COMBO_TIGHT",
            "block RV>108 ∨ R5>15% ∨ gap≤−5%",
            lambda f: (f.get("w20_rv") or 0) > 108.0
            or (f.get("ret5") or 0) > 0.15
            or (f.get("gap") or 0) <= -0.05,
        ),
    ]


def build_chase_features_for_day(
    *,
    close: pd.DataFrame,
    open_px: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    dates: list[str],
    as_of: str,
    stock_ids: set[str],
) -> dict[str, dict[str, float | None]]:
    i = dates.index(as_of)
    signal = dates[i - 1] if i >= 1 else None
    out: dict[str, dict[str, float | None]] = {}
    for sid in stock_ids:
        out[sid] = {
            "w20_rv": _w20_rv(rs_ratio, sid, signal) if signal else None,
            "ret3": _prior_return(close, sid, dates, i, 3),
            "ret5": _prior_return(close, sid, dates, i, 5),
            "gap": _open_gap(open_px, close, sid, dates, i),
        }
    return out


def apply_chase_to_whitelist(
    base_gate: dict[str, set[str]],
    *,
    close: pd.DataFrame,
    open_px: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    trade_dates: list[str],
    full_dates: list[str],
    spec: ChaseGateSpec,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    blocked_n = 0
    cand_n = 0
    out: dict[str, set[str]] = {}
    for d in trade_dates:
        allowed = set(base_gate.get(d) or set())
        cand_n += len(allowed)
        if not allowed or d not in full_dates:
            out[d] = allowed
            continue
        feats = build_chase_features_for_day(
            close=close,
            open_px=open_px,
            rs_ratio=rs_ratio,
            dates=full_dates,
            as_of=d,
            stock_ids=allowed,
        )
        keep: set[str] = set()
        for sid in allowed:
            if spec.pred(feats.get(sid) or {}):
                blocked_n += 1
                continue
            keep.add(sid)
        out[d] = keep
    meta = {
        "variant_id": spec.variant_id,
        "label": spec.label,
        "candidate_stock_days": cand_n,
        "blocked_stock_days": blocked_n,
        "block_rate_pct": round(100.0 * blocked_n / max(cand_n, 1), 2),
    }
    return out, meta


def _profile_legs_chase(
    periods: list[dict[str, Any]],
    *,
    close: pd.DataFrame,
    open_px: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    full_dates: list[str],
) -> dict[str, Any]:
    """Post-hoc: chase-flag lift on baseline executed legs."""
    date_set = set(full_dates)
    rows: list[dict[str, Any]] = []
    for p in periods:
        ed = str(p.get("entry_date") or "")
        sid = str(p.get("stock_id") or "")
        if ed not in date_set or not sid:
            continue
        feats = build_chase_features_for_day(
            close=close,
            open_px=open_px,
            rs_ratio=rs_ratio,
            dates=full_dates,
            as_of=ed,
            stock_ids={sid},
        )[sid]
        ret = float(p.get("return_pct") or 0.0)
        rows.append(
            {
                "stock_id": sid,
                "entry_date": ed,
                "return_pct": ret,
                "excess_pct": (
                    float(p["return_pct"]) - float(p["bench_return_pct"])
                    if p.get("return_pct") is not None and p.get("bench_return_pct") is not None
                    else None
                ),
                **feats,
                "flag_rv105": bool((feats.get("w20_rv") or 0) > 105),
                "flag_r5_12": bool((feats.get("ret5") or 0) > 0.12),
                "flag_gap3": bool((feats.get("gap") or 0) <= -0.03),
                "flag_combo": bool(
                    (feats.get("w20_rv") or 0) > 105
                    or (feats.get("ret5") or 0) > 0.12
                    or (feats.get("gap") or 0) <= -0.03
                ),
            }
        )

    def _cohort(mask_key: str) -> dict[str, Any]:
        pos = [r for r in rows if r.get(mask_key)]
        neg = [r for r in rows if not r.get(mask_key)]
        def _m(xs: list[dict[str, Any]], key: str) -> float | None:
            vs = [float(x[key]) for x in xs if x.get(key) is not None]
            return round(sum(vs) / len(vs), 4) if vs else None
        return {
            "n_flag": len(pos),
            "n_clear": len(neg),
            "mean_ret_flag": _m(pos, "return_pct"),
            "mean_ret_clear": _m(neg, "return_pct"),
            "mean_excess_flag": _m(pos, "excess_pct"),
            "mean_excess_clear": _m(neg, "excess_pct"),
            "win_flag": round(100.0 * sum(1 for r in pos if r["return_pct"] > 0) / max(len(pos), 1), 2)
            if pos
            else None,
            "win_clear": round(100.0 * sum(1 for r in neg if r["return_pct"] > 0) / max(len(neg), 1), 2)
            if neg
            else None,
        }

    return {
        "n_legs": len(rows),
        "by_flag": {
            "rv105": _cohort("flag_rv105"),
            "r5_12": _cohort("flag_r5_12"),
            "gap3": _cohort("flag_gap3"),
            "combo": _cohort("flag_combo"),
        },
    }


def _exit_configs() -> list[ScoreSwapCConfig]:
    base = replace(champion_score_swap_c_config(), candidate_pool="fresh", n_slots=3)
    return [
        replace(base, variant_id="HS8", label="S2 + close hard stop −8%", hard_stop_loss_pct=-0.08),
        replace(base, variant_id="HS10", label="S2 + close hard stop −10%", hard_stop_loss_pct=-0.10),
        replace(
            base,
            variant_id="S2_NO_MINHOLD",
            label="S2 weakening 不需 min_hold",
            quad_force_exit_requires_min_hold=False,
        ),
        replace(
            base,
            variant_id="LW5",
            label="loss_waiver −5% → min_hold=2",
            loss_waiver_threshold=-0.05,
            loss_waiver_min_hold=2,
        ),
    ]


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def _adopt_row(
    *,
    variant_id: str,
    full: dict[str, Any],
    is_s: dict[str, Any],
    oos: dict[str, Any],
    baseline_full: dict[str, Any],
    baseline_is: dict[str, Any],
    baseline_oos: dict[str, Any],
) -> dict[str, Any]:
    d_oos = _delta(oos.get("mean_excess_pct"), baseline_oos.get("mean_excess_pct"))
    d_is = _delta(is_s.get("mean_excess_pct"), baseline_is.get("mean_excess_pct"))
    d_full = _delta(full.get("mean_excess_pct"), baseline_full.get("mean_excess_pct"))
    n_oos = int(oos.get("n_legs") or 0)
    adopt = bool(
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and d_oos >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and d_is >= ADOPT_IS_MIN_DELTA_PP
    )
    return {
        "variant_id": variant_id,
        "n_full": full.get("n_legs"),
        "n_oos": n_oos,
        "mean_excess_full": full.get("mean_excess_pct"),
        "mean_excess_oos": oos.get("mean_excess_pct"),
        "mean_excess_is": is_s.get("mean_excess_pct"),
        "delta_full_pp": d_full,
        "delta_oos_pp": d_oos,
        "delta_is_pp": d_is,
        "win_rate_full": full.get("win_rate_gross_pct"),
        "adopt": adopt,
    }


def _run_one(
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


def run_chase_align_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    chase_specs: list[ChaseGateSpec] | None = None,
    run_exit_variants: bool = True,
    run_confirm_variant: bool = True,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = replace(champion_score_swap_c_config(), candidate_pool="fresh", n_slots=3)
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench) if _poll5m_needs_spread_rs_panel(champ) else None
    )
    ctx = {
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
    open_px = _load_open_panel(conn, close)

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file():
        base_gate, gate_meta, c0, c1 = load_gate_cache(str(gate_path))
        if c0 != trade_dates[0] or c1 != trade_dates[-1]:
            raise ValueError(f"gate cache {c0}..{c1} != {trade_dates[0]}..{trade_dates[-1]}")
    else:
        base_gate, gate_meta = _ensure_avoid_mixed_gate(
            conn,
            trade_dates,
            gate_cache_path=gate_path,
            live_aligned=True,
            n_slots=3,
        )
        if base_gate is None:
            raise ValueError("avoid_mixed gate required for live-aligned baseline")

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS dates after is_end={is_end}")

    # --- Baseline B0 ---
    b_periods, b_summary, b_stats = _run_one(
        conn,
        ctx=ctx,
        trade_dates=trade_dates,
        cfg=champ,
        confirm_bars=2,
        gate=base_gate,
        label="B0 · fresh · confirm=2 · avoid_mixed · S2",
    )
    b_is = _period_stats(_slice_periods(b_periods, start=date_start, end=is_end))
    b_oos = _period_stats(_slice_periods(b_periods, start=oos_start, end=end))
    profile = _profile_legs_chase(
        b_periods, close=close, open_px=open_px, rs_ratio=rs_ratio, full_dates=full_dates
    )

    variants: list[dict[str, Any]] = []
    adopt_rows: list[dict[str, Any]] = []

    def _record(
        vid: str,
        label: str,
        periods: list[dict[str, Any]],
        summary: dict[str, Any],
        stats: dict[str, Any],
        *,
        family: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        is_s = _period_stats(_slice_periods(periods, start=date_start, end=is_end))
        oos = _period_stats(_slice_periods(periods, start=oos_start, end=end))
        row = {
            "variant_id": vid,
            "label": label,
            "family": family,
            "stats_full": stats,
            "stats_is": is_s,
            "stats_oos": oos,
            "summary": {k: summary.get(k) for k in ("swaps_total", "force_exits", "n_periods")},
        }
        if extra:
            row.update(extra)
        variants.append(row)
        adopt_rows.append(
            _adopt_row(
                variant_id=vid,
                full=stats,
                is_s=is_s,
                oos=oos,
                baseline_full=b_stats,
                baseline_is=b_is,
                baseline_oos=b_oos,
            )
        )

    _record("B0", "baseline live-aligned", b_periods, b_summary, b_stats, family="baseline")

    specs = chase_specs or chase_gate_specs()
    for spec in specs:
        gated, meta = apply_chase_to_whitelist(
            base_gate,
            close=close,
            open_px=open_px,
            rs_ratio=rs_ratio,
            trade_dates=trade_dates,
            full_dates=full_dates,
            spec=spec,
        )
        periods, summary, stats = _run_one(
            conn,
            ctx=ctx,
            trade_dates=trade_dates,
            cfg=champ,
            confirm_bars=2,
            gate=gated,
            label=f"{spec.variant_id} · {spec.label}",
        )
        _record(
            spec.variant_id,
            spec.label,
            periods,
            summary,
            stats,
            family="chase_gate",
            extra={"gate_meta": meta},
        )

    if run_confirm_variant:
        periods, summary, stats = _run_one(
            conn,
            ctx=ctx,
            trade_dates=trade_dates,
            cfg=champ,
            confirm_bars=1,
            gate=base_gate,
            label="C1 · confirm=1 (misaligned live)",
        )
        _record("C1", "confirm_bars=1", periods, summary, stats, family="confirm")

    if run_exit_variants:
        for cfg in _exit_configs():
            periods, summary, stats = _run_one(
                conn,
                ctx=ctx,
                trade_dates=trade_dates,
                cfg=cfg,
                confirm_bars=2,
                gate=base_gate,
                label=f"{cfg.variant_id} · {cfg.label}",
            )
            hard_n = sum(
                1
                for p in periods
                if str(p.get("exit_reason") or "") in ("hard_stop", "hard_stop_intraday")
            )
            _record(
                str(cfg.variant_id),
                str(cfg.label),
                periods,
                summary,
                stats,
                family="exit",
                extra={"hard_stop_exits": hard_n},
            )

    adoptable = [r for r in adopt_rows if r["variant_id"] != "B0" and r.get("adopt")]
    # confirm=2 preferred operationally even if alpha flat
    c1 = next((r for r in adopt_rows if r["variant_id"] == "C1"), None)
    confirm_note = None
    if c1 and c1.get("delta_oos_pp") is not None:
        confirm_note = (
            "confirm=2 preferred for live anti-flicker"
            if float(c1["delta_oos_pp"]) <= 0.5
            else "confirm=1 OOS higher — revisit"
        )

    verdict = {
        "baseline": {
            "n": b_stats.get("n_legs"),
            "mean_excess_full": b_stats.get("mean_excess_pct"),
            "mean_excess_oos": b_oos.get("mean_excess_pct"),
            "mean_excess_is": b_is.get("mean_excess_pct"),
        },
        "adoptable": [r["variant_id"] for r in adoptable],
        "best_chase_oos": None,
        "confirm_note": confirm_note,
        "pool_override_note": (
            "pool_override 無法在回測公正模擬 · 規格上禁止 · 視為 live 對齊硬規則"
        ),
        "summary": "",
    }
    chase_rows = [r for r in adopt_rows if r["variant_id"].startswith("G_")]
    if chase_rows:
        best = max(
            chase_rows,
            key=lambda r: (r.get("delta_oos_pp") is not None, r.get("delta_oos_pp") or -999),
        )
        verdict["best_chase_oos"] = {
            "variant_id": best["variant_id"],
            "delta_oos_pp": best.get("delta_oos_pp"),
            "adopt": best.get("adopt"),
        }
    verdict["summary"] = (
        f"B0 n={b_stats.get('n_legs')} OOS excess={b_oos.get('mean_excess_pct')}% · "
        f"adoptable={verdict['adoptable'] or ['none']} · "
        f"best_chase={verdict['best_chase_oos']}"
    )

    return {
        "schema": "c18acc_chase_align_study-v1",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "frozen_baseline": {
            "candidate_pool": "fresh",
            "confirm_bars": 2,
            "avoid_spread_mixed": True,
            "n_slots": 3,
            "exit": "S2",
        },
        "gate_meta": gate_meta,
        "posthoc_chase_profile": profile,
        "variants": variants,
        "adoption_table": adopt_rows,
        "verdict": verdict,
        "thresholds": {
            "oos_min_n": ADOPT_OOS_MIN_N,
            "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
            "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
        },
    }


def render_chase_align_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc · 追高／延伸 gate + confirm／S2 對齊研究",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"IS ≤ **{payload['is_end']}** · OOS ≥ **{payload['oos_start']}**",
        "",
        "Frozen baseline：`fresh` · `confirm_bars=2` · `avoid_spread_mixed` · S2 · 3 槽",
        "",
        "## 採納門檻",
        "",
        f"- OOS n≥{payload['thresholds']['oos_min_n']} · "
        f"Δexcess≥{payload['thresholds']['oos_min_delta_pp']}pp · "
        f"IS Δ≥{payload['thresholds']['is_min_delta_pp']}pp",
        "",
        "## Baseline",
        "",
    ]
    v = payload.get("verdict") or {}
    b = v.get("baseline") or {}
    lines += [
        f"- n={b.get('n')} · FULL excess **{b.get('mean_excess_full')}%** · "
        f"IS **{b.get('mean_excess_is')}%** · OOS **{b.get('mean_excess_oos')}%**",
        "",
        "## Post-hoc · 追高旗標（baseline 腿）",
        "",
        "| flag | n_flag | mean_ret flag | mean_ret clear | win flag | win clear |",
        "|------|-------:|--------------:|---------------:|---------:|----------:|",
    ]
    by = ((payload.get("posthoc_chase_profile") or {}).get("by_flag") or {})
    for key in ("rv105", "r5_12", "gap3", "combo"):
        c = by.get(key) or {}
        lines.append(
            f"| {key} | {c.get('n_flag')} | {c.get('mean_ret_flag')} | "
            f"{c.get('mean_ret_clear')} | {c.get('win_flag')} | {c.get('win_clear')} |"
        )

    lines += [
        "",
        "## Slot re-sim · 採納表",
        "",
        "| variant | family | n | OOS excess | ΔOOS | ΔIS | ΔFULL | adopt |",
        "|---------|--------|--:|-----------:|-----:|----:|------:|:-----:|",
    ]
    fam = {r["variant_id"]: r.get("family") for r in payload.get("variants") or []}
    for r in payload.get("adoption_table") or []:
        lines.append(
            f"| {r.get('variant_id')} | {fam.get(r.get('variant_id'), '')} | "
            f"{r.get('n_full')} | {r.get('mean_excess_oos')} | {r.get('delta_oos_pp')} | "
            f"{r.get('delta_is_pp')} | {r.get('delta_full_pp')} | "
            f"{'✓' if r.get('adopt') else '—'} |"
        )

    lines += [
        "",
        "## 判決",
        "",
        f"- **可採納變體**：{', '.join(v.get('adoptable') or []) or '無'}",
        f"- **最佳 chase（OOS Δ）**：{v.get('best_chase_oos')}",
        f"- **confirm**：{v.get('confirm_note')}",
        f"- **pool_override**：{v.get('pool_override_note')}",
        f"- 摘要：{v.get('summary')}",
        "",
        "模組：`src/research/backtest/c18acc_chase_align_study.py`",
        "",
    ]
    return "\n".join(lines)


# ── G3 breadth + G_R5_12 deep dive ──────────────────────────────────────────

R5_THRESHOLDS = (0.08, 0.10, 0.12, 0.14, 0.15, 0.18)


def _attach_breadth_zone(
    periods: list[dict[str, Any]],
    zone_by_date: dict[str, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in periods:
        q = dict(p)
        if not q.get("breadth_zone_200"):
            sig = str(q.get("signal_date") or q.get("entry_date") or "")
            q["breadth_zone_200"] = zone_by_date.get(sig, "unknown")
        out.append(q)
    return out


def _blocked_leg_autopsy(
    baseline: list[dict[str, Any]],
    gated: list[dict[str, Any]],
) -> dict[str, Any]:
    """Legs present in baseline but absent in gated · what did we skip?"""
    b_keys = {(str(p["stock_id"]), str(p["entry_date"])): p for p in baseline}
    g_keys = {(str(p["stock_id"]), str(p["entry_date"])) for p in gated}
    blocked = [b_keys[k] for k in b_keys if k not in g_keys]
    added = [
        p
        for p in gated
        if (str(p["stock_id"]), str(p["entry_date"])) not in b_keys
    ]
    def _mean_ret(xs: list[dict[str, Any]]) -> float | None:
        vs = [float(x["return_pct"]) for x in xs if x.get("return_pct") is not None]
        return round(sum(vs) / len(vs), 4) if vs else None

    samples = sorted(
        blocked,
        key=lambda p: float(p.get("return_pct") or 0),
    )
    return {
        "n_blocked": len(blocked),
        "n_added_replacement": len(added),
        "mean_ret_blocked": _mean_ret(blocked),
        "mean_ret_added": _mean_ret(added),
        "win_rate_blocked": round(
            100.0 * sum(1 for p in blocked if float(p.get("return_pct") or 0) > 0) / max(len(blocked), 1),
            2,
        )
        if blocked
        else None,
        "worst_blocked": [
            {
                "stock_id": p.get("stock_id"),
                "entry_date": p.get("entry_date"),
                "return_pct": p.get("return_pct"),
                "breadth_zone_200": p.get("breadth_zone_200"),
            }
            for p in samples[:8]
        ],
        "best_blocked": [
            {
                "stock_id": p.get("stock_id"),
                "entry_date": p.get("entry_date"),
                "return_pct": p.get("return_pct"),
                "breadth_zone_200": p.get("breadth_zone_200"),
            }
            for p in list(reversed(samples))[:5]
        ],
    }


def _monthly_delta(
    baseline: list[dict[str, Any]],
    gated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from collections import defaultdict

    def _by_month(periods: list[dict[str, Any]]) -> dict[str, list[float]]:
        m: dict[str, list[float]] = defaultdict(list)
        for p in periods:
            ed = str(p.get("entry_date") or "")
            if len(ed) < 7 or p.get("return_pct") is None:
                continue
            ret = float(p["return_pct"])
            bench = p.get("bench_return_pct")
            ex = ret - float(bench) if bench is not None else ret
            m[ed[:7]].append(ex)
        return m

    b, g = _by_month(baseline), _by_month(gated)
    rows = []
    for month in sorted(set(b) | set(g)):
        bv, gv = b.get(month) or [], g.get(month) or []
        mb = sum(bv) / len(bv) if bv else None
        mg = sum(gv) / len(gv) if gv else None
        rows.append(
            {
                "month": month,
                "n_b0": len(bv),
                "n_r5": len(gv),
                "mean_excess_b0": round(mb, 4) if mb is not None else None,
                "mean_excess_r5": round(mg, 4) if mg is not None else None,
                "delta_pp": round(mg - mb, 4) if mb is not None and mg is not None else None,
            }
        )
    return rows


def run_r5_12_g3_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    r5_thresholds: tuple[float, ...] = R5_THRESHOLDS,
) -> dict[str, Any]:
    """G3 breadth stratification + R5 threshold sensitivity around G_R5_12."""
    from research.backtest.c18acc_entry_rank_study import evaluate_g3_breadth

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS after is_end={is_end}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = replace(champion_score_swap_c_config(), candidate_pool="fresh", n_slots=3)
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench) if _poll5m_needs_spread_rs_panel(champ) else None
    )
    ctx = {
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
    open_px = _load_open_panel(conn, close)

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file():
        base_gate, gate_meta, c0, c1 = load_gate_cache(str(gate_path))
        if c0 != trade_dates[0] or c1 != trade_dates[-1]:
            raise ValueError(f"gate cache {c0}..{c1} != {trade_dates[0]}..{trade_dates[-1]}")
    else:
        base_gate, gate_meta = _ensure_avoid_mixed_gate(
            conn, trade_dates, gate_cache_path=gate_path, live_aligned=True, n_slots=3
        )
        if base_gate is None:
            raise ValueError("avoid_mixed gate required")

    # B0
    b_periods, b_summary, b_stats = _run_one(
        conn, ctx=ctx, trade_dates=trade_dates, cfg=champ, confirm_bars=2, gate=base_gate, label="B0"
    )
    b_periods = _attach_breadth_zone(b_periods, zone_by_date)
    b_is = _period_stats(_slice_periods(b_periods, start=date_start, end=is_end))
    b_oos = _period_stats(_slice_periods(b_periods, start=oos_start, end=end))

    # G_R5_12 champion
    r5_12_spec = ChaseGateSpec(
        "G_R5_12", "block prior5d >12%", lambda f, thr=0.12: (f.get("ret5") or 0) > thr
    )
    gate_12, meta_12 = apply_chase_to_whitelist(
        base_gate,
        close=close,
        open_px=open_px,
        rs_ratio=rs_ratio,
        trade_dates=trade_dates,
        full_dates=full_dates,
        spec=r5_12_spec,
    )
    g_periods, g_summary, g_stats = _run_one(
        conn,
        ctx=ctx,
        trade_dates=trade_dates,
        cfg=champ,
        confirm_bars=2,
        gate=gate_12,
        label="G_R5_12",
    )
    g_periods = _attach_breadth_zone(g_periods, zone_by_date)
    g_is = _period_stats(_slice_periods(g_periods, start=date_start, end=is_end))
    g_oos = _period_stats(_slice_periods(g_periods, start=oos_start, end=end))

    g3_full = evaluate_g3_breadth(r0_periods=b_periods, r2_periods=g_periods)
    # strip nested period dumps from export later — evaluate returns r0/r2 strat only
    g3_full_export = {k: v for k, v in g3_full.items() if k not in ("r0", "r2")}
    g3_full_export["r0_by_bucket"] = (g3_full.get("r0") or {}).get("by_bucket")
    g3_full_export["r2_by_bucket"] = (g3_full.get("r2") or {}).get("by_bucket")
    g3_full_export["r0_by_zone"] = (g3_full.get("r0") or {}).get("by_zone")
    g3_full_export["r2_by_zone"] = (g3_full.get("r2") or {}).get("by_zone")

    b_oos_p = _slice_periods(b_periods, start=oos_start, end=end)
    g_oos_p = _slice_periods(g_periods, start=oos_start, end=end)
    g3_oos = evaluate_g3_breadth(r0_periods=b_oos_p, r2_periods=g_oos_p)
    g3_oos_export = {k: v for k, v in g3_oos.items() if k not in ("r0", "r2")}
    g3_oos_export["r0_by_bucket"] = (g3_oos.get("r0") or {}).get("by_bucket")
    g3_oos_export["r2_by_bucket"] = (g3_oos.get("r2") or {}).get("by_bucket")

    # Sensitivity: R5 thresholds + R5_12∩RV105
    sensitivity: list[dict[str, Any]] = []
    for thr in r5_thresholds:
        vid = f"G_R5_{int(round(thr * 100)):02d}"
        spec = ChaseGateSpec(
            vid, f"block prior5d >{thr:.0%}", lambda f, t=thr: (f.get("ret5") or 0) > t
        )
        gated, meta = apply_chase_to_whitelist(
            base_gate,
            close=close,
            open_px=open_px,
            rs_ratio=rs_ratio,
            trade_dates=trade_dates,
            full_dates=full_dates,
            spec=spec,
        )
        # reuse G_R5_12 periods if same gate
        if vid == "G_R5_12":
            periods, summary, stats = g_periods, g_summary, g_stats
            meta = meta_12
        else:
            periods, summary, stats = _run_one(
                conn,
                ctx=ctx,
                trade_dates=trade_dates,
                cfg=champ,
                confirm_bars=2,
                gate=gated,
                label=vid,
            )
        is_s = _period_stats(_slice_periods(periods, start=date_start, end=is_end))
        oos = _period_stats(_slice_periods(periods, start=oos_start, end=end))
        sensitivity.append(
            {
                **_adopt_row(
                    variant_id=vid,
                    full=stats,
                    is_s=is_s,
                    oos=oos,
                    baseline_full=b_stats,
                    baseline_is=b_is,
                    baseline_oos=b_oos,
                ),
                "threshold": thr,
                "gate_meta": meta,
                "swaps": summary.get("swaps_total"),
            }
        )

    # Combo: R5_12 AND also block RV>105
    combo_spec = ChaseGateSpec(
        "G_R5_12_RV105",
        "block R5>12% ∨ RV>105",
        lambda f: (f.get("ret5") or 0) > 0.12 or (f.get("w20_rv") or 0) > 105.0,
    )
    combo_gate, combo_meta = apply_chase_to_whitelist(
        base_gate,
        close=close,
        open_px=open_px,
        rs_ratio=rs_ratio,
        trade_dates=trade_dates,
        full_dates=full_dates,
        spec=combo_spec,
    )
    c_periods, c_summary, c_stats = _run_one(
        conn,
        ctx=ctx,
        trade_dates=trade_dates,
        cfg=champ,
        confirm_bars=2,
        gate=combo_gate,
        label="G_R5_12_RV105",
    )
    c_is = _period_stats(_slice_periods(c_periods, start=date_start, end=is_end))
    c_oos = _period_stats(_slice_periods(c_periods, start=oos_start, end=end))
    combo_row = {
        **_adopt_row(
            variant_id="G_R5_12_RV105",
            full=c_stats,
            is_s=c_is,
            oos=c_oos,
            baseline_full=b_stats,
            baseline_is=b_is,
            baseline_oos=b_oos,
        ),
        "gate_meta": combo_meta,
        "swaps": c_summary.get("swaps_total"),
    }

    autopsy = _blocked_leg_autopsy(b_periods, g_periods)
    monthly = _monthly_delta(b_periods, g_periods)

    d_oos = _delta(g_oos.get("mean_excess_pct"), b_oos.get("mean_excess_pct"))
    d_is = _delta(g_is.get("mean_excess_pct"), b_is.get("mean_excess_pct"))
    d_full = _delta(g_stats.get("mean_excess_pct"), b_stats.get("mean_excess_pct"))
    g2_pass = bool(
        int(g_oos.get("n_legs") or 0) >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and d_oos >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and d_is >= ADOPT_IS_MIN_DELTA_PP
    )
    g3_pass = bool(g3_full.get("g3_pass"))
    full_ok = d_full is not None and d_full >= -0.2
    adopt = g2_pass and g3_pass and full_ok
    if adopt:
        outcome = "GO_ADOPT_G_R5_12"
    elif g2_pass and not g3_pass:
        outcome = "NO_GO_G3"
    else:
        outcome = "NO_GO"

    decision = {
        "outcome": outcome,
        "recommend_adopt": adopt,
        "g2_pass": g2_pass,
        "g3_pass": g3_pass,
        "delta_oos_pp": d_oos,
        "delta_is_pp": d_is,
        "delta_full_pp": d_full,
        "gates": {
            "G1": "passed",
            "G2": "passed" if g2_pass else "failed",
            "G3": "passed" if g3_pass else "failed",
            "G4": "drafted",
            "G5": "pending" if adopt else "na",
            "G6": "drafted",
        },
        "summary": (
            f"{outcome} · OOS Δ={d_oos}pp · IS Δ={d_is}pp · FULL Δ={d_full}pp · "
            f"G3 {g3_full.get('summary')}"
        ),
    }

    return {
        "schema": "c18acc_r5_12_g3_study-v1",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "frozen_baseline": {
            "candidate_pool": "fresh",
            "confirm_bars": 2,
            "avoid_spread_mixed": True,
            "n_slots": 3,
        },
        "gate_meta": gate_meta,
        "b0": {"stats_full": b_stats, "stats_is": b_is, "stats_oos": b_oos},
        "g_r5_12": {
            "stats_full": g_stats,
            "stats_is": g_is,
            "stats_oos": g_oos,
            "gate_meta": meta_12,
        },
        "g3_full": g3_full_export,
        "g3_oos": g3_oos_export,
        "r5_sensitivity": sensitivity,
        "combo_r5_rv105": combo_row,
        "blocked_autopsy": autopsy,
        "monthly_delta": monthly,
        "decision": decision,
    }


def render_r5_12_g3_md(payload: dict[str, Any]) -> str:
    d = payload.get("decision") or {}
    g3 = payload.get("g3_full") or {}
    g3o = payload.get("g3_oos") or {}
    b0 = payload.get("b0") or {}
    g = payload.get("g_r5_12") or {}
    lines = [
        "# C18acc · G_R5_12 · G3 廣度分層 + 敏感度",
        "",
        f"**Outcome**：`{d.get('outcome')}` · **採納建議**：{'是' if d.get('recommend_adopt') else '否'}",
        "",
        f"{d.get('summary')}",
        "",
        f"窗口：**{payload['date_start']}** .. **{payload['date_end']}** · "
        f"IS ≤ {payload['is_end']} · OOS ≥ {payload['oos_start']}",
        "",
        "Frozen：fresh · confirm=2 · avoid_mixed · S2 · 3 槽",
        "",
        "## 1 · G2 對照（B0 vs G_R5_12）",
        "",
        "| window | B0 excess% | G_R5_12 excess% | Δ pp | n B0 | n R5 |",
        "|--------|-----------:|----------------:|-----:|-----:|-----:|",
    ]
    for win, key in (("FULL", "stats_full"), ("IS", "stats_is"), ("OOS", "stats_oos")):
        bb, gg = b0.get(key) or {}, g.get(key) or {}
        mb, mg = bb.get("mean_excess_pct"), gg.get("mean_excess_pct")
        delta = round(float(mg) - float(mb), 4) if mb is not None and mg is not None else None
        lines.append(
            f"| {win} | {mb} | {mg} | {delta} | {bb.get('n_legs')} | {gg.get('n_legs')} |"
        )

    lines += [
        "",
        f"- G2：{'passed' if d.get('g2_pass') else 'failed'} · "
        f"OOS Δ={d.get('delta_oos_pp')}pp",
        "",
        "## 2 · G3 廣度分層（FULL）",
        "",
        f"**{g3.get('summary')}**",
        "",
        "| bucket | n B0 | n R5 | mean B0% | mean R5% | Δ pp | eligible |",
        "|--------|-----:|-----:|---------:|---------:|-----:|:--------:|",
    ]
    for r in g3.get("by_bucket_delta") or []:
        lines.append(
            f"| {r.get('label') or r.get('id')} | {r.get('n_r0')} | {r.get('n_r2')} | "
            f"{r.get('mean_r0')} | {r.get('mean_r2')} | {r.get('delta_excess_pp')} | "
            f"{'✓' if r.get('eligible') else '—'} |"
        )
    lines += [
        "",
        "| zone | n B0 | n R5 | mean B0% | mean R5% | Δ pp |",
        "|------|-----:|-----:|---------:|---------:|-----:|",
    ]
    for r in g3.get("by_zone_delta") or []:
        lines.append(
            f"| {r.get('label') or r.get('id')} | {r.get('n_r0')} | {r.get('n_r2')} | "
            f"{r.get('mean_r0')} | {r.get('mean_r2')} | {r.get('delta_excess_pp')} |"
        )

    lines += [
        "",
        "## 3 · G3 OOS（參考）",
        "",
        f"**{g3o.get('summary')}**",
        "",
        "| bucket (OOS) | n B0 | n R5 | Δ pp |",
        "|--------------|-----:|-----:|-----:|",
    ]
    for r in g3o.get("by_bucket_delta") or []:
        if not r.get("eligible") and int(r.get("n_r0") or 0) == 0:
            continue
        lines.append(
            f"| {r.get('label') or r.get('id')} | {r.get('n_r0')} | {r.get('n_r2')} | "
            f"{r.get('delta_excess_pp')} |"
        )

    lines += [
        "",
        "## 4 · R5 門檻敏感度",
        "",
        "| variant | thr | n | ΔOOS | ΔIS | ΔFULL | block% | adopt |",
        "|---------|----:|--:|-----:|----:|------:|-------:|:-----:|",
    ]
    for r in payload.get("r5_sensitivity") or []:
        gm = r.get("gate_meta") or {}
        lines.append(
            f"| {r.get('variant_id')} | {r.get('threshold')} | {r.get('n_full')} | "
            f"{r.get('delta_oos_pp')} | {r.get('delta_is_pp')} | {r.get('delta_full_pp')} | "
            f"{gm.get('block_rate_pct')} | {'✓' if r.get('adopt') else '—'} |"
        )

    combo = payload.get("combo_r5_rv105") or {}
    lines += [
        "",
        "## 5 · 疊加 RV105",
        "",
        f"- **G_R5_12_RV105**：n={combo.get('n_full')} · ΔOOS={combo.get('delta_oos_pp')} · "
        f"ΔIS={combo.get('delta_is_pp')} · adopt={'✓' if combo.get('adopt') else '—'}",
        f"- block_rate={(combo.get('gate_meta') or {}).get('block_rate_pct')}%",
        "",
        "## 6 · 被擋腿剖析（B0 有、G_R5_12 無）",
        "",
    ]
    au = payload.get("blocked_autopsy") or {}
    lines += [
        f"- 擋掉 **{au.get('n_blocked')}** 筆 · 替補新進 **{au.get('n_added_replacement')}** 筆",
        f"- 被擋 mean ret **{au.get('mean_ret_blocked')}%** · 勝率 {au.get('win_rate_blocked')}%",
        f"- 替補 mean ret **{au.get('mean_ret_added')}%**",
        "",
        "最差被擋（本可不做）：",
        "",
        "| stock | entry | ret% | zone |",
        "|-------|-------|-----:|------|",
    ]
    for s in au.get("worst_blocked") or []:
        lines.append(
            f"| {s.get('stock_id')} | {s.get('entry_date')} | {s.get('return_pct')} | "
            f"{s.get('breadth_zone_200')} |"
        )
    lines += [
        "",
        "最好被擋（誤殺風險）：",
        "",
        "| stock | entry | ret% | zone |",
        "|-------|-------|-----:|------|",
    ]
    for s in au.get("best_blocked") or []:
        lines.append(
            f"| {s.get('stock_id')} | {s.get('entry_date')} | {s.get('return_pct')} | "
            f"{s.get('breadth_zone_200')} |"
        )

    lines += [
        "",
        "## 7 · 月度 Δexcess（參考）",
        "",
        "| month | n B0 | n R5 | B0% | R5% | Δ pp |",
        "|-------|-----:|-----:|----:|----:|-----:|",
    ]
    for r in payload.get("monthly_delta") or []:
        if (r.get("n_b0") or 0) + (r.get("n_r5") or 0) == 0:
            continue
        lines.append(
            f"| {r.get('month')} | {r.get('n_b0')} | {r.get('n_r5')} | "
            f"{r.get('mean_excess_b0')} | {r.get('mean_excess_r5')} | {r.get('delta_pp')} |"
        )

    gates = d.get("gates") or {}
    lines += [
        "",
        "## 8 · 判決",
        "",
        f"- G1={gates.get('G1')} · G2={gates.get('G2')} · G3={gates.get('G3')}",
        f"- **{d.get('outcome')}** · recommend_adopt={d.get('recommend_adopt')}",
        "- 若 GO：建議 `strategy.yaml` funnel 加 `entry_chase_gate: prior_5d_ret_max=0.12`",
        "",
        "模組：`src/research/backtest/c18acc_chase_align_study.py` · `run_r5_12_g3_study`",
        "",
    ]
    return "\n".join(lines)
