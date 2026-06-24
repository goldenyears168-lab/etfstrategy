"""RRG mono + seg_last + 3-slot hold7 backtest segmented by 200MA breadth zone."""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls
from typing import Any, Literal

import pandas as pd

from flow_returns import stock_open, trading_dates_after
from .copytrade_backtest import bench_return_entry_to_exit
from .finpilot_local_backtest import load_price_panels, summarize_periods
from market_benchmark import load_benchmark_close
from market_breadth_ma import BREADTH_ZONE_DISPLAY, BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel
from project_config import DEFAULT_ETF_CODES
from rrg_mono_daily_brief import (
    HOLD_DAYS,
    LOOKBACK,
    MAX_SLOTS,
    TOP_N,
    ScanRow,
    _apply_entries,
    _backfill_exit_dates,
    _expire_slots,
    _exit_date_from_entry,
    _feat,
    _fresh_mono,
    _mono_tier2,
    _mono_up_gate,
    _mono_up_simple,
    _tier2,
    _tier2_gate,
)
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect, load_etf_constituent_watchlist

BreadthZoneFilter = Literal["oversold", "weak", "neutral", "strong", "overbought"] | None
EntryPriceMode = Literal["close", "next_open"]
EmptyFreshPolicy = Literal[
    "baseline",
    "prev_day",
    "no_fresh",
    "no_mono",
    "no_mono_improving",
    "no_leading",
    "mono_up_plain",
]
Tier2EndQ = Literal["leading", "improving"]

WEEKDAY_ZH: dict[int, str] = {
    0: "週一 Mon",
    1: "週二 Tue",
    2: "週三 Wed",
    3: "週四 Thu",
    4: "週五 Fri",
}


def build_fresh_mono_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback: int = LOOKBACK,
) -> dict[str, list[ScanRow]]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = max(2, int(lookback))

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        fresh: list[ScanRow] = []
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if not _mono_tier2(f):
                continue
            if not _fresh_mono(rs_ratio, rs_mom, full_dates, si, sid, lb=lb):
                continue
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            fresh.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=True,
                    mono=True,
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        fresh.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = fresh
    return out


def build_mono_all_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback: int = LOOKBACK,
) -> dict[str, list[ScanRow]]:
    """當日 mono_tier2 全池（含非 fresh）· 依 seg_last 排序。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = max(2, int(lookback))

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        pool: list[ScanRow] = []
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if not _mono_tier2(f):
                continue
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            pool.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=False,
                    mono=True,
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        pool.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = pool
    return out


def build_tier2_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback: int = LOOKBACK,
    end_q: Tier2EndQ = "leading",
) -> dict[str, list[ScanRow]]:
    """當日 tier2 全池（up_right + end_q + disp∈[1,2)，不要求 mono_up）· 依 seg_last 排序。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = max(2, int(lookback))

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        pool: list[ScanRow] = []
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if not _tier2_gate(f, end_q=end_q):
                continue
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            pool.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=False,
                    mono=bool(f and f.get("mono_up")),
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        pool.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = pool
    return out


def build_mono_up_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback: int = LOOKBACK,
    require_disp: bool = True,
) -> dict[str, list[ScanRow]]:
    """up_right + mono_up（可選 disp∈[1,2)）· 不要求 leading · 依 seg_last 排序。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = max(2, int(lookback))
    gate = _mono_up_gate if require_disp else _mono_up_simple

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        pool: list[ScanRow] = []
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if not gate(f):
                continue
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            pool.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=False,
                    mono=True,
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        pool.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = pool
    return out


def build_mono_up_fresh_calendar(
    conn: sqlite3.Connection,
    trade_dates: list[str],
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback: int = LOOKBACK,
) -> dict[str, list[ScanRow]]:
    """up_right + mono_up + disp∈[1,2) · 今日新進（昨日未過）· 不要求 leading。"""
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=20)
    daily_pct = close.pct_change(fill_method=None) * 100.0
    full_dates = close.index.astype(str).tolist()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]
    date_set = set(trade_dates)
    lb = max(2, int(lookback))

    out: dict[str, list[ScanRow]] = {d: [] for d in trade_dates}
    for si, as_of in enumerate(full_dates):
        if as_of not in date_set or si < lb:
            continue
        fresh: list[ScanRow] = []
        for sid in universe:
            f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
            if not _mono_up_gate(f):
                continue
            prev = _feat(rs_ratio, rs_mom, full_dates, si - 1, sid, lb=lb)
            if _mono_up_gate(prev):
                continue
            pct = float(daily_pct.at[as_of, sid]) if sid in daily_pct.columns else None
            if pct != pct:
                pct = None
            fresh.append(
                ScanRow(
                    stock_id=sid,
                    stock_name=name_map.get(sid, ""),
                    fresh=True,
                    mono=True,
                    seg_last=float(f["seg_last"]),
                    disp=float(f["disp"]),
                    segs=[float(x) for x in f["segs"]],
                    quadrants=[q or "?" for q in f["quadrants"]],
                    rs_ratio=float(f["rs_ratio"]),
                    rs_momentum=float(f["rs_momentum"]),
                    daily_pct=pct,
                )
            )
        fresh.sort(key=lambda r: (-r.seg_last, r.stock_id))
        out[as_of] = fresh
    return out


def _last_nonempty_fresh(
    fresh_by_date: dict[str, list[ScanRow]],
    trade_dates: list[str],
    as_of: str,
) -> list[ScanRow]:
    """as_of 之前最近一個有 fresh mono 的交易日名單。"""
    try:
        idx = trade_dates.index(as_of)
    except ValueError:
        return []
    for j in range(idx - 1, -1, -1):
        prev = fresh_by_date.get(trade_dates[j], [])
        if prev:
            return prev
    return []


def _resolve_entry_pool(
    as_of: str,
    *,
    fresh_mono: list[ScanRow],
    policy: EmptyFreshPolicy,
    fresh_by_date: dict[str, list[ScanRow]],
    trade_dates: list[str],
    mono_by_date: dict[str, list[ScanRow]] | None = None,
    tier2_by_date: dict[str, list[ScanRow]] | None = None,
    tier2_improving_by_date: dict[str, list[ScanRow]] | None = None,
    mono_up_by_date: dict[str, list[ScanRow]] | None = None,
    mono_up_plain_by_date: dict[str, list[ScanRow]] | None = None,
) -> tuple[list[ScanRow], str]:
    """回傳 (候選池, pool_source)。"""
    if fresh_mono:
        return fresh_mono, "fresh"
    if policy == "baseline":
        return [], "empty"
    if policy == "prev_day":
        prev = _last_nonempty_fresh(fresh_by_date, trade_dates, as_of)
        return prev, "prev_day" if prev else "empty"
    if policy == "no_fresh":
        pool = (mono_by_date or {}).get(as_of, [])
        return pool, "mono_all" if pool else "empty"
    if policy == "no_mono":
        pool = (tier2_by_date or {}).get(as_of, [])
        return pool, "tier2_leading" if pool else "empty"
    if policy == "no_mono_improving":
        pool = (tier2_improving_by_date or {}).get(as_of, [])
        return pool, "tier2_improving" if pool else "empty"
    if policy == "no_leading":
        pool = (mono_up_by_date or {}).get(as_of, [])
        return pool, "mono_up" if pool else "empty"
    if policy == "mono_up_plain":
        pool = (mono_up_plain_by_date or {}).get(as_of, [])
        return pool, "mono_up_plain" if pool else "empty"
    return [], "empty"


def simulate_mono_hold7_empty_policy(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    fresh_by_date: dict[str, list[ScanRow]],
    policy: EmptyFreshPolicy,
    mono_by_date: dict[str, list[ScanRow]] | None = None,
    tier2_by_date: dict[str, list[ScanRow]] | None = None,
    tier2_improving_by_date: dict[str, list[ScanRow]] | None = None,
    mono_up_by_date: dict[str, list[ScanRow]] | None = None,
    mono_up_plain_by_date: dict[str, list[ScanRow]] | None = None,
    entry_price_mode: EntryPriceMode = "close",
) -> tuple[list[dict], dict]:
    state: dict = {"slots": [], "history": []}
    periods: list[dict] = []
    zero_fresh_days = 0
    fallback_entries = 0

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_trade(conn, close, pos, entry_price_mode=entry_price_mode)
            if row is None:
                continue
            periods.append(row)

        fresh_mono = fresh_by_date.get(as_of, [])
        if not fresh_mono:
            zero_fresh_days += 1

        pool, source = _resolve_entry_pool(
            as_of,
            fresh_mono=fresh_mono,
            policy=policy,
            fresh_by_date=fresh_by_date,
            trade_dates=trade_dates,
            mono_by_date=mono_by_date,
            tier2_by_date=tier2_by_date,
            tier2_improving_by_date=tier2_improving_by_date,
            mono_up_by_date=mono_up_by_date,
            mono_up_plain_by_date=mono_up_plain_by_date,
        )
        before = len(state.get("slots", []))
        _apply_entries_timed(
            conn,
            state,
            as_of,
            pool,
            full_dates=full_dates,
            entry_price_mode=entry_price_mode,
        )
        if source != "fresh" and source != "empty" and len(state.get("slots", [])) > before:
            fallback_entries += len(state.get("slots", [])) - before

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_trade(conn, close, pos, entry_price_mode=entry_price_mode)
            if row:
                periods.append(row)

    summary = _summarize(periods)
    summary["empty_fresh_policy"] = policy
    summary["zero_fresh_days"] = zero_fresh_days
    summary["fallback_entries"] = fallback_entries
    return periods, summary


def run_empty_fresh_fallback_comparison(
    conn: sqlite3.Connection | None = None,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-12-31",
) -> dict:
    """mono fresh=0 時比較：維持空槽 / 前日名單 / 放寬 mono / 放寬 fresh。"""
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_all_calendar(conn, trade_dates)
    tier2_by_date = build_tier2_calendar(conn, trade_dates, end_q="leading")
    tier2_improving_by_date = build_tier2_calendar(conn, trade_dates, end_q="improving")
    mono_up_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=True)
    mono_up_plain_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=False)

    policies: tuple[tuple[EmptyFreshPolicy, str], ...] = (
        ("baseline", "fresh 為零 → 空槽（現行）"),
        ("prev_day", "fresh 為零 → 前一日 fresh 名單"),
        ("no_fresh", "fresh 為零 → mono tier2 leading（不要求 fresh）"),
        ("no_leading", "fresh 為零 → mono_up + disp∈[1,2)"),
        ("mono_up_plain", "fresh 為零 → up_right + mono_up（無 disp 門檻）"),
        ("no_mono", "fresh 為零 → tier2 leading（不要求 mono_up）"),
        ("no_mono_improving", "fresh 為零 → tier2 improving（不要求 mono_up）"),
    )
    by_policy: dict[str, dict] = {}
    for policy, label in policies:
        periods, summary = simulate_mono_hold7_empty_policy(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            fresh_by_date=fresh_by_date,
            policy=policy,
            mono_by_date=mono_by_date,
            tier2_by_date=tier2_by_date,
            tier2_improving_by_date=tier2_improving_by_date,
            mono_up_by_date=mono_up_by_date,
            mono_up_plain_by_date=mono_up_plain_by_date,
        )
        by_policy[policy] = {
            "label": label,
            "summary": summary,
            "periods": periods,
        }

    fresh_counts = [len(fresh_by_date.get(d, [])) for d in trade_dates]
    pool_sizes = {
        "fresh_mean": round(sum(fresh_counts) / len(fresh_counts), 2) if fresh_counts else 0,
        "mono_mean": round(
            sum(len(mono_by_date.get(d, [])) for d in trade_dates) / len(trade_dates), 2
        ),
        "tier2_leading_mean": round(
            sum(len(tier2_by_date.get(d, [])) for d in trade_dates) / len(trade_dates), 2
        ),
        "tier2_improving_mean": round(
            sum(len(tier2_improving_by_date.get(d, [])) for d in trade_dates)
            / len(trade_dates),
            2,
        ),
        "mono_up_mean": round(
            sum(len(mono_up_by_date.get(d, [])) for d in trade_dates) / len(trade_dates), 2
        ),
        "mono_up_plain_mean": round(
            sum(len(mono_up_plain_by_date.get(d, [])) for d in trade_dates)
            / len(trade_dates),
            2,
        ),
        "zero_fresh_days": sum(1 for c in fresh_counts if c == 0),
        "n_trade_days": len(trade_dates),
    }

    if own:
        conn.close()

    return {
        "date_start": date_start,
        "date_end": date_end,
        "pool_sizes": pool_sizes,
        "by_policy": by_policy,
    }


def render_empty_fresh_fallback_markdown(results: dict) -> str:
    ds, de = results["date_start"], results["date_end"]
    ps = results["pool_sizes"]
    lines = [
        f"# RRG mono fresh 為零 · 候選池 fallback 回測 · {ds}～{de}",
        "",
        "策略基礎：**3 槽 hold7 · D4 收盤進場 · 依 seg_last 排序**",
        "",
        "## mono fresh 是什麼？",
        "",
        "在 4 日 RRG 軌跡窗（lookback=4）上，候選須同時滿足：",
        "",
        "| 條件 | 說明 |",
        "|------|------|",
        "| **tier2** | 軌跡趨勢 `up_right` · 終點象限 **leading** · 位移 `disp` ∈ [1, 2) |",
        "| **mono（單軌）** | 3 段軌跡長度嚴格遞增（`seg[1] > seg[0] > …`） |",
        "| **fresh** | **今日**通過 mono tier2，且**昨日**未通過 mono tier2（新進單軌） |",
        "",
        f"樣本期間：**{ps['n_trade_days']}** 個交易日 · fresh=0 共 **{ps['zero_fresh_days']}** 天"
        f"（{round(ps['zero_fresh_days'] / ps['n_trade_days'] * 100, 1)}%）",
        "",
        f"日均候選數：fresh **{ps['fresh_mean']}** · mono **{ps['mono_mean']}** · "
        f"tier2 leading **{ps['tier2_leading_mean']}** · tier2 improving **{ps['tier2_improving_mean']}** · "
        f"mono_up+disp **{ps['mono_up_mean']}** · mono_up plain **{ps['mono_up_plain_mean']}**",
        "",
        "## 比較方案（僅 fresh=0 日啟用 fallback）",
        "",
        "| 方案 | fresh>0 | fresh=0 |",
        "|------|---------|---------|",
        "| **baseline** | 當日 fresh | 空槽不補 |",
        "| **prev_day** | 當日 fresh | 最近有訊號日的 fresh 名單 |",
        "| **no_fresh** | 當日 fresh | mono tier2 **leading**（含非 fresh） |",
        "| **no_leading** | 當日 fresh | mono_up + **disp∈[1,2)** |",
        "| **mono_up_plain** | 當日 fresh | **up_right + mono_up**（無 disp） |",
        "| **no_mono** | 當日 fresh | tier2 **leading**（不要求 mono_up） |",
        "| **no_mono_improving** | 當日 fresh | tier2 **improving**（不要求 mono_up） |",
        "",
        "## 回測結果",
        "",
        "| 方案 | 成交筆數 | fallback 進場 | 勝率 vs 基準 | 均報酬 | 均超額 | 累計超額 |",
        "|------|---------|--------------|-------------|--------|--------|---------|",
    ]
    ranked = sorted(
        results["by_policy"].items(),
        key=lambda x: x[1]["summary"].get("mean_excess_pct") or -9999,
        reverse=True,
    )
    for policy, data in ranked:
        s = data["summary"]
        lines.append(
            f"| **{policy}** · {data['label']} | {s.get('n_periods', 0)} | "
            f"{s.get('fallback_entries', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_return_pct', '—')}% | "
            f"{s.get('mean_excess_pct', '—')}% | "
            f"{s.get('total_excess_pct', '—')}% |"
        )

    best_policy, best = ranked[0]
    bs = best["summary"]
    lines.extend(
        [
            "",
            f"**最佳均超額**：`{best_policy}` — {best['label']}"
            f"（均超額 {bs.get('mean_excess_pct', '—')}% · n={bs.get('n_periods', 0)}）",
            "",
            "## 解讀備註",
            "",
            "- **baseline** 筆數較少：fresh=0 日空槽無新進場，資金利用率低但訊號品質最高。",
            "- **prev_day** 沿用過去名單，訊號已非 PIT fresh，可能買在加速末段。",
            "- **no_fresh** 放寬為當日 mono 全池，候選最多但含「已持續 mono 數日」標的。",
            "- **no_leading** 保留 **mono_up** 但拿掉 leading 終點；介於 mono tier2 與 tier2 leading 之間。",
            "- **no_mono / no_mono_improving** 拿掉 mono_up，只保留 tier2 軌跡條件。",
            "",
            "---",
            "模組：`rrg_mono_backtest.run_empty_fresh_fallback_comparison`",
        ]
    )
    return "\n".join(lines) + "\n"


def _entry_date_for_signal(
    conn: sqlite3.Connection,
    signal_date: str,
    *,
    entry_price_mode: EntryPriceMode,
) -> str | None:
    if entry_price_mode == "close":
        return signal_date
    nxt = trading_dates_after(conn, signal_date, count=1)
    return nxt[0] if nxt else None


def _settle_trade(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    pos: dict,
    *,
    entry_price_mode: EntryPriceMode = "close",
) -> dict | None:
    signal_date = str(pos.get("signal_date") or pos["entry_date"])
    entry = str(pos["entry_date"])
    exit_d = str(pos.get("exit_date") or "")
    sid = str(pos["stock_id"])
    if not exit_d or sid not in close.columns or exit_d not in close.index:
        return None
    if entry_price_mode == "close":
        if entry not in close.index:
            return None
        c0 = float(close.at[entry, sid])
        bench_mode = "close"
    else:
        c0 = stock_open(conn, sid, entry)
        bench_mode = "open"
    c1 = float(close.at[exit_d, sid])
    if c0 is None or c0 <= 0 or c1 != c1 or c0 != c0:
        return None
    ret = (c1 / c0 - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode=bench_mode)
    if bench is None:
        return None
    wd = date_cls.fromisoformat(signal_date).weekday()
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": signal_date,
        "signal_weekday": wd,
        "signal_weekday_zh": WEEKDAY_ZH.get(wd, str(wd)),
        "entry_date": entry,
        "exit_date": exit_d,
        "entry_price_mode": entry_price_mode,
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "seg_last": pos.get("seg_last"),
        "slot": pos.get("slot"),
    }


def _close_trade(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    pos: dict,
) -> dict | None:
    return _settle_trade(conn, close, pos, entry_price_mode="close")


def _summarize(periods: list[dict]) -> dict:
    summary = summarize_periods(periods)
    if periods:
        summary["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / len(periods), 4)
        summary["total_return_pct"] = round(sum(p["return_pct"] for p in periods), 4)
        summary["total_excess_pct"] = round(sum(p["excess_pct"] for p in periods), 4)
    else:
        summary["mean_excess_pct"] = None
        summary["total_return_pct"] = None
        summary["total_excess_pct"] = None
    return summary


def _apply_entries_timed(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    signal_date: str,
    fresh_mono: list[ScanRow],
    *,
    full_dates: list[str] | None = None,
    entry_price_mode: EntryPriceMode = "close",
) -> list[dict[str, Any]]:
    """空槽依 seg_last 填入 fresh 訊號；可選隔日開盤進場。"""
    entry_date = _entry_date_for_signal(conn, signal_date, entry_price_mode=entry_price_mode)
    if not entry_date:
        return []

    held = {p["stock_id"] for p in state.get("slots", [])}
    used_slots = {int(p["slot"]) for p in state.get("slots", [])}
    free_slots = [i for i in range(MAX_SLOTS) if i not in used_slots]
    added: list[dict[str, Any]] = []

    for row in fresh_mono[:TOP_N]:
        if not free_slots:
            break
        if row.stock_id in held:
            continue
        exit_d = _exit_date_from_entry(conn, full_dates or [], entry_date, HOLD_DAYS)
        slot = free_slots.pop(0)
        pos = {
            "slot": slot,
            "stock_id": row.stock_id,
            "stock_name": row.stock_name,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_d or "",
            "seg_last": round(row.seg_last, 4),
            "disp": round(row.disp, 4),
        }
        if exit_d is None:
            pos["exit_pending"] = True
        state.setdefault("slots", []).append(pos)
        held.add(row.stock_id)
        added.append(pos)
    return added


def simulate_mono_hold7(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    zone_by_date: dict[str, str],
    fresh_by_date: dict[str, list[ScanRow]],
    zone_filter: BreadthZoneFilter = None,
    entry_price_mode: EntryPriceMode = "close",
) -> tuple[list[dict], dict]:
    state: dict = {"slots": [], "history": []}
    periods: list[dict] = []

    for as_of in trade_dates:
        expired = _expire_slots(state, as_of)
        _backfill_exit_dates(conn, state, full_dates)
        for pos in expired:
            row = _settle_trade(conn, close, pos, entry_price_mode=entry_price_mode)
            if row is None:
                continue
            sig = row["signal_date"]
            row["breadth_zone_200"] = zone_by_date.get(sig, "unknown")
            periods.append(row)

        if zone_filter is not None and zone_by_date.get(as_of) != zone_filter:
            continue

        fresh_mono = fresh_by_date.get(as_of, [])
        _apply_entries_timed(
            conn,
            state,
            as_of,
            fresh_mono,
            full_dates=full_dates,
            entry_price_mode=entry_price_mode,
        )

    for pos in list(state.get("slots", [])):
        exit_d = str(pos.get("exit_date") or "")
        if exit_d and exit_d <= trade_dates[-1]:
            row = _settle_trade(conn, close, pos, entry_price_mode=entry_price_mode)
            if row:
                sig = row["signal_date"]
                row["breadth_zone_200"] = zone_by_date.get(sig, "unknown")
                periods.append(row)

    summary = _summarize(periods)
    summary["zone_filter"] = zone_filter
    summary["entry_price_mode"] = entry_price_mode
    return periods, summary


def run_breadth_zone_comparison(
    conn: sqlite3.Connection | None = None,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-12-31",
) -> dict:
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)

    results: dict = {
        "date_start": date_start,
        "date_end": date_end,
        "by_zone": {},
        "pooled_by_entry_zone": {},
    }

    for zone in BREADTH_ZONES_ORDER:
        periods, summary = simulate_mono_hold7(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            zone_filter=zone,
        )
        results["by_zone"][zone] = {
            "summary": summary,
            "periods": periods,
            "display": BREADTH_ZONE_DISPLAY[zone],
            "zh": BREADTH_ZONE_ZH[zone],
        }

    pooled_periods, pooled_summary = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        zone_filter=None,
    )
    results["pooled_all"] = {"summary": pooled_summary, "periods": pooled_periods}

    buckets: dict[str, list[dict]] = {z: [] for z in BREADTH_ZONES_ORDER}
    for p in pooled_periods:
        z = p.get("breadth_zone_200")
        if z in buckets:
            buckets[z].append(p)
    for zone, sub in buckets.items():
        results["pooled_by_entry_zone"][zone] = _summarize(sub)

    if own:
        conn.close()
    return results


def _weekday_buckets(periods: list[dict]) -> dict[int, list[dict]]:
    buckets: dict[int, list[dict]] = {i: [] for i in range(5)}
    for p in periods:
        wd = p.get("signal_weekday")
        if isinstance(wd, int) and wd in buckets:
            buckets[wd].append(p)
    return buckets


def run_execution_timing_comparison(
    conn: sqlite3.Connection | None = None,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-12-31",
) -> dict:
    """Compare signal-day close vs next-day open entry for RRG mono hold7."""
    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)

    modes: tuple[tuple[EntryPriceMode, str], ...] = (
        ("close", "訊號日收盤（D4 close）"),
        ("next_open", "隔日開盤（T+1 open）"),
    )
    by_mode: dict[str, dict] = {}
    for mode, label in modes:
        periods, summary = simulate_mono_hold7(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date={},
            fresh_by_date=fresh_by_date,
            zone_filter=None,
            entry_price_mode=mode,
        )
        wd_summary = {wd: _summarize(sub) for wd, sub in _weekday_buckets(periods).items()}
        fri_only = [p for p in periods if p.get("signal_weekday") == 4]
        by_mode[mode] = {
            "label": label,
            "summary": summary,
            "periods": periods,
            "by_weekday": wd_summary,
            "friday_signals": _summarize(fri_only),
            "friday_n": len(fri_only),
        }

    if own:
        conn.close()

    return {
        "date_start": date_start,
        "date_end": date_end,
        "by_mode": by_mode,
    }


def render_execution_timing_markdown(results: dict) -> str:
    ds, de = results["date_start"], results["date_end"]
    close = results["by_mode"]["close"]
    nxt = results["by_mode"]["next_open"]
    lines = [
        f"# RRG mono hold7 · 進場時點比較 · {ds}～{de}",
        "",
        "策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**",
        "",
        "訊號一律以 **D4 收盤 RRG 軌跡** 判定（PIT：僅用 `date ≤ T` 資料）。",
        "比較兩種**可執行**進場假設：",
        "",
        "| 模式 | 進場 | 出場 | 典型週末情境 |",
        "|------|------|------|-------------|",
        "| **close** | 訊號日 T 收盤 | T+7 收盤 | 週五訊號 → 週五收盤買 |",
        "| **next_open** | T+1 開盤 | entry+7 收盤 | 週五訊號 → **週一開盤**買 |",
        "",
        "> 週末 gap：next_open 不會用週末資料污染訊號，但週一開盤價已反映週末消息；",
        "> 若 close 優於 next_open，代表「當日收盤立即執行」有 edge，而非資料 lookahead。",
        "",
        "## 全樣本對照",
        "",
        "| 進場模式 | 成交筆數 | 勝率 vs IX0001 | 均報酬 | 均超額 | 累計超額 |",
        "|---------|---------|---------------|--------|--------|---------|",
    ]
    for mode_key in ("close", "next_open"):
        s = results["by_mode"][mode_key]["summary"]
        label = results["by_mode"][mode_key]["label"]
        lines.append(
            f"| **{label}** | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_return_pct', '—')}% | "
            f"{s.get('mean_excess_pct', '—')}% | "
            f"{s.get('total_excess_pct', '—')}% |"
        )

    delta_ex = None
    c_ex = close["summary"].get("mean_excess_pct")
    n_ex = nxt["summary"].get("mean_excess_pct")
    if c_ex is not None and n_ex is not None:
        delta_ex = round(c_ex - n_ex, 4)
    lines.extend(
        [
            "",
            f"**close − next_open 均超額差**：{delta_ex if delta_ex is not None else '—'} pp",
            "",
            "## 僅週五訊號（週五 close vs 週一 open）",
            "",
            "| 進場模式 | n | 勝率 vs 基準 | 均超額 |",
            "|---------|---|-------------|--------|",
        ]
    )
    for mode_key in ("close", "next_open"):
        fs = results["by_mode"][mode_key]["friday_signals"]
        label = results["by_mode"][mode_key]["label"]
        lines.append(
            f"| {label} | {results['by_mode'][mode_key]['friday_n']} | "
            f"{fs.get('win_rate_vs_bench_pct', '—')}% | "
            f"{fs.get('mean_excess_pct', '—')}% |"
        )

    lines.extend(
        [
            "",
            "## 依訊號日星期分組（close 進場）",
            "",
            "| 訊號日 | n | 勝率 vs 基準 | 均超額 |",
            "|--------|---|-------------|--------|",
        ]
    )
    wd_close = close["by_weekday"]
    ranked_wd = sorted(
        range(5),
        key=lambda wd: wd_close.get(wd, {}).get("mean_excess_pct") or -9999,
        reverse=True,
    )
    for wd in ranked_wd:
        s = wd_close.get(wd, {})
        lines.append(
            f"| {WEEKDAY_ZH[wd]} | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_excess_pct', '—')}% |"
        )

    best_wd = ranked_wd[0] if ranked_wd else None
    lines.extend(
        [
            "",
            "## 依訊號日星期分組（next_open 進場）",
            "",
            "| 訊號日 | n | 勝率 vs 基準 | 均超額 |",
            "|--------|---|-------------|--------|",
        ]
    )
    wd_open = nxt["by_weekday"]
    ranked_wd_o = sorted(
        range(5),
        key=lambda wd: wd_open.get(wd, {}).get("mean_excess_pct") or -9999,
        reverse=True,
    )
    for wd in ranked_wd_o:
        s = wd_open.get(wd, {})
        lines.append(
            f"| {WEEKDAY_ZH[wd]} | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_excess_pct', '—')}% |"
        )

    lines.extend(["", "## 解讀備註", ""])
    if best_wd is not None:
        bs = wd_close.get(best_wd, {})
        lines.append(
            f"- close 模式下最佳訊號日：**{WEEKDAY_ZH[best_wd]}**"
            f"（均超額 {bs.get('mean_excess_pct', '—')}% · n={bs.get('n_periods', 0)}）"
        )
    if delta_ex is not None:
        if delta_ex > 0.5:
            lines.append(
                f"- close 均超額比 next_open 高 **{delta_ex} pp** → "
                "延遲到隔日開盤會**侵蝕** edge（週末 gap / 隔日跳空）"
            )
        elif delta_ex < -0.5:
            lines.append(
                f"- next_open 均超額比 close 高 **{-delta_ex} pp** → "
                "隔日開盤反而更好（close 當日可能有過度反應）"
            )
        else:
            lines.append(
                f"- 兩種進場均超額差距僅 {delta_ex} pp → "
                "執行時點對績效影響不大"
            )
    lines.extend(
        [
            "",
            "---",
            "模組：`rrg_mono_backtest.run_execution_timing_comparison`",
        ]
    )
    return "\n".join(lines) + "\n"


def _rank_zones(results: dict) -> list[tuple[str, dict]]:
    ranked = [(z, results["by_zone"][z]["summary"]) for z in BREADTH_ZONES_ORDER]
    ranked.sort(
        key=lambda x: (
            x[1].get("mean_excess_pct") if x[1].get("mean_excess_pct") is not None else -9999,
            x[1].get("win_rate_vs_bench_pct") if x[1].get("win_rate_vs_bench_pct") is not None else -1,
            x[1].get("n_periods") or 0,
        ),
        reverse=True,
    )
    return ranked


def render_comparison_markdown(results: dict) -> str:
    ds, de = results["date_start"], results["date_end"]
    lines = [
        f"# RRG mono × 200MA 廣度區間回測 · {ds}～{de}",
        "",
        "策略：**單軌濾網 + fresh 訊號 + 依軌跡排序 + 3 槽 + 持有 7 日**（第 4 日收盤進場 / 第 11 日收盤出場）",
        "",
        "方法：各區間**獨立**模擬 — 僅在該日 `zone_200` 符合時允許新進場；持倉照常持有至到期。",
        "",
        "## 區間獨立回測（僅該區間日可開新倉）",
        "",
        "| 排名 | 200MA 區間 | 成交筆數 | 勝率 vs 基準 | 均報酬 | 均超額 | 累計超額 |",
        "|------|-----------|---------|-------------|--------|--------|---------|",
    ]
    for i, (zone, s) in enumerate(_rank_zones(results), 1):
        lines.append(
            f"| {i} | **{BREADTH_ZONE_ZH[zone]}** | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_return_pct', '—')}% | "
            f"{s.get('mean_excess_pct', '—')}% | "
            f"{s.get('total_excess_pct', '—')}% |"
        )

    best_zone, best_s = _rank_zones(results)[0]
    lines.extend(
        [
            "",
            f"**最佳區間（獨立模擬）**：{BREADTH_ZONE_ZH[best_zone]}（`{best_zone}`）"
            f" — 均超額 {best_s.get('mean_excess_pct', '—')}%，"
            f"n={best_s.get('n_periods', 0)}，"
            f"勝率 vs IX0001 {best_s.get('win_rate_vs_bench_pct', '—')}%",
            "",
            "## 全樣本進場 · 依進場日區間分組（對照）",
            "",
            "同一條件跑全程，再按進場日 `zone_200` 分桶：",
            "",
            "| 200MA 區間 | 成交筆數 | 勝率 vs 基準 | 均報酬 | 均超額 |",
            "|-----------|---------|-------------|--------|--------|",
        ]
    )
    pooled = results["pooled_by_entry_zone"]
    for zone in sorted(
        BREADTH_ZONES_ORDER,
        key=lambda z: pooled[z].get("mean_excess_pct") or -9999,
        reverse=True,
    ):
        s = pooled[zone]
        lines.append(
            f"| {BREADTH_ZONE_ZH[zone]} | {s.get('n_periods', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')}% | "
            f"{s.get('mean_return_pct', '—')}% | "
            f"{s.get('mean_excess_pct', '—')}% |"
        )

    pa = results["pooled_all"]["summary"]
    lines.extend(
        [
            "",
            f"全樣本合計：n={pa.get('n_periods', 0)}，"
            f"均超額 {pa.get('mean_excess_pct', '—')}%，"
            f"勝率 vs 基準 {pa.get('win_rate_vs_bench_pct', '—')}%",
            "",
            "---",
            "模組：`rrg_mono_backtest.py` · 廣度：`market_breadth_ma.zone_200`",
        ]
    )
    return "\n".join(lines) + "\n"
