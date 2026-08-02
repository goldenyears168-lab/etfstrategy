"""RRG mono swap-accel（C18acc）· 盤中 live screen · Strategy layer（rrg-mono-swap-accel · enabled: true）。

严格对齐回测：
  · 信号层：同日近收盤 fresh（≥13:00 kbar 暫定 · ≈EOD）· `champion_score_swap_c_config`
  · 执行层：5m K · entry avg_accel · no_trade_before=13:00 · poll→13:30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field, replace
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
    _kbar_px,
    intraday_price_scale,
    rank_shortlist,
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
    build_daily_spread_rs_panel,
    build_pit_candidate_pool,
    candidate_shortlist_is_passthrough,
    champion_score_swap_c_config,
    held_accel_bypass_on_spread_lost,
    prior_ret_gate_allows,
)
from order.c18acc_order_config import assert_pool_matches_champion
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
from rrg_universe_intraday_panel import spread_class_snapshots_at_minute
from market_benchmark import load_benchmark_close
from price_panels import load_price_panels
from stock_db import DATA_DIR, DEFAULT_DB_PATH, LOGS_DIR, PROJECT_ROOT, connect, load_etf_constituent_watchlist
from stock_db.kbar import (
    kbar_5m_fresh_for_poll,
    kbar_day_has_data,
    upsert_1m_rows_as_5m,
    upsert_stock_kbar_1m,
    upsert_yahoo_5m_rows,
)
from c18acc_extension_overlay import (
    ExtensionAlert,
    append_overlay_log,
    overlay_enabled,
    poll_interval_for_now,
    scan_held_extension_alerts,
    write_overlay_snapshot,
)
from yahoo_chart_sync import fetch_tw_intraday_kbar_rows

STATE_PATH = DATA_DIR / "rrg_c18acc_slots.json"
STATE_SCHEMA = "rrg-c18acc-slots-v1"
INTENTS_DIR = PROJECT_ROOT / "reports" / "order" / "intents"
TICK_LOG_PATH = LOGS_DIR / "intraday" / "rrg_c18acc_poll_tick.log"
CLOSE_PANEL_CACHE_PATH = DATA_DIR / "cache" / "c18acc_close_bench.pkl"
# Default aligned with champion no_trade_before; runtime uses _spread_gate_anchor()
SPREAD_GATE_NO_TRADE_BEFORE = "13:00"
ALIGN_MODE = "backtest_pit"
POOL_MODE_PRIOR_PIT = "prior_day_pit"
POOL_MODE_SAME_DAY = "same_day_near_close"
# Rolling WMA RRG · terminal values identical once lookback ≥ ~3×LENGTH (LENGTH=20 → 60).
# 150 keeps a comfortable margin for accel lookback + session placeholder rows.
RRG_LIVE_LOOKBACK_BARS = 150

ActionKind = Literal["entry", "swap", "max_hold_exit", "extension_exit", "pyramid_add"]


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
    pyramid_parent_cid: str | None = None
    slot: int | None = None


@dataclass
class ScreenResult:
    session_date: str
    polled_at: str
    config: ScoreSwapCConfig
    mono_top10: list[ScanRow] = field(default_factory=list)  # 昨收 PIT · fresh mono 全池（legacy 欄位名）
    pool_as_of: str = ""  # 信號日（同日近收盤 or 昨交易日）
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
    extension_alerts: list[ExtensionAlert] = field(default_factory=list)


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


def _spread_gate_anchor(config: ScoreSwapCConfig | None = None) -> str:
    """Spread-gate / entry open · SSOT = champion no_trade_before (+ env)."""
    env = os.environ.get("C18ACC_NO_TRADE_BEFORE", "").strip()
    if env:
        return env
    cfg = config or _resolve_champion_config()
    return str(cfg.no_trade_before or SPREAD_GATE_NO_TRADE_BEFORE)


def _uses_same_day_near_close_pool(config: ScoreSwapCConfig | None = None) -> bool:
    """Afternoon entry (≥13:00) → lock same-session provisional fresh pool."""
    return _spread_gate_anchor(config) >= "13:00"


def lock_same_day_near_close_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    close: pd.DataFrame,
    bench: pd.Series,
    poll_minute: str,
    config: ScoreSwapCConfig | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[ScanRow], str, int]:
    """同日近收盤 fresh 池 · kbar 覆寫 session close（≈13:30 名單）。"""
    from rrg_universe_snapshot import _intraday_benchmark_price, kbar_close_at_minute

    cfg = config or _resolve_champion_config()
    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    if session not in prov.index.astype(str):
        prov.loc[session] = float("nan")
        bench_p = bench_p.reindex(prov.index).astype(float)
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    universe = [str(w["stock_id"]) for w in watch]
    for sid in universe:
        if sid not in prov.columns:
            continue
        px = kbar_close_at_minute(conn, sid, session, poll_minute)
        if px is not None and px > 0:
            prov.at[session, sid] = float(px)
    bpx = _intraday_benchmark_price(conn, session, poll_minute, price_mode="kbar")
    if bpx is not None and bpx > 0 and session in bench_p.index:
        bench_p.at[session] = float(bpx)
    all_mono, fresh_mono = scan_rows_from_panels(
        conn, session, prov, bench_p, etf_codes=etf_codes
    )
    if cfg.candidate_pool == "fresh":
        ranked = sorted(fresh_mono, key=lambda r: (-r.seg_last, r.stock_id))
        return ranked, session, len(fresh_mono)
    close_sig, bench_sig, rs_ratio, rs_mom, signal_dates = _signal_rrg_panels(
        prov, bench_p, session
    )
    ranked = build_pit_candidate_pool(
        fresh_mono=fresh_mono,
        mono_rows=all_mono,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=signal_dates,
        as_of=session,
        config=cfg,
    )
    return ranked, session, len(fresh_mono)


def _resolve_champion_config(config: ScoreSwapCConfig | None = None) -> ScoreSwapCConfig:
    if config is not None:
        return config
    cfg = champion_score_swap_c_config()
    env_ntb = os.environ.get("C18ACC_NO_TRADE_BEFORE", "").strip()
    if env_ntb and str(cfg.no_trade_before) != env_ntb:
        cfg = replace(cfg, no_trade_before=env_ntb)
    assert_pool_matches_champion(str(cfg.candidate_pool))
    return cfg


def _pool_section_title(candidate_pool: str) -> str:
    if candidate_pool == "fresh":
        return "fresh mono 全池"
    if candidate_pool == "fresh_union_accel":
        return "fresh∪accel 全池"
    return "fresh mono 全池"


def lock_pit_fresh_pool(
    conn: sqlite3.Connection,
    *,
    session: str,
    close: pd.DataFrame,
    bench: pd.Series,
    config: ScoreSwapCConfig | None = None,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[ScanRow], str, int]:
    """昨收 PIT · champion candidate pool（依 seg_last 排序 · 不裁 top10）。"""
    cfg = config or _resolve_champion_config()
    full_dates = close.index.astype(str).tolist()
    signal_as_of = _signal_date_for_session(full_dates, session)
    if not signal_as_of:
        return [], "", 0
    all_mono, fresh_mono = scan_rows_from_panels(
        conn, signal_as_of, close, bench, etf_codes=etf_codes
    )
    if cfg.candidate_pool == "fresh":
        ranked = sorted(fresh_mono, key=lambda r: (-r.seg_last, r.stock_id))
        return ranked, signal_as_of, len(fresh_mono)
    close_sig, bench_sig, rs_ratio, rs_mom, signal_dates = _signal_rrg_panels(
        close, bench, signal_as_of
    )
    ranked = build_pit_candidate_pool(
        fresh_mono=fresh_mono,
        mono_rows=all_mono,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=signal_dates,
        as_of=signal_as_of,
        config=cfg,
    )
    return ranked, signal_as_of, len(fresh_mono)


def _parse_pool_override(session: str) -> list[str] | None:
    """C18ACC_POOL_OVERRIDE · 預設關閉（採納對齊 · 需 C18ACC_ALLOW_POOL_OVERRIDE=1）。"""
    if not _env_flag("C18ACC_ALLOW_POOL_OVERRIDE", "0"):
        return None
    raw = os.environ.get("C18ACC_POOL_OVERRIDE", "").strip()
    if not raw:
        return None
    scope = os.environ.get("C18ACC_POOL_OVERRIDE_DATE", "").strip() or session
    if scope != session:
        return None
    ids = [x.strip() for x in raw.replace(" ", "").split(",") if x.strip()]
    return ids or None


def _resolve_prior_ret_max() -> float | None:
    """G_R5_12 · C18ACC_PRIOR_5D_RET_MAX（預設 0.12 · 空字串=關閉）。"""
    raw = os.environ.get("C18ACC_PRIOR_5D_RET_MAX", "0.12").strip()
    if not raw or raw.lower() in ("off", "none", "null"):
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.12


def _resolve_prior_ret_days() -> int:
    raw = os.environ.get("C18ACC_PRIOR_RET_DAYS", "").strip()
    if not raw:
        cfg = champion_score_swap_c_config()
        return max(1, int(cfg.entry_prior_ret_days or 5))
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _prior_ret_gate_allows(
    *,
    close: pd.DataFrame | None,
    full_dates: list[str] | None,
    session: str,
    stock_id: str,
) -> bool:
    max_ret = _resolve_prior_ret_max()
    if max_ret is None or close is None or not full_dates:
        return True
    return prior_ret_gate_allows(
        close,
        stock_id,
        full_dates,
        as_of=session,
        n_days=_resolve_prior_ret_days(),
        max_ret=max_ret,
    )


def _prior_ret_gate_blocker_clause(
    *,
    close: pd.DataFrame | None,
    full_dates: list[str] | None,
    session: str,
    stock_id: str,
) -> str:
    max_ret = _resolve_prior_ret_max()
    if max_ret is None or close is None or not full_dates:
        return ""
    if _prior_ret_gate_allows(
        close=close, full_dates=full_dates, session=session, stock_id=stock_id
    ):
        return ""
    from research.backtest.rrg_mono_score_swap_c import prior_n_day_return

    ret = prior_n_day_return(
        close,
        stock_id,
        full_dates,
        as_of=session,
        n_days=_resolve_prior_ret_days(),
    )
    pct = f"{ret * 100:.1f}%" if ret is not None else "?"
    return (
        f"{stock_id}：prior{_resolve_prior_ret_days()}d={pct} > {max_ret:.0%} · chase gate"
    )


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
    config: ScoreSwapCConfig | None = None,
    override_ids: list[str] | None = None,
    poll_minute: str | None = None,
) -> tuple[list[ScanRow], str, int, list[str]]:
    cfg = config or _resolve_champion_config()
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
        state["pool_mode"] = POOL_MODE_PRIOR_PIT
        state["pool"] = [_scan_row_to_dict(r) for r in pool]
        return pool, signal_as_of, len(pool), override_ids

    had_override = bool(state.get("pool_override"))
    state.pop("pool_override", None)
    if had_override:
        state.pop("pool", None)
        state.pop("pool_as_of", None)
        state.pop("pool_fresh_n", None)
        state.pop("pool_session", None)
        state.pop("pool_mode", None)

    ntb = _spread_gate_anchor(cfg)
    want_same_day = _uses_same_day_near_close_pool(cfg)
    if want_same_day:
        if not poll_minute or poll_minute < ntb:
            # Pre-window: keep prior-day pool for held exits / diagnostics only
            if (
                state.get("pool_session") == session
                and state.get("pool_mode") == POOL_MODE_PRIOR_PIT
                and state.get("pool")
            ):
                rows = [_scan_row_from_dict(x) for x in state["pool"] if isinstance(x, dict)]
                return (
                    rows,
                    str(state.get("pool_as_of") or ""),
                    int(state.get("pool_fresh_n") or len(rows)),
                    [],
                )
            pool, signal_as_of, fresh_n = lock_pit_fresh_pool(
                conn, session=session, close=close, bench=bench, config=cfg
            )
            state["pool_session"] = session
            state["pool_as_of"] = signal_as_of
            state["pool_fresh_n"] = fresh_n
            state["pool_mode"] = POOL_MODE_PRIOR_PIT
            state["pool"] = [_scan_row_to_dict(r) for r in pool]
            return pool, signal_as_of, fresh_n, []

        # At/after open: same-day near-close lock (invalidate prior-day cache once)
        if (
            state.get("pool_session") == session
            and state.get("pool_mode") == POOL_MODE_SAME_DAY
            and state.get("pool")
        ):
            rows = [_scan_row_from_dict(x) for x in state["pool"] if isinstance(x, dict)]
            return (
                rows,
                str(state.get("pool_as_of") or ""),
                int(state.get("pool_fresh_n") or len(rows)),
                [],
            )
        pool, signal_as_of, fresh_n = lock_same_day_near_close_pool(
            conn,
            session=session,
            close=close,
            bench=bench,
            poll_minute=poll_minute,
            config=cfg,
        )
        state["pool_session"] = session
        state["pool_as_of"] = signal_as_of
        state["pool_fresh_n"] = fresh_n
        state["pool_mode"] = POOL_MODE_SAME_DAY
        state["pool"] = [_scan_row_to_dict(r) for r in pool]
        # New pool → reset entry confirm streak
        state["entry_confirm"] = {}
        state.pop("spread_gate_snapshot", None)
        return pool, signal_as_of, fresh_n, []

    if state.get("pool_session") == session and state.get("pool"):
        rows = [_scan_row_from_dict(x) for x in state["pool"] if isinstance(x, dict)]
        return rows, str(state.get("pool_as_of") or ""), int(state.get("pool_fresh_n") or len(rows)), []
    pool, signal_as_of, fresh_n = lock_pit_fresh_pool(
        conn, session=session, close=close, bench=bench, config=cfg
    )
    state["pool_session"] = session
    state["pool_as_of"] = signal_as_of
    state["pool_fresh_n"] = fresh_n
    state["pool_mode"] = POOL_MODE_PRIOR_PIT
    state["pool"] = [_scan_row_to_dict(r) for r in pool]
    return pool, signal_as_of, fresh_n, []


def sync_watchlist_kbar(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    trade_date: str,
    *,
    env_key: str = "C18ACC_KBAR_SYNC",
    sleep_sec: float = 0.35,
    poll_minute: str | None = None,
    skip_fresh: bool | None = None,
) -> int:
    """主線盤中 K sync · Yahoo 原生 5m 寫入 stock_kbar_5m。

    ``poll_minute`` 有值且 ``skip_fresh`` 時，若 DB 已有覆蓋該格點的 5m bar 則跳過 API
    （env ``KBAR_SYNC_SKIP_FRESH`` 預設 1）。``KBAR_SYNC_YAHOO_INTERVAL`` 預設 ``5m``；
    設 ``1m`` 可回退舊路徑（聚合寫入）。設 ``KBAR_SYNC_STORE_1M=1`` 會**額外**拉 1m
    寫入 stock_kbar_1m（research / extension）。
    """
    if not _env_flag(env_key, "1"):
        return 0
    if skip_fresh is None:
        skip_fresh = os.environ.get("KBAR_SYNC_SKIP_FRESH", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
    store_1m = os.environ.get("KBAR_SYNC_STORE_1M", "0").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    yahoo_interval = os.environ.get("KBAR_SYNC_YAHOO_INTERVAL", "5m").strip().lower()
    if yahoo_interval not in ("5m", "1m"):
        yahoo_interval = "5m"
    d = date.fromisoformat(trade_date)
    total = 0
    for sid in sorted({str(x) for x in stock_ids if x}):
        if skip_fresh and poll_minute and kbar_5m_fresh_for_poll(
            conn, sid, trade_date, poll_minute
        ):
            continue
        try:
            if yahoo_interval == "5m":
                rows_5m, _ = fetch_tw_intraday_kbar_rows(sid, d, d, interval="5m")
                if rows_5m:
                    total += upsert_yahoo_5m_rows(conn, sid, rows_5m)
            else:
                rows_1m, _ = fetch_tw_intraday_kbar_rows(sid, d, d, interval="1m")
                if rows_1m:
                    total += upsert_1m_rows_as_5m(conn, sid, rows_1m)
                    if store_1m:
                        total += upsert_stock_kbar_1m(conn, rows_1m)
            if store_1m and yahoo_interval == "5m":
                rows_1m, _ = fetch_tw_intraday_kbar_rows(sid, d, d, interval="1m")
                if rows_1m:
                    total += upsert_stock_kbar_1m(conn, rows_1m)
        except Exception:
            continue
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return total


def _daily_bars_fingerprint(conn: sqlite3.Connection) -> tuple[str, int]:
    row = conn.execute(
        "SELECT COALESCE(MAX(trade_date), ''), COUNT(*) FROM stock_daily_bars"
    ).fetchone()
    return (str(row[0] or ""), int(row[1] or 0))


def load_close_bench_for_screen(
    conn: sqlite3.Connection,
    *,
    use_cache: bool | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """Load close + benchmark for live screen.

    Same-day disk cache keyed by ``(max(trade_date), count(*))`` — intraday ticks
    share one panel load; morning/evening sync bumps the fingerprint and rebuilds.
    """
    if use_cache is None:
        use_cache = _env_flag("C18ACC_CLOSE_PANEL_CACHE", "1")
    fp = _daily_bars_fingerprint(conn)
    if use_cache and CLOSE_PANEL_CACHE_PATH.is_file():
        try:
            cached = pd.read_pickle(CLOSE_PANEL_CACHE_PATH)
            if (
                isinstance(cached, dict)
                and cached.get("fingerprint") == fp
                and isinstance(cached.get("close"), pd.DataFrame)
                and isinstance(cached.get("bench"), pd.Series)
            ):
                return cached["close"], cached["bench"]
        except Exception:
            pass
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    if use_cache:
        try:
            CLOSE_PANEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            pd.to_pickle(
                {"fingerprint": fp, "close": close, "bench": bench},
                CLOSE_PANEL_CACHE_PATH,
            )
        except Exception:
            pass
    return close, bench


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
    *,
    stock_ids: list[str] | None = None,
    lookback_bars: int | None = RRG_LIVE_LOOKBACK_BARS,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.DataFrame, list[str]]:
    """日線 RRG 僅用到 signal_as_of（昨收 PIT）。

    ``stock_ids`` / ``lookback_bars`` 僅縮計算範圍；rolling WMA 在足夠 lookback
    下與全歷史終端值等價（live screen 熱路徑）。
    """
    mask = close.index.astype(str) <= signal_as_of
    close_sig = close.loc[mask]
    if stock_ids is not None:
        cols = [sid for sid in stock_ids if sid in close_sig.columns]
        close_sig = close_sig[cols] if cols else close_sig.iloc[:, :0]
    if (
        lookback_bars is not None
        and lookback_bars > 0
        and len(close_sig.index) > lookback_bars
    ):
        close_sig = close_sig.iloc[-lookback_bars:]
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


def _entry_px_blocker_clause(
    stock_id: str,
    poll_minute: str,
    *,
    px_ok: bool,
    rank: int | None = None,
) -> str:
    if px_ok:
        return ""
    rank_bit = f" · 排序 #{rank}" if rank is not None else ""
    return f"{stock_id}{rank_bit}：尚無盤中報價 @ {poll_minute}"


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
    state: dict[str, Any] | None = None,
    score_mode: str = "avg_accel",
) -> str:
    if has_entry_action:
        return "—"
    if slots_n >= MAX_SLOTS:
        return "槽位已滿（3/3）· 僅評估換倉"
    st = state or {}
    use_scale = score_mode == "scale"
    for i, row in enumerate(ranked[:MAX_SLOTS]):
        sid = row.stock_id
        if sid in held or sid in entries_today:
            continue
        c = int(confirm.get(sid, 0))
        if c < need_confirm:
            continue
        spread_clause = _spread_gate_blocker_clause(st, sid, poll_minute)
        if spread_clause:
            return spread_clause
        prior_clause = _prior_ret_gate_blocker_clause(
            close=close,
            full_dates=list(close.index.astype(str)),
            session=session,
            stock_id=sid,
        )
        if prior_clause:
            return prior_clause
        close_ok, px_ok = _c0_scale_diagnostics(
            conn, sid, session, poll_minute, close, kbar_cache
        )
        if use_scale:
            clause = _c0_scale_blocker_clause(
                sid, poll_minute, close_ok=close_ok, px_ok=px_ok, rank=i + 1
            )
        else:
            clause = _entry_px_blocker_clause(
                sid, poll_minute, px_ok=px_ok, rank=i + 1
            )
        if clause:
            return clause
    sid = lead_row.stock_id
    if sid in held:
        return f"{sid}：已持有"
    if sid in entries_today:
        return f"{sid}：本日已進場"
    spread_clause = _spread_gate_blocker_clause(st, sid, poll_minute)
    if spread_clause:
        return spread_clause
    prior_clause = _prior_ret_gate_blocker_clause(
        close=close,
        full_dates=list(close.index.astype(str)),
        session=session,
        stock_id=sid,
    )
    if prior_clause:
        return prior_clause
    close_ok, px_ok = _c0_scale_diagnostics(
        conn, sid, session, poll_minute, close, kbar_cache
    )
    if use_scale:
        scale_clause = _c0_scale_blocker_clause(
            sid, poll_minute, close_ok=close_ok, px_ok=px_ok, rank=lead_rank
        )
        if scale_clause:
            return scale_clause
    else:
        px_clause = _entry_px_blocker_clause(
            sid, poll_minute, px_ok=px_ok, rank=lead_rank
        )
        if px_clause:
            return px_clause
    c = int(confirm.get(sid, 0))
    if c < need_confirm:
        label = "C0" if use_scale else "avg_accel"
        return f"{sid} · {label} 排序 #{lead_rank}：confirm_bars {c}/{need_confirm}"
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


def _rank_entry_pool(
    pool: list[ScanRow],
    *,
    c0_cfg: Any,
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series | None,
    session: str,
    poll_minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
    pool_as_of: str = "",
    accel_lookback: int = 4,
) -> list[ScanRow]:
    score_mode = str(getattr(c0_cfg, "score_mode", "avg_accel") or "avg_accel")
    rank_as_of = pool_as_of if score_mode == "avg_accel" and pool_as_of else session
    return rank_shortlist(
        pool,
        config=c0_cfg,
        conn=conn,
        close=close,
        bench=bench if bench is not None else pd.Series(dtype=float),
        trade_date=rank_as_of,
        minute=poll_minute,
        kbar_cache=kbar_cache,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=full_dates,
        accel_lookback=accel_lookback,
    )


def _entry_rank_score(
    row: ScanRow | None,
    *,
    score_mode: str,
    conn: sqlite3.Connection,
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    rs_ratio: pd.DataFrame | None,
    rs_mom: pd.DataFrame | None,
    full_dates: list[str] | None,
    pool_as_of: str,
    accel_lookback: int,
) -> float | None:
    if row is None:
        return None
    if score_mode == "avg_accel":
        if rs_ratio is None or rs_mom is None or not full_dates or not pool_as_of:
            return None
        a = _avg_accel_scalar(
            rs_ratio,
            rs_mom,
            full_dates,
            pool_as_of,
            row.stock_id,
            lb=max(2, int(accel_lookback)),
        )
        return float(a) if a is not None else None
    scaled, _ = _row_scale_metrics(conn, row, session, poll_minute, close, kbar_cache)
    return scaled


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
    bench: pd.Series | None = None,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
    pool_as_of: str = "",
    accel_lookback: int = 4,
) -> dict[str, Any] | None:
    last_poll = state.get("last_poll") if isinstance(state.get("last_poll"), dict) else None
    score_mode = str(getattr(c0_cfg, "score_mode", "avg_accel") or "avg_accel")

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
        entry_score = _entry_rank_score(
            lead_row,
            score_mode=score_mode,
            conn=conn,
            session=session,
            poll_minute=poll_minute,
            close=close,
            kbar_cache=kbar_cache,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=full_dates,
            pool_as_of=pool_as_of,
            accel_lookback=accel_lookback,
        )
        lead_confirm = int((state.get("entry_confirm") or {}).get(act.stock_id, 0))
        if lead_row is not None:
            result.lead_drift = _lead_drift_plain(
                lead=lead_row,
                rank=1,
                confirm=lead_confirm,
                scaled=entry_score,
                last_poll=last_poll,
            )
        else:
            result.lead_drift = "首輪尚無比較" if not last_poll else "與上輪差不多"
        return {
            "minute": poll_minute,
            "lead": act.stock_id,
            "rank": 1,
            "confirm": lead_confirm,
            "scaled": entry_score,
        }

    if not _entry_allowed(now):
        result.entry_gate = "目前不在進場時段"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = "盤外時段 · 不評估進場"
        return None

    if not pool:
        result.entry_gate = "候選池為空"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = "PIT 候選池為空"
        return None

    ranked = _rank_entry_pool(
        pool,
        c0_cfg=c0_cfg,
        conn=conn,
        close=close,
        bench=bench,
        session=session,
        poll_minute=poll_minute,
        kbar_cache=kbar_cache,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=full_dates,
        pool_as_of=pool_as_of,
        accel_lookback=accel_lookback,
    )
    if not ranked:
        result.entry_gate = "無法評估進場排序"
        result.lead = "—"
        result.lead_drift = "—"
        result.blocker = (
            "盤中 kbar 不足 · 無法 C0 scale 排序"
            if score_mode == "scale"
            else "PIT 面板不足 · 無法 avg_accel 排序"
        )
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
    entry_score = _entry_rank_score(
        lead_row,
        score_mode=score_mode,
        conn=conn,
        session=session,
        poll_minute=poll_minute,
        close=close,
        kbar_cache=kbar_cache,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=full_dates,
        pool_as_of=pool_as_of,
        accel_lookback=accel_lookback,
    )
    need_confirm = int(c0_cfg.confirm_bars)
    px = _kbar_px_at(conn, lead_row.stock_id, session, poll_minute, close, kbar_cache)
    ready = (
        lead_confirm >= need_confirm
        and lead_row.stock_id not in held
        and lead_row.stock_id not in entries_today
        and px is not None
        and px > 0
        and _spread_gate_allows(state, lead_row.stock_id, poll_minute=poll_minute)
        and _prior_ret_gate_allows(
            close=close, full_dates=full_dates, session=session, stock_id=lead_row.stock_id
        )
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
        scaled=entry_score,
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
        state=state,
        score_mode=score_mode,
    )
    return {
        "minute": poll_minute,
        "lead": lead_row.stock_id,
        "rank": lead_rank,
        "confirm": lead_confirm,
        "scaled": entry_score,
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
    ext_bits = [
        f"{a.stock_id}:heat={a.heat_score}@{a.minute}" for a in result.extension_alerts
    ]
    ext_s = ";".join(ext_bits) if ext_bits else "—"
    line = (
        f"{result.polled_at} | session={result.session_date} | signal={signal} | "
        f"pool_as_of={result.pool_as_of or '—'} | minute={result.poll_minute or '—'} | "
        f"override={override_s} | pool={top_ids} | fresh_n={result.disp_universe_n} | "
        f"kbar_sync={result.kbar_sync_n} | slots={len(result.slots)}/{MAX_SLOTS} | "
        f"swaps_today={result.swaps_today} | actions={actions_s} | ext_overlay={ext_s} | "
        f"skip={skip} | entry_gate={entry_gate} | lead={lead} | lead_drift={lead_drift} | "
        f"blocker={blocker}\n"
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


def _resolve_entry_c_cfg() -> Any:
    """C18acc champion entry · avg_accel rank · confirm via C18ACC_CONFIRM_BARS (default 1)."""
    from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config

    bars = _env_int("C18ACC_CONFIRM_BARS", 1)
    return champion_entry_c_config(confirm_bars=bars)


def _resolve_avoid_spread_mixed() -> bool:
    """Entry+swap buy gate · spread_class != mixed at first poll ≥ no_trade_before."""
    return _env_flag("C18ACC_AVOID_SPREAD_MIXED", "1")


def _ensure_spread_gate_snapshot(
    state: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    session: str,
    poll_minute: str,
    stock_ids: list[str],
    close: pd.DataFrame,
    bench: pd.Series,
    config: ScoreSwapCConfig | None = None,
) -> None:
    if not _resolve_avoid_spread_mixed():
        return
    anchor = _spread_gate_anchor(config)
    if poll_minute < anchor:
        return
    snap = state.get("spread_gate_snapshot")
    if isinstance(snap, dict) and snap.get("_poll_minute"):
        return
    spreads = spread_class_snapshots_at_minute(
        conn,
        close=close,
        bench=bench,
        trade_date=session,
        minute=poll_minute,
        stock_ids=stock_ids,
    )
    state["spread_gate_snapshot"] = {**spreads, "_poll_minute": poll_minute}


def _spread_gate_allows(
    state: dict[str, Any],
    stock_id: str,
    *,
    poll_minute: str,
    config: ScoreSwapCConfig | None = None,
) -> bool:
    if not _resolve_avoid_spread_mixed():
        return True
    anchor = _spread_gate_anchor(config)
    snap = state.get("spread_gate_snapshot")
    if not isinstance(snap, dict) or not snap.get("_poll_minute"):
        return poll_minute >= anchor
    sc = snap.get(stock_id)
    if sc is None:
        return True
    return sc != "mixed"


def _spread_gate_blocker_clause(
    state: dict[str, Any],
    stock_id: str,
    poll_minute: str,
    config: ScoreSwapCConfig | None = None,
) -> str:
    if not _resolve_avoid_spread_mixed():
        return ""
    anchor = _spread_gate_anchor(config)
    if poll_minute < anchor:
        return (
            f"{stock_id}：spread gate 待 {anchor} 錨點"
            f"（W5/W20 · avoid spread_mixed）"
        )
    snap = state.get("spread_gate_snapshot")
    if not isinstance(snap, dict) or not snap.get("_poll_minute"):
        return ""
    if snap.get(stock_id) == "mixed":
        return f"{stock_id}：spread_class=mixed · avoid gate"
    return ""


def _now_local() -> datetime:
    return datetime.now()


def _poll_window_ok(now: datetime | None = None) -> bool:
    now = now or _now_local()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(9, 0) <= t <= dt_time(13, 30)


def _swap_allowed(
    now: datetime | None = None,
    *,
    config: ScoreSwapCConfig | None = None,
) -> bool:
    now = now or _now_local()
    anchor = _spread_gate_anchor(config)
    hh, mm = (int(x) for x in anchor.split(":")[:2])
    return now.time() >= dt_time(hh, mm)


def _entry_allowed(
    now: datetime | None = None,
    *,
    config: ScoreSwapCConfig | None = None,
) -> bool:
    now = now or _now_local()
    t = now.time()
    anchor = _spread_gate_anchor(config)
    hh, mm = (int(x) for x in anchor.split(":")[:2])
    return dt_time(hh, mm) <= t <= dt_time(13, 30)


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
        state.pop("pool_mode", None)
        state.pop("last_poll", None)
        state.pop("spread_gate_snapshot", None)


def _lot_shares(price: float, budget_twd: float, *, board_lot: bool = False) -> int:
    """依槽位預算計算股數。預設零股（floor 預算／價）；C18ACC_BOARD_LOT=1 為整張。"""
    if price <= 0 or budget_twd <= 0 or math.isnan(price):
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
    bench: pd.Series | None = None,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
    pool_as_of: str = "",
    accel_lookback: int = 4,
) -> tuple[ScanRow | None, float | None]:
    """单轮 poll · champion entry rank（avg_accel）· confirm 跨轮累计。"""
    slots = state.get("slots") or []
    if len(slots) >= MAX_SLOTS or not pool:
        return None, None
    ranked = _rank_entry_pool(
        pool,
        c0_cfg=c0_cfg,
        conn=conn,
        close=close,
        bench=bench,
        session=session,
        poll_minute=poll_minute,
        kbar_cache=kbar_cache,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=full_dates,
        pool_as_of=pool_as_of,
        accel_lookback=accel_lookback,
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
        if not _spread_gate_allows(state, sid, poll_minute=poll_minute):
            continue
        if not _prior_ret_gate_allows(
            close=close, full_dates=full_dates, session=session, stock_id=sid
        ):
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
    poll_minute: str = "",
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
        elif act.kind == "extension_exit" and act.side == "sell":
            if _env_flag("C18ACC_EXTENSION_APPLY_STATE", "0"):
                slots = [p for p in slots if str(p["stock_id"]) != act.stock_id]
                state.setdefault("history", []).append(
                    {
                        "stock_id": act.stock_id,
                        "stock_name": act.stock_name,
                        "exit_date": session_date,
                        "exit_reason": "extension_overlay",
                    }
                )
        elif act.kind == "swap" and act.side == "sell" and act.counterparty_id:
            sell_id = act.stock_id
            buy_id = act.counterparty_id
            buy_name = act.counterparty_name or ""
            buy_in_batch = any(
                a.kind == "swap" and a.side == "buy" and a.stock_id == buy_id for a in actions
            )
            slot_id = next(
                (p.get("slot") for p in slots if str(p["stock_id"]) == sell_id),
                len(slots),
            )
            slots = [p for p in slots if str(p["stock_id"]) != sell_id]
            if buy_in_batch:
                slots.append(
                    {
                        "slot": slot_id,
                        "stock_id": buy_id,
                        "stock_name": buy_name,
                        "signal_date": session_date,
                        "entry_date": session_date,
                        "entry_px": act.price,
                        "entry_poll_minute": poll_minute or None,
                        "seg_last": 0.0,
                        "disp": 0.0,
                        "entry_leg": config.entry_leg,
                    }
                )
                swaps_today += 1
                entries_today.append(buy_id)
            else:
                state.setdefault("history", []).append(
                    {
                        "stock_id": sell_id,
                        "stock_name": act.stock_name,
                        "exit_date": session_date,
                        "exit_reason": "swap_out_pending_buy",
                        "counterparty_id": buy_id,
                    }
                )
        elif act.kind == "swap" and act.side == "buy":
            sell_in_batch = any(
                a.kind == "swap" and a.side == "sell" and a.counterparty_id == act.stock_id
                for a in actions
            )
            if sell_in_batch:
                continue
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
                    "entry_poll_minute": poll_minute or None,
                    "seg_last": 0.0,
                    "disp": 0.0,
                    "entry_leg": config.entry_leg,
                }
            )
            swaps_today += 1
            entries_today.append(act.stock_id)
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
                    "entry_poll_minute": poll_minute or None,
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
        meta: dict[str, Any] = {"action_kind": act.kind}
        if act.pyramid_parent_cid:
            meta["pyramid_parent_cid"] = act.pyramid_parent_cid
        if act.slot is not None:
            meta["slot"] = act.slot
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
                "metadata": meta,
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
        f"> spread gate：**{'avoid mixed' if _resolve_avoid_spread_mixed() else 'off'}** · anchor ≥ {SPREAD_GATE_NO_TRADE_BEFORE} · W5/W20",
        f"> chase gate：**prior{_resolve_prior_ret_days()}d ≤ {_resolve_prior_ret_max() if _resolve_prior_ret_max() is not None else 'off'}**（G_R5_12）",
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
        else f"## {_pool_section_title(cfg.candidate_pool)}（昨收 PIT · {result.pool_as_of or '—'}）"
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

    lines.extend(["## Extension overlay（X4 · dry-run）", ""])
    if not result.extension_alerts:
        lines.append("_持倉無 heat≥70 觸發。_")
    else:
        lines.append("| 代號 | 時間 | 價 | heat | 區間 | ext_prev% | 備註 |")
        lines.append("|------|------|-----|------|------|-----------|------|")
        for a in result.extension_alerts:
            lines.append(
                f"| {a.stock_id} | {a.minute} | {a.price:.2f} | {a.heat_score} | "
                f"{a.zone} | {a.ext_prev_pct:.1f} | {a.note} |"
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
    cfg = config or _resolve_champion_config()
    c0_cfg = _resolve_entry_c_cfg()
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
        result.skip_reason = "outside poll window (Mon–Fri 09:00–13:30)"
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

    close, bench = load_close_bench_for_screen(conn)
    if session not in close.index.astype(str):
        close = close.copy()
        close.loc[session] = float("nan")
        bench = bench.reindex(close.index).astype(float)

    state = load_slot_state()
    _reset_daily_counters(state, session)
    override_ids = _parse_pool_override(session)
    pool, pool_as_of, fresh_n, active_override = load_or_lock_pool(
        state,
        conn,
        session=session,
        close=close,
        bench=bench,
        config=cfg,
        override_ids=override_ids,
        poll_minute=poll_minute,
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
    result.kbar_sync_n = sync_watchlist_kbar(
        conn, watch_ids, session, poll_minute=poll_minute
    )
    result.tick_stock_n = result.kbar_watch_n

    if not pool_as_of:
        result.skip_reason = "無信號日可鎖定選股池"
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

    rrg_ids = sorted({str(x) for x in watch_ids if x})
    close_sig, _bench_sig, rs_ratio, rs_mom, signal_dates = _signal_rrg_panels(
        close,
        bench,
        pool_as_of,
        stock_ids=rrg_ids,
        lookback_bars=RRG_LIVE_LOOKBACK_BARS,
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
    slot_ids = [str(p["stock_id"]) for p in slots]
    if cfg.accel_sell_bypass_on_spread_lost and slot_ids:
        spread_rs_panel = build_daily_spread_rs_panel(
            close,
            bench,
            stock_ids=slot_ids,
            lookback_bars=RRG_LIVE_LOOKBACK_BARS,
        )
    else:
        spread_rs_panel = None
    spread_bypass = held_accel_bypass_on_spread_lost(
        config=cfg,
        spread_rs_panel=spread_rs_panel,
        as_of=pool_as_of,
        slot_stock_ids=slot_ids,
    )

    _ensure_spread_gate_snapshot(
        state,
        conn=conn,
        session=session,
        poll_minute=poll_minute,
        stock_ids=sorted(set(watch_ids)),
        close=close,
        bench=bench,
        config=cfg,
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
        _swap_allowed(now, config=cfg)
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
            held_accel_bypass=spread_bypass,
        )
        if sell is not None and buy is not None:
            hold_days = _trading_days_between(session_dates, str(sell["entry_date"]), session)
            if hold_days < cfg.min_hold_days:
                sell, buy = None, None
            elif not _spread_gate_allows(
                state, buy.stock_id, poll_minute=poll_minute, config=cfg
            ):
                sell, buy = None, None
            elif not _prior_ret_gate_allows(
                close=close,
                full_dates=session_dates,
                session=session,
                stock_id=buy.stock_id,
            ):
                sell, buy = None, None
        if sell is not None and buy is not None:
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

    _entry_pool_ok = (
        state.get("pool_mode") == POOL_MODE_SAME_DAY
        if _uses_same_day_near_close_pool(cfg)
        else True
    )
    if (
        _entry_allowed(now, config=cfg)
        and poll_minute >= cfg.no_trade_before
        and len(slots) < MAX_SLOTS
        and pool
        and _entry_pool_ok
    ):
        entry_row, entry_px = _try_c0_entry(
            conn,
            state=state,
            pool=pool,
            session=session,
            poll_minute=poll_minute,
            close=close,
            kbar_cache=kbar_cache,
            c0_cfg=c0_cfg,
            bench=bench,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=signal_dates,
            pool_as_of=pool_as_of,
            accel_lookback=int(cfg.accel_lookback),
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
                note=f"avg_accel @ {poll_minute} confirm={c0_cfg.confirm_bars}",
            )
            if act:
                actions.append(act)

    result.actions = actions

    if overlay_enabled() and slots and pool_as_of:
        ext_interval = poll_interval_for_now(now)
        session_dates = close.index.astype(str).tolist()
        alerts = scan_held_extension_alerts(
            conn,
            slots=slots,
            session=session,
            poll_minute=poll_minute,
            full_dates=session_dates,
            min_hold_days=cfg.min_hold_days,
            poll_interval_min=ext_interval,
        )
        result.extension_alerts = alerts
        append_overlay_log(session=session, poll_minute=poll_minute, alerts=alerts)
        write_overlay_snapshot(session=session, poll_minute=poll_minute, alerts=alerts)
        if _env_flag("C18ACC_EXTENSION_EMIT_INTENT", "0"):
            sold_ids = {a.stock_id for a in actions if a.side == "sell"}
            for alert in alerts:
                if alert.stock_id in sold_ids:
                    continue
                pos = next((p for p in slots if str(p["stock_id"]) == alert.stock_id), None)
                if not pos:
                    continue
                act = _make_action(
                    kind="extension_exit",
                    row=pos,
                    side="sell",
                    price=alert.price,
                    quantity_shares=_lot_shares(alert.price, budget, board_lot=board_lot),
                    note=f"extension {alert.exit_mode} · {alert.note}",
                )
                if act:
                    actions.append(act)
                    sold_ids.add(alert.stock_id)
            result.actions = actions

    from order.c18acc_monitor_config import load_c18acc_pyramid_add_monitor_config
    from order.c18acc_position_monitor import scan_c18acc_pyramid_adds
    from order.c18acc_pyramid_ledger import append_pyramid_entry, load_c18acc_pyramid_ledger

    pyr_cfg = load_c18acc_pyramid_add_monitor_config()
    if pyr_cfg.enabled:
        session_dates = close.index.astype(str).tolist()
        pyr_scan = scan_c18acc_pyramid_adds(
            conn,
            state={"slots": slots},
            session_date=session,
            poll_minute=poll_minute,
            trading_dates=session_dates,
            pyramid_cfg=pyr_cfg,
            ledger=load_c18acc_pyramid_ledger(),
        )
        for hit in pyr_scan.hits:
            px = _kbar_px_at(
                conn, hit.position.symbol, session, poll_minute, close, kbar_cache
            )
            if px is None or px <= 0:
                continue
            qty = _lot_shares(px, budget * pyr_cfg.budget_fraction, board_lot=board_lot)
            actions.append(
                ScreenAction(
                    kind="pyramid_add",
                    stock_id=hit.position.symbol,
                    stock_name=hit.position.stock_name,
                    side="buy",
                    price=float(px),
                    quantity_shares=qty,
                    note=f"{hit.reason} @ {poll_minute} slot={hit.position.slot}",
                    pyramid_parent_cid=hit.position.parent_cid,
                    slot=hit.position.slot,
                )
            )
            append_pyramid_entry(
                pyramid_parent_cid=hit.position.parent_cid,
                symbol=hit.position.symbol,
                session_date=session,
                poll_minute=poll_minute,
                slot=hit.position.slot,
                status="dry_run" if dry else "intent_emitted",
                dry_run=dry,
                note=hit.reason,
                ledger=load_c18acc_pyramid_ledger(),
            )
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
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=signal_dates,
        pool_as_of=pool_as_of,
        accel_lookback=int(cfg.accel_lookback),
    )

    state_actions = actions
    order_submit: dict[str, Any] | None = None
    from order.c18acc_order_config import load_c18acc_order_config

    c18_order_cfg = load_c18acc_order_config()
    if c18_order_cfg.order_enabled and (c18_order_cfg.auto_submit or not dry):
        from order.c18acc_order import process_c18acc_orders

        order_submit = process_c18acc_orders(
            actions,
            session_date=session,
            poll_minute=poll_minute,
            dry_run=dry or not c18_order_cfg.auto_submit,
        )
        if not dry and c18_order_cfg.auto_submit:
            state_actions = list(order_submit.get("applied_actions") or [])

    if apply:
        work_state = {**state, "slots": [dict(p) for p in state.get("slots") or []]}
        if state_actions:
            _apply_actions_to_state(
                work_state,
                state_actions,
                session_date=session,
                config=cfg,
                poll_minute=poll_minute,
            )
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

    if order_submit is not None:
        result.order_submit = order_submit  # type: ignore[attr-defined]

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
        f"actions={len(result.actions)} ext_alerts={len(result.extension_alerts)} "
        f"dry_run={result.dry_run} skip={result.skip_reason or '—'} tick_log={tick_log}"
    )
    if result.extension_alerts:
        for a in result.extension_alerts:
            print(f"EXT_OVERLAY {a.stock_id} heat={a.heat_score} @ {a.minute} px={a.price:.2f} ({a.note})")
    print(f"C18ACC_SIGNAL={signal} actions={len(result.actions)}")
    if intent_path:
        print(f"C18acc intent: {intent_path}")
    order_submit = getattr(result, "order_submit", None)
    if isinstance(order_submit, dict):
        rows = [r for r in (order_submit.get("results") or []) if isinstance(r, dict)]
        if rows:
            from order.order_fill_report import maybe_send_fill_report

            maybe_send_fill_report(
                rows,
                sleeve="C18acc",
                env_flag="RUN_RRG_C18ACC_EMAIL",
                header="C18acc 下單結果（白話成交報告）",
            )
    # E@NTB（13:00）：同日候選池鎖定輪 · 寄候選 + 擋單原因（即使無成交）
    import json
    from pathlib import Path

    from stock_db import DATA_DIR
    from order.order_fill_report import maybe_send_c18acc_pool_digest

    state: dict = {}
    slots_path = DATA_DIR / "rrg_c18acc_slots.json"
    if slots_path.is_file():
        try:
            loaded = json.loads(slots_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError):
            state = {}
    pool_rows = [
        {
            "stock_id": getattr(r, "stock_id", ""),
            "stock_name": getattr(r, "stock_name", ""),
            "seg_last": getattr(r, "seg_last", None),
            "disp": getattr(r, "disp", None),
        }
        for r in (result.mono_top10 or [])
    ]
    maybe_send_c18acc_pool_digest(
        session_date=result.session_date,
        poll_minute=result.poll_minute,
        pool_as_of=result.pool_as_of or "",
        pool=pool_rows,
        slots=list(result.slots or []),
        lead=result.lead or "",
        lead_drift=result.lead_drift or "",
        entry_gate=result.entry_gate or "",
        blocker=result.blocker or "",
        spread_gate_snapshot=state.get("spread_gate_snapshot")
        if isinstance(state.get("spread_gate_snapshot"), dict)
        else None,
        entry_confirm=state.get("entry_confirm")
        if isinstance(state.get("entry_confirm"), dict)
        else None,
        actions_n=len(result.actions or []),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
