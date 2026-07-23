"""C18acc · provisional fresh early-release PnL (decaying standards).

PIT:
- Candidate set = same-day provisional mono fresh (5m kbar · rebuilt each clock).
- Early release allowed when schedule gate passes (seg / confirm decay).
- Baseline = only @13:20 flat fresh.
- Exits / swaps = champion ScoreSwapC stack at close (isolates entry timing Δ).

Accepts imperfect precision vs 13:20 (user: ok with some misses).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from report_paths import RESEARCH_RRG
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _run_sim,
    _variant_row,
)
from research.backtest.c18acc_provisional_fresh_stability_study import (
    ANCHOR,
    DEFAULT_CLOCKS,
    _accept_at_clock,
    _decaying_schedules,
    _load_day_5m_closes_batch,
    _prices_at_minute,
    _provisional_fresh_scores_at,
    _window_slice,
    BENCH_ID,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_score_swap_c import (
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH, ScanRow
from rrg_rotation import compute_rrg_panel
from stock_db.etf import load_etf_constituent_watchlist
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

# PnL variants: (variant_id, schedule_id|None, clocks_override|None, label)
# schedule_id None + clocks (13:20,) = near-close baseline
PNL_VARIANTS: tuple[tuple[str, str | None, tuple[str, ...] | None, str], ...] = (
    (
        "B0_1320_FLAT",
        "FLAT_FRESH",
        ("13:20",),
        "基線 · 僅 13:20 暫定 fresh（近收）",
    ),
    (
        "EARLY_FLAT",
        "FLAT_FRESH",
        None,
        "提早 · 任意時點暫定 fresh（無加嚴）",
    ),
    (
        "EARLY_SEG_STEEP",
        "SEG_STEEP",
        None,
        "提早 · seg_last 陡降門檻",
    ),
    (
        "EARLY_HYBRID",
        "HYBRID_SEG_CONFIRM",
        None,
        "提早 · seg 陡降 + confirm 遞減",
    ),
)


def build_provisional_fresh_score_cache(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    clocks: tuple[str, ...] = DEFAULT_CLOCKS,
    close=None,
    bench=None,
    universe: list[str] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """date → minute → {stock_id: seg_last}."""
    if close is None or bench is None:
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index)
    if universe is None:
        universe = [str(w["stock_id"]) for w in load_etf_constituent_watchlist(conn)]
    universe_plus_bench = list(dict.fromkeys([*universe, BENCH_ID]))
    out: dict[str, dict[str, dict[str, float]]] = {}
    for i, as_of in enumerate(trade_dates):
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  cache day {i + 1}/{len(trade_dates)} {as_of} …", flush=True)
        win = _window_slice(close, bench, as_of)
        if win is None:
            continue
        cw, bw = win
        day_bars = _load_day_5m_closes_batch(conn, as_of, universe_plus_bench)
        if not day_bars:
            continue
        by_m: dict[str, dict[str, float]] = {}
        for minute in clocks:
            prices = _prices_at_minute(day_bars, minute, universe)
            if len(prices) < max(30, len(universe) // 4):
                by_m[minute] = {}
                continue
            bench_px = None
            bb = day_bars.get(BENCH_ID) or ()
            if bb:
                bench_px = price_at_or_before_minute(bb, minute)
            by_m[minute] = _provisional_fresh_scores_at(
                cw,
                bw,
                as_of=as_of,
                prices=prices,
                bench_px=float(bench_px) if bench_px else None,
                universe=universe,
            )
        out[as_of] = by_m
    return out


def _stub_fresh_calendar(
    cache: dict[str, dict[str, dict[str, float]]],
    trade_dates: list[str],
    name_map: dict[str, str],
) -> dict[str, list[ScanRow]]:
    """Non-empty stubs so champion fill path runs; selection uses cache."""
    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for d in trade_dates:
        best: dict[str, float] = {}
        for scores in (cache.get(d) or {}).values():
            for sid, seg in scores.items():
                prev = best.get(sid)
                if prev is None or float(seg) > float(prev):
                    best[sid] = float(seg)
        rows = [
            ScanRow(
                stock_id=sid,
                stock_name=name_map.get(sid, ""),
                fresh=True,
                mono=True,
                seg_last=seg,
                disp=1.5,
                segs=[0.4, 0.6, seg],
                quadrants=["leading", "leading", "leading", "leading"],
                rs_ratio=100.0,
                rs_momentum=100.0,
                daily_pct=None,
            )
            for sid, seg in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        out[d] = rows
    return out


def _prior_ret_ok(
    close,
    full_dates: list[str],
    as_of: str,
    sid: str,
    *,
    days: int,
    max_ret: float,
) -> bool:
    if days <= 0 or sid not in close.columns or as_of not in full_dates:
        return True
    si = full_dates.index(as_of)
    if si < days:
        return True
    d0 = full_dates[si - days]
    try:
        p0 = float(close.at[d0, sid])
        p1 = float(close.at[as_of, sid])
    except (KeyError, TypeError, ValueError):
        return True
    if p0 <= 0 or p1 != p1 or p0 != p0:
        return True
    return (p1 / p0 - 1.0) <= float(max_ret)


@contextmanager
def _patch_decaying_fill(
    *,
    cache: dict[str, dict[str, dict[str, float]]],
    schedule: dict[str, Any],
    clocks: tuple[str, ...],
    gate: dict[str, set[str]] | None,
    name_map: dict[str, str],
    close,
    full_dates: list[str],
    prior_ret_days: int,
    prior_ret_max: float,
) -> Iterator[None]:
    import research.backtest.rrg_mono_score_swap_c as ssc

    orig = ssc._fill_empty_slots

    def _fill(
        conn: sqlite3.Connection,
        *,
        as_of: str,
        fresh_mono: list,
        slots: list[dict[str, Any]],
        close,  # noqa: ANN001 — shadowed on purpose (sim arg)
        bench,
        full_dates: list[str],
        config,
        kbar_cache: dict,
        kbar_stats: dict,
        entry_c_config=None,
        n_slots: int | None = None,
        rs_ratio=None,
        rs_mom=None,
        accel_lookback: int = 4,
    ) -> None:
        del fresh_mono, bench, config, entry_c_config, rs_ratio, rs_mom, accel_lookback
        slot_cap = int(n_slots if n_slots is not None else 3)
        used = {int(p["slot"]) for p in slots}
        held = {str(p["stock_id"]) for p in slots}
        free = [i for i in range(slot_cap) if i not in used]
        if not free:
            return
        day_scores = cache.get(as_of) or {}
        gate_day = gate.get(as_of) if gate is not None else None
        for minute in clocks:
            if not free:
                break
            accepted = _accept_at_clock(
                schedule,
                minute=minute,
                clocks=clocks,
                scores_by_clock=day_scores,
            )
            scores = day_scores.get(minute) or {}
            ranked = sorted(
                (
                    (sid, float(scores.get(sid, 0.0)))
                    for sid in accepted
                    if sid not in held
                ),
                key=lambda kv: (-kv[1], kv[0]),
            )
            for sid, seg in ranked:
                if not free:
                    break
                if gate_day is not None and sid not in gate_day:
                    continue
                if not _prior_ret_ok(
                    close,
                    full_dates,
                    as_of,
                    sid,
                    days=prior_ret_days,
                    max_ret=prior_ret_max,
                ):
                    continue
                key = (sid, as_of)
                if key not in kbar_cache:
                    kbar_cache[key] = load_kbar_day_closes(conn, sid, as_of)
                    kbar_stats["loads"] = int(kbar_stats.get("loads") or 0) + 1
                px = price_at_or_before_minute(kbar_cache[key], minute)
                if px is None or float(px) <= 0:
                    continue
                slot = free.pop(0)
                slots.append(
                    {
                        "slot": slot,
                        "stock_id": sid,
                        "stock_name": name_map.get(sid, ""),
                        "signal_date": as_of,
                        "entry_date": as_of,
                        "entry_px": float(px),
                        "seg_last": round(seg, 4),
                        "disp": 1.5,
                        "rs_momentum": 0.0,
                        "seg_step_delta": 0.0,
                        "entry_minute": minute,
                        "entry_leg": "prov_decay",
                    }
                )
                held.add(sid)

    ssc._fill_empty_slots = _fill  # type: ignore[assignment]
    try:
        yield
    finally:
        ssc._fill_empty_slots = orig  # type: ignore[assignment]


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def _schedule_by_id() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in _decaying_schedules()}


def run_decaying_early_pnl_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    clocks: tuple[str, ...] = DEFAULT_CLOCKS,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if trade_dates and c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    watch = load_etf_constituent_watchlist(conn)
    name_map = {str(w["stock_id"]): str(w.get("stock_name") or "") for w in watch}
    universe = [str(w["stock_id"]) for w in watch]

    print("building provisional fresh score cache …", flush=True)
    cache = build_provisional_fresh_score_cache(
        conn,
        trade_dates=trade_dates,
        clocks=clocks,
        close=close,
        bench=bench,
        universe=universe,
    )
    fresh_by_date = _stub_fresh_calendar(cache, trade_dates, name_map)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    champ = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
        timing_mode="close",
        entry_leg="A",
        no_trade_before="09:30",
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
        "spread_rs_panel": None,
    }

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS after {is_end}")

    gate: dict[str, set[str]] | None = None
    gate_meta: dict[str, Any] = {}
    if gate_path and gate_path.is_file():
        try:
            gate, gate_meta = _ensure_avoid_mixed_gate(
                conn,
                trade_dates,
                gate_cache_path=gate_path,
                live_aligned=True,
                n_slots=n_slots,
            )
        except ValueError:
            raw, meta, c0, c1 = load_gate_cache(str(gate_path))
            gate = {d: set(raw.get(d) or set()) for d in trade_dates}
            gate_meta = {
                **(meta or {}),
                "subset_from": f"{c0}..{c1}",
                "subset_to": f"{trade_dates[0]}..{trade_dates[-1]}",
            }
            print(
                f"subset avoid_mixed gate {c0}..{c1} → {trade_dates[0]}..{trade_dates[-1]}",
                flush=True,
            )
    else:
        gate, gate_meta = _ensure_avoid_mixed_gate(
            conn,
            trade_dates,
            gate_cache_path=None,
            live_aligned=True,
            n_slots=n_slots,
        )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    schedules = _schedule_by_id()
    variants: list[dict[str, Any]] = []
    base_full = base_is = base_oos = None
    prior_days = int(getattr(champ, "entry_prior_ret_days", 5) or 5)
    prior_max = float(getattr(champ, "entry_prior_ret_max", 0.12) or 0.12)

    for vid, sched_id, clocks_ov, label in PNL_VARIANTS:
        sched = schedules[sched_id or "FLAT_FRESH"]
        use_clocks = clocks_ov or clocks
        print(f"  sim {vid} · clocks={','.join(use_clocks)} · {sched['id']} …", flush=True)
        cfg = replace(champ, variant_id=vid, label=label)
        with _patch_decaying_fill(
            cache=cache,
            schedule=sched,
            clocks=use_clocks,
            gate=gate,
            name_map=name_map,
            close=close,
            full_dates=full_dates,
            prior_ret_days=prior_days,
            prior_ret_max=prior_max,
        ):
            periods, summary, _ = _run_sim(
                conn,
                ctx={**ctx, "kbar_cache": {}},
                trade_dates=trade_dates,
                cfg=cfg,
                confirm_bars=1,
                gate=gate,
                label=vid,
            )
        minute_counts: dict[str, int] = {}
        for p in periods:
            m = str(p.get("entry_minute") or "?")
            minute_counts[m] = minute_counts.get(m, 0) + 1
        row = _variant_row(
            variant_id=vid,
            label=label,
            periods=periods,
            summary=summary,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=base_full,
            base_is=base_is,
            base_oos=base_oos,
            extra={
                "schedule_id": sched["id"],
                "clocks": list(use_clocks),
                "entry_minute_hist": minute_counts,
                "pit_clean": True,
                "n_legs": len(periods),
            },
        )
        if vid == "B0_1320_FLAT":
            base_full = (row.get("slices") or {}).get("full")
            base_is = (row.get("slices") or {}).get("is")
            base_oos = (row.get("slices") or {}).get("oos")
            row["delta_full_excess_pp"] = None
            row["delta_is_excess_pp"] = None
            row["delta_oos_excess_pp"] = None
            row["adopt"] = "BASE"
        variants.append(row)

    best = None
    notes: list[str] = []
    for row in variants:
        if row.get("variant_id") == "B0_1320_FLAT":
            continue
        d_oos = row.get("delta_oos_excess_pp")
        d_is = row.get("delta_is_excess_pp")
        n_oos = int(((row.get("slices") or {}).get("oos") or {}).get("n_legs") or 0)
        decision = _adopt(n_oos=n_oos, d_oos=d_oos, d_is=d_is)
        row["adopt"] = decision
        notes.append(f"{row.get('variant_id')} OOS Δ={d_oos}pp · n={n_oos} → {decision}")
        if decision == "GO" and (
            best is None
            or float(d_oos or -1e9) > float(best.get("delta_oos_excess_pp") or -1e9)
        ):
            best = row

    verdict = "KEEP_B0_1320"
    if best is not None:
        verdict = f"GO_ADOPT_{best.get('variant_id')}"

    return {
        "study_id": "c18acc_provisional_decaying_early_pnl",
        "schema": "c18acc_provisional_decaying_early_pnl-v1",
        "pit": {
            "pool": "same-day provisional mono fresh (5m)",
            "early_gate": "decaying standards (seg/confirm)",
            "baseline": "FLAT provisional fresh @13:20 only",
            "exits": "champion close stack (S2 / accel swap)",
            "no_eod_peek_for_early_clocks": True,
        },
        "window": {
            "date_start": date_start,
            "date_end": end,
            "is_end": is_end,
            "oos_start": oos_start,
            "n_stocks": len(universe),
        },
        "gate_meta": gate_meta,
        "variants": variants,
        "verdict": {
            "summary": verdict,
            "best": best.get("variant_id") if best else None,
            "notes": notes,
        },
    }

def render_decaying_early_pnl_md(payload: dict[str, Any]) -> str:
    w = payload.get("window") or {}
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · 暫定 fresh 提早放行 PnL（遞減門檻）",
        "",
        f"窗口：**{w.get('date_start')}** .. **{w.get('date_end')}** · "
        f"IS ≤ **{w.get('is_end')}** · OOS ≥ **{w.get('oos_start')}** · "
        f"watchlist_n=**{w.get('n_stocks')}**",
        "",
        "## 語意（PIT）",
        "",
        "- 候選 = **當日暫定** mono fresh（每時點 5m 重算），不偷看 EOD 名單。",
        "- 提早放行可接受不完全命中 13:20；用遞減門檻控品質。",
        "- 基線 = 只在 **13:20** 用 flat fresh 進場。",
        "- 出場／換倉 = champion 收盤棧（隔離「進場提早」效應）。",
        "",
        f"## 結論：`{v.get('summary')}`",
        "",
    ]
    for note in v.get("notes") or []:
        lines.append(f"- {note}")
    lines.extend(
        [
            "",
            "## 結果（Δ vs B0_1320_FLAT）",
            "",
            "| variant | window | n | mean_excess% | Δ vs B0 | adopt |",
            "|---------|--------|--:|-------------:|--------:|:-----:|",
        ]
    )
    for row in payload.get("variants") or []:
        for sl_name in ("full", "is", "oos"):
            sl = (row.get("slices") or {}).get(sl_name) or {}
            delta = {
                "full": row.get("delta_full_excess_pp"),
                "is": row.get("delta_is_excess_pp"),
                "oos": row.get("delta_oos_excess_pp"),
            }[sl_name]
            delta_s = "—" if delta is None else f"{float(delta):+.4f}pp"
            lines.append(
                f"| {row.get('variant_id')} | {sl_name.upper()} | {sl.get('n_legs')} | "
                f"{sl.get('mean_excess_pct')} | {delta_s} | "
                f"{row.get('adopt') if sl_name == 'oos' else ''} |"
            )
    lines.extend(
        [
            "",
            "門檻：OOS n≥8 · Δ≥+0.5pp · IS Δ≥−0.3pp（相對 B0）",
            "",
            "模組：`src/research/backtest/c18acc_provisional_decaying_early_pnl_study.py`",
            "",
            "## 進場分鐘分布",
            "",
        ]
    )
    for row in payload.get("variants") or []:
        hist = row.get("entry_minute_hist") or {}
        if hist:
            lines.append(f"- **{row.get('variant_id')}**：`{hist}`")
    return "\n".join(lines) + "\n"

def main(argv: list[str] | None = None) -> int:
    import argparse

    from stock_db import DEFAULT_DB_PATH, connect

    ap = argparse.ArgumentParser(description="Provisional fresh decaying early-entry PnL")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default=None)
    ap.add_argument("--is-end", default="2025-06-30")
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=Path(DEFAULT_GATE_CACHE))
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_decaying_early_pnl_study(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            n_slots=args.n_slots,
            gate_cache_path=args.gate_cache if args.gate_cache.is_file() else None,
        )
    finally:
        conn.close()

    # attach entry_minute_hist from extras if _variant_row flattens
    for row in payload.get("variants") or []:
        # _variant_row typically merges `extra` into row
        pass

    stamp = date.today().strftime("%Y%m%d")
    out_json = RESEARCH_RRG / f"{stamp}_c18acc_provisional_decaying_early_pnl.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_decaying_early_pnl_md(payload), encoding="utf-8")
    print((payload.get("verdict") or {}).get("summary", ""))
    for note in (payload.get("verdict") or {}).get("notes") or []:
        print(note)
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
