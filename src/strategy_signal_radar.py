"""Advisory signal radar · buy (C0 entry) / sell (universe extension + holdings overlay).

Human-in-the-loop: email on held intersection by default · no auto orders.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import sqlite3

from c18acc_extension_overlay import ExtensionAlert, overlay_scan_config, scan_held_extension_alerts
from project_config import DEFAULT_ETF_CODES
from project_dotenv import load_project_dotenv
from report_paths import REPORTS_DIR
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, rank_shortlist_scale
from rrg_mono_daily_brief import ScanRow
from rrg_mono_intraday_watch import _session_date
from rrg_mono_swap_accel_screen import (
    _env_flag,
    _env_int,
    _kbar_px_at,
    _now_local,
    _parse_pool_override,
    _poll_minute,
    load_or_lock_pool,
    sync_watchlist_kbar,
)
from buy_observation import (
    BuyObservationPoolSpec,
    PoolObservation,
    evaluate_pool_observation,
    load_buy_observation_config,
    role_label_zh,
    _pool_confirm_state,
    _slice_top_n,
    build_observation_pool,
)
from stock_db import PROJECT_ROOT, load_etf_constituent_watchlist
from stock_db.kbar import kbar_mainline_day_has_data

DEDUP_PATH = PROJECT_ROOT / "data" / "signal_radar_dedup.json"
BUY_STATE_PATH = PROJECT_ROOT / "data" / "signal_radar_buy_state.json"
POSITION_META_PATH = PROJECT_ROOT / "data" / "fubon_position_meta.json"
BUY_STRATEGY_ID = "buy-signal-radar"
SELL_STRATEGY_ID = "sell-signal-radar"

# Buy/sell radar windows · independent of C18acc no_trade_before (E@13:00).
# ABC v3+f1 evaluates on the 30m grid from 09:30 (skip 09:00); do not inherit C18 NTB.
_BUY_RADAR_POLL_START = dt_time(9, 0)
_BUY_RADAR_POLL_END = dt_time(13, 20)
_BUY_RADAR_ENTRY_START = dt_time(9, 5)
_BUY_RADAR_ENTRY_END = dt_time(13, 20)


def _buy_radar_poll_window_ok(now: datetime | None = None) -> bool:
    now = now or _now_local()
    if now.weekday() >= 5:
        return False
    return _BUY_RADAR_POLL_START <= now.time() <= _BUY_RADAR_POLL_END


def _buy_radar_entry_allowed(now: datetime | None = None) -> bool:
    """ABC / C0 advisory entry window · not C18acc champion NTB."""
    now = now or _now_local()
    if now.weekday() >= 5:
        return False
    return _BUY_RADAR_ENTRY_START <= now.time() <= _BUY_RADAR_ENTRY_END


# Replay-only caches · set SIGNAL_RADAR_REPLAY_FAST=1 in replay scripts · live 不啟用
_REPLAY_PANEL_CACHE: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series] | None = None
_REPLAY_BUY_POOL_CACHE: dict[tuple[str, str], tuple[list[ScanRow], str]] = {}
_REPLAY_SELL_ALERTS: dict[str, list[ExtensionAlert]] = {}


def replay_fast_enabled() -> bool:
    return _env_flag("SIGNAL_RADAR_REPLAY_FAST", "0")


def clear_signal_radar_replay_caches() -> None:
    global _REPLAY_PANEL_CACHE
    _REPLAY_PANEL_CACHE = None
    _REPLAY_BUY_POOL_CACHE.clear()
    _REPLAY_SELL_ALERTS.clear()


def _load_market_panels_cached(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    global _REPLAY_PANEL_CACHE
    if replay_fast_enabled() and _REPLAY_PANEL_CACHE is not None:
        return _REPLAY_PANEL_CACHE
    close, opn, vol = load_price_panels(conn)
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn).reindex(close.index)
    if replay_fast_enabled():
        _REPLAY_PANEL_CACHE = (close, opn, vol, bench)
    return close, opn, vol, bench


def _cached_observation_pool(
    conn: sqlite3.Connection,
    spec: BuyObservationPoolSpec,
    *,
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    bench: pd.Series,
    ephemeral: dict[str, Any],
) -> tuple[list[ScanRow], str]:
    if replay_fast_enabled():
        key = (session, spec.id, poll_minute)
        hit = _REPLAY_BUY_POOL_CACHE.get(key)
        if hit is not None:
            return hit
    pool, pool_as_of = build_observation_pool(
        conn,
        spec,
        session=session,
        poll_minute=poll_minute,
        close=close,
        bench=bench,
        ephemeral=ephemeral,
    )
    if replay_fast_enabled():
        _REPLAY_BUY_POOL_CACHE[(session, spec.id, poll_minute)] = (pool, pool_as_of)
    return pool, pool_as_of


def _extension_minute_leq(alert_minute: str, poll_minute: str) -> bool:
    am = alert_minute[:5] if len(alert_minute) >= 5 else alert_minute
    pm = poll_minute[:5] if len(poll_minute) >= 5 else poll_minute
    return am <= pm


def _sell_replay_day_extension_alerts(
    conn: sqlite3.Connection,
    *,
    session: str,
    universe_slots: list[dict[str, Any]],
    session_dates: list[str],
    universe_min_hold: int,
) -> list[ExtensionAlert]:
    if session in _REPLAY_SELL_ALERTS:
        return _REPLAY_SELL_ALERTS[session]
    prev_overlay = os.environ.get("RUN_C18ACC_EXTENSION_OVERLAY")
    os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = "1"
    try:
        alerts = scan_held_extension_alerts(
            conn,
            slots=universe_slots,
            session=session,
            poll_minute="13:30",
            full_dates=session_dates,
            min_hold_days=universe_min_hold,
            poll_interval_min=5,
            scan_config=overlay_scan_config(poll_interval_min=5),
        )
    finally:
        if prev_overlay is None:
            os.environ.pop("RUN_C18ACC_EXTENSION_OVERLAY", None)
        else:
            os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = prev_overlay
    _REPLAY_SELL_ALERTS[session] = alerts
    return alerts


@dataclass(frozen=True)
class BuySignal:
    stock_id: str
    stock_name: str
    action: Literal["buy", "observe"] = "buy"
    reason: str = ""
    price: float = 0.0
    poll_minute: str = ""
    pool_id: str = ""
    w3_mom: float | None = None  # 盤中 dual-wma / abc-v3：MV3
    w5_mom: float | None = None  # 盤中 dual-wma / abc-v3：MV5

    def reason_key(self) -> str:
        base = "observe" if self.action == "observe" else "c0_entry"
        return f"{self.pool_id}:{base}" if self.pool_id else base


@dataclass(frozen=True)
class SellSignal:
    stock_id: str
    stock_name: str
    action: Literal["sell"] = "sell"
    reason: str = ""
    price: float = 0.0
    poll_minute: str = ""
    exit_mode: str = ""
    in_holdings: bool = False
    quantity: int = 0
    member_holders: tuple[str, ...] = ()

    def reason_key(self) -> str:
        return self.exit_mode or "extension_exit"


@dataclass
class BuyRadarResult:
    session_date: str
    polled_at: str
    poll_minute: str
    signals: list[BuySignal] = field(default_factory=list)
    new_signals: list[BuySignal] = field(default_factory=list)
    skip_reason: str | None = None
    pool_as_of: str = ""
    pool_n: int = 0
    pool_observations: list[PoolObservation] = field(default_factory=list)
    abc_v3_f1_orders: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SellRadarResult:
    session_date: str
    polled_at: str
    poll_minute: str
    signals: list[SellSignal] = field(default_factory=list)  # universe extension 觸發
    held_signals: list[SellSignal] = field(default_factory=list)  # 持倉交集（extension）
    watch_signals: list[SellSignal] = field(default_factory=list)  # 宇宙觸發 · 未持有
    structural_signals: list[SellSignal] = field(default_factory=list)  # legacy · research only
    extension_held_signals: list[SellSignal] = field(default_factory=list)  # extension · 持倉
    new_signals: list[SellSignal] = field(default_factory=list)  # 持倉交集 · 今日首次（寄信）
    skip_reason: str | None = None
    holdings_n: int = 0
    holdings_error: str | None = None
    universe_n: int = 0
    universe_kbar_n: int = 0
    portfolio_exit_mode: bool | None = None
    gate_ran: bool = False
    holdings_stress: bool = False
    holdings_stress_count: int = 0


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else dict(default)
    except (json.JSONDecodeError, OSError):
        return dict(default)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dedup_state(session_date: str) -> dict[str, Any]:
    state = _load_json(DEDUP_PATH, {"date": session_date, "notified": []})
    if state.get("date") != session_date:
        state = {"date": session_date, "notified": []}
    state.setdefault("notified", [])
    return state


def dedup_key(side: str, stock_id: str, reason_key: str) -> str:
    return f"{side}:{stock_id}:{reason_key}"


def filter_new_signals(
    side: Literal["buy", "sell"],
    signals: list[BuySignal] | list[SellSignal],
    *,
    session_date: str,
) -> tuple[list[Any], list[Any]]:
    state = load_dedup_state(session_date)
    seen = {
        dedup_key(str(n["side"]), str(n["stock_id"]), str(n["reason_key"]))
        for n in state.get("notified") or []
        if isinstance(n, dict)
    }
    new: list[Any] = []
    for sig in signals:
        rk = sig.reason_key()
        key = dedup_key(side, sig.stock_id, rk)
        if key not in seen:
            new.append(sig)
    return signals, new


def mark_notified(
    side: Literal["buy", "sell"],
    signals: list[BuySignal] | list[SellSignal],
    *,
    session_date: str,
) -> None:
    if not signals:
        return
    state = load_dedup_state(session_date)
    notified: list[dict[str, str]] = list(state.get("notified") or [])
    existing = {
        dedup_key(str(n["side"]), str(n["stock_id"]), str(n["reason_key"]))
        for n in notified
        if isinstance(n, dict)
    }
    for sig in signals:
        rk = sig.reason_key()
        key = dedup_key(side, sig.stock_id, rk)
        if key in existing:
            continue
        notified.append({"side": side, "stock_id": sig.stock_id, "reason_key": rk})
        existing.add(key)
    state["date"] = session_date
    state["notified"] = notified
    _save_json(DEDUP_PATH, state)


def _buy_pool_title_map() -> dict[str, str]:
    _, specs = load_buy_observation_config()
    return {s.id: s.title for s in specs}


def _buy_pool_order() -> list[str]:
    _, specs = load_buy_observation_config()
    return [s.id for s in specs]


def _format_buy_pool_sources(pool_ids: list[str], titles: dict[str, str]) -> str:
    seen: list[str] = []
    for pid in pool_ids:
        if pid and pid not in seen:
            seen.append(pid)
    labels = [titles.get(pid, pid) for pid in seen]
    return " + ".join(labels) if labels else "—"


def _merge_buy_signals_by_symbol(
    signals: list[BuySignal],
    *,
    pool_order: list[str] | None = None,
) -> list[tuple[BuySignal, list[BuySignal]]]:
    if not signals:
        return []
    order = pool_order or _buy_pool_order()
    groups: dict[str, list[BuySignal]] = {}
    for sig in signals:
        groups.setdefault(sig.stock_id, []).append(sig)

    def _rank(sid: str) -> tuple[int, str]:
        pool_ids = [s.pool_id for s in groups[sid]]
        ranks = [order.index(p) if p in order else 999 for p in pool_ids]
        return (min(ranks), sid)

    merged: list[tuple[BuySignal, list[BuySignal]]] = []
    for sid in sorted(groups.keys(), key=_rank):
        group = groups[sid]
        primary = min(group, key=lambda s: order.index(s.pool_id) if s.pool_id in order else 999)
        merged.append((primary, group))
    return merged


def _buy_overlap_symbols(signals: list[BuySignal]) -> dict[str, list[str]]:
    by_sym: dict[str, list[str]] = {}
    for sig in signals:
        if sig.pool_id:
            by_sym.setdefault(sig.stock_id, []).append(sig.pool_id)
    return {sid: pools for sid, pools in by_sym.items() if len(pools) > 1}


def load_buy_confirm_state(session_date: str) -> dict[str, Any]:
    state = _load_json(
        BUY_STATE_PATH,
        {"session_date": session_date, "entry_confirm": {}, "pools": {}},
    )
    if state.get("session_date") != session_date:
        state = {"session_date": session_date, "entry_confirm": {}, "pools": {}}
    state.setdefault("entry_confirm", {})
    state.setdefault("pools", {})
    return state


def save_buy_confirm_state(state: dict[str, Any]) -> None:
    _save_json(BUY_STATE_PATH, state)


def _try_c0_entry_advisory(
    conn: sqlite3.Connection,
    *,
    confirm_state: dict[str, Any],
    pool: list[ScanRow],
    session: str,
    poll_minute: str,
    close: pd.DataFrame,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
    c0_cfg: Any,
    held_ids: set[str],
) -> tuple[ScanRow | None, float | None, str]:
    """C0 scale entry · no slot capacity limit · excludes already-held symbols."""
    if not pool:
        return None, None, "empty pool"
    ranked = rank_shortlist_scale(
        pool,
        conn=conn,
        close=close,
        trade_date=session,
        minute=poll_minute,
        kbar_cache=kbar_cache,
    )
    if not ranked:
        return None, None, "no C0 scale rank"
    confirm: dict[str, int] = confirm_state.setdefault("entry_confirm", {})
    top_ids = {r.stock_id for r in ranked[:3]}
    for sid in list(confirm.keys()):
        if sid not in top_ids:
            confirm[sid] = 0
    for row in ranked[:3]:
        if row.stock_id not in held_ids:
            confirm[row.stock_id] = confirm.get(row.stock_id, 0) + 1
    need = int(c0_cfg.confirm_bars)
    for row in ranked:
        sid = row.stock_id
        if sid in held_ids:
            continue
        if confirm.get(sid, 0) < need:
            continue
        px = _kbar_px_at(conn, sid, session, poll_minute, close, kbar_cache)
        if px is None or px <= 0:
            continue
        reason = f"C0 scale @ {poll_minute} confirm={need}"
        return row, float(px), reason
    return None, None, f"confirm<{need}"


def lookup_stock_name(conn: sqlite3.Connection, stock_id: str) -> str:
    row = conn.execute(
        "SELECT stock_name FROM etf_holdings WHERE stock_id = ? AND stock_name IS NOT NULL "
        "AND TRIM(stock_name) != '' ORDER BY snapshot_date DESC LIMIT 1",
        (stock_id,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    return ""


def fetch_fubon_holdings() -> tuple[dict[str, int], str | None]:
    """Return (symbol→shares, error). Graceful fail when credentials missing."""
    override = os.environ.get("SIGNAL_RADAR_HOLDINGS_OVERRIDE", "").strip()
    if override:
        out: dict[str, int] = {}
        for part in override.replace(" ", "").split(","):
            if not part or ":" not in part:
                continue
            sym, qty = part.split(":", 1)
            if sym.strip():
                out[sym.strip()] = int(float(qty.strip()))
        return out, None if out else "SIGNAL_RADAR_HOLDINGS_OVERRIDE empty"

    if not _env_flag("SIGNAL_RADAR_USE_FUBON", "1"):
        return {}, "SIGNAL_RADAR_USE_FUBON=0"
    try:
        from order.fubon_session import connect_fubon
        from order.fubon_orders import holdings_shares_by_symbol

        session = connect_fubon(realtime=False)
        return holdings_shares_by_symbol(session), None
    except Exception as exc:
        return {}, str(exc)


def load_sell_radar_holdings(
    conn: sqlite3.Connection,
    session_date: str,
) -> tuple[dict[str, int], str | None, str]:
    """富邦 live → 當日 order_holdings_snapshot → override。Returns (holdings, error, source)."""
    holdings, err = fetch_fubon_holdings()
    if holdings:
        return holdings, None, "fubon"
    from stock_db import load_latest_order_holdings_snapshot

    snap = load_latest_order_holdings_snapshot(conn, on_or_before=session_date)
    if snap:
        return snap, err, "snapshot"
    return {}, err, "none"


def load_position_meta() -> dict[str, dict[str, Any]]:
    path = Path(os.environ.get("SIGNAL_RADAR_POSITION_META_PATH", str(POSITION_META_PATH)))
    raw = _load_json(path, {})
    positions = raw.get("positions") if isinstance(raw.get("positions"), dict) else raw
    if not isinstance(positions, dict):
        return {}
    return {str(k): v for k, v in positions.items() if isinstance(v, dict)}


def holdings_to_slots(
    holdings: dict[str, int],
    *,
    conn: sqlite3.Connection,
    session: str,
    meta: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    meta = meta or {}
    slots: list[dict[str, Any]] = []
    for sid, qty in sorted(holdings.items()):
        if qty <= 0:
            continue
        m = meta.get(sid) or {}
        entry_date = str(m.get("entry_date") or session)
        slots.append(
            {
                "stock_id": sid,
                "stock_name": str(m.get("stock_name") or lookup_stock_name(conn, sid)),
                "entry_date": entry_date,
                "quantity_shares": qty,
            }
        )
    return slots


def load_member_holdings() -> dict[str, dict[str, int]]:
    """Optional member portfolios · {member_id: {symbol: shares}}."""
    path = os.environ.get("SIGNAL_RADAR_MEMBER_HOLDINGS_PATH", "").strip()
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    members = raw.get("members") if isinstance(raw.get("members"), dict) else raw
    if not isinstance(members, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for mid, port in members.items():
        if not isinstance(port, dict):
            continue
        cleaned: dict[str, int] = {}
        for sym, qty in port.items():
            if sym and int(qty) > 0:
                cleaned[str(sym)] = int(qty)
        if cleaned:
            out[str(mid)] = cleaned
    return out


def member_holders_for_symbol(
    stock_id: str,
    member_holdings: dict[str, dict[str, int]],
) -> tuple[str, ...]:
    return tuple(sorted(mid for mid, port in member_holdings.items() if port.get(stock_id, 0) > 0))


def load_sell_scan_universe(
    conn: sqlite3.Connection,
    session: str,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> tuple[list[str], dict[str, str], int]:
    """ETF 監測清單 ∩ 當日有 5m K（mode=kbar）或全清單（mode=watchlist）。"""
    mode = os.environ.get("SIGNAL_RADAR_SELL_UNIVERSE", "kbar").strip().lower()
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    name_map = {str(w["stock_id"]): str(w.get("stock_name") or "") for w in watch if w.get("stock_id")}
    all_ids = sorted(name_map.keys())
    if mode == "watchlist":
        return all_ids, name_map, len(all_ids)
    with_kbar = [sid for sid in all_ids if kbar_mainline_day_has_data(conn, sid, session)]
    return with_kbar, name_map, len(all_ids)


def _universe_watch_entry_date(full_dates: list[str], session: str) -> str:
    """非持倉標的 · 用前一交易日作虛擬進場日（extension 盤中掃描）。"""
    if session not in full_dates:
        return session
    idx = full_dates.index(session)
    return full_dates[max(0, idx - 1)]


def build_extension_universe_slots(
    stock_ids: list[str],
    *,
    conn: sqlite3.Connection,
    session: str,
    name_map: dict[str, str],
    full_dates: list[str],
) -> list[dict[str, Any]]:
    watch_entry = _universe_watch_entry_date(full_dates, session)
    slots: list[dict[str, Any]] = []
    for sid in stock_ids:
        slots.append(
            {
                "stock_id": sid,
                "stock_name": str(name_map.get(sid) or lookup_stock_name(conn, sid)),
                "entry_date": watch_entry,
            }
        )
    return slots


def extension_alerts_to_sell_signals(
    alerts: list[ExtensionAlert],
    poll_minute: str,
) -> list[SellSignal]:
    return [_alert_to_sell_signal(alert, poll_minute) for alert in alerts]


def _alert_to_sell_signal(
    alert: ExtensionAlert,
    poll_minute: str,
    *,
    in_holdings: bool = False,
    quantity: int = 0,
    member_holders: tuple[str, ...] = (),
) -> SellSignal:
    return SellSignal(
        stock_id=alert.stock_id,
        stock_name=alert.stock_name,
        action="sell",
        reason=alert.note,
        price=alert.price,
        poll_minute=poll_minute,
        exit_mode=alert.exit_mode,
        in_holdings=in_holdings,
        quantity=quantity,
        member_holders=member_holders,
    )


def split_universe_sell_signals(
    alerts: list[ExtensionAlert],
    poll_minute: str,
    *,
    holdings: dict[str, int],
    member_holdings: dict[str, dict[str, int]],
) -> tuple[list[SellSignal], list[SellSignal], list[SellSignal]]:
    """Return (universe_all, held_intersection, watch_only)."""
    universe: list[SellSignal] = []
    held: list[SellSignal] = []
    watch: list[SellSignal] = []
    for alert in alerts:
        sid = alert.stock_id
        qty = int(holdings.get(sid, 0))
        in_held = qty > 0
        members = member_holders_for_symbol(sid, member_holdings)
        sig = _alert_to_sell_signal(
            alert,
            poll_minute,
            in_holdings=in_held,
            quantity=qty,
            member_holders=members,
        )
        universe.append(sig)
        if in_held:
            held.append(sig)
        else:
            watch.append(sig)
    return universe, held, watch


def format_radar_markdown(
    *,
    buy: list[BuySignal] | None = None,
    sell: list[SellSignal] | None = None,
    session_date: str = "",
    poll_minute: str = "",
    side: Literal["buy", "sell", "both"] = "both",
) -> str:
    lines = [
        f"# Signal radar · {session_date or '—'}",
        "",
        f"> poll `{poll_minute or '—'}` · advisory only · human confirms trade",
        "",
    ]
    if side in ("buy", "both") and buy is not None:
        lines.append("## Buy signals（C0 entry）")
        lines.append("")
        if buy:
            titles = _buy_pool_title_map()
            merged = _merge_buy_signals_by_symbol(buy)
            for primary, group in merged:
                pools = _format_buy_pool_sources([s.pool_id for s in group], titles)
                overlap = " · 多軌重疊" if len(group) > 1 else ""
                lines.append(
                    f"- **{primary.stock_id}** {primary.stock_name} · **{primary.action}** · "
                    f"ref **{primary.price:.2f}** · 來源 **{pools}**{overlap} · {primary.reason}"
                )
            mv_table = _format_mv_gap_table_md(
                [
                    (f"{primary.stock_id} {primary.stock_name}", primary.w3_mom, primary.w5_mom)
                    for primary, _ in merged
                ]
            )
            if mv_table:
                lines.append("")
                lines.extend(mv_table)
        else:
            lines.append("_本輪無新買進訊號_")
        lines.append("")
    if side in ("sell", "both") and sell is not None:
        structural = [s for s in sell if s.exit_mode in ("trigger_s2",)]
        extension = [s for s in sell if s.exit_mode not in ("trigger_s2",)]
        if structural:
            lines.append("## Sell signals（structural · holdings）")
            lines.append("")
            for s in structural:
                mode = s.exit_mode or "structural"
                qty = f" ×{s.quantity}" if s.quantity else ""
                lines.append(
                    f"- **{s.stock_id}** {s.stock_name}{qty} · **{s.action}** · "
                    f"ref **{s.price:.2f}** · **{mode}** · {s.reason}"
                )
            lines.append("")
        if extension:
            lines.append("## Sell signals（extension · universe）")
            lines.append("")
            for s in extension:
                mode = s.exit_mode or "extension"
                lines.append(
                    f"- **{s.stock_id}** {s.stock_name} · **{s.action}** · "
                    f"ref **{s.price:.2f}** · **{mode}** · {s.reason}"
                )
            lines.append("")
        elif not structural:
            lines.append("## Sell signals")
            lines.append("")
            lines.append("_本輪無賣出訊號_")
            lines.append("")
    return "\n".join(lines)


def write_radar_report(
    side: Literal["buy", "sell"],
    signals: list[BuySignal] | list[SellSignal],
    *,
    session_date: str,
    poll_minute: str,
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = session_date.replace("-", "")
    if side == "buy":
        md = format_radar_markdown(buy=list(signals), session_date=session_date, poll_minute=poll_minute, side="buy")
        path = REPORTS_DIR / f"{stamp}_buy_signal_radar.md"
        latest = REPORTS_DIR / "buy_signal_radar.md"
    else:
        md = format_radar_markdown(sell=list(signals), session_date=session_date, poll_minute=poll_minute, side="sell")
        path = REPORTS_DIR / f"{stamp}_sell_signal_radar.md"
        latest = REPORTS_DIR / "sell_signal_radar.md"
    path.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    return path


_UNIVERSE_INTRADAY_SOURCES = (
    "dual_wma_lead_pullback",
    "abc_v3_f1_pullback",
    "abc_v3_skip09_pullback",
)


def _presync_universe_intraday_kbar(
    conn: sqlite3.Connection,
    pool_specs: list[BuyObservationPoolSpec],
    *,
    session: str,
    poll_minute: str,
) -> int:
    """Pre-sync full ETF-constituent 5m K before building intraday universe pools.

    盤中宇宙池需要全宇宙 5m K 才能建出候選；radar 內建 sync 只補「已在池內」的股，
    對這些池是先有雞先有蛋。此函式在建池前先把全宇宙 5m K 備妥。
    以 env `SIGNAL_RADAR_UNIVERSE_KBAR_SYNC`（預設 1）控管，避免非必要時的重載。
    """
    if not poll_minute:
        return 0
    if not _env_flag("SIGNAL_RADAR_UNIVERSE_KBAR_SYNC", "1"):
        return 0
    needs = any(
        spec.source in _UNIVERSE_INTRADAY_SOURCES for spec in pool_specs
    )
    if not needs:
        return 0
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    universe_ids = sorted({str(w["stock_id"]) for w in watch if w.get("stock_id")})
    if not universe_ids:
        return 0
    try:
        sleep_sec = float(os.environ.get("SIGNAL_RADAR_UNIVERSE_KBAR_SLEEP", "0.15"))
    except ValueError:
        sleep_sec = 0.15
    return sync_watchlist_kbar(
        conn,
        universe_ids,
        session,
        sleep_sec=sleep_sec,
        poll_minute=poll_minute,
    )


def run_buy_signal_radar(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    now: datetime | None = None,
    mark_dedup: bool = True,
) -> BuyRadarResult:
    now = now or _now_local()
    session = session_date or _session_date()
    poll_minute = _poll_minute(now, interval_min=5)
    result = BuyRadarResult(
        session_date=session,
        polled_at=now.isoformat(timespec="seconds"),
        poll_minute=poll_minute,
    )

    if not _env_flag("RUN_BUY_SIGNAL_RADAR", "1"):
        result.skip_reason = "RUN_BUY_SIGNAL_RADAR=0"
        return result
    if not _buy_radar_poll_window_ok(now):
        result.skip_reason = "outside poll window (Mon–Fri 09:00–13:20)"
        return result
    if not _buy_radar_entry_allowed(now):
        result.skip_reason = "outside entry window (09:05–13:20)"
        return result

    _, pool_specs = load_buy_observation_config()
    if not pool_specs:
        result.skip_reason = "buy_observation.yaml missing or empty"
        return result

    close, _, _, bench = _load_market_panels_cached(conn)
    if session not in close.index.astype(str):
        close = close.copy()
        close.loc[session] = float("nan")

    ephemeral = load_buy_confirm_state(session)

    # 盤中宇宙池（dual-wma / abc-v3-f1 / abc-v3-skip09）掃全 ETF 成分股，需先備妥全宇宙 5m K。
    # 否則盤中即時只有持倉/小池的 5m K，這些池永遠為空（見 20260708 診斷）。
    _presync_universe_intraday_kbar(conn, pool_specs, session=session, poll_minute=poll_minute)

    # 先建各池以彙總 kbar sync
    built: list[tuple[BuyObservationPoolSpec, list[ScanRow], str]] = []
    watch_ids: set[str] = set()
    for spec in pool_specs:
        pool, pool_as_of = _cached_observation_pool(
            conn,
            spec,
            session=session,
            poll_minute=poll_minute,
            close=close,
            bench=bench,
            ephemeral=ephemeral,
        )
        built.append((spec, pool, pool_as_of))
        for row in _slice_top_n(pool, spec.top_n):
            watch_ids.add(row.stock_id)

    if watch_ids:
        sync_watchlist_kbar(
            conn, sorted(watch_ids), session, poll_minute=poll_minute
        )

    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    notify_signals: list[BuySignal] = []
    primary_as_of = ""

    for spec, pool, pool_as_of in built:
        if spec.source == "rrg_fresh_mono":
            result.pool_n = len(pool)
            result.pool_as_of = pool_as_of
            primary_as_of = pool_as_of

        pool_state = _pool_confirm_state(ephemeral, spec.id)
        obs = evaluate_pool_observation(
            conn,
            spec,
            pool=pool,
            pool_as_of=pool_as_of,
            session=session,
            poll_minute=poll_minute,
            close=close,
            kbar_cache=kbar_cache,
            confirm_state=pool_state,
            c0_cfg=c0_cfg,
        )
        result.pool_observations.append(obs)

        if obs.entry_stock_id and obs.entry_price > 0:
            sig = BuySignal(
                stock_id=obs.entry_stock_id,
                stock_name=obs.entry_stock_name,
                action="buy",
                reason=obs.entry_reason,
                price=obs.entry_price,
                poll_minute=poll_minute,
                pool_id=spec.id,
            )
            if spec.notify:
                notify_signals.append(sig)
        elif spec.observe_only and spec.notify and obs.top_rows:
            # observe-only 池：無 C0 進場，命中候選即寄信（advisory 軌 · 每檔每日去重）
            for row in obs.top_rows:
                notify_signals.append(
                    BuySignal(
                        stock_id=row.stock_id,
                        stock_name=row.stock_name,
                        action="observe",
                        reason=f"{spec.title} 命中"
                        + (f" · {row.note}" if row.note else ""),
                        price=float(row.price or 0.0),
                        poll_minute=poll_minute,
                        pool_id=spec.id,
                        w3_mom=row.w3_mom,
                        w5_mom=row.w5_mom,
                    )
                )

    save_buy_confirm_state(ephemeral)

    if not primary_as_of and result.pool_observations:
        primary_as_of = next((o.pool_as_of for o in result.pool_observations if o.pool_as_of), "")
    result.pool_as_of = primary_as_of

    result.signals = notify_signals
    _, result.new_signals = filter_new_signals("buy", notify_signals, session_date=session)
    if mark_dedup and result.new_signals:
        mark_notified("buy", result.new_signals, session_date=session)
    return result


def run_sell_signal_radar(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    now: datetime | None = None,
    mark_dedup: bool = True,
) -> SellRadarResult:
    now = now or _now_local()
    session = session_date or _session_date()
    poll_minute = _poll_minute(now, interval_min=5)
    result = SellRadarResult(
        session_date=session,
        polled_at=now.isoformat(timespec="seconds"),
        poll_minute=poll_minute,
    )

    if not _env_flag("RUN_SELL_SIGNAL_RADAR", "1"):
        result.skip_reason = "RUN_SELL_SIGNAL_RADAR=0"
        return result
    if not _buy_radar_poll_window_ok(now):
        result.skip_reason = "outside poll window (Mon–Fri 09:00–13:20)"
        return result

    scan_ids, name_map, universe_total = load_sell_scan_universe(conn, session)
    result.universe_n = universe_total
    result.universe_kbar_n = len(scan_ids)
    if not scan_ids:
        result.skip_reason = "extension universe empty (no kbar on session)"
        return result

    close, _, _, _ = _load_market_panels_cached(conn)
    session_dates = close.index.astype(str).tolist()
    universe_slots = build_extension_universe_slots(
        scan_ids,
        conn=conn,
        session=session,
        name_map=name_map,
        full_dates=session_dates,
    )

    sync_watchlist_kbar(conn, scan_ids, session, poll_minute=poll_minute)

    universe_min_hold = _env_int("SIGNAL_RADAR_UNIVERSE_MIN_HOLD_DAYS", 0)

    if replay_fast_enabled():
        all_day = _sell_replay_day_extension_alerts(
            conn,
            session=session,
            universe_slots=universe_slots,
            session_dates=session_dates,
            universe_min_hold=universe_min_hold,
        )
        universe_alerts = [a for a in all_day if _extension_minute_leq(a.minute, poll_minute)]
    else:
        prev_overlay = os.environ.get("RUN_C18ACC_EXTENSION_OVERLAY")
        os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = "1"
        try:
            universe_alerts = scan_held_extension_alerts(
                conn,
                slots=universe_slots,
                session=session,
                poll_minute=poll_minute,
                full_dates=session_dates,
                min_hold_days=universe_min_hold,
                poll_interval_min=5,
                scan_config=overlay_scan_config(poll_interval_min=5),
            )
        finally:
            if prev_overlay is None:
                os.environ.pop("RUN_C18ACC_EXTENSION_OVERLAY", None)
            else:
                os.environ["RUN_C18ACC_EXTENSION_OVERLAY"] = prev_overlay

    holdings, holdings_error, _holdings_source = load_sell_radar_holdings(conn, session)
    result.holdings_n = len(holdings)
    result.holdings_error = holdings_error if not holdings else None
    member_holdings = load_member_holdings()

    universe_sigs, held_sigs, watch_sigs = split_universe_sell_signals(
        universe_alerts,
        poll_minute,
        holdings=holdings,
        member_holdings=member_holdings,
    )
    result.signals = universe_sigs
    result.extension_held_signals = held_sigs
    result.watch_signals = watch_sigs

    # 結構停損（S0/S1a/S2/S2-lite）已退回 Research · 不下單層／策略雷達掃描
    result.structural_signals = []
    result.portfolio_exit_mode = None
    result.gate_ran = False
    result.holdings_stress = False
    result.holdings_stress_count = 0

    result.held_signals = list(held_sigs)

    email_held_only = _env_flag("SIGNAL_RADAR_SELL_HELD_ONLY", "1")
    notify_pool = result.held_signals if email_held_only else universe_sigs
    _, result.new_signals = filter_new_signals("sell", notify_pool, session_date=session)
    if mark_dedup and result.new_signals:
        mark_notified("sell", result.new_signals, session_date=session)
    return result


def signal_to_dict(sig: BuySignal | SellSignal) -> dict[str, Any]:
    return asdict(sig)


def _log_rule(char: str = "─", width: int = 52) -> str:
    return char * width


_SKIP_REASON_CN: dict[str, str] = {
    "outside poll window (Mon–Fri 09:00–13:20)": "非盤中掃描時段（週一～五 09:00–13:20）",
    "outside entry window (09:05–13:20)": "尚不可進場（09:05–13:20）",
    "no PIT fresh mono pool": "無 PIT 新鮮 mono 候選池",
    "buy_observation.yaml missing or empty": "buy_observation.yaml 未設定",
    "RUN_BUY_SIGNAL_RADAR=0": "買方雷達已關閉（RUN_BUY_SIGNAL_RADAR=0）",
    "RUN_SELL_SIGNAL_RADAR=0": "賣方雷達已關閉（RUN_SELL_SIGNAL_RADAR=0）",
    "extension universe empty (no kbar on session)": "當日無 5m K · extension 宇宙為空",
}


def _skip_reason_cn(reason: str) -> str:
    return _SKIP_REASON_CN.get(reason, reason)


# H-ABC-FILTER-1 門檻（mirror triple_wma_pullback_sweep.W3_ABC_F1_W5_W3_MV_SPREAD_MAX）：
# F 門通過條件 = W5 MV − W3 MV < 0.01（避免中線 W5 動能壓過短線 W3）。
_F1_MV_SPREAD_MAX = 0.01


def _cjk_pad(text: str, width: int) -> str:
    """以顯示寬度（CJK 全形字佔 2）將 text 右補空白至 width。"""
    disp = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
    return text + " " * max(0, width - disp)


def _mv_gap_rows(
    rows: list[tuple[str, float | None, float | None]],
) -> list[tuple[str, float, float, float, bool]]:
    """(label, MV3, MV5) → (label, MV3, MV5, 差值=MV3−MV5, F門通過)，依差值由大到小（正者優先）。"""
    out: list[tuple[str, float, float, float, bool]] = []
    for label, mv3, mv5 in rows:
        if mv3 is None or mv5 is None:
            continue
        diff = float(mv3) - float(mv5)
        f_pass = (float(mv5) - float(mv3)) < _F1_MV_SPREAD_MAX
        out.append((label, float(mv3), float(mv5), diff, f_pass))
    out.sort(key=lambda r: (-r[3], r[0]))
    return out


def _format_mv_gap_table_text(
    rows: list[tuple[str, float | None, float | None]],
) -> list[str]:
    """log 純文字表：標的 / MV3 / MV5 / 差值(MV3−MV5) / F門。"""
    ranked = _mv_gap_rows(rows)
    if not ranked:
        return []
    lines = ["  MV3−MV5 排序（差值正者優先 · F 門＝避免 W5 MV>W3 MV）："]
    lines.append(f"    {_cjk_pad('標的', 16)}{'MV3':>7}{'MV5':>8}{'差值':>8}  F門")
    for label, mv3, mv5, diff, f_pass in ranked:
        gate = "✅" if f_pass else "❌"
        lines.append(f"    {_cjk_pad(label, 16)}{mv3:7.2f}{mv5:8.2f}{diff:+8.2f}  {gate}")
    return lines


def _format_mv_gap_table_md(
    rows: list[tuple[str, float | None, float | None]],
) -> list[str]:
    """email markdown 表：標的 / MV3 / MV5 / 差值(MV3−MV5) / F門。"""
    ranked = _mv_gap_rows(rows)
    if not ranked:
        return []
    lines = [
        "### MV3−MV5 排序（差值正者優先 · F 門＝避免 W5 MV>W3 MV）",
        "",
        "| 標的 | MV3 | MV5 | 差值 | F門 |",
        "| --- | ---: | ---: | ---: | :---: |",
    ]
    for label, mv3, mv5, diff, f_pass in ranked:
        gate = "✅" if f_pass else "❌"
        lines.append(f"| {label} | {mv3:.2f} | {mv5:.2f} | {diff:+.2f} | {gate} |")
    return lines


def format_buy_radar_log(result: BuyRadarResult) -> str:
    """Human-readable buy observation board for launchd / replay."""
    titles = _buy_pool_title_map()
    lines = [
        _log_rule("═"),
        f"【買入觀測】{result.session_date} {result.poll_minute}",
        f"掃描時間：{result.polled_at}",
    ]
    if result.skip_reason:
        lines.extend(
            [
                f"狀態：略過 · {_skip_reason_cn(result.skip_reason)}",
                "→ 本輪動作：無",
                "寄信：否",
                _log_rule("═"),
            ]
        )
        return "\n".join(lines)

    notify_obs = [o for o in result.pool_observations if o.notify]
    notify_track_label = "、".join(o.pool_id for o in notify_obs) or "—"
    merged_new = _merge_buy_signals_by_symbol(result.new_signals)
    merged_triggered = _merge_buy_signals_by_symbol(result.signals)

    lines.append(f"信號日（PIT）：{result.pool_as_of or '—'} · fresh mono 池 {result.pool_n} 檔")
    lines.append(f"寄信軌：{len(notify_obs)}（{notify_track_label}）")
    lines.append(
        f"觀測池：{len(result.pool_observations)} · "
        f"寄信軌觸發 {len(result.signals)} 筆 · 合併 {len(merged_triggered)} 檔 · "
        f"今日首次 {len(merged_new)} 檔"
    )
    overlap = _buy_overlap_symbols(result.signals)
    if overlap:
        overlap_bits = []
        for sid, pool_ids in sorted(overlap.items()):
            pools = _format_buy_pool_sources(pool_ids, titles)
            overlap_bits.append(f"{sid}（{pools}）")
        lines.append(f"多軌重疊：{', '.join(overlap_bits)}")
    lines.append("")

    for obs in result.pool_observations:
        role = role_label_zh(obs.role)
        notify = "寄信" if obs.notify else "僅觀測"
        lines.append(f"▸ {obs.title}（{obs.pool_id}）· {role} · {notify} · 池 {obs.pool_n} 檔 · PIT {obs.pool_as_of or '—'}")
        if obs.entry_stock_id:
            lines.append(
                f"  → C0 觸發 {obs.entry_stock_id} {obs.entry_stock_name} @ {obs.entry_price:.2f} · {obs.entry_reason}"
            )
        elif obs.skip_note:
            lines.append(f"  → 本輪無 C0 觸發 · {obs.skip_note}")
        else:
            lines.append("  → 本輪無 C0 觸發（confirm 未滿）")
        if obs.top_rows:
            if obs.skip_note and obs.skip_note.startswith("observe-only"):
                lines.append(f"  top {len(obs.top_rows)}（RV · sig · 昨收）：")
                for row in obs.top_rows:
                    px = f"{row.price:.2f}" if row.price is not None else "—"
                    extra = f" · {row.note}" if row.note else ""
                    lines.append(f"    {row.stock_id} {row.stock_name} RV={row.seg_last:.1f} @ {px}{extra}")
                lines.extend(
                    _format_mv_gap_table_text(
                        [
                            (f"{r.stock_id} {r.stock_name}", r.w3_mom, r.w5_mom)
                            for r in obs.top_rows
                        ]
                    )
                )
            else:
                lines.append(f"  top {len(obs.top_rows)}（scale · confirm · 價）：")
                for row in obs.top_rows:
                    px = f"{row.price:.2f}" if row.price is not None else "—"
                    lines.append(
                        f"    {row.stock_id} {row.stock_name} seg={row.seg_last:.3f} @ {px} c={row.confirm}"
                    )
        lines.append("")

    if result.new_signals:
        lines.append("→ 寄信（請人工確認後下單）：")
        for primary, group in merged_new:
            pools = _format_buy_pool_sources([s.pool_id for s in group], titles)
            overlap_note = " · 多軌重疊" if len(group) > 1 else ""
            verb = "觀測命中" if primary.action == "observe" else "買進"
            lines.append(
                f"  · {verb} {primary.stock_id} {primary.stock_name} @ {primary.price:.2f} · "
                f"來源：{pools}{overlap_note} · {primary.reason}"
            )
        lines.append("  你可做：限價買入或略過 · 系統不會自動送單（ABC Order 已退役）")
        lines.append("寄信：是")
    elif result.signals:
        lines.append("→ 寄信軌：訊號已於今日寄過（分軌去重）")
        for primary, group in merged_triggered:
            pools = _format_buy_pool_sources([s.pool_id for s in group], titles)
            overlap_note = " · 多軌重疊" if len(group) > 1 else ""
            lines.append(
                f"  （重複）{primary.stock_id} @ {primary.price:.2f} · 來源：{pools}{overlap_note}"
            )
        lines.append("寄信：否")
    else:
        lines.append("→ 寄信軌：本輪無 C0 買進觸發")
        lines.append("寄信：否")

    lines.append(_log_rule("═"))
    return "\n".join(lines)


def format_sell_radar_log(result: SellRadarResult) -> str:
    """Human-readable sell observation log · universe extension + holdings overlay."""
    lines = [
        _log_rule("═"),
        f"【賣出觀測】{result.session_date} {result.poll_minute}",
        f"掃描時間：{result.polled_at}",
        f"宇宙：監測 {result.universe_n} 檔 · 當日有 5m K {result.universe_kbar_n} 檔",
        f"持倉：{result.holdings_n} 檔"
        + (f" · ⚠ {result.holdings_error}" if result.holdings_error else ""),
    ]

    if result.skip_reason:
        lines.extend(
            [
                f"狀態：略過 · {_skip_reason_cn(result.skip_reason)}",
                "→ 本輪動作：無",
                "寄信：否",
                _log_rule("═"),
            ]
        )
        return "\n".join(lines)

    held_only = _env_flag("SIGNAL_RADAR_SELL_HELD_ONLY", "1")
    lines.append(
        f"extension 宇宙 {len(result.signals)} 檔 · "
        f"extension 持倉 {len(result.extension_held_signals)} · "
        f"今日首次寄信 {len(result.new_signals)} 檔"
        + ("（僅持倉）" if held_only else "（宇宙）")
    )
    lines.append("")

    if result.extension_held_signals:
        lines.append("→ extension 持倉交集：")
        for s in result.extension_held_signals:
            mode = s.exit_mode or "extension"
            lines.append(
                f"  · {s.stock_id} {s.stock_name or '—'} ×{s.quantity}（{mode}）"
                f" @ {s.price:.2f} · {s.reason}"
            )
        lines.append("")
    elif result.signals:
        lines.append("→ 本輪無持倉交集 extension 觸發")
        lines.append("")

    if result.watch_signals:
        lines.append(f"→ 宇宙其他（未持有 · {len(result.watch_signals)} 檔）：")
        for s in result.watch_signals[:5]:
            lines.append(f"  · {s.stock_id} @ {s.price:.2f}")
        if len(result.watch_signals) > 5:
            lines.append(f"  … 另有 {len(result.watch_signals) - 5} 檔")
        lines.append("")

    if result.new_signals:
        lines.append("→ 寄信（持倉 · 請人工確認後下單）：")
        for s in result.new_signals:
            mode = s.exit_mode or "extension"
            qty = f" ×{s.quantity}" if s.quantity else ""
            lines.append(
                f"  · 賣出 {s.stock_id} {s.stock_name or '—'}{qty} @ {s.price:.2f} · {mode} · {s.reason}"
            )
        lines.append("  你可做：限價賣出或略過 · 系統不會自動送單")
        lines.append("寄信：是")
    elif result.held_signals:
        lines.append("→ 寄信：持倉訊號已於今日寄過（去重）")
        for s in result.held_signals:
            lines.append(f"  （重複）{s.stock_id} @ {s.price:.2f}")
        lines.append("寄信：否")
    else:
        lines.append("寄信：否")

    lines.append(_log_rule("═"))
    return "\n".join(lines)


def print_buy_radar_log(result: BuyRadarResult) -> None:
    block = format_buy_radar_log(result)
    print(block)
    if result.new_signals:
        print("BUY_SIGNAL_RADAR=1")
    else:
        print("BUY_SIGNAL_RADAR=0")


def print_sell_radar_log(result: SellRadarResult) -> None:
    block = format_sell_radar_log(result)
    print(block)
    if result.new_signals:
        print("SELL_SIGNAL_RADAR=1")
    else:
        print("SELL_SIGNAL_RADAR=0")
