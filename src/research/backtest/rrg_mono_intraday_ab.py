"""RRG mono hold7 · A/B/C 建倉對照 + C 腿變體 sweep · VCP 池 H3（D0/Db/D）。

SSG（訊號生成）：每日 D4 收盤 mono fresh + mono_tier2（`build_fresh_mono_calendar`）。
Shortlist：當日 fresh 池依收盤 seg_last 取前十（TOP_N）。
A/B/C 差異在 shortlist 內的建倉排序／時點／盤中重算方式，非訊號定義本身。

H3 · 日線 VCP pivot gate 池 + 盤中 RRG 重排（`build_vcp_pivot_calendar`）：
  D0 · 收盤依 VCP composite 填槽
  Db · VCP 池前十 → 盤中定點 seg_last 重排
  D  · VCP 池前十 → 盤中輪詢 seg_last 重排
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.chunge_funnel_backtest import VCP_PIVOT_GATE
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import (
    _summarize,
    build_fresh_mono_calendar,
    simulate_mono_hold7,
)
from stock_db import load_vcp_screen_v2_for_date
from vcp_funnel_screen import MODEL_ID as VCP_FUNNEL_MODEL_ID
from research.backtest.rrg_lens_score_swap import _rebalance_minutes
from rrg_mono_daily_brief import (
    HOLD_DAYS,
    LENGTH,
    LOOKBACK,
    MAX_SLOTS,
    TOP_N,
    ScanRow,
    _backfill_exit_dates,
    _expire_slots,
    _exit_date_from_entry,
    _feat,
    _fresh_mono,
    _mono_tier2,
)
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import KbarBar, load_kbar_day_bars, load_kbar_day_closes, price_at_or_before_minute
from analytics.bench import bench_return_entry_to_exit

EntryLeg = Literal["A", "B", "C"]
VcpEntryLeg = Literal["D0", "Db", "D"]
ScoreMode = Literal["scale", "full_rrg", "signal_seg_last"]
EntrySchedule = Literal["same_day", "d2_accel_d3"]
EntryFillMode = Literal["poll_px", "bone_zone", "vwap_reclaim", "vwap_bounce"]

LEG_LABELS: dict[EntryLeg, str] = {
    "A": "收盤 seg_last 填槽（hold7 基線）",
    "B": "日線前十 → 盤中定點 seg_last 重排",
    "C": "日線前十 → 盤中輪詢重排",
}

VCP_LEG_LABELS: dict[VcpEntryLeg, str] = {
    "D0": "VCP pivot 池 · 收盤 composite 填槽",
    "Db": "VCP pivot 池前十 → 盤中定點 seg_last 重排",
    "D": "VCP pivot 池前十 → 盤中輪詢 seg_last 重排",
}


@dataclass
class CVariantConfig:
    """C 腿變體 · sweep 維度。"""

    variant_id: str = "C0"
    label: str = "C 基線 · scale · 5m · confirm=1"
    rebalance_interval_min: int = 5
    score_mode: ScoreMode = "scale"
    confirm_bars: int = 1
    entry_schedule: EntrySchedule = "same_day"
    no_swap_before: str = "09:30"
    entry_fill_mode: EntryFillMode = "poll_px"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# sweep 格 · 對應使用者假說
DEFAULT_C_SWEEP: list[CVariantConfig] = [
    CVariantConfig("C0", "基線 scale 5m confirm=1", 5, "scale", 1, "same_day"),
    CVariantConfig("C1", "scale 30m confirm=1", 30, "scale", 1, "same_day"),
    CVariantConfig("C2", "full_rrg 30m confirm=1", 30, "full_rrg", 1, "same_day"),
    CVariantConfig("C3", "scale 5m confirm=2", 5, "scale", 2, "same_day"),
    CVariantConfig("C4", "full_rrg 5m confirm=2", 5, "full_rrg", 2, "same_day"),
    CVariantConfig("C5", "full_rrg 30m confirm=2", 30, "full_rrg", 2, "same_day"),
    CVariantConfig("C6", "full_rrg 15m confirm=1", 15, "full_rrg", 1, "same_day"),
    CVariantConfig("C7", "d2加速→d3盤中 full_rrg 5m", 5, "full_rrg", 1, "d2_accel_d3"),
    CVariantConfig("C8", "d2加速→d3盤中 full_rrg 30m", 30, "full_rrg", 1, "d2_accel_d3"),
]

# C18acc · 進場 fill 模式 sweep（SSG 不變 · 僅執行層）
C18ACC_ENTRY_FILL_SWEEP: list[CVariantConfig] = [
    next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0"),
    next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C3"),
    CVariantConfig(
        "C18acc-vwap",
        "confirm=1 · VWAP reclaim 1m",
        5,
        "scale",
        1,
        "same_day",
        entry_fill_mode="vwap_reclaim",
    ),
    CVariantConfig(
        "C18acc-bone",
        "confirm=1 · Bone Zone 1m",
        5,
        "scale",
        1,
        "same_day",
        entry_fill_mode="bone_zone",
    ),
    CVariantConfig(
        "C18acc-cfm2-vwap",
        "confirm=2 · VWAP reclaim 1m",
        5,
        "scale",
        2,
        "same_day",
        entry_fill_mode="vwap_reclaim",
    ),
]


@dataclass
class _PendingFunnel:
    signal_date: str
    shortlist: list[ScanRow]
    stage: int = 0


def close_shortlist(fresh_mono: list[ScanRow]) -> list[ScanRow]:
    ranked = sorted(fresh_mono, key=lambda r: (-r.seg_last, r.stock_id))
    return ranked[:TOP_N]


def vcp_close_shortlist(pool: list[ScanRow]) -> list[ScanRow]:
    ranked = sorted(pool, key=lambda r: (-(r.composite_score or 0.0), r.stock_id))
    return ranked[:TOP_N]


def build_vcp_pivot_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
) -> dict[str, list[ScanRow]]:
    """日線 VCP pivot gate 候選（PIT：當日 vcp_screen close）· 附 RRG 軌跡欄位供盤中重排。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    date_set = set(trade_dates)
    si_by_date = {d: full_dates.index(d) for d in trade_dates if d in full_dates}

    min_composite = float(VCP_PIVOT_GATE["min_composite"])
    states = tuple(VCP_PIVOT_GATE["execution_states"])
    min_dist = float(VCP_PIVOT_GATE["min_dist_pivot_pct"])
    max_dist = float(VCP_PIVOT_GATE["max_dist_pivot_pct"])
    require_pivot = bool(VCP_PIVOT_GATE.get("require_pivot"))

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for as_of in trade_dates:
        si = si_by_date.get(as_of)
        if si is None or si < LOOKBACK:
            continue
        rows = load_vcp_screen_v2_for_date(
            conn,
            as_of,
            model_id=VCP_FUNNEL_MODEL_ID,
            min_score=min_composite,
            execution_states=states,
        )
        pool: list[ScanRow] = []
        for r in rows:
            sid = str(r["stock_id"])
            dist = (
                float(r["distance_from_pivot_pct"])
                if r["distance_from_pivot_pct"] is not None
                else None
            )
            pivot = float(r["pivot_price"]) if r["pivot_price"] else None
            if require_pivot and (pivot is None or pivot <= 0):
                continue
            if dist is not None and dist < min_dist:
                continue
            if dist is not None and dist > max_dist:
                continue
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            if f is not None:
                pool.append(
                    ScanRow(
                        stock_id=sid,
                        stock_name=str(r["stock_name"] or ""),
                        fresh=False,
                        mono=False,
                        seg_last=float(f["seg_last"]),
                        disp=float(f["disp"]),
                        segs=[float(x) for x in f["segs"]],
                        quadrants=[q or "?" for q in f["quadrants"]],
                        rs_ratio=float(f["rs_ratio"]),
                        rs_momentum=float(f["rs_momentum"]),
                        daily_pct=pct,
                        composite_score=float(r["composite_score"] or 0.0),
                    )
                )
            else:
                pool.append(
                    ScanRow(
                        stock_id=sid,
                        stock_name=str(r["stock_name"] or ""),
                        fresh=False,
                        mono=False,
                        seg_last=0.0,
                        disp=0.0,
                        segs=[],
                        quadrants=[],
                        rs_ratio=100.0,
                        rs_momentum=100.0,
                        daily_pct=pct,
                        composite_score=float(r["composite_score"] or 0.0),
                    )
                )
        pool.sort(key=lambda row: (-(row.composite_score or 0.0), row.stock_id))
        out[as_of] = pool
    return out


def intraday_price_scale(
    close_px: float,
    intraday_px: float | None,
    *,
    lo: float = 0.25,
    hi: float = 2.5,
) -> float:
    if intraday_px is None or close_px <= 0:
        return 1.0
    return max(lo, min(hi, intraday_px / close_px))


def scaled_seg_last(row: ScanRow, scale: float) -> float:
    return float(row.seg_last) * scale


def _trading_offset(full_dates: list[str], start: str, offset: int) -> str | None:
    if start not in full_dates:
        return None
    idx = full_dates.index(start) + offset
    return full_dates[idx] if 0 <= idx < len(full_dates) else None


def _close_seg_last(
    *,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
) -> float | None:
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < LOOKBACK:
        return None
    feat = _feat(rs_ratio, rs_mom, full_dates, si, stock_id)
    if feat is None:
        return None
    return float(feat.get("seg_last") or 0.0)


def _kbar_px(
    conn: sqlite3.Connection,
    sid: str,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    close: pd.DataFrame,
) -> float | None:
    key = (sid, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, sid, trade_date)
    px = price_at_or_before_minute(kbar_cache[key], minute)
    if px is None and trade_date in close.index and sid in close.columns:
        px = float(close.at[trade_date, sid])
    return px


def rank_shortlist_scale(
    shortlist: list[ScanRow],
    *,
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> list[ScanRow]:
    scored: list[tuple[float, ScanRow]] = []
    for row in shortlist:
        sid = row.stock_id
        if trade_date not in close.index or sid not in close.columns:
            continue
        close_px = float(close.at[trade_date, sid])
        if close_px <= 0:
            continue
        px = _kbar_px(conn, sid, trade_date, minute, kbar_cache, close)
        scale = intraday_price_scale(close_px, px)
        scored.append((scaled_seg_last(row, scale), row))
    scored.sort(key=lambda x: (-x[0], x[1].stock_id))
    return [row for _, row in scored]


def rank_shortlist_full_rrg(
    shortlist: list[ScanRow],
    *,
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
) -> list[ScanRow]:
    if not shortlist or trade_date not in close.index:
        return []
    sid_key = tuple(sorted(r.stock_id for r in shortlist))
    cache_key = (trade_date, minute, sid_key)
    if full_rrg_cache is not None and cache_key in full_rrg_cache:
        return list(full_rrg_cache[cache_key])

    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    allow = {r.stock_id for r in shortlist}
    name_map = {r.stock_id: r.stock_name for r in shortlist}
    for row in shortlist:
        sid = row.stock_id
        px = _kbar_px(conn, sid, trade_date, minute, kbar_cache, close)
        if px is not None and px > 0:
            prov.at[trade_date, sid] = float(px)

    rs_r, rs_m, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)
    full_dates = prov.index.astype(str).tolist()
    si = full_dates.index(trade_date)
    fresh: list[ScanRow] = []
    for sid in allow:
        f = _feat(rs_r, rs_m, full_dates, si, sid)
        if f is None or not _mono_tier2(f) or not _fresh_mono(rs_r, rs_m, full_dates, si, sid):
            continue
        base = next(r for r in shortlist if r.stock_id == sid)
        fresh.append(
            ScanRow(
                stock_id=sid,
                stock_name=name_map.get(sid, ""),
                fresh=True,
                mono=True,
                seg_last=float(f["seg_last"]),
                disp=float(f["disp"]),
                segs=base.segs,
                quadrants=base.quadrants,
                rs_ratio=float(f["rs_ratio"]),
                rs_momentum=float(f["rs_momentum"]),
                daily_pct=base.daily_pct,
            )
        )
    fresh.sort(key=lambda r: (-r.seg_last, r.stock_id))
    if not fresh:
        ranked = rank_shortlist_scale(
            shortlist,
            conn=conn,
            close=close,
            trade_date=trade_date,
            minute=minute,
            kbar_cache=kbar_cache,
        )
    else:
        ranked = fresh
    if full_rrg_cache is not None:
        full_rrg_cache[cache_key] = list(ranked)
    return ranked


def rank_shortlist(
    shortlist: list[ScanRow],
    *,
    config: CVariantConfig,
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
) -> list[ScanRow]:
    if config.score_mode == "signal_seg_last":
        return sorted(shortlist, key=lambda r: (-r.seg_last, r.stock_id))
    if config.score_mode == "full_rrg":
        return rank_shortlist_full_rrg(
            shortlist,
            conn=conn,
            close=close,
            bench=bench,
            trade_date=trade_date,
            minute=minute,
            kbar_cache=kbar_cache,
            full_rrg_cache=full_rrg_cache,
        )
    return rank_shortlist_scale(
        shortlist,
        conn=conn,
        close=close,
        trade_date=trade_date,
        minute=minute,
        kbar_cache=kbar_cache,
    )


# backward-compat alias
rank_shortlist_intraday = rank_shortlist_scale


def _accel_filter_shortlist(
    pending: _PendingFunnel,
    *,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
) -> list[ScanRow]:
    snap = {r.stock_id: float(r.seg_last) for r in pending.shortlist}
    kept: list[ScanRow] = []
    for row in pending.shortlist:
        sid = row.stock_id
        now = _close_seg_last(
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=full_dates,
            trade_date=as_of,
            stock_id=sid,
        )
        if now is not None and now > snap.get(sid, 0.0):
            kept.append(
                ScanRow(
                    stock_id=row.stock_id,
                    stock_name=row.stock_name,
                    fresh=row.fresh,
                    mono=row.mono,
                    seg_last=now,
                    disp=row.disp,
                    segs=row.segs,
                    quadrants=row.quadrants,
                    rs_ratio=row.rs_ratio,
                    rs_momentum=row.rs_momentum,
                    daily_pct=row.daily_pct,
                )
            )
    kept.sort(key=lambda r: (-r.seg_last, r.stock_id))
    return kept[:TOP_N]


def _settle_with_entry_px(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    pos: dict[str, Any],
) -> dict[str, Any] | None:
    signal_date = str(pos.get("signal_date") or pos["entry_date"])
    entry = str(pos["entry_date"])
    exit_d = str(pos.get("exit_date") or "")
    sid = str(pos["stock_id"])
    if not exit_d or sid not in close.columns or exit_d not in close.index:
        return None
    entry_px = pos.get("entry_px")
    if entry_px is None:
        if entry not in close.index:
            return None
        entry_px = float(close.at[entry, sid])
    else:
        entry_px = float(entry_px)
    exit_px = float(close.at[exit_d, sid])
    if entry_px <= 0 or exit_px != exit_px:
        return None
    ret = (exit_px / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode="close")
    if bench is None:
        return None
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": signal_date,
        "entry_date": entry,
        "exit_date": exit_d,
        "entry_px": round(entry_px, 4),
        "entry_minute": pos.get("entry_minute"),
        "entry_leg": pos.get("entry_leg"),
        "variant_id": pos.get("variant_id"),
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "seg_last": pos.get("seg_last"),
        "slot": pos.get("slot"),
    }


def _append_position(
    *,
    conn: sqlite3.Connection,
    state: dict[str, Any],
    row: ScanRow,
    signal_date: str,
    entry_date: str,
    entry_px: float,
    full_dates: list[str],
    entry_leg: str,
    entry_minute: str | None,
    variant_id: str | None = None,
) -> dict[str, Any] | None:
    held = {p["stock_id"] for p in state.get("slots", [])}
    used_slots = {int(p["slot"]) for p in state.get("slots", [])}
    free_slots = [i for i in range(MAX_SLOTS) if i not in used_slots]
    if not free_slots or row.stock_id in held:
        return None
    exit_d = _exit_date_from_entry(conn, full_dates, entry_date, HOLD_DAYS)
    slot = free_slots[0]
    pos = {
        "slot": slot,
        "stock_id": row.stock_id,
        "stock_name": row.stock_name,
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_d or "",
        "entry_px": float(entry_px),
        "entry_leg": entry_leg,
        "entry_minute": entry_minute,
        "variant_id": variant_id,
        "seg_last": round(row.seg_last, 4),
        "disp": round(row.disp, 4),
    }
    if exit_d is None:
        pos["exit_pending"] = True
    state.setdefault("slots", []).append(pos)
    return pos


def _expert_fill_mode(config: CVariantConfig) -> EntryFillMode | None:
    mode = config.entry_fill_mode
    if mode == "poll_px":
        return None
    return mode


def _apply_intraday_entries(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    *,
    signal_date: str,
    entry_date: str,
    shortlist: list[ScanRow],
    close: pd.DataFrame,
    bench: pd.Series,
    full_dates: list[str],
    config: CVariantConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    kbar_stats: dict[str, int],
    full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
    entry_leg: str = "C",
    ohlcv_cache: dict[tuple[str, str], tuple[KbarBar, ...]] | None = None,
) -> list[dict[str, Any]]:
    from research.backtest.rrg_mono_expert_entry import (
        ExpertEntryMode,
        detect_expert_entry_after,
    )

    minutes = _rebalance_minutes(
        interval_min=config.rebalance_interval_min,
        no_swap_before=config.no_swap_before,
    )
    confirm: dict[str, int] = {}
    confirm_ready_at: dict[str, str] = {}
    added: list[dict[str, Any]] = []
    expert_mode: ExpertEntryMode | None = _expert_fill_mode(config)  # type: ignore[assignment]
    bars_cache = ohlcv_cache if ohlcv_cache is not None else {}
    for minute in minutes:
        if len(state.get("slots", [])) >= MAX_SLOTS:
            break
        ranked = rank_shortlist(
            shortlist,
            config=config,
            conn=conn,
            close=close,
            bench=bench,
            trade_date=entry_date,
            minute=minute,
            kbar_cache=kbar_cache,
            full_rrg_cache=full_rrg_cache,
        )
        if not ranked:
            continue
        top_ids = {r.stock_id for r in ranked[:MAX_SLOTS]}
        for sid in list(confirm.keys()):
            if sid not in top_ids:
                confirm[sid] = 0
                confirm_ready_at.pop(sid, None)
        for sid in top_ids:
            if sid in {p["stock_id"] for p in state.get("slots", [])}:
                continue
            confirm[sid] = confirm.get(sid, 0) + 1
            if confirm[sid] >= config.confirm_bars and sid not in confirm_ready_at:
                confirm_ready_at[sid] = minute
        for row in ranked:
            if len(state.get("slots", [])) >= MAX_SLOTS:
                break
            sid = row.stock_id
            if sid in {p["stock_id"] for p in state.get("slots", [])}:
                continue
            if confirm.get(sid, 0) < config.confirm_bars:
                continue
            ready_at = confirm_ready_at.get(sid, minute)
            key = (sid, entry_date)
            if key not in kbar_cache:
                kbar_cache[key] = load_kbar_day_closes(conn, sid, entry_date)
            if kbar_cache[key]:
                kbar_stats["hits"] += 1
            kbar_stats["checks"] += 1
            entry_minute: str | None = minute
            if expert_mode is not None:
                if key not in bars_cache:
                    bars_cache[key] = load_kbar_day_bars(conn, sid, entry_date)
                day_bars = bars_cache[key]
                if not day_bars:
                    continue
                trig = detect_expert_entry_after(
                    expert_mode,
                    day_bars,
                    not_before_minute=ready_at,
                    at_or_before_minute=minute,
                )
                if trig is None:
                    continue
                px = float(trig.entry_px)
                entry_minute = trig.entry_minute
            else:
                px = _kbar_px(conn, sid, entry_date, minute, kbar_cache, close)
            if px is None or px <= 0:
                continue
            pos = _append_position(
                conn=conn,
                state=state,
                row=row,
                signal_date=signal_date,
                entry_date=entry_date,
                entry_px=float(px),
                full_dates=full_dates,
                entry_leg=entry_leg,
                entry_minute=entry_minute,
                variant_id=config.variant_id,
            )
            if pos:
                if expert_mode is not None:
                    pos["entry_fill_mode"] = expert_mode
                added.append(pos)
                confirm.pop(sid, None)
                confirm_ready_at.pop(sid, None)
                break
    return added


def _apply_entries_leg_b(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    signal_date: str,
    shortlist: list[ScanRow],
    *,
    close: pd.DataFrame,
    full_dates: list[str],
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    kbar_stats: dict[str, int],
    entry_leg: str = "B",
) -> list[dict[str, Any]]:
    ranked = rank_shortlist_scale(
        shortlist,
        conn=conn,
        close=close,
        trade_date=signal_date,
        minute=minute,
        kbar_cache=kbar_cache,
    )
    added: list[dict[str, Any]] = []
    for row in ranked:
        if len(state.get("slots", [])) >= MAX_SLOTS:
            break
        sid = row.stock_id
        key = (sid, signal_date)
        if key not in kbar_cache:
            kbar_cache[key] = load_kbar_day_closes(conn, sid, signal_date)
        if kbar_cache[key]:
            kbar_stats["hits"] += 1
        kbar_stats["checks"] += 1
        px = _kbar_px(conn, sid, signal_date, minute, kbar_cache, close)
        if px is None or px <= 0:
            continue
        pos = _append_position(
            conn=conn,
            state=state,
            row=row,
            signal_date=signal_date,
            entry_date=signal_date,
            entry_px=float(px),
            full_dates=full_dates,
            entry_leg=entry_leg,
            entry_minute=minute,
        )
        if pos:
            added.append(pos)
    return added


def _apply_vcp_close_entries(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    signal_date: str,
    pool: list[ScanRow],
    *,
    close: pd.DataFrame,
    full_dates: list[str],
) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    for row in vcp_close_shortlist(pool):
        if len(state.get("slots", [])) >= MAX_SLOTS:
            break
        sid = row.stock_id
        if sid in {p["stock_id"] for p in state.get("slots", [])}:
            continue
        if signal_date not in close.index or sid not in close.columns:
            continue
        px = float(close.at[signal_date, sid])
        if px <= 0:
            continue
        pos = _append_position(
            conn=conn,
            state=state,
            row=row,
            signal_date=signal_date,
            entry_date=signal_date,
            entry_px=px,
            full_dates=full_dates,
            entry_leg="D0",
            entry_minute=None,
        )
        if pos:
            added.append(pos)
    return added


def simulate_leg_c_variant(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    bench: pd.Series,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    zone_by_date: dict[str, str],
    fresh_by_date: dict[str, list[ScanRow]],
    config: CVariantConfig,
    full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {"slots": [], "history": []}
    periods: list[dict[str, Any]] = []
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    kbar_stats = {"hits": 0, "checks": 0}
    rrg_cache = full_rrg_cache if full_rrg_cache is not None else {}
    pending: list[_PendingFunnel] = []

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_with_entry_px(conn, close, pos)
            if row is None:
                continue
            row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
            periods.append(row)

        still_pending: list[_PendingFunnel] = []
        for p in pending:
            d1 = _trading_offset(full_dates, p.signal_date, 1)
            d2 = _trading_offset(full_dates, p.signal_date, 2)
            if p.stage == 0 and d1 == as_of:
                filtered = _accel_filter_shortlist(
                    p,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    full_dates=full_dates,
                    as_of=as_of,
                )
                if filtered:
                    still_pending.append(_PendingFunnel(p.signal_date, filtered, stage=1))
            elif p.stage == 1 and d2 == as_of:
                _apply_intraday_entries(
                    conn,
                    state,
                    signal_date=p.signal_date,
                    entry_date=as_of,
                    shortlist=p.shortlist,
                    close=close,
                    bench=bench,
                    full_dates=full_dates,
                    config=config,
                    kbar_cache=kbar_cache,
                    kbar_stats=kbar_stats,
                    full_rrg_cache=rrg_cache,
                )
            elif p.stage == 0 and d1 and as_of < d1:
                still_pending.append(p)
            elif p.stage == 1 and d2 and as_of < d2:
                still_pending.append(p)
        pending = still_pending

        fresh_mono = fresh_by_date.get(as_of, [])
        if config.entry_schedule == "same_day":
            _apply_intraday_entries(
                conn,
                state,
                signal_date=as_of,
                entry_date=as_of,
                shortlist=close_shortlist(fresh_mono),
                close=close,
                bench=bench,
                full_dates=full_dates,
                config=config,
                kbar_cache=kbar_cache,
                kbar_stats=kbar_stats,
                full_rrg_cache=rrg_cache,
            )
        elif fresh_mono:
            pending.append(_PendingFunnel(as_of, close_shortlist(fresh_mono), stage=0))

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_with_entry_px(conn, close, pos)
            if row:
                row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
                periods.append(row)

    summary = _summarize(periods)
    summary.update(config.to_dict())
    checks = kbar_stats["checks"]
    summary["kbar_coverage_pct"] = round(kbar_stats["hits"] / checks * 100.0, 2) if checks else 0.0
    return periods, summary


def simulate_mono_hold7_ab(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    zone_by_date: dict[str, str],
    fresh_by_date: dict[str, list[ScanRow]],
    leg: EntryLeg,
    intraday_minute: str = "10:00",
    rebalance_interval_min: int = 5,
    no_swap_before: str = "09:30",
    bench: pd.Series | None = None,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if bench is None:
        bench = load_benchmark_close(conn).reindex(close.index)
    if rs_ratio is None or rs_mom is None:
        rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)

    if leg == "A":
        periods, summary = simulate_mono_hold7(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            zone_filter=None,
            entry_price_mode="close",
        )
        summary = dict(summary)
        summary["entry_leg"] = "A"
        summary["kbar_coverage_pct"] = 0.0
        for p in periods:
            p["entry_leg"] = "A"
        return periods, summary

    state: dict[str, Any] = {"slots": [], "history": []}
    periods: list[dict[str, Any]] = []
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    kbar_stats = {"hits": 0, "checks": 0}

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_with_entry_px(conn, close, pos)
            if row is None:
                continue
            row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
            periods.append(row)

        fresh_mono = fresh_by_date.get(as_of, [])
        if leg == "B":
            _apply_entries_leg_b(
                conn,
                state,
                as_of,
                close_shortlist(fresh_mono),
                close=close,
                full_dates=full_dates,
                minute=intraday_minute,
                kbar_cache=kbar_cache,
                kbar_stats=kbar_stats,
                entry_leg="B",
            )
        else:
            cfg = CVariantConfig(
                variant_id="C",
                label=LEG_LABELS["C"],
                rebalance_interval_min=rebalance_interval_min,
                score_mode="scale",
                confirm_bars=1,
                entry_schedule="same_day",
                no_swap_before=no_swap_before,
            )
            _apply_intraday_entries(
                conn,
                state,
                signal_date=as_of,
                entry_date=as_of,
                shortlist=close_shortlist(fresh_mono),
                close=close,
                bench=bench,
                full_dates=full_dates,
                config=cfg,
                kbar_cache=kbar_cache,
                kbar_stats=kbar_stats,
                entry_leg="C",
            )

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_with_entry_px(conn, close, pos)
            if row:
                row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
                periods.append(row)

    summary = _summarize(periods)
    summary["entry_leg"] = leg
    summary["intraday_minute"] = intraday_minute if leg == "B" else None
    summary["rebalance_interval_min"] = rebalance_interval_min if leg == "C" else None
    checks = kbar_stats["checks"]
    summary["kbar_coverage_pct"] = round(kbar_stats["hits"] / checks * 100.0, 2) if checks else 0.0
    return periods, summary


def run_hold7_ab_comparison(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    intraday_minute: str = "10:00",
    rebalance_interval_min: int = 5,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    bench = load_benchmark_close(conn).reindex(close.index)

    from market_breadth_ma import build_breadth_panel

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    legs: dict[str, Any] = {}
    for leg in ("A", "B", "C"):
        periods, summary = simulate_mono_hold7_ab(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            leg=leg,  # type: ignore[arg-type]
            intraday_minute=intraday_minute,
            rebalance_interval_min=rebalance_interval_min,
            bench=bench,
        )
        legs[leg] = {
            "label": LEG_LABELS[leg],  # type: ignore[index]
            "summary": summary,
            "n_periods": summary.get("n_periods") or len(periods),
        }

    base_excess = legs["A"]["summary"].get("mean_excess_pct")
    for leg_id, payload in legs.items():
        excess = payload["summary"].get("mean_excess_pct")
        payload["delta_vs_a_pp"] = (
            round(float(excess) - float(base_excess), 4)
            if excess is not None and base_excess is not None
            else None
        )

    return {
        "date_start": date_start,
        "date_end": date_end,
        "intraday_minute": intraday_minute,
        "rebalance_interval_min": rebalance_interval_min,
        "ssg_note": "A/B/C 共用 D4 mono fresh SSG + 收盤 seg_last 前十 shortlist",
        "legs": legs,
    }


def simulate_vcp_intraday_rrg(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    zone_by_date: dict[str, str],
    pool_by_date: dict[str, list[ScanRow]],
    leg: VcpEntryLeg,
    intraday_minute: str = "10:00",
    rebalance_interval_min: int = 5,
    no_swap_before: str = "09:30",
    bench: pd.Series | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if bench is None:
        bench = load_benchmark_close(conn).reindex(close.index)

    state: dict[str, Any] = {"slots": [], "history": []}
    periods: list[dict[str, Any]] = []
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    kbar_stats = {"hits": 0, "checks": 0}

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_with_entry_px(conn, close, pos)
            if row is None:
                continue
            row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
            periods.append(row)

        pool = pool_by_date.get(as_of, [])
        if leg == "D0":
            _apply_vcp_close_entries(
                conn, state, as_of, pool, close=close, full_dates=full_dates
            )
        elif leg == "Db":
            _apply_entries_leg_b(
                conn,
                state,
                as_of,
                vcp_close_shortlist(pool),
                close=close,
                full_dates=full_dates,
                minute=intraday_minute,
                kbar_cache=kbar_cache,
                kbar_stats=kbar_stats,
                entry_leg="Db",
            )
        else:
            cfg = CVariantConfig(
                variant_id="D",
                label=VCP_LEG_LABELS["D"],
                rebalance_interval_min=rebalance_interval_min,
                score_mode="scale",
                confirm_bars=1,
                entry_schedule="same_day",
                no_swap_before=no_swap_before,
            )
            _apply_intraday_entries(
                conn,
                state,
                signal_date=as_of,
                entry_date=as_of,
                shortlist=vcp_close_shortlist(pool),
                close=close,
                bench=bench,
                full_dates=full_dates,
                config=cfg,
                kbar_cache=kbar_cache,
                kbar_stats=kbar_stats,
                entry_leg="D",
            )

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_with_entry_px(conn, close, pos)
            if row:
                row["breadth_zone_200"] = zone_by_date.get(str(row["signal_date"]), "unknown")
                periods.append(row)

    summary = _summarize(periods)
    summary["entry_leg"] = leg
    summary["intraday_minute"] = intraday_minute if leg == "Db" else None
    summary["rebalance_interval_min"] = rebalance_interval_min if leg == "D" else None
    checks = kbar_stats["checks"]
    summary["kbar_coverage_pct"] = round(kbar_stats["hits"] / checks * 100.0, 2) if checks else 0.0
    return periods, summary


def run_vcp_intraday_rrg_comparison(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    intraday_minute: str = "10:00",
    rebalance_interval_min: int = 5,
) -> dict[str, Any]:
    """H3 · 日線 VCP pivot 池 + hold7 出場 · D0/Db/D 對照。"""
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    pool_by_date = build_vcp_pivot_calendar(conn, trade_dates)
    bench = load_benchmark_close(conn).reindex(close.index)

    from market_breadth_ma import build_breadth_panel

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    pool_days = sum(1 for d in trade_dates if pool_by_date.get(d))
    pool_sizes = [len(pool_by_date.get(d, [])) for d in trade_dates if pool_by_date.get(d)]

    legs: dict[str, Any] = {}
    for leg in ("D0", "Db", "D"):
        periods, summary = simulate_vcp_intraday_rrg(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date=zone_by_date,
            pool_by_date=pool_by_date,
            leg=leg,  # type: ignore[arg-type]
            intraday_minute=intraday_minute,
            rebalance_interval_min=rebalance_interval_min,
            bench=bench,
        )
        legs[leg] = {
            "label": VCP_LEG_LABELS[leg],  # type: ignore[index]
            "summary": summary,
            "n_periods": summary.get("n_periods") or len(periods),
        }

    base_excess = legs["D0"]["summary"].get("mean_excess_pct")
    for leg_id, payload in legs.items():
        excess = payload["summary"].get("mean_excess_pct")
        payload["delta_vs_d0_pp"] = (
            round(float(excess) - float(base_excess), 4)
            if excess is not None and base_excess is not None
            else None
        )

    # 交叉對照：同日 A 腿 RRG mono fresh（若已算過可選）
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_periods, mono_summary = simulate_mono_hold7_ab(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        leg="A",
        bench=bench,
    )
    mono_excess = mono_summary.get("mean_excess_pct")

    return {
        "date_start": date_start,
        "date_end": date_end,
        "intraday_minute": intraday_minute,
        "rebalance_interval_min": rebalance_interval_min,
        "hypothesis": "H3 · daily VCP pivot pool + intraday RRG seg_last rank · hold7 exit",
        "vcp_gate": {
            "min_composite": VCP_PIVOT_GATE["min_composite"],
            "execution_states": list(VCP_PIVOT_GATE["execution_states"]),
            "min_dist_pivot_pct": VCP_PIVOT_GATE["min_dist_pivot_pct"],
            "max_dist_pivot_pct": VCP_PIVOT_GATE["max_dist_pivot_pct"],
        },
        "pool_stats": {
            "days_with_candidates": pool_days,
            "mean_pool_size": round(sum(pool_sizes) / len(pool_sizes), 2) if pool_sizes else 0.0,
            "max_pool_size": max(pool_sizes) if pool_sizes else 0,
        },
        "reference_rrg_mono_a": {
            "n_periods": mono_summary.get("n_periods") or len(mono_periods),
            "mean_excess_pct": mono_excess,
        },
        "ssg_note": "D0/Db/D 共用當日 VCP pivot gate 池（top10 by composite）· 出場 hold7",
        "legs": legs,
    }


def run_c_variant_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[CVariantConfig] | None = None,
    baseline_variant_id: str = "C0",
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)

    from market_breadth_ma import build_breadth_panel

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    grid = configs or DEFAULT_C_SWEEP
    summaries: list[dict[str, Any]] = []
    shared_full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] = {}
    for cfg in grid:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        periods, summary = simulate_leg_c_variant(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            config=cfg,
            full_rrg_cache=shared_full_rrg_cache,
        )
        summaries.append({**summary, "n_periods": summary.get("n_periods") or len(periods)})
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"mean_excess={summary.get('mean_excess_pct')}%",
            flush=True,
        )

    base_excess = None
    for s in summaries:
        if s.get("variant_id") == baseline_variant_id:
            base_excess = s.get("mean_excess_pct")
            break
    if base_excess is None and summaries:
        base_excess = summaries[0].get("mean_excess_pct")

    for s in summaries:
        excess = s.get("mean_excess_pct")
        s["delta_vs_baseline_pp"] = (
            round(float(excess) - float(base_excess), 4)
            if excess is not None and base_excess is not None
            else None
        )

    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )

    return {
        "date_start": date_start,
        "date_end": date_end,
        "ssg_note": "全部變體共用 D4 mono fresh SSG + 收盤 seg_last 前十 shortlist",
        "baseline_variant_id": baseline_variant_id,
        "summaries": summaries,
        "best": ranked[0] if ranked else None,
    }


def summarize_periods_by_zone(periods: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from market_breadth_ma import BREADTH_ZONES_ORDER

    buckets: dict[str, list[dict[str, Any]]] = {z: [] for z in BREADTH_ZONES_ORDER}
    for p in periods:
        z = str(p.get("breadth_zone_200") or "unknown")
        if z in buckets:
            buckets[z].append(p)

    out: dict[str, dict[str, Any]] = {}
    for zone, legs in buckets.items():
        n = len(legs)
        if n == 0:
            out[zone] = {"n": 0, "mean_excess_pct": None, "win_rate_vs_bench_pct": None}
            continue
        wins = sum(1 for p in legs if p.get("beat_bench"))
        out[zone] = {
            "n": n,
            "mean_excess_pct": round(sum(p["excess_pct"] for p in legs) / n, 4),
            "win_rate_vs_bench_pct": round(wins / n * 100.0, 2),
        }
    return out


def audit_shortlist_kbar_coverage(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    fresh_by_date: dict[str, list[ScanRow]],
) -> dict[str, Any]:
    from stock_db.kbar import kbar_day_coverage, kbar_day_has_data

    checks = 0
    hits = 0
    finmind_hits = 0
    yahoo_hits = 0
    by_date: list[dict[str, Any]] = []

    for d in trade_dates:
        shortlist = close_shortlist(fresh_by_date.get(d, []))
        if not shortlist:
            continue
        day_checks = 0
        day_hits = 0
        for row in shortlist:
            checks += 1
            day_checks += 1
            sid = row.stock_id
            if kbar_day_has_data(conn, sid, d):
                hits += 1
                day_hits += 1
            if kbar_day_coverage(conn, sid, d, source="finmind") >= 4:
                finmind_hits += 1
            if kbar_day_coverage(conn, sid, d, source="yahoo") >= 4:
                yahoo_hits += 1
        by_date.append(
            {
                "trade_date": d,
                "shortlist_n": len(shortlist),
                "kbar_hits": day_hits,
                "kbar_pct": round(day_hits / day_checks * 100.0, 2) if day_checks else 0.0,
            }
        )

    return {
        "stock_days": checks,
        "kbar_hits": hits,
        "coverage_pct": round(hits / checks * 100.0, 2) if checks else 0.0,
        "finmind_stock_days": finmind_hits,
        "yahoo_stock_days": yahoo_hits,
        "by_date": by_date,
        "production_note": (
            "本地 stock_kbar_1m · 非 daily_sync 自動寫入；"
            "C4 實盤需排程 backfill（yahoo 1m 約 30 日）或 FinMind sponsor"
        ),
    }


def run_c4_validation(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
) -> dict[str, Any]:
    """近窗 C4 vs hold7 A · 廣度分桶 · shortlist kbar 覆蓋稽核。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)

    from market_breadth_ma import BREADTH_ZONE_DISPLAY, build_breadth_panel

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    c0 = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    c4 = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C4")

    variants: dict[str, Any] = {}
    a_periods, a_summary = simulate_mono_hold7_ab(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        leg="A",
        bench=bench,
    )
    variants["A"] = {
        "label": LEG_LABELS["A"],
        "summary": a_summary,
        "by_zone": summarize_periods_by_zone(a_periods),
    }

    for cfg in (c0, c4):
        periods, summary = simulate_leg_c_variant(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            config=cfg,
        )
        variants[cfg.variant_id] = {
            "label": cfg.label,
            "summary": summary,
            "by_zone": summarize_periods_by_zone(periods),
        }

    a_excess = variants["A"]["summary"].get("mean_excess_pct")
    for vid, payload in variants.items():
        if vid == "A":
            continue
        ex = payload["summary"].get("mean_excess_pct")
        payload["delta_vs_a_pp"] = (
            round(float(ex) - float(a_excess), 4) if ex is not None and a_excess is not None else None
        )

    zone_compare: dict[str, dict[str, Any]] = {}
    for zone in ("strong", "overbought"):
        zone_compare[zone] = {
            "display": BREADTH_ZONE_DISPLAY.get(zone, zone),
            "A": variants["A"]["by_zone"].get(zone),
            "C0": variants["C0"]["by_zone"].get(zone),
            "C4": variants["C4"]["by_zone"].get(zone),
        }
        a_z = zone_compare[zone]["A"].get("mean_excess_pct")
        c4_z = zone_compare[zone]["C4"].get("mean_excess_pct")
        zone_compare[zone]["c4_vs_a_pp"] = (
            round(float(c4_z) - float(a_z), 4)
            if c4_z is not None and a_z is not None
            else None
        )
        zone_compare[zone]["pass_0p5pp"] = (
            zone_compare[zone]["c4_vs_a_pp"] is not None
            and zone_compare[zone]["c4_vs_a_pp"] >= 0.5
        )

    kbar_audit = audit_shortlist_kbar_coverage(
        conn, trade_dates=trade_dates, fresh_by_date=fresh_by_date
    )

    overbought_pass = zone_compare["overbought"].get("pass_0p5pp") is True
    strong_n = zone_compare["strong"]["C4"].get("n") or 0
    strong_pass = (
        zone_compare["strong"].get("pass_0p5pp") is True if strong_n > 0 else None
    )

    return {
        "date_start": date_start,
        "date_end": date_end,
        "variants": variants,
        "zone_compare": zone_compare,
        "kbar_audit": kbar_audit,
        "adoption_gate_0p5pp_overbought": overbought_pass,
        "adoption_gate_0p5pp_strong": strong_pass,
        "adoption_gate_0p5pp_both": (
            overbought_pass and strong_pass is True
        ),
    }
