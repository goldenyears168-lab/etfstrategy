"""C18acc · SNAP early entry with 12:00→12:30 stability (expanded universe).

Baseline: SNAP_1320（暫定 RRG@13:20 · 近收）
Challenger: SNAP_1200_STABLE1230 —
  · 12:00 與 12:30 暫定 RRG 的 top-N 交集（訊號需撐過觀察窗）
  · 成交 @12:30（觀察結束後才下單）

Universe: current ``load_etf_constituent_watchlist``（含 2026-07-13 supplemental ≈238）。
Avoid-mixed:
  · default: legacy live gate cache + passthrough unscored fresh names
  · ``rebuild_gate=True``: rebuild full-universe gate from current fresh calendar (no passthrough)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from report_paths import RESEARCH_RRG
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (
    build_avoid_mixed_gate_lookup_live,
    load_gate_cache,
    save_gate_cache,
)
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _run_sim,
    _variant_row,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH, MAX_SLOTS, ScanRow
from rrg_rotation import compute_rrg_panel
from stock_db.etf import load_etf_constituent_watchlist


@contextmanager
def _snapshot_minute(minute: str) -> Iterator[None]:
    import research.backtest.c18acc_snapshot_1300 as snap

    old_m = snap.SNAPSHOT_1300_MINUTE
    old_sql = snap.SNAPSHOT_1300_MINUTE_SQL
    snap.SNAPSHOT_1300_MINUTE = minute
    snap.SNAPSHOT_1300_MINUTE_SQL = f"{minute}:00" if len(minute) == 5 else minute
    snap.clear_snapshot_panel_cache()
    try:
        yield
    finally:
        snap.SNAPSHOT_1300_MINUTE = old_m
        snap.SNAPSHOT_1300_MINUTE_SQL = old_sql
        snap.clear_snapshot_panel_cache()


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def _expand_gate_for_new_fresh(
    gate: dict[str, set[str]],
    fresh_by_date: dict[str, list[ScanRow]],
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Allow fresh names never scored in the cached avoid_mixed union (passthrough)."""
    original_union = {sid for sids in gate.values() for sid in sids}
    out = {d: set(sids) for d, sids in gate.items()}
    passthrough_ids: set[str] = set()
    passthrough_stock_days = 0
    for d, rows in fresh_by_date.items():
        for row in rows:
            sid = str(row.stock_id)
            if sid in original_union:
                continue
            out.setdefault(d, set()).add(sid)
            passthrough_ids.add(sid)
            passthrough_stock_days += 1
    return out, {
        "original_union_n": len(original_union),
        "passthrough_stock_n": len(passthrough_ids),
        "passthrough_stock_days": passthrough_stock_days,
        "passthrough_ids_sample": sorted(passthrough_ids)[:20],
    }


def build_snap_observe_stability_gate(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    fresh_by_date: dict[str, list[ScanRow]],
    close,
    bench,
    observe_start: str = "12:00",
    observe_end: str = "12:30",
    top_n: int = MAX_SLOTS,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    """Whitelist: stock in provisional top_n at both observe_start and observe_end."""
    from research.backtest.c18acc_snapshot_1300 import rerank_shortlist_snapshot_1300

    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    gate: dict[str, set[str]] = {}
    n_empty_fresh = 0
    n_empty_stable = 0
    overlap_sizes: list[int] = []

    for i, d in enumerate(trade_dates):
        rows = list(fresh_by_date.get(d) or [])
        if not rows:
            gate[d] = set()
            n_empty_fresh += 1
            continue
        with _snapshot_minute(observe_start):
            r0 = rerank_shortlist_snapshot_1300(
                rows,
                conn=conn,
                close=close,
                bench=bench,
                trade_date=d,
                kbar_cache=kbar_cache,
            )
        with _snapshot_minute(observe_end):
            r1 = rerank_shortlist_snapshot_1300(
                rows,
                conn=conn,
                close=close,
                bench=bench,
                trade_date=d,
                kbar_cache=kbar_cache,
            )
        s0 = {r.stock_id for r in r0[:top_n]}
        s1 = {r.stock_id for r in r1[:top_n]}
        stable = s0 & s1
        gate[d] = stable
        overlap_sizes.append(len(stable))
        if not stable:
            n_empty_stable += 1
        if (i + 1) % 50 == 0:
            print(
                f"    stability gate {i + 1}/{len(trade_dates)} · "
                f"last={d} overlap={len(stable)}",
                flush=True,
            )

    meta = {
        "observe_start": observe_start,
        "observe_end": observe_end,
        "top_n": top_n,
        "n_dates": len(trade_dates),
        "n_empty_fresh": n_empty_fresh,
        "n_empty_stable": n_empty_stable,
        "mean_overlap": round(sum(overlap_sizes) / len(overlap_sizes), 3) if overlap_sizes else 0.0,
    }
    return gate, meta


def _intersect_gates(
    a: dict[str, set[str]],
    b: dict[str, set[str]],
    trade_dates: list[str],
) -> dict[str, set[str]]:
    return {d: set(a.get(d) or ()) & set(b.get(d) or ()) for d in trade_dates}


def _full_universe_gate_cache_path(date_start: str, date_end: str) -> Path:
    return (
        RESEARCH_RRG
        / f"20260713_c18acc_avoid_mixed_gate_live_poll5m_u238_{date_start}_{date_end}.json"
    )


def run_snap_stable1200_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-06-30",
    confirm_bars: int = 1,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    observe_start: str = "12:00",
    observe_end: str = "12:30",
    stability_top_n: int = MAX_SLOTS,
    rebuild_gate: bool = False,
    allow_passthrough: bool | None = None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    watch_n = len({str(r["stock_id"]) for r in load_etf_constituent_watchlist(conn)})
    print(f"watchlist_n={watch_n}", flush=True)

    use_passthrough = (
        (not rebuild_gate) if allow_passthrough is None else bool(allow_passthrough)
    )
    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if rebuild_gate:
        gate_path = _full_universe_gate_cache_path(trade_dates[0], trade_dates[-1])
        print(f"rebuild_gate → {gate_path}", flush=True)
    elif gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncate trade window to gate cache end {end}", flush=True)

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
        raise ValueError(f"no OOS after {is_end}")

    expand_meta: dict[str, Any] = {
        "original_union_n": None,
        "passthrough_stock_n": 0,
        "passthrough_stock_days": 0,
        "passthrough_ids_sample": [],
        "mode": "none",
    }
    if rebuild_gate:
        if gate_path is not None and gate_path.is_file():
            print(f"loading rebuilt gate cache {gate_path}", flush=True)
            gate_base, gate_meta, c0, c1 = load_gate_cache(str(gate_path))
            if c0 != trade_dates[0] or c1 != trade_dates[-1]:
                raise ValueError(
                    f"rebuilt gate window {c0}..{c1} != study {trade_dates[0]}..{trade_dates[-1]}"
                )
        else:
            print(
                f"building full-universe avoid_mixed gate "
                f"{trade_dates[0]}→{trade_dates[-1]} ({len(trade_dates)} td) …",
                flush=True,
            )
            gate_base, gate_meta = build_avoid_mixed_gate_lookup_live(
                conn,
                trade_dates=trade_dates,
                ctx=ctx,
                cfg=champ,
                no_trade_before=str(champ.no_trade_before or "13:20"),
                gate_anchor_minute="09:30",
                passthrough_pool=True,
            )
            gate_meta = {
                **gate_meta,
                "watchlist_n": watch_n,
                "rebuild": True,
                "universe_note": "fresh mono from current load_etf_constituent_watchlist",
            }
            if gate_path is not None:
                gate_path.parent.mkdir(parents=True, exist_ok=True)
                save_gate_cache(
                    str(gate_path),
                    gate=gate_base,
                    meta=gate_meta,
                    date_start=trade_dates[0],
                    date_end=trade_dates[-1],
                )
                print(f"saved full-universe gate → {gate_path}", flush=True)
        expand_meta["mode"] = "full_rebuild_no_passthrough"
        expand_meta["original_union_n"] = int(gate_meta.get("n_stocks_union") or 0)
        print(
            f"gate rebuilt · n_stocks_union={gate_meta.get('n_stocks_union')} · "
            f"mixed_pct={gate_meta.get('candidate_mixed_pct')}",
            flush=True,
        )
    else:
        gate_raw, gate_meta = _ensure_avoid_mixed_gate(
            conn,
            trade_dates,
            gate_cache_path=gate_path,
            live_aligned=True,
            n_slots=n_slots,
        )
        if gate_raw is None:
            raise ValueError("avoid_mixed gate required")
        if use_passthrough:
            gate_base, expand_meta = _expand_gate_for_new_fresh(gate_raw, fresh_by_date)
            expand_meta["mode"] = "legacy_cache_plus_passthrough"
            print(
                f"gate expand · original_union={expand_meta['original_union_n']} · "
                f"passthrough_stocks={expand_meta['passthrough_stock_n']} · "
                f"passthrough_stock_days={expand_meta['passthrough_stock_days']}",
                flush=True,
            )
        else:
            gate_base = gate_raw
            expand_meta["mode"] = "legacy_cache_no_passthrough"
            expand_meta["original_union_n"] = len(
                {sid for sids in gate_raw.values() for sid in sids}
            )

    print(
        f"building stability gate {observe_start}∩{observe_end} top_n={stability_top_n} …",
        flush=True,
    )
    stable_gate, stable_meta = build_snap_observe_stability_gate(
        conn,
        trade_dates=trade_dates,
        fresh_by_date=fresh_by_date,
        close=close,
        bench=bench,
        observe_start=observe_start,
        observe_end=observe_end,
        top_n=stability_top_n,
    )
    print(f"  stability meta={stable_meta}", flush=True)
    gate_stable = _intersect_gates(gate_base, stable_gate, trade_dates)

    variants: list[dict[str, Any]] = []

    print("  sim SNAP_1320 …", flush=True)
    cfg_b = replace(
        champ,
        variant_id="SNAP_1320",
        label="暫定 RRG@13:20 · ≥13:20 成交（現行近收）",
        timing_mode="snapshot_1300",
        no_trade_before="13:20",
        snapshot_strict_kbar=False,
    )
    with _snapshot_minute("13:20"):
        p_b, s_b, _ = _run_sim(
            conn,
            ctx={**ctx, "kbar_cache": {}},
            trade_dates=trade_dates,
            cfg=cfg_b,
            confirm_bars=confirm_bars,
            gate=gate_base,
            label="SNAP_1320",
        )
    base_row = _variant_row(
        variant_id="SNAP_1320",
        label=cfg_b.label,
        periods=p_b,
        summary=s_b,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
        extra={
            "snapshot_minute": "13:20",
            "observe_window": None,
            "pit_clean": True,
            "live_executable": True,
        },
    )
    bf, bi, bo = base_row["slices"]["full"], base_row["slices"]["is"], base_row["slices"]["oos"]
    variants.append(base_row)

    print(f"  sim SNAP_1200_STABLE{observe_end.replace(':', '')} …", flush=True)
    vid = f"SNAP_1200_STABLE{observe_end.replace(':', '')}"
    cfg_s = replace(
        champ,
        variant_id=vid,
        label=f"12:00→{observe_end} 穩定 · 暫定 RRG 交集 top{stability_top_n} · @{observe_end} 成交",
        timing_mode="snapshot_1300",
        no_trade_before=observe_end,
        snapshot_strict_kbar=False,
    )
    with _snapshot_minute(observe_end):
        p_s, s_s, _ = _run_sim(
            conn,
            ctx={**ctx, "kbar_cache": {}},
            trade_dates=trade_dates,
            cfg=cfg_s,
            confirm_bars=confirm_bars,
            gate=gate_stable,
            label=vid,
        )
    chall = _variant_row(
        variant_id=vid,
        label=cfg_s.label,
        periods=p_s,
        summary=s_s,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
        base_full=bf,
        base_is=bi,
        base_oos=bo,
        extra={
            "snapshot_minute": observe_end,
            "observe_window": f"{observe_start}→{observe_end}",
            "stability_top_n": stability_top_n,
            "pit_clean": True,
            "live_executable": True,
        },
    )
    variants.append(chall)

    n_oos = int((chall["slices"]["oos"] or {}).get("n_legs") or 0)
    adopt = _adopt(
        n_oos=n_oos,
        d_oos=chall.get("delta_oos_excess_pp"),
        d_is=chall.get("delta_is_excess_pp"),
    )
    return {
        "study_id": "c18acc_snap_stable1200",
        "schema": "c18acc_snap_stable1200-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "watchlist_n": watch_n,
        "gate_meta": gate_meta,
        "gate_expand_meta": expand_meta,
        "gate_cache_path": str(gate_path) if gate_path else None,
        "rebuild_gate": bool(rebuild_gate),
        "stability_meta": stable_meta,
        "intent": "early provisional entry after 12:00–12:30 signal stability",
        "variants": variants,
        "verdict": {
            "baseline": "SNAP_1320",
            "challenger": vid,
            "adopt": adopt,
            "recommendation": "KEEP_SNAP_1320" if adopt != "GO" else f"CONSIDER_{vid}",
            "summary": (
                f"{vid} vs SNAP_1320 OOS Δ={chall.get('delta_oos_excess_pp')}pp · "
                f"IS Δ={chall.get('delta_is_excess_pp')}pp · n_oos={n_oos} → {adopt}"
            ),
            "thresholds": {
                "oos_n_min": ADOPT_OOS_MIN_N,
                "oos_delta_pp_min": ADOPT_OOS_MIN_DELTA_PP,
                "is_delta_pp_min": ADOPT_IS_MIN_DELTA_PP,
            },
            "caveat": (
                f"watchlist_n={watch_n}. gate_mode={expand_meta.get('mode')} · "
                f"n_stocks_union={gate_meta.get('n_stocks_union')} · "
                f"passthrough_stocks={expand_meta.get('passthrough_stock_n', 0)}. "
                f"Stability = top{stability_top_n} @ {observe_start} ∩ @{observe_end}."
            ),
        },
    }


def render_snap_stable1200_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · SNAP 提早入 + 12:00→12:30 穩定窗（擴大宇宙）",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}** · "
        f"confirm_bars={payload.get('confirm_bars')} · watchlist_n=**{payload.get('watchlist_n')}**",
        "",
        f"意圖：{payload.get('intent')}",
        "",
        f"- rebuild_gate={payload.get('rebuild_gate')} · gate={payload.get('gate_cache_path')}",
        "",
        "## 對照",
        "",
        "| id | 訊號 | 成交 |",
        "|----|------|------|",
    ]
    for row in payload.get("variants") or []:
        obs = row.get("observe_window") or "—"
        m = row.get("snapshot_minute") or "—"
        lines.append(f"| {row['variant_id']} | {obs} / SNAP@{m} | ≥{m} |")
    sm = payload.get("stability_meta") or {}
    em = payload.get("gate_expand_meta") or {}
    gm = payload.get("gate_meta") or {}
    lines.extend(
        [
            "",
            f"- 穩定窗：{sm.get('observe_start')}→{sm.get('observe_end')} · "
            f"top_n={sm.get('top_n')} · mean_overlap={sm.get('mean_overlap')} · "
            f"empty_stable_days={sm.get('n_empty_stable')}",
            f"- Gate：mode={em.get('mode')} · n_stocks_union={gm.get('n_stocks_union')} · "
            f"mixed_pct={gm.get('candidate_mixed_pct')} · "
            f"passthrough_stocks={em.get('passthrough_stock_n')}",
            "",
            str(v.get("caveat") or ""),
            "",
            f"## 結論：{v.get('recommendation', '—')}",
            "",
            f"- {v.get('summary', '')}",
            "",
            "## 結果（Δ vs SNAP_1320）",
            "",
            "| variant | window | n | mean_excess% | Δ vs SNAP_1320 |",
            "|---------|--------|--:|-------------:|---------------:|",
        ]
    )
    for row in payload.get("variants") or []:
        for slice_name in ("full", "is", "oos"):
            s = (row.get("slices") or {}).get(slice_name) or {}
            delta = {
                "full": row.get("delta_full_excess_pp"),
                "is": row.get("delta_is_excess_pp"),
                "oos": row.get("delta_oos_excess_pp"),
            }[slice_name]
            d_s = "—" if delta is None else f"{float(delta):+.4f}pp"
            lines.append(
                f"| {row['variant_id']} | {slice_name.upper()} | {s.get('n_legs', 0)} | "
                f"{s.get('mean_excess_pct')} | {d_s} |"
            )
    lines.extend(
        [
            "",
            f"門檻：OOS n≥{ADOPT_OOS_MIN_N} · Δ≥+{ADOPT_OOS_MIN_DELTA_PP}pp · "
            f"IS Δ≥{ADOPT_IS_MIN_DELTA_PP}pp",
            "",
            "模組：`src/research/backtest/c18acc_snap_stable1200_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
