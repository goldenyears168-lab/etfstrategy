"""RRG mono swap-accel（C18acc）· 盤中 live screen · Strategy layer（rrg-mono-swap-accel · enabled: false）。

严格对齐回测：
  · 信号层：昨收 PIT · fresh mono 全池（`build_fresh_mono_calendar` 同款）
  · 执行层：1 分 K · C0 scale · 5m 格点 · poll_5m 换仓（`rrg_mono_intraday_ab` + `score_swap_c`）
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import sqlite3

from project_config import DEFAULT_ETF_CODES
from project_dotenv import load_project_dotenv
from report_paths import REPORTS_DIR
from research.backtest.rrg_lens_score_swap import _prior_trading_date
from research.backtest.rrg_mono_intraday_ab import (
    DEFAULT_C_SWEEP,
    _kbar_px,
    intraday_price_scale,
    rank_shortlist_scale,
    scaled_seg_last,
)
from research.backtest.rrg_mono_score_swap_c import (
    RRG_MONO_SWAP_ACCEL_SHORT,
    RRG_MONO_SWAP_ACCEL_SLUG,
    ScoreSwapCConfig,
    _avg_accel_scalar,
    _last_va_dot,
    _pick_swap_pair,
    _trading_days_between,
    candidate_shortlist_is_passthrough,
    champion_score_swap_c_config,
)
from rrg_mono_daily_brief import (
    LENGTH,
    MAX_SLOTS,
    TOP_N,
    ScanRow,
    _feat,
    _fresh_mono,
    scan_rows_from_panels,
)
from rrg_mono_intraday_watch import _session_date
from rrg_rotation import compute_rrg_panel
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect, load_etf_constituent_watchlist, upsert_stock_kbar_1m
from stock_db.kbar import kbar_day_has_data
from yahoo_chart_sync import fetch_tw_intraday_kbar_rows

STATE_PATH = PROJECT_ROOT / "data" / "rrg_c18acc_slots.json"
STATE_SCHEMA = "rrg-c18acc-slots-v1"
INTENTS_DIR = PROJECT_ROOT / "reports" / "order" / "intents"
TICK_LOG_PATH = PROJECT_ROOT / "logs" / "rrg_c18acc_poll_tick.log"
ALIGN_MODE = "backtest_pit"

ActionKind = Literal["entry", "swap", "max_hold_exit"]


@dataclass
class ScreenAction:
    kind: ActionKind
    stock_id: str
    stock_name: str
    side: Literal["buy", "sell"]
    price: float
    quantity_shares: int
    note: str
    counterparty_id: str | None = None
    counterparty_name: str | None = None


@dataclass
class ScreenResult:
    session_date: str
    polled_at: str
    config: ScoreSwapCConfig
    mono_top10: list[ScanRow] = field(default_factory=list)  # 昨收 PIT · fresh mono 全池（legacy 欄位名）
    pool_as_of: str = ""  # 信號日（昨交易日）
    disp_universe_n: int = 0  # fresh mono 池大小（信號日）
    all_mono_n: int = 0
    slots: list[dict[str, Any]] = field(default_factory=list)
    swaps_today: int = 0
    actions: list[ScreenAction] = field(default_factory=list)
    skip_reason: str | None = None
    dry_run: bool = True
    poll_minute: str = ""
    pool_override: list[str] = field(default_factory=list)
    kbar_sync_n: int = 0
    kbar_watch_n: int = 0
    tick_stock_n: int = 0  # 相容舊 log · 同 kbar_watch_n
    universe_n: int = 0
    tick_error: str | None = None
    entry_gate: str = ""
    lead: str = ""
    lead_drift: str = ""
    blocker: str = ""


def _scan_row_to_dict(row: ScanRow) -> dict[str, Any]:
    return asdict(row)


def _scan_row_from_dict(raw: dict[str, Any]) -> ScanRow:
    return ScanRow(
        stock_id=str(raw["stock_id"]),
        stock_name=str(raw.get("stock_name") or ""),
        fresh=bool(raw.get("fresh")),
        mono=bool(raw.get("mono")),
        seg_last=float(raw.get("seg_last") or 0),
        disp=float(raw.get("disp") or 0),
        segs=[float(x) for x in (raw.get("segs") or [])],
        quadrants=[str(q) for q in (raw.get("quadrants") or [])],
        rs_ratio=float(raw.get("rs_ratio") or 0),
        rs_momentum=float(raw.get("rs_momentum") or 0),
        daily_pct=raw.get("daily_pct"),
        composite_score=raw.get("composite_score"),
    )


def _signal_date_for_session(full_dates: list[str], session: str) -> str | None:
    """昨交易日 · session 可尚未入日線 panel。"""
    if session in full_dates:
        return _prior_trading_date(full_dates, session)
    prior = [d for d in full_dates if d < session]
    return prior[-1] if prior else None


def lock_pit_fresh_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    close: pd.DataFrame,
    bench: pd.Series,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[ScanRow], str, int]:
    """昨收 PIT · fresh mono 全池（依 seg_last 排序 · 不裁 top10）。"""
    full_dates = close.index.astype(str).tolist()
    signal_as_of = _signal_date_for_session(full_dates, session)
    if not signal_as_of:
        return [], "", 0
    _all_mono, fresh_mono = scan_rows_from_panels(
        conn, signal_as_of, close, bench, etf_codes=etf_codes
    )
    ranked = sorted(fresh_mono, key=lambda r: (-r.seg_last, r.stock_id))
    return ranked, signal_as_of, len(fresh_mono)


def _parse_pool_override(session: str) -> list[str] | None:
    """C18ACC_POOL_OVERRIDE=3711,6488 · 可選 C18ACC_POOL_OVERRIDE_DATE=YYYY-MM-DD。"""
    raw = os.environ.get("C18ACC_POOL_OVERRIDE", "").strip()
    if not raw:
        return None
    scope = os.environ.get("C18ACC_POOL_OVERRIDE_DATE", "").strip() or session
    if scope != session:
        return None
    ids = [x.strip() for x in raw.replace(" ", "").split(",") if x.strip()]
    return ids or None


def build_manual_pool(
    conn: sqlite3.Connection,
    signal_as_of: str,
    close: pd.DataFrame,
    bench: pd.Series,
    stock_ids: list[str],
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[ScanRow]:
    """手動候選 · 仍用 signal_as_of 日線 RRG 特徵（seg_last / disp）。"""
    mask = close.index.astype(str) <= signal_as_of
    close_sig = close.loc[mask]
    bench_sig = bench.reindex(close_sig.index).astype(float)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close_sig, bench_sig, length=LENGTH)
    daily_pct = close_sig.pct_change(fill_method=None) * 100.0
    full_dates = close_sig.index.astype(str).tolist()
    if signal_as_of not in full_dates:
        return []
    si = full_dates.index(signal_as_of)

    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}

    rows: list[ScanRow] = []
    for sid in stock_ids:
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid)
        if not f:
            continue
        pct = float(daily_pct.at[signal_as_of, sid]) if sid in daily_pct.columns else None
        if pct != pct:
            pct = None
        rows.append(
            ScanRow(
                stock_id=sid,
                stock_name=name_map.get(sid, ""),
                fresh=_fresh_mono(rs_ratio, rs_mom, full_dates, si, sid),
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
    rows.sort(key=lambda r: (-r.seg_last, r.stock_id))
    return rows


def load_or_lock_pool(
    state: dict[str, Any],
    conn: sqlite3.Connection,
    *,
    session: str,
    close: pd.DataFrame,
    bench: pd.Series,
    override_ids: list[str] | None = None,
) -> tuple[list[ScanRow], str, int, list[str]]:
    if override_ids:
        if (
            state.get("pool_session") == session
            and state.get("pool_override") == list(override_ids)
            and state.get("pool")
        ):
            rows = [_scan_row_from_dict(x) for x in state["pool"] if isinstance(x, dict)]
            return (
                rows,
                str(state.get("pool_as_of") or ""),
                int(state.get("pool_fresh_n") or len(rows)),
                override_ids,
            )
        signal_as_of = _signal_date_for_session(close.index.astype(str).tolist(), session)
        if not signal_as_of:
            return [], "", 0, override_ids
        pool = build_manual_pool(conn, signal_as_of, close, bench, override_ids)
        state["pool_session"] = session
        state["pool_as_of"] = signal_as_of
        state["pool_fresh_n"] = len(pool)
        state["pool_override"] = list(override_ids)
        state["pool"] = [_scan_row_to_dict(r) for r in pool]
        return pool, signal_as_of, len(pool), override_ids

    state.pop("pool_override", None)
    if state.get("pool_session") == session and state.get("pool"):
        rows = [_scan_row_from_dict(x) for x in state["pool"] if isinstance(x, dict)]
        return rows, str(state.get("pool_as_of") or ""), int(state.get("pool_fresh_n") or len(rows)), []
    pool, signal_as_of, fresh_n = lock_pit_fresh_pool(conn, session=session, close=close, bench=bench)
    state["pool_session"] = session
    state["pool_as_of"] = signal_as_of
    state["pool_fresh_n"] = fresh_n
    state["pool"] = [_scan_row_to_dict(r) for r in pool]
    return pool, signal_as_of, fresh_n, []


def sync_watchlist_kbar(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    trade_date: str,
) -> int:
    if not _env_flag("C18ACC_KBAR_SYNC", "1"):
        return 0
    d = date.fromisoformat(trade_date)
    total = 0
    for sid in sorted({str(x) for x in stock_ids if x}):
        try:
            rows, _ = fetch_tw_intraday_kbar_rows(sid, d, d, interval="1m")
            if rows:
                total += upsert_stock_kbar_1m(conn, rows)
        except Exception:
            continue
        time.sleep(0.35)
    return total


def _poll_minute(now: datetime, *, interval_min: int = 5) -> str:
    total = now.hour * 60 + now.minute
    total = (total // interval_min) * interval_min
    hh, mm = divmod(total, 60)
    return f"{hh:02d}:{mm:02d}"


def _kbar_px_at(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> float | None:
    return _kbar_px(conn, stock_id, trade_date, minute, kbar_cache, close)


def _signal_rrg_panels(
    close: pd.DataFrame,
    bench: pd.Series,
    signal_as_of: str,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.DataFrame, list[str]]:
    """日線 RRG 僅用到 signal_as_of（昨收 PIT）。"""
    mask = close.index.astype(str) <= signal_as_of
    close_sig = close.loc[mask]
    bench_sig = bench.reindex(close_sig.index).astype(float)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close_sig, bench_sig, length=LENGTH)
    full_dates = close_sig.index.astype(str).tolist()
    return close_sig, bench_sig, rs_ratio, rs_mom, full_dates


def _skip_reason_plain(reason: str) -> str:
    if reason == "RUN_RRG_C18ACC_SCREEN=0":
        return "盤中掃描已關閉"
    if reason.startswith("outside poll window"):
        return "盤外時段，不評估進場"
    if reason == "無昨交易日可鎖定 PIT 池":
        return "尚無昨收信號日"
    return f"略過：{reason}"


def _lead_label(row: ScanRow | None) -> str:
    if row is None:
        return "暫無領先標的"
    name = (row.stock_name or "").strip()
    return f"{row.stock_id} {name}" if name else row.stock_id


def _row_scale_metrics(
    conn: sqlite3.Connection,
    row: ScanRow,
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, float | None]:
    if session not in close.index.astype(str) or row.stock_id not in close.columns:
        return None, None
    close_px = float(close.at[session, row.stock_id])
    if close_px <= 0 or close_px != close_px:
        return None, None
    px = _kbar_px_at(conn, row.stock_id, session, poll_minute, close, kbar_cache)
    scale = intraday_price_scale(close_px, px)
    return scaled_seg_last(row, scale), scale


def _c0_scale_diagnostics(
    conn: sqlite3.Connection,
    stock_id: str,
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[bool, bool]:
    """(session 收盤基準價可用, 盤中 kbar 可用) · C0 scaled seg_last 前置條件。"""
    close_ok = False
    if session in close.index.astype(str) and stock_id in close.columns:
        close_px = float(close.at[session, stock_id])
        close_ok = close_px > 0 and close_px == close_px
    px = _kbar_px_at(conn, stock_id, session, poll_minute, close, kbar_cache)
    px_ok = px is not None and px > 0
    return close_ok, px_ok


def _c0_scale_blocker_clause(
    stock_id: str,
    poll_minute: str,
    *,
    close_ok: bool,
    px_ok: bool,
    rank: int | None = None,
) -> str:
    rank_bit = f" · C0 排序 #{rank}" if rank is not None else ""
    if not close_ok and not px_ok:
        return (
            f"{stock_id}{rank_bit}：C0 scaled seg_last 無法計算"
            f"（session 無收盤基準價 · {poll_minute} 無盤中 kbar）"
        )
    if not close_ok:
        return (
            f"{stock_id}{rank_bit}：C0 scaled seg_last 無法計算"
            f"（session 尚無有效收盤基準價）"
        )
    if not px_ok:
        return (
            f"{stock_id}{rank_bit}：C0 scaled seg_last 無法計算"
            f"（{poll_minute} 尚無盤中 kbar）"
        )
    return ""


def _entry_blocker_plain(
    *,
    ranked: list[ScanRow],
    confirm: dict[str, int],
    need_confirm: int,
    held: set[str],
    entries_today: set[str],
    session: str,
    poll_minute: str,
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    lead_row: ScanRow,
    lead_rank: int,
    slots_n: int,
    has_entry_action: bool,
) -> str:
    if has_entry_action:
        return "—"
    if slots_n >= MAX_SLOTS:
        return "槽位已滿（3/3）· 僅評估換倉"
    for i, row in enumerate(ranked[:MAX_SLOTS]):
        sid = row.stock_id
        if sid in held or sid in entries_today:
            continue
        c = int(confirm.get(sid, 0))
        if c < need_confirm:
            continue
        close_ok, px_ok = _c0_scale_diagnostics(
            conn, sid, session, poll_minute, close, kbar_cache
        )
        clause = _c0_scale_blocker_clause(
            sid, poll_minute, close_ok=close_ok, px_ok=px_ok, rank=i + 1
        )
        if clause:
            return clause
    sid = lead_row.stock_id
    if sid in held:
        return f"{sid}：已持有"
    if sid in entries_today:
        return f"{sid}：本日已進場"
    close_ok, px_ok = _c0_scale_diagnostics(
        conn, sid, session, poll_minute, close, kbar_cache
    )
    scale_clause = _c0_scale_blocker_clause(
        sid, poll_minute, close_ok=close_ok, px_ok=px_ok, rank=lead_rank
    )
    if scale_clause:
        return scale_clause
    c = int(confirm.get(sid, 0))
    if c < need_confirm:
        return f"{sid} · C0 排序 #{lead_rank}：confirm_bars {c}/{need_confirm}"
    budget = _env_int("C18ACC_BUDGET_TWD_PER_SLOT", 20000)
    board_lot = _env_flag("C18ACC_BOARD_LOT", "0")
    px = _kbar_px_at(conn, sid, session, poll_minute, close, kbar_cache)
    if px is not None and px > 0 and _lot_shares(px, budget, board_lot=board_lot) <= 0:
        mode = "整張" if board_lot else "零股"
        return f"槽位預算不足（{sid} · {mode} · {budget:,} TWD @ {px:.2f}）"
    return "—"


def _lead_drift_plain(
    *,
    lead: ScanRow | None,
    rank: int | None,
    confirm: int,
    scaled: float | None,
    last_poll: dict[str, Any] | None,
) -> str:
    if lead is None:
        return "—"
    if not last_poll:
        return "首輪尚無比較"
    prev_lead = str(last_poll.get("lead") or "")
    if prev_lead and prev_lead != lead.stock_id:
        return "領先標的已換人"
    closer = 0
    farther = 0
    prev_rank = last_poll.get("rank")
    if rank is not None and prev_rank is not None:
        if int(rank) < int(prev_rank):
            closer += 1
        elif int(rank) > int(prev_rank):
            farther += 1
    prev_confirm = int(last_poll.get("confirm") or 0)
    if confirm > prev_confirm:
        closer += 1
    elif confirm < prev_confirm:
        farther += 1
    prev_scaled = last_poll.get("scaled")
    if scaled is not None and prev_scaled is not None:
        try:
            delta = float(scaled) - float(prev_scaled)
        except (TypeError, ValueError):
            delta = 0.0
        if delta > 1e-6:
            closer += 1
        elif delta < -1e-6:
            farther += 1
    if closer > farther:
        return "較上輪更靠近買點"
    if farther > closer:
        return "較上輪更遠離買點"
    return "與上輪差不多"


def _tracked_stock_away(
    last_poll: dict[str, Any] | None,
    confirm: dict[str, int],
    top3_ids: set[str],
) -> bool:
    if not last_poll:
        return False
    sid = str(last_poll.get("lead") or "")
    if not sid:
        return False
    if int(last_poll.get("confirm") or 0) <= 0:
        return False
    if sid not in top3_ids:
        return True
    return int(confirm.get(sid, 0)) <= 0


def _fill_entry_narrative(
    result: ScreenResult,
    *,
    state: dict[str, Any],
    pool: list[ScanRow],
    conn: sqlite3.Connection,
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    c0_cfg: Any,
    now: datetime,
    entries_today: set[str],
) -> dict[str, Any] | None:
    last_poll = state.get("last_poll") if isinstance(state.get("last_poll"), dict) else None

    if result.skip_reason:
        result.entry_gate = _skip_reason_plain(result.skip_reason)
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = result.entry_gate
        return None

    if len(result.slots) >= MAX_SLOTS:
        result.entry_gate = "三槽已滿，不進新倉"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = "槽位已滿（3/3）· 僅評估換倉"
        return None

    entry_actions = [a for a in result.actions if a.kind == "entry" and a.side == "buy"]
    if entry_actions:
        act = entry_actions[0]
        result.entry_gate = "本輪已觸發買進"
        result.lead = f"{act.stock_id} {act.stock_name}".strip() if act.stock_name else act.stock_id
        result.blocker = "—"
        lead_row = next((r for r in pool if r.stock_id == act.stock_id), None)
        scaled, _ = (
            _row_scale_metrics(conn, lead_row, session, poll_minute, close, kbar_cache)
            if lead_row is not None
            else (None, None)
        )
        if lead_row is not None:
            result.lead_drift = _lead_drift_plain(
                lead=lead_row,
                rank=1,
                confirm=int(c0_cfg.confirm_bars),
                scaled=scaled,
                last_poll=last_poll,
            )
        else:
            result.lead_drift = "首輪尚無比較" if not last_poll else "與上輪差不多"
        return {
            "minute": poll_minute,
            "lead": act.stock_id,
            "rank": 1,
            "confirm": int(c0_cfg.confirm_bars),
            "scaled": scaled,
        }

    if not _entry_allowed(now):
        result.entry_gate = "目前不在進場時段"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = "盤外時段 · 不評估 C0 進場"
        return None

    if not pool:
        result.entry_gate = "候選池為空"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = "PIT 候選池為空"
        return None

    ranked = rank_shortlist_scale(
        pool,
        conn=conn,
        close=close,
        trade_date=session,
        minute=poll_minute,
        kbar_cache=kbar_cache,
    )
    if not ranked:
        result.entry_gate = "盤中報價不足，無法評估"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = "盤中 kbar 不足 · 無法 C0 scale 排序"
        return None

    confirm: dict[str, int] = {
        str(k): int(v) for k, v in (state.get("entry_confirm") or {}).items()
    }
    held = {str(p["stock_id"]) for p in result.slots}
    top3 = ranked[:MAX_SLOTS]
    top3_ids = {r.stock_id for r in top3}
    lead_row = ranked[0]
    rank_by_id = {row.stock_id: i + 1 for i, row in enumerate(ranked)}
    lead_rank = rank_by_id[lead_row.stock_id]
    lead_confirm = int(confirm.get(lead_row.stock_id, 0))
    scaled, _ = _row_scale_metrics(conn, lead_row, session, poll_minute, close, kbar_cache)
    need_confirm = int(c0_cfg.confirm_bars)
    px = _kbar_px_at(conn, lead_row.stock_id, session, poll_minute, close, kbar_cache)
    ready = (
        lead_confirm >= need_confirm
        and lead_row.stock_id not in held
        and lead_row.stock_id not in entries_today
        and px is not None
        and px > 0
    )

    if ready:
        result.entry_gate = "條件已齊，可進場"
    elif _tracked_stock_away(last_poll, confirm, top3_ids):
        result.entry_gate = "先前關注標的已遠離買點"
    elif any(
        int(confirm.get(r.stock_id, 0)) > 0
        for r in top3
        if r.stock_id not in held
    ):
        result.entry_gate = "有標的正在靠近買點"
    elif lead_row.stock_id in top3_ids:
        result.entry_gate = "領先者剛進前三，觀察中"
    else:
        result.entry_gate = "有空槽，暫無明確領先"

    result.lead = _lead_label(lead_row)
    result.lead_drift = _lead_drift_plain(
        lead=lead_row,
        rank=lead_rank,
        confirm=lead_confirm,
        scaled=scaled,
        last_poll=last_poll,
    )
    result.blocker = _entry_blocker_plain(
        ranked=ranked,
        confirm=confirm,
        need_confirm=need_confirm,
        held=held,
        entries_today=entries_today,
        session=session,
        poll_minute=poll_minute,
        conn=conn,
        close=close,
        kbar_cache=kbar_cache,
        lead_row=lead_row,
        lead_rank=lead_rank,
        slots_n=len(result.slots),
        has_entry_action=False,
    )
    return {
        "minute": poll_minute,
        "lead": lead_row.stock_id,
        "rank": lead_rank,
        "confirm": lead_confirm,
        "scaled": scaled,
    }


def append_poll_tick_log(result: ScreenResult, *, log_path: Path | None = None) -> Path:
    path = log_path or TICK_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    top_ids = ",".join(r.stock_id for r in result.mono_top10) or "—"
    override_s = ",".join(result.pool_override) if result.pool_override else "—"
    action_bits = [
        f"{a.kind}:{a.side}:{a.stock_id}" for a in result.actions
    ]
    actions_s = ";".join(action_bits) if action_bits else "—"
    skip = result.skip_reason or "—"
    signal = 1 if result.actions else 0
    entry_gate = result.entry_gate or "—"
    lead = result.lead or "—"
    lead_drift = result.lead_drift or "—"
    blocker = result.blocker or "—"
    line = (
        f"{result.polled_at} | session={result.session_date} | signal={signal} | "
        f"pool_as_of={result.pool_as_of or '—'} | minute={result.poll_minute or '—'} | "
        f"override={override_s} | pool={top_ids} | fresh_n={result.disp_universe_n} | "
        f"kbar_sync={result.kbar_sync_n} | slots={len(result.slots)}/{MAX_SLOTS} | "
        f"swaps_today={result.swaps_today} | actions={actions_s} | skip={skip} | "
        f"entry_gate={entry_gate} | lead={lead} | lead_drift={lead_drift} | blocker={blocker}\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _now_local() -> datetime:
    return datetime.now()


def _poll_window_ok(now: datetime | None = None) -> bool:
    now = now or _now_local()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(9, 0) <= t <= dt_time(13, 20)


def _swap_allowed(now: datetime | None = None) -> bool:
    now = now or _now_local()
    return now.time() >= dt_time(9, 30)


def _entry_allowed(now: datetime | None = None) -> bool:
    now = now or _now_local()
    t = now.time()
    return dt_time(9, 5) <= t <= dt_time(13, 20)


def load_slot_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"schema_version": STATE_SCHEMA, "slots": [], "history": []}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("schema_version", STATE_SCHEMA)
    state.setdefault("slots", [])
    state.setdefault("history", [])
    return state


def save_slot_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["schema_version"] = STATE_SCHEMA
    state["updated"] = _now_local().isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _reset_daily_counters(state: dict[str, Any], session_date: str) -> None:
    if state.get("session_date") != session_date:
        state["session_date"] = session_date
        state["swaps_today"] = 0
        state["entries_today"] = []
        state["entry_confirm"] = {}
        state.pop("pool_session", None)
        state.pop("last_poll", None)


def _lot_shares(price: float, budget_twd: float, *, board_lot: bool = False) -> int:
    """依槽位預算計算股數。預設零股（floor 預算／價）；C18ACC_BOARD_LOT=1 為整張。"""
    if price <= 0 or budget_twd <= 0:
        return 0
    if board_lot:
        lots = int(budget_twd / price) // 1000
        return max(0, lots * 1000)
    return max(0, int(budget_twd / price))


def _intent_market_type(quantity_shares: int) -> str:
    return "common" if quantity_shares >= 1000 else "odd"


def _limit_price(px: float) -> str:
    return f"{px:.2f}"


def _build_accel_maps(
    *,
    config: ScoreSwapCConfig,
    pool: list[ScanRow],
    slots: list[dict[str, Any]],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
) -> tuple[
    dict[str, float],
    dict[str, str],
    dict[str, str],
    dict[str, float],
    dict[str, float],
]:
    held_today: dict[str, float] = {}
    held_trend: dict[str, str] = {}
    challenger_trend: dict[str, str] = {}
    challenger_va_dot: dict[str, float] = {}
    challenger_avg_accel: dict[str, float] = {}

    if config.sort_key == "avg_accel_decel":
        for pos in slots:
            sid = str(pos["stock_id"])
            scalar = _avg_accel_scalar(
                rs_ratio, rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
            )
            if scalar is not None:
                held_today[sid] = float(scalar)

    top_n = max(1, int(config.candidate_top_n))
    kin_rows = pool if candidate_shortlist_is_passthrough(config) else pool[:top_n]
    for row in kin_rows:
        sid = row.stock_id
        if config.buy_sort_key == "accel_decel":
            dot = _last_va_dot(rs_ratio, rs_mom, full_dates, as_of, sid)
            if dot is not None:
                challenger_va_dot[sid] = float(dot)
        if config.buy_sort_key == "avg_accel_decel":
            scalar = _avg_accel_scalar(
                rs_ratio, rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
            )
            if scalar is not None:
                challenger_avg_accel[sid] = float(scalar)

    return held_today, held_trend, challenger_trend, challenger_va_dot, challenger_avg_accel


def _try_c0_entry(
    conn: sqlite3.Connection,
    *,
    state: dict[str, Any],
    pool: list[ScanRow],
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    c0_cfg: Any,
) -> tuple[ScanRow | None, float | None]:
    """单轮 poll · C0 scale 排序 · confirm 跨轮累计。"""
    slots = state.get("slots") or []
    if len(slots) >= MAX_SLOTS or not pool:
        return None, None
    ranked = rank_shortlist_scale(
        pool,
        conn=conn,
        close=close,
        trade_date=session,
        minute=poll_minute,
        kbar_cache=kbar_cache,
    )
    if not ranked:
        return None, None
    confirm: dict[str, int] = state.setdefault("entry_confirm", {})
    held = {str(p["stock_id"]) for p in slots}
    top_ids = {r.stock_id for r in ranked[:MAX_SLOTS]}
    for sid in list(confirm.keys()):
        if sid not in top_ids:
            confirm[sid] = 0
    for row in ranked[:MAX_SLOTS]:
        if row.stock_id not in held:
            confirm[row.stock_id] = confirm.get(row.stock_id, 0) + 1
    for row in ranked:
        if len(state.get("slots") or []) >= MAX_SLOTS:
            break
        sid = row.stock_id
        if sid in held:
            continue
        if confirm.get(sid, 0) < int(c0_cfg.confirm_bars):
            continue
        px = _kbar_px_at(conn, sid, session, poll_minute, close, kbar_cache)
        if px is None or px <= 0:
            continue
        return row, float(px)
    return None, None


def _make_action(
    *,
    kind: ActionKind,
    row: ScanRow | dict[str, Any],
    side: Literal["buy", "sell"],
    price: float,
    quantity_shares: int,
    note: str,
    counterparty: ScanRow | dict[str, Any] | None = None,
) -> ScreenAction | None:
    if quantity_shares <= 0 or price <= 0:
        return None
    if isinstance(row, ScanRow):
        sid, name = row.stock_id, row.stock_name
    else:
        sid, name = str(row["stock_id"]), str(row.get("stock_name") or "")
    cp_id = cp_name = None
    if counterparty is not None:
        if isinstance(counterparty, ScanRow):
            cp_id, cp_name = counterparty.stock_id, counterparty.stock_name
        else:
            cp_id = str(counterparty.get("stock_id") or "")
            cp_name = str(counterparty.get("stock_name") or "")
    return ScreenAction(
        kind=kind,
        stock_id=sid,
        stock_name=name,
        side=side,
        price=price,
        quantity_shares=quantity_shares,
        note=note,
        counterparty_id=cp_id,
        counterparty_name=cp_name,
    )


def _apply_actions_to_state(
    state: dict[str, Any],
    actions: list[ScreenAction],
    *,
    session_date: str,
    config: ScoreSwapCConfig,
) -> None:
    slots: list[dict[str, Any]] = list(state.get("slots") or [])
    entries_today: list[str] = list(state.get("entries_today") or [])
    swaps_today = int(state.get("swaps_today") or 0)

    for act in actions:
        if act.kind == "max_hold_exit" and act.side == "sell":
            slots = [p for p in slots if str(p["stock_id"]) != act.stock_id]
            state.setdefault("history", []).append(
                {
                    "stock_id": act.stock_id,
                    "stock_name": act.stock_name,
                    "exit_date": session_date,
                    "exit_reason": "max_hold",
                }
            )
        elif act.kind == "swap" and act.side == "sell" and act.counterparty_id:
            sell_id = act.stock_id
            buy_id = act.counterparty_id
            buy_name = act.counterparty_name or ""
            slot_id = next(
                (p.get("slot") for p in slots if str(p["stock_id"]) == sell_id),
                len(slots),
            )
            slots = [p for p in slots if str(p["stock_id"]) != sell_id]
            slots.append(
                {
                    "slot": slot_id,
                    "stock_id": buy_id,
                    "stock_name": buy_name,
                    "signal_date": session_date,
                    "entry_date": session_date,
                    "entry_px": act.price,
                    "seg_last": 0.0,
                    "disp": 0.0,
                    "entry_leg": config.entry_leg,
                }
            )
            swaps_today += 1
            entries_today.append(buy_id)
        elif act.kind == "entry" and act.side == "buy":
            used = {int(p["slot"]) for p in slots}
            free = next((i for i in range(MAX_SLOTS) if i not in used), None)
            if free is None:
                continue
            slots.append(
                {
                    "slot": free,
                    "stock_id": act.stock_id,
                    "stock_name": act.stock_name,
                    "signal_date": session_date,
                    "entry_date": session_date,
                    "entry_px": act.price,
                    "seg_last": 0.0,
                    "disp": 0.0,
                    "entry_leg": config.entry_leg,
                }
            )
            entries_today.append(act.stock_id)
            confirm = state.get("entry_confirm") or {}
            confirm.pop(act.stock_id, None)
            state["entry_confirm"] = confirm

    state["slots"] = slots
    state["swaps_today"] = swaps_today
    state["entries_today"] = entries_today
    state["session_date"] = session_date


def build_intent_batch(result: ScreenResult) -> dict[str, Any] | None:
    if not result.actions:
        return None
    intents: list[dict[str, Any]] = []
    for act in result.actions:
        intents.append(
            {
                "symbol": act.stock_id,
                "side": act.side,
                "quantity_shares": act.quantity_shares,
                "price": _limit_price(act.price),
                "price_type": "limit",
                "market_type": _intent_market_type(act.quantity_shares),
                "time_in_force": "rod",
                "order_type": "stock",
                "user_def": f"{RRG_MONO_SWAP_ACCEL_SHORT}:{act.kind}",
                "note": act.note,
            }
        )
    return {
        "schema_version": "order-intent-v1",
        "strategy_id": RRG_MONO_SWAP_ACCEL_SLUG,
        "as_of": result.session_date,
        "metadata": {
            "layer": "research",
            "align_mode": ALIGN_MODE,
            "pool_as_of": result.pool_as_of,
            "pool_override": result.pool_override or None,
            "poll_minute": result.poll_minute,
            "variant_id": result.config.variant_id,
            "dry_run": result.dry_run,
            "polled_at": result.polled_at,
            "actions": [asdict(a) for a in result.actions],
        },
        "intents": intents,
    }


def write_outputs(result: ScreenResult) -> tuple[Path, Path | None]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = result.session_date.replace("-", "")
    md_path = REPORTS_DIR / f"{stamp}_rrg_c18acc_screen.md"
    latest_md = REPORTS_DIR / "rrg_c18acc_screen.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    latest_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

    intent_path: Path | None = None
    batch = build_intent_batch(result)
    if batch is not None:
        INTENTS_DIR.mkdir(parents=True, exist_ok=True)
        intent_path = INTENTS_DIR / f"{RRG_MONO_SWAP_ACCEL_SLUG}_{result.session_date}.json"
        intent_path.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return md_path, intent_path


def render_markdown(result: ScreenResult) -> str:
    cfg = result.config
    mode = "dry-run" if result.dry_run else "live"
    lines = [
        f"# RRG mono swap-accel（C18acc）· {result.session_date}",
        "",
        f"> polled {result.polled_at} · **{mode}** · align `{ALIGN_MODE}` · pool_as_of **{result.pool_as_of or '—'}**",
        f"> variant `{cfg.variant_id}` · poll {cfg.poll_interval_min}m @ `{result.poll_minute or '—'}` · no swap before {cfg.no_trade_before}",
        "",
        f"- kbar sync：**{result.kbar_sync_n}** bars · watchlist **{result.kbar_watch_n}** 檔 · swaps today：**{result.swaps_today}**",
        f"- slots：**{len(result.slots)} / {MAX_SLOTS}** · 候選 **{len(result.mono_top10)}** 檔 · 信號日 **{result.pool_as_of or '—'}**",
    ]
    if result.pool_override:
        lines.append(f"- **手動候選（今日暫定）**：{', '.join(result.pool_override)}")
    if result.skip_reason:
        lines.append(f"- skip：`{result.skip_reason}`")
    if result.tick_error:
        lines.append(f"- warn：`{result.tick_error}`")
    lines.append("")

    pool_title = (
        f"## 手動候選（{result.session_date} · 信號日 {result.pool_as_of or '—'}）"
        if result.pool_override
        else f"## fresh mono 全池（昨收 PIT · {result.pool_as_of or '—'}）"
    )
    lines.extend([pool_title, ""])
    if not result.mono_top10:
        if result.pool_override:
            lines.append("_手動名單無可用日線 RRG 特徵。_")
        else:
            lines.append("_信號日無 fresh mono 候選。_")
    else:
        lines.append("| # | 代號 | 名稱 | fr | seg_last | 位移 |")
        lines.append("|---|------|------|----|----------|------|")
        for i, r in enumerate(result.mono_top10[:TOP_N], 1):
            fr = "★" if r.fresh else ""
            lines.append(
                f"| {i} | {r.stock_id} | {r.stock_name} | {fr} | {r.seg_last:.3f} | {r.disp:.2f} |"
            )
    lines.append("")

    lines.extend(["## 持倉（C18acc state）", ""])
    if not result.slots:
        lines.append("_空槽。_")
    else:
        lines.append("| 槽 | 代號 | 名稱 | 進場 | hold |")
        lines.append("|---|------|------|------|------|")
        for p in sorted(result.slots, key=lambda x: int(x.get("slot", 0))):
            hold = "—"
            if p.get("entry_date"):
                hold = str(p.get("entry_date"))
            lines.append(
                f"| {int(p.get('slot', 0)) + 1} | {p['stock_id']} | {p.get('stock_name', '')} | "
                f"{p.get('entry_date', '')} | {hold} |"
            )
    lines.append("")

    lines.extend(["## 本輪動作", ""])
    if not result.actions:
        lines.append("_無（觀察 / 條件未滿足）。_")
    else:
        for act in result.actions:
            cp = ""
            if act.counterparty_id:
                cp = f" → {act.counterparty_id} {act.counterparty_name or ''}"
            lines.append(
                f"- **{act.kind}** {act.side} {act.stock_id} {act.stock_name} "
                f"{act.quantity_shares} @ {act.price:.2f}{cp} · {act.note}"
            )
    lines.extend(
        [
            "",
            "---",
            "下單預覽：`.venv-fubon/bin/python scripts/order/submit_intents.py "
            f"reports/order/intents/{RRG_MONO_SWAP_ACCEL_SLUG}_{result.session_date}.json --dry-run`",
            "",
        ]
    )
    return "\n".join(lines)


def run_screen(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    config: ScoreSwapCConfig | None = None,
    dry_run: bool | None = None,
    apply_state: bool | None = None,
    now: datetime | None = None,
) -> ScreenResult:
    now = now or _now_local()
    session = session_date or _session_date()
    cfg = config or champion_score_swap_c_config()
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    dry = _env_flag("ORDER_C18ACC_DRY_RUN", "1") if dry_run is None else dry_run
    apply = _env_flag("ORDER_C18ACC_APPLY_STATE", "1") if apply_state is None else apply_state
    budget = _env_int("C18ACC_BUDGET_TWD_PER_SLOT", 20000)
    board_lot = _env_flag("C18ACC_BOARD_LOT", "0")
    poll_minute = _poll_minute(now, interval_min=int(cfg.poll_interval_min))

    result = ScreenResult(
        session_date=session,
        polled_at=now.strftime("%Y-%m-%d %H:%M"),
        config=cfg,
        dry_run=dry,
        poll_minute=poll_minute,
    )

    if not _env_flag("RUN_RRG_C18ACC_SCREEN", "1"):
        result.skip_reason = "RUN_RRG_C18ACC_SCREEN=0"
        _fill_entry_narrative(
            result,
            state={},
            pool=[],
            conn=conn,
            session=session,
            poll_minute=poll_minute,
            close=pd.DataFrame(),
            kbar_cache={},
            c0_cfg=c0_cfg,
            now=now,
            entries_today=set(),
        )
        return result
    if not _poll_window_ok(now):
        result.skip_reason = "outside poll window (Mon–Fri 09:00–13:20)"
        state = load_slot_state()
        result.slots = list(state.get("slots") or [])
        result.swaps_today = int(state.get("swaps_today") or 0)
        _fill_entry_narrative(
            result,
            state=state,
            pool=[],
            conn=conn,
            session=session,
            poll_minute=poll_minute,
            close=pd.DataFrame(),
            kbar_cache={},
            c0_cfg=c0_cfg,
            now=now,
            entries_today=set(),
        )
        return result

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    if session not in close.index.astype(str):
        close = close.copy()
        close.loc[session] = float("nan")
        bench = bench.reindex(close.index).astype(float)

    state = load_slot_state()
    _reset_daily_counters(state, session)
    override_ids = _parse_pool_override(session)
    pool, pool_as_of, fresh_n, active_override = load_or_lock_pool(
        state, conn, session=session, close=close, bench=bench, override_ids=override_ids
    )
    result.mono_top10 = pool
    result.pool_as_of = pool_as_of
    result.pool_override = active_override
    result.disp_universe_n = fresh_n
    result.all_mono_n = fresh_n

    slots: list[dict[str, Any]] = [dict(p) for p in state.get("slots") or []]
    swaps_today = int(state.get("swaps_today") or 0)
    entries_today = set(state.get("entries_today") or [])
    result.slots = list(slots)
    result.swaps_today = swaps_today

    watch_ids = [str(p["stock_id"]) for p in slots] + [r.stock_id for r in pool]
    result.kbar_watch_n = len(set(watch_ids))
    result.kbar_sync_n = sync_watchlist_kbar(conn, watch_ids, session)
    result.tick_stock_n = result.kbar_watch_n

    if not pool_as_of:
        result.skip_reason = "無昨交易日可鎖定 PIT 池"
        _fill_entry_narrative(
            result,
            state=state,
            pool=pool,
            conn=conn,
            session=session,
            poll_minute=poll_minute,
            close=close,
            kbar_cache={},
            c0_cfg=c0_cfg,
            now=now,
            entries_today=entries_today,
        )
        return result

    close_sig, _bench_sig, rs_ratio, rs_mom, signal_dates = _signal_rrg_panels(
        close, bench, pool_as_of
    )
    session_dates = close.index.astype(str).tolist()

    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    missing_kbar = [
        sid for sid in set(watch_ids)
        if not kbar_day_has_data(conn, sid, session)
    ]
    if missing_kbar:
        result.tick_error = f"kbar 缺 {len(missing_kbar)} 檔：{','.join(missing_kbar[:5])}{'…' if len(missing_kbar) > 5 else ''}"

    held_today, held_trend, chall_trend, chall_va, chall_avg = _build_accel_maps(
        config=cfg,
        pool=pool,
        slots=slots,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=signal_dates,
        as_of=pool_as_of,
    )

    actions: list[ScreenAction] = []

    for pos in list(slots):
        hold_days = _trading_days_between(session_dates, str(pos["entry_date"]), session)
        if hold_days < cfg.max_hold_days:
            continue
        px = _kbar_px_at(conn, str(pos["stock_id"]), session, poll_minute, close, kbar_cache)
        if px is None:
            continue
        act = _make_action(
            kind="max_hold_exit",
            row=pos,
            side="sell",
            price=px,
            quantity_shares=_lot_shares(px, budget, board_lot=board_lot),
            note=f"max_hold={cfg.max_hold_days}d @ {poll_minute}",
        )
        if act:
            actions.append(act)

    if (
        _swap_allowed(now)
        and poll_minute >= cfg.no_trade_before
        and swaps_today < cfg.max_swaps_per_day
        and len(slots) >= MAX_SLOTS
        and pool
    ):
        sell, buy = _pick_swap_pair(
            slots,
            pool,
            held_ids={str(p["stock_id"]) for p in slots},
            config=cfg,
            held_today=held_today,
            held_trend=held_trend,
            challenger_trend=chall_trend,
            challenger_va_dot=chall_va,
            challenger_avg_accel=chall_avg,
        )
        if sell is not None and buy is not None:
            hold_days = _trading_days_between(session_dates, str(sell["entry_date"]), session)
            if hold_days >= cfg.min_hold_days:
                sell_px = _kbar_px_at(conn, str(sell["stock_id"]), session, poll_minute, close, kbar_cache)
                buy_px = _kbar_px_at(conn, buy.stock_id, session, poll_minute, close, kbar_cache)
                if sell_px and buy_px:
                    sell_act = _make_action(
                        kind="swap",
                        row=sell,
                        side="sell",
                        price=sell_px,
                        quantity_shares=_lot_shares(sell_px, budget, board_lot=board_lot),
                        note=f"swap out @ {poll_minute} margin={cfg.effective_margin}",
                        counterparty=buy,
                    )
                    buy_act = _make_action(
                        kind="swap",
                        row=buy,
                        side="buy",
                        price=buy_px,
                        quantity_shares=_lot_shares(buy_px, budget, board_lot=board_lot),
                        note=f"swap in @ {poll_minute}",
                        counterparty=sell,
                    )
                    if sell_act and buy_act:
                        actions.extend([sell_act, buy_act])

    if _entry_allowed(now) and len(slots) < MAX_SLOTS and pool:
        entry_row, entry_px = _try_c0_entry(
            conn,
            state=state,
            pool=pool,
            session=session,
            poll_minute=poll_minute,
            close=close,
            kbar_cache=kbar_cache,
            c0_cfg=c0_cfg,
        )
        if (
            entry_row is not None
            and entry_px is not None
            and entry_row.stock_id not in entries_today
        ):
            act = _make_action(
                kind="entry",
                row=entry_row,
                side="buy",
                price=entry_px,
                quantity_shares=_lot_shares(entry_px, budget, board_lot=board_lot),
                note=f"C0 scale @ {poll_minute} confirm={c0_cfg.confirm_bars}",
            )
            if act:
                actions.append(act)

    result.actions = actions

    new_last_poll = _fill_entry_narrative(
        result,
        state=state,
        pool=pool,
        conn=conn,
        session=session,
        poll_minute=poll_minute,
        close=close,
        kbar_cache=kbar_cache,
        c0_cfg=c0_cfg,
        now=now,
        entries_today=entries_today,
    )

    if apply:
        work_state = {**state, "slots": [dict(p) for p in state.get("slots") or []]}
        if actions:
            _apply_actions_to_state(work_state, actions, session_date=session, config=cfg)
        work_state["pool_session"] = session
        work_state["pool_as_of"] = pool_as_of
        work_state["pool_fresh_n"] = fresh_n
        work_state["pool"] = [_scan_row_to_dict(r) for r in pool]
        work_state["entry_confirm"] = state.get("entry_confirm") or {}
        if new_last_poll is not None:
            work_state["last_poll"] = new_last_poll
        save_slot_state(work_state)
        result.slots = list(work_state.get("slots") or [])
        result.swaps_today = int(work_state.get("swaps_today") or 0)
    else:
        result.slots = list(state.get("slots") or [])

    return result


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="RRG mono swap-accel（C18acc）live screen")
    parser.add_argument("--date", help="session YYYY-MM-DD（預設 today）")
    parser.add_argument("--dry-run", action="store_true", help="intent metadata dry_run=true（預設）")
    parser.add_argument("--live-intent", action="store_true", help="metadata dry_run=false（仍不自動送單）")
    parser.add_argument("--no-apply-state", action="store_true", help="不更新 data/rrg_c18acc_slots.json")
    args = parser.parse_args(argv)

    dry = not args.live_intent
    if os.environ.get("ORDER_C18ACC_DRY_RUN", "1").strip() in ("0", "false") and not args.live_intent:
        dry = False

    conn = connect(DEFAULT_DB_PATH)
    try:
        result = run_screen(
            conn,
            session_date=args.date,
            dry_run=dry,
            apply_state=not args.no_apply_state,
        )
    finally:
        conn.close()

    md_path, intent_path = write_outputs(result)
    tick_log = append_poll_tick_log(result)
    signal = 1 if result.actions else 0
    print(
        f"C18acc screen: report={md_path} pool_as_of={result.pool_as_of} fresh_pool_n={len(result.mono_top10)} "
        f"minute={result.poll_minute} kbar={result.kbar_sync_n} "
        f"actions={len(result.actions)} dry_run={result.dry_run} skip={result.skip_reason or '—'} "
        f"tick_log={tick_log}"
    )
    print(f"C18ACC_SIGNAL={signal} actions={len(result.actions)}")
    if intent_path:
        print(f"C18acc intent: {intent_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
