"""C18acc · PIT-legal early trade with provisional RRG + caution by earliness.

Design (user 2026-07-14):
- RRG remains EOD-centric for the candidate universe.
- Trade day T uses **T−1 EOD fresh** (known overnight · PIT) as the pool seed.
- On T, **provisional SNAP RRG** at signal_minute ranks that pool (live-executable).
- Fill at signal_minute + (confirm_bars−1)×5m — earlier signal ⇒ longer wait
  (= more caution) before committing capital.

Baseline: SNAP@13:20 · confirm=1 · fill@13:20（近收盤）
Challengers: earlier SNAP + higher confirm (e.g. 12:00/c3 → fill 12:10).

Not the same as peeking same-day EOD fresh at the open.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
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
from research.backtest.c18acc_snap_window_study import _snapshot_minute
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH, ScanRow
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

# (variant_id, signal_minute, confirm_bars, label)
# fill_minute = signal + (confirm−1)×5m
VARIANTS: tuple[tuple[str, str, int, str], ...] = (
    ("SNAP_1320_c1", "13:20", 1, "基線 · 暫定@13:20 · confirm=1 · 立即成交"),
    ("SNAP_1300_c2", "13:00", 2, "暫定@13:00 · confirm=2 · 延後 5m 成交"),
    ("SNAP_1240_c2", "12:40", 2, "暫定@12:40 · confirm=2 · 延後 5m 成交"),
    ("SNAP_1200_c3", "12:00", 3, "暫定@12:00 · confirm=3 · 延後 10m 成交"),
    ("SNAP_1100_c3", "11:00", 3, "暫定@11:00 · confirm=3 · 延後 10m 成交"),
    ("SNAP_0930_c3", "09:30", 3, "暫定@09:30 · confirm=3 · 延後 10m 成交"),
    # ablation：早開窗但不謹慎
    ("SNAP_1200_c1", "12:00", 1, "對照 · 暫定@12:00 · confirm=1 · 立即成交（低謹慎）"),
)


def _add_minutes(minute: str, delta: int) -> str:
    h, m = minute.split(":")
    total = int(h) * 60 + int(m) + int(delta)
    if total < 0:
        total = 0
    return f"{total // 60:02d}:{total % 60:02d}"


def _fill_minute(signal_minute: str, confirm_bars: int) -> str:
    return _add_minutes(signal_minute, max(0, int(confirm_bars) - 1) * 5)


def shift_fresh_to_prior_day(
    fresh_by_date: dict[str, list[ScanRow]],
    trade_dates: list[str],
) -> dict[str, list[ScanRow]]:
    """Trade day T uses T−1 EOD fresh · PIT overnight（隔夜可知）."""
    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for i, d in enumerate(trade_dates):
        if i == 0:
            continue
        prev = trade_dates[i - 1]
        out[d] = list(fresh_by_date.get(prev) or [])
    return out


@contextmanager
def _delayed_snap_fill(fill_minute: str) -> Iterator[None]:
    """Rank with SNAPSHOT_1300_MINUTE · fill px at fill_minute (confirm delay)."""
    import research.backtest.c18acc_snapshot_1300 as snap

    orig = snap.fill_empty_slots_snapshot_1300

    def _fill(
        conn: sqlite3.Connection,
        *,
        as_of: str,
        rows: list,
        slots: list[dict[str, Any]],
        close,
        bench,
        kbar_cache: dict,
        n_slots: int,
        entry_leg: str,
        full_rrg_cache=None,
    ) -> int:
        used = {int(p["slot"]) for p in slots}
        held = {str(p["stock_id"]) for p in slots}
        free = [i for i in range(n_slots) if i not in used]
        if not free or not rows:
            return 0
        ranked = snap.rerank_shortlist_snapshot_1300(
            rows,
            conn=conn,
            close=close,
            bench=bench,
            trade_date=as_of,
            kbar_cache=kbar_cache,
            full_rrg_cache=full_rrg_cache,
        )
        added = 0
        for row in ranked:
            if not free:
                break
            if row.stock_id in held:
                continue
            key = (row.stock_id, as_of)
            if key not in kbar_cache:
                kbar_cache[key] = load_kbar_day_closes(conn, row.stock_id, as_of)
            px = price_at_or_before_minute(kbar_cache[key], fill_minute)
            if px is None or float(px) <= 0:
                continue
            slot = free.pop(0)
            slots.append(
                {
                    "slot": slot,
                    "stock_id": row.stock_id,
                    "stock_name": row.stock_name,
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "entry_px": float(px),
                    "seg_last": round(row.seg_last, 4),
                    "disp": round(row.disp, 4),
                    "rs_momentum": float(row.rs_momentum),
                    "seg_step_delta": row.segs[-1] - row.segs[-2] if len(row.segs) >= 2 else 0.0,
                    "entry_minute": fill_minute,
                    "signal_minute": snap.SNAPSHOT_1300_MINUTE,
                    "entry_leg": entry_leg,
                }
            )
            held.add(row.stock_id)
            added += 1
        return added

    snap.fill_empty_slots_snapshot_1300 = _fill  # type: ignore[assignment]
    try:
        yield
    finally:
        snap.fill_empty_slots_snapshot_1300 = orig  # type: ignore[assignment]


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_snap_adaptive_caution_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
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
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    eod_fresh = build_fresh_mono_calendar(conn, trade_dates)
    fresh_by_date = shift_fresh_to_prior_day(eod_fresh, trade_dates)
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
        raise ValueError(f"no OOS after {is_end}")

    gate, gate_meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_path,
        live_aligned=True,
        n_slots=n_slots,
    )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    variants: list[dict[str, Any]] = []
    base_full = base_is = base_oos = None
    for vid, signal_m, confirm, label in VARIANTS:
        fill_m = _fill_minute(signal_m, confirm)
        print(
            f"  sim {vid} · pool=T-1 EOD fresh · SNAP@{signal_m} · "
            f"confirm={confirm} · fill@{fill_m} …",
            flush=True,
        )
        cfg = replace(
            champ,
            variant_id=vid,
            label=label,
            timing_mode="snapshot_1300",
            no_trade_before=fill_m,
            snapshot_strict_kbar=False,
        )
        with _snapshot_minute(signal_m), _delayed_snap_fill(fill_m):
            periods, summary, _ = _run_sim(
                conn,
                ctx={**ctx, "kbar_cache": {}},
                trade_dates=trade_dates,
                cfg=cfg,
                confirm_bars=confirm,
                gate=gate,
                label=vid,
            )
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
                "pool": "prior_day_eod_fresh",
                "signal_minute": signal_m,
                "fill_minute": fill_m,
                "confirm_bars": confirm,
                "confirm_delay_min": max(0, confirm - 1) * 5,
                "pit_clean": True,
                "live_executable": True,
                "snap_meta": {
                    "snapshot_days_skipped": (summary or {}).get("snapshot_days_skipped"),
                    "n_legs": len(periods),
                },
            },
        )
        if vid == "SNAP_1320_c1":
            base_full = (row.get("slices") or {}).get("full")
            base_is = (row.get("slices") or {}).get("is")
            base_oos = (row.get("slices") or {}).get("oos")
            row["delta_full_excess_pp"] = 0.0
            row["delta_is_excess_pp"] = 0.0
            row["delta_oos_excess_pp"] = 0.0
        variants.append(row)

    adopt: dict[str, str] = {}
    notes: list[str] = []
    best_id = None
    best_d = None
    for v in variants:
        vid = str(v["variant_id"])
        if vid == "SNAP_1320_c1":
            adopt[vid] = "BASELINE"
            continue
        oos = (v.get("slices") or {}).get("oos") or {}
        d_oos = v.get("delta_oos_excess_pp")
        d_is = v.get("delta_is_excess_pp")
        n_oos = int(oos.get("n_legs") or 0)
        flag = _adopt(n_oos=n_oos, d_oos=d_oos, d_is=d_is)
        adopt[vid] = flag
        notes.append(
            f"{vid}: signal={v.get('signal_minute')} fill={v.get('fill_minute')} "
            f"OOS Δ={d_oos}pp n={n_oos} · IS Δ={d_is}pp → {flag}"
        )
        if d_oos is not None and (best_d is None or float(d_oos) > float(best_d)):
            best_d = float(d_oos)
            best_id = vid

    any_go = any(v == "GO" for k, v in adopt.items() if k != "SNAP_1320_c1")
    # Prefer GO among cautious early; if only low-caution ablation GO, note it
    summary = (
        f"GO_ADOPT_{best_id}" if any_go and best_id else "KEEP_SNAP_1320"
    )

    return {
        "schema": "c18acc-snap-adaptive-caution-v1",
        "date_start": date_start,
        "date_end": end,
        "is_end": is_end,
        "oos_start": oos_start,
        "n_trade_dates": len(trade_dates),
        "n_slots": n_slots,
        "baseline": "SNAP_1320_c1",
        "timeline": {
            "pool": "T-1 EOD fresh mono（隔夜可知）",
            "signal": "T 暫定 SNAP RRG @ signal_minute",
            "fill": "T @ signal_minute + (confirm−1)×5m",
            "not": "禁止用 T EOD fresh 在 T 早盤成交",
        },
        "gate_meta": gate_meta,
        "variants": variants,
        "verdict": {
            "summary": summary,
            "adopt": adopt,
            "best_oos_variant": best_id,
            "best_oos_delta_pp": best_d,
            "thresholds": {
                "oos_min_n": ADOPT_OOS_MIN_N,
                "oos_min_delta_pp": ADOPT_OOS_MIN_DELTA_PP,
                "is_min_delta_pp": ADOPT_IS_MIN_DELTA_PP,
            },
            "notes": notes,
            "caveat": (
                "候選池 = 昨收 EOD fresh；訊號/排序 = 當日暫定 RRG；"
                "confirm 以延後成交分鐘近似（非連續 poll 計數）。"
                "共用 09:30 avoid_mixed gate。"
            ),
        },
    }


def render_snap_adaptive_caution_md(payload: dict[str, Any]) -> str:
    vdict = payload.get("verdict") or {}
    tl = payload.get("timeline") or {}
    lines = [
        "# C18acc · 合法提早交易：昨收池 + 暫定 RRG + 越早越謹慎",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}** · "
        f"n_slots={payload.get('n_slots')}",
        "",
        "## 時間軸（PIT）",
        "",
        f"- 定池：{tl.get('pool')}",
        f"- 訊號：{tl.get('signal')}",
        f"- 成交：{tl.get('fill')}",
        f"- 禁止：{tl.get('not')}",
        "",
        str(vdict.get("caveat") or ""),
        "",
        f"## 結論：{vdict.get('summary')}",
        "",
    ]
    for n in vdict.get("notes") or []:
        lines.append(f"- {n}")
    lines += [
        "",
        "## 結果（Δ vs SNAP_1320_c1）",
        "",
        "| variant | signal | fill | c | window | n | mean_excess% | Δ vs B0 |",
        "|---------|--------|------|--:|--------|--:|-------------:|--------:|",
    ]
    for row in payload.get("variants") or []:
        for slice_name in ("full", "is", "oos"):
            sl = (row.get("slices") or {}).get(slice_name) or {}
            if slice_name == "full":
                dlt = row.get("delta_full_excess_pp")
            elif slice_name == "is":
                dlt = row.get("delta_is_excess_pp")
            else:
                dlt = row.get("delta_oos_excess_pp")
            if row.get("variant_id") == "SNAP_1320_c1" or dlt is None:
                dlt_s = "—"
            else:
                dlt_s = f"{float(dlt):+.4f}pp"
            lines.append(
                f"| {row.get('variant_id')} | {row.get('signal_minute')} | "
                f"{row.get('fill_minute')} | {row.get('confirm_bars')} | "
                f"{slice_name.upper()} | {sl.get('n_legs')} | "
                f"{sl.get('mean_excess_pct')} | {dlt_s} |"
            )
    thresh = vdict.get("thresholds") or {}
    lines += [
        "",
        f"門檻：OOS n≥{thresh.get('oos_min_n')} · Δ≥{thresh.get('oos_min_delta_pp')}pp · "
        f"IS Δ≥{thresh.get('is_min_delta_pp')}pp",
        "",
        "模組：`src/research/backtest/c18acc_snap_adaptive_caution_study.py`",
        "",
    ]
    return "\n".join(lines)
