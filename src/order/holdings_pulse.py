"""持倉即時脈動 · 富邦 snapshot + 現價 + RRG 收盤 vs 盤中（觀測 · 非結構停損執行）。"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from pathlib import Path
from typing import Any, Literal

from stock_db import DATA_DIR, PROJECT_ROOT

from .morning_holdings_brief import (
    CORE4,
    HoldingBriefRow,
    _gate_2330,
    _load_morning_risk,
    _pit_bar_date,
    _pit_rrg_session,
    enrich_holdings,
    fetch_fubon_snapshot,
    structure_tier,
    try_refresh_morning_risk,
)

_TZ = __import__("zoneinfo").ZoneInfo("Asia/Taipei")
_STRUCTURAL_MIN_MINUTE = "09:05"
_TREND_ARROW = {
    "up_right": "↗",
    "down_right": "↘",
    "up_left": "↖",
    "down_left": "↙",
}
MarketPhase = Literal["pre_open", "observe", "gate", "active", "late", "after_close"]


@dataclass(frozen=True)
class RrgPoint:
    session_date: str | None
    quadrant: str | None
    rs_ratio: float | None
    rs_momentum: float | None
    seg_last: float | None
    trend: str | None
    tick_ok: bool | None = None


@dataclass(frozen=True)
class HoldingPulseRow:
    base: HoldingBriefRow
    current_price: float | None
    price_source: str
    daily_pct: float | None
    pnl_pct: float | None
    market_value: float | None
    weight_pct: float | None
    rrg_close: RrgPoint
    rrg_intraday: RrgPoint
    rrg_quad_delta: str | None
    rrg_seg_delta: float | None
    dist_gate_2pct_pct: float | None
    dist_s1b_pct: float | None
    dist_extension_pct: float | None
    vcp_composite: float | None
    vcp_state: str | None
    hold_days: int | None
    structural_flags: tuple[str, ...] = field(default_factory=tuple)
    exit_log_today: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class HoldingsPulse:
    generated_at: str
    session_date: str
    poll_minute: str
    market_phase: MarketPhase
    account_label: str
    cash_available: int | None
    total_market_value: float | None
    total_unrealized_pnl: int
    bar_as_of: str | None
    rrg_close_as_of: str | None
    rrg_intraday_as_of: str | None
    tx_gap_pct: float | None
    tx_price: float | None
    tw_spot_prev_close: float | None
    te_gap_pct: float | None
    morning_risk_note: str | None
    gate_2330_995: float | None
    gate_2330_price: float | None
    gate_2330_hit: bool | None
    core4_gate_hits: int
    portfolio_exit_preview: bool | None
    portfolio_exit_mode: bool | None
    portfolio_gate_ran: bool
    holdings_stress: bool
    holdings_stress_count: int
    rrg_intraday_tick_n: int | None
    rrg_intraday_kbar_n: int | None
    rrg_intraday_price_mode: str | None
    rrg_intraday_error: str | None
    holdings: tuple[HoldingPulseRow, ...]
    fubon_error: str | None = None
    price_errors: tuple[str, ...] = field(default_factory=tuple)
    session_note: str | None = None


def trend_arrow(trend: str | None) -> str:
    if not trend:
        return "—"
    return _TREND_ARROW.get(trend, trend)


def market_phase(now: datetime | None = None) -> MarketPhase:
    dt = now or datetime.now(_TZ)
    t = dt.time()
    if t < time(9, 0):
        return "pre_open"
    if t < time(9, 5):
        return "observe"
    if t < time(9, 6):
        return "gate"
    if t < time(13, 20):
        return "active"
    if t < time(13, 30):
        return "late"
    return "after_close"


def market_phase_note(phase: MarketPhase) -> str:
    notes = {
        "pre_open": "開盤前 · 現價可能為試撮或昨收",
        "observe": "09:00–09:04 只觀察、不賣",
        "gate": "09:05 組合閘門時段",
        "active": "09:06–13:20 盤中監控",
        "late": "13:20–13:30 停止自動減碼區",
        "after_close": "收盤後 · 現價 fallback 昨收",
    }
    return notes.get(phase, "")


def _fmt_pct(val: float | None, *, signed: bool = True) -> str:
    if val is None:
        return "—"
    if signed:
        return f"{val:+.2f}%"
    return f"{val:.2f}%"


def _fmt_px(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:.2f}"


def _load_rrg_point(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    screen_kind: str,
    on_or_before: str,
    exact_date: str | None = None,
) -> RrgPoint:
    if exact_date:
        row = conn.execute(
            """
            SELECT session_date, quadrant, rs_ratio, rs_momentum, seg_last, trend, tick_ok
            FROM rrg_universe_scores
            WHERE stock_id = ? AND screen_kind = ? AND session_date = ?
            LIMIT 1
            """,
            (stock_id, screen_kind, exact_date),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT session_date, quadrant, rs_ratio, rs_momentum, seg_last, trend, tick_ok
            FROM rrg_universe_scores
            WHERE stock_id = ? AND screen_kind = ? AND session_date <= ?
            ORDER BY session_date DESC LIMIT 1
            """,
            (stock_id, screen_kind, on_or_before),
        ).fetchone()
    if not row:
        return RrgPoint(None, None, None, None, None, None, None)
    tick_ok = None
    if row[6] is not None:
        tick_ok = bool(int(row[6]))
    return RrgPoint(
        str(row[0]) if row[0] else None,
        str(row[1]) if row[1] else None,
        float(row[2]) if row[2] is not None else None,
        float(row[3]) if row[3] is not None else None,
        float(row[4]) if row[4] is not None else None,
        str(row[5]) if row[5] else None,
        tick_ok,
    )


def _load_position_meta() -> dict[str, dict[str, Any]]:
    path = DATA_DIR / "fubon_position_meta.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    positions = raw.get("positions") if isinstance(raw.get("positions"), dict) else raw
    if not isinstance(positions, dict):
        return {}
    return {str(k): v for k, v in positions.items() if isinstance(v, dict)}


def _hold_days(entry_date: str | None, session_date: str) -> int | None:
    if not entry_date:
        return None
    try:
        start = datetime.fromisoformat(entry_date).date()
        end = datetime.fromisoformat(session_date).date()
        return max(0, (end - start).days)
    except ValueError:
        return None


def _load_exit_log_today(
    conn: sqlite3.Connection,
    trade_date: str,
    stock_id: str,
) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT event, phase FROM order_intraday_exit_log
        WHERE trade_date = ? AND stock_id = ?
        ORDER BY checked_at DESC LIMIT 3
        """,
        (trade_date, stock_id),
    ).fetchall()
    return tuple(f"{r[1]}:{r[0]}" for r in rows if r[0])


def _distance_pct(current: float | None, gate: float | None, prev_close: float | None) -> float | None:
    if current is None or gate is None or prev_close is None or prev_close <= 0:
        return None
    return (current - gate) / prev_close * 100.0


def _minute_ge(a: str, b: str) -> bool:
    am = a[:5] if len(a) >= 5 else a
    bm = b[:5] if len(b) >= 5 else b
    return am >= bm


def _structural_flags(
    row: HoldingBriefRow,
    *,
    current_price: float | None,
    poll_minute: str,
    portfolio_exit_mode: bool | None,
) -> tuple[str, ...]:
    if current_price is None or row.prev_close is None:
        return ()
    if not _minute_ge(poll_minute, _STRUCTURAL_MIN_MINUTE):
        return ("09:05前",)

    flags: list[str] = []
    if row.gate_2pct is not None and current_price <= row.gate_2pct:
        flags.append("≤-2%gate")
    if row.extension_spike is not None and current_price >= row.extension_spike:
        flags.append("ext≥4%")
    return tuple(flags)


def fetch_live_prices(
    symbols: list[str],
    *,
    prev_close_by_symbol: dict[str, float | None],
    use_fubon: bool = True,
) -> tuple[dict[str, float], dict[str, str], list[str]]:
    """Return (prices, sources, errors)."""
    prices: dict[str, float] = {}
    sources: dict[str, str] = {}
    errors: list[str] = []

    if use_fubon and symbols:
        try:
            from fubon_neo.constant import MarketType

            from order.fubon_orders import _result_ok
            from order.fubon_session import connect_fubon

            session = connect_fubon(realtime=True)
            account = session.primary
            for sym in symbols:
                if sym in prices:
                    continue
                got: float | None = None
                for mt in (MarketType.Common, MarketType.IntradayOdd):
                    res = session.sdk.stock.query_symbol_quote(account, sym, mt)
                    if not _result_ok(res) or res.data is None:
                        continue
                    d = res.data
                    for key in ("last_price", "open_price", "ask_price"):
                        val = getattr(d, key, None)
                        if val is not None and float(val) > 0:
                            got = float(val)
                            break
                    if got is not None:
                        break
                if got is not None:
                    prices[sym] = got
                    sources[sym] = "fubon"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"富邦報價：{exc}")

    missing = [s for s in symbols if s not in prices]
    if missing:
        try:
            from finmind_client import fetch_tick_snapshots
            from rrg_mono_intraday_watch import _tick_map

            tick_rows, tick_err = fetch_tick_snapshots(missing)
            tick_map = _tick_map(tick_rows or [])
            for sym in missing:
                if sym in tick_map:
                    prices[sym] = tick_map[sym]
                    sources[sym] = "finmind_tick"
            if tick_err and not tick_map:
                errors.append(f"FinMind tick：{tick_err}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"FinMind tick：{exc}")

    for sym in symbols:
        if sym in prices:
            continue
        pc = prev_close_by_symbol.get(sym)
        if pc is not None and pc > 0:
            prices[sym] = pc
            sources[sym] = "prev_close"

    return prices, sources, errors


def _holdings_pulse_rrg_kbar_enabled() -> bool:
    return os.environ.get("HOLDINGS_PULSE_RRG_KBAR", "1").strip() not in ("0", "false", "False")


def _sync_intraday_rrg_impl(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
    holding_ids: list[str] | None = None,
) -> tuple[int, int, int, str | None]:
    from rrg_mono_intraday_watch import BENCH_TICK_IDS
    from rrg_mono_swap_accel_screen import sync_watchlist_kbar
    from rrg_universe_snapshot import run_intraday_universe_snapshot

    price_mode = "kbar" if _holdings_pulse_rrg_kbar_enabled() else "tick"
    if price_mode == "kbar" and holding_ids:
        sync_ids = sorted(
            {str(s) for s in holding_ids if s} | {"2330"} | set(BENCH_TICK_IDS)
        )
        sync_watchlist_kbar(
            conn,
            sync_ids,
            session_date,
            env_key="HOLDINGS_PULSE_KBAR_SYNC",
            poll_minute=poll_minute,
        )

    n, _, priced_n, kbar_n = run_intraday_universe_snapshot(
        conn,
        session_date=session_date,
        poll_minute=poll_minute,
        price_mode=price_mode,
    )
    return n, priced_n, kbar_n, None


def _parse_rrg_sync_worker_stdout(stdout: str) -> tuple[int, int, int] | None:
    """Parse worker line: ``RRG 盤中同步 OK · rows=N · priced=P · kbar=K``."""
    import re

    match = re.search(
        r"rows=(\d+)\s*·\s*priced=(\d+)\s*·\s*kbar=(\d+)",
        stdout or "",
    )
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def sync_intraday_rrg(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
    holding_ids: list[str] | None = None,
) -> tuple[int, int, int, str | None]:
    """寫入 rrg_universe_scores（screen_kind=intraday）。富邦 venv 缺 pandas 時委派主 .venv worker。"""
    if os.environ.get("HOLDINGS_RRG_SYNC_WORKER") == "1":
        try:
            return _sync_intraday_rrg_impl(
                conn,
                session_date=session_date,
                poll_minute=poll_minute,
                holding_ids=holding_ids,
            )
        except Exception as exc:  # noqa: BLE001
            return 0, 0, 0, str(exc)

    main_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    worker = PROJECT_ROOT / "scripts" / "order" / "sync_holdings_rrg_intraday_worker.py"
    if main_py.is_file() and worker.is_file():
        import subprocess

        cmd = [
            str(main_py),
            str(worker),
            "--session",
            session_date,
            "--poll-minute",
            poll_minute,
        ]
        if holding_ids:
            cmd.extend(["--holdings", *holding_ids])
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            return 0, 0, 0, err
        # Worker already wrote DB. Do NOT re-run impl here: .venv-fubon
        # often lacks numpy/pandas (fresh mini install) and would crash.
        parsed = _parse_rrg_sync_worker_stdout(proc.stdout or "")
        if parsed is not None:
            n, priced_n, kbar_n = parsed
            return n, priced_n, kbar_n, None
        return 0, 0, 0, None

    try:
        return _sync_intraday_rrg_impl(
            conn,
            session_date=session_date,
            poll_minute=poll_minute,
            holding_ids=holding_ids,
        )
    except Exception as exc:  # noqa: BLE001
        return 0, 0, 0, str(exc)


def _rrg_intraday_source_note(pulse: HoldingsPulse) -> str:
    if pulse.rrg_intraday_as_of is None:
        return "—"
    if pulse.rrg_intraday_price_mode == "kbar":
        kbar_n = pulse.rrg_intraday_kbar_n or 0
        priced_n = pulse.rrg_intraday_tick_n or 0
        fallback = max(0, priced_n - kbar_n)
        base = f"5m K @{pulse.poll_minute}（{kbar_n} 檔"
        if fallback:
            return f"{base} · tick fallback {fallback}）"
        return f"{base}）"
    return "FinMind tick"


def _load_portfolio_exit_state(
    conn: sqlite3.Connection,
    trade_date: str,
) -> tuple[bool | None, bool]:
    try:
        from order.intraday_structural_exit import load_portfolio_exit_mode

        return load_portfolio_exit_mode(conn, trade_date)
    except Exception:  # noqa: BLE001
        return None, False


def _load_holdings_stress(
    conn: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
    holdings: dict[str, int],
    snapshot_rows: list[HoldingBriefRow],
    prices: dict[str, float],
) -> tuple[bool, int]:
    if not holdings or not _minute_ge(poll_minute, _STRUCTURAL_MIN_MINUTE):
        return False, 0
    count = 0
    for row in snapshot_rows:
        qty = holdings.get(row.stock_id, 0)
        if qty <= 0 or row.prev_close is None:
            continue
        px = prices.get(row.stock_id)
        if px is None:
            continue
        if px <= round(row.prev_close * 0.98, 2):
            count += 1
    return count >= 3, count


def _has_rrg_close(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    as_of_session: str,
) -> bool:
    """是否具備收盤 RRG 素材（rrg_universe_scores screen_kind=close）。"""
    row = conn.execute(
        """
        SELECT 1 FROM rrg_universe_scores
        WHERE stock_id = ? AND screen_kind = 'close' AND session_date <= ?
        LIMIT 1
        """,
        (stock_id, as_of_session),
    ).fetchone()
    return row is not None


def _normalize_watch_symbols(symbols: list[str] | None) -> list[str]:
    if not symbols:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols:
        sid = str(raw or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def build_holdings_pulse(
    conn: sqlite3.Connection,
    *,
    session_date: str | None = None,
    use_fubon: bool = True,
    sync_rrg_intraday: bool = False,
    refresh_morning_risk: bool = True,
    symbols: list[str] | None = None,
    require_rrg_close: bool = False,
) -> HoldingsPulse:
    now = datetime.now(_TZ).replace(microsecond=0)
    today = session_date or now.date().isoformat()
    poll_minute = now.strftime("%H:%M")
    phase = market_phase(now)
    generated_at = now.isoformat()

    watch_symbols = _normalize_watch_symbols(symbols)
    watch_mode = bool(watch_symbols)

    fubon_snap: dict[str, Any] | None = None
    fubon_error: str | None = None
    # Watchlist mode：不用富邦持倉，但可選仍用富邦報價
    if use_fubon and not watch_mode:
        fubon_snap, fubon_error = fetch_fubon_snapshot()

    holdings_raw: list[dict[str, Any]] = []
    cash_available: int | None = None
    account_label = "—"
    pnl_map: dict[str, tuple[int, float | None]] = {}
    if watch_mode:
        account_label = "watchlist"
        kept = watch_symbols
        if require_rrg_close:
            kept = [s for s in watch_symbols if _has_rrg_close(conn, s, as_of_session=today)]
        # 偽持倉：股數=1，僅供現價／RRG／介紹脈動（無真實成本／損益）
        holdings_raw = [
            {"stock_no": sid, "today_qty": 1, "tradable_qty": 1}
            for sid in kept
        ]
    elif fubon_snap:
        account_label = str(fubon_snap.get("_account_label") or "—")
        cash = fubon_snap.get("cash") or {}
        cash_available = int(cash.get("available_balance", 0) or 0)
        holdings_raw = list(fubon_snap.get("holdings") or [])
        pnl_map = {
            str(item.get("stock_no", "") or ""): (
                int(item.get("unrealized_profit", 0) or 0)
                - int(item.get("unrealized_loss", 0) or 0),
                float(item.get("cost_price", 0) or 0) or None,
            )
            for item in (fubon_snap.get("unrealized_pnl") or [])
            if item.get("stock_no")
        }

    base_rows = enrich_holdings(
        conn,
        holdings_raw,
        as_of_session=today,
        pnl_by_symbol=pnl_map,
    )
    prev_close_map = {r.stock_id: r.prev_close for r in base_rows}
    symbols = [r.stock_id for r in base_rows]
    if "2330" not in symbols:
        row = conn.execute(
            """
            SELECT close FROM stock_daily_bars
            WHERE stock_id = '2330' AND trade_date < ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            (today,),
        ).fetchone()
        symbols.append("2330")
        prev_close_map["2330"] = float(row[0]) if row and row[0] is not None else None

    prices, price_sources, price_errors = fetch_live_prices(
        symbols,
        prev_close_by_symbol=prev_close_map,
        use_fubon=use_fubon,
    )

    rrg_intraday_tick_n: int | None = None
    rrg_intraday_kbar_n: int | None = None
    rrg_intraday_price_mode: str | None = None
    rrg_intraday_error: str | None = None
    if sync_rrg_intraday and phase in ("observe", "gate", "active", "late"):
        _, priced_n, kbar_n, rrg_intraday_error = sync_intraday_rrg(
            conn,
            session_date=today,
            poll_minute=poll_minute,
            holding_ids=symbols,
        )
        rrg_intraday_tick_n = priced_n
        rrg_intraday_kbar_n = kbar_n
        rrg_intraday_price_mode = "kbar" if _holdings_pulse_rrg_kbar_enabled() else "tick"

    rrg_close_as_of = _pit_rrg_session(conn, today)
    rrg_intraday_as_of = today if phase != "after_close" else None

    meta = _load_position_meta()
    portfolio_exit_mode, portfolio_gate_ran = _load_portfolio_exit_state(conn, today)
    holdings_qty = {r.stock_id: r.shares for r in base_rows}

    pulse_rows: list[HoldingPulseRow] = []
    total_mv = 0.0
    total_pnl = 0

    for base in base_rows:
        px = prices.get(base.stock_id)
        src = price_sources.get(base.stock_id, "—")
        daily_pct = None
        if px is not None and base.prev_close and base.prev_close > 0:
            daily_pct = (px - base.prev_close) / base.prev_close * 100.0
        pnl_pct = None
        if px is not None and base.cost_price and base.cost_price > 0:
            pnl_pct = (px - base.cost_price) / base.cost_price * 100.0
        mv = (px * base.shares) if px is not None else None
        if mv is not None:
            total_mv += mv
        total_pnl += base.unrealized_pnl

        rrg_close = _load_rrg_point(
            conn,
            base.stock_id,
            screen_kind="close",
            on_or_before=rrg_close_as_of or today,
        )
        rrg_intra = _load_rrg_point(
            conn,
            base.stock_id,
            screen_kind="intraday",
            on_or_before=today,
            exact_date=today if rrg_intraday_as_of else None,
        )
        quad_delta = None
        if rrg_close.quadrant and rrg_intra.quadrant and rrg_close.quadrant != rrg_intra.quadrant:
            quad_delta = f"{rrg_close.quadrant}→{rrg_intra.quadrant}"
        seg_delta = None
        if rrg_close.seg_last is not None and rrg_intra.seg_last is not None:
            seg_delta = rrg_intra.seg_last - rrg_close.seg_last

        # 持倉脈動／RRG HTML：tier 只看 RRG 象限，不混入 VCP（避免 Overextended 噪音）
        tier = structure_tier(rrg_intra.quadrant or rrg_close.quadrant)
        base_with_tier = HoldingBriefRow(
            stock_id=base.stock_id,
            stock_name=base.stock_name,
            shares=base.shares,
            cost_price=base.cost_price,
            unrealized_pnl=base.unrealized_pnl,
            prev_close=base.prev_close,
            bar_date=base.bar_date,
            rrg_quadrant=rrg_intra.quadrant or base.rrg_quadrant,
            rrg_seg=rrg_intra.seg_last if rrg_intra.seg_last is not None else base.rrg_seg,
            rrg_session=rrg_intra.session_date or base.rrg_session,
            structure_tier=tier,
            gate_2pct=base.gate_2pct,
            trigger_s1b=base.trigger_s1b if tier == "weak" else None,
            extension_spike=base.extension_spike,
            notes=base.notes,
        )

        pulse_rows.append(
            HoldingPulseRow(
                base=base_with_tier,
                current_price=px,
                price_source=src,
                daily_pct=daily_pct,
                pnl_pct=pnl_pct,
                market_value=mv,
                weight_pct=None,
                rrg_close=rrg_close,
                rrg_intraday=rrg_intra,
                rrg_quad_delta=quad_delta,
                rrg_seg_delta=seg_delta,
                dist_gate_2pct_pct=_distance_pct(px, base.gate_2pct, base.prev_close),
                dist_s1b_pct=_distance_pct(px, base_with_tier.trigger_s1b, base.prev_close),
                dist_extension_pct=_distance_pct(px, base.extension_spike, base.prev_close)
                if base.extension_spike and px is not None
                else None,
                vcp_composite=None,
                vcp_state=None,
                hold_days=_hold_days(str((meta.get(base.stock_id) or {}).get("entry_date") or ""), today),
                structural_flags=_structural_flags(
                    base_with_tier,
                    current_price=px,
                    poll_minute=poll_minute,
                    portfolio_exit_mode=portfolio_exit_mode,
                ),
                exit_log_today=_load_exit_log_today(conn, today, base.stock_id),
            )
        )

    if total_mv > 0:
        pulse_rows = [
            replace(
                row,
                weight_pct=(row.market_value / total_mv * 100.0)
                if row.market_value is not None
                else None,
            )
            for row in pulse_rows
        ]

    pulse_rows.sort(
        key=lambda r: (
            r.base.structure_tier != "weak",
            -(r.weight_pct or 0),
            r.base.unrealized_pnl,
            r.base.stock_id,
        )
    )

    gate_2330 = _gate_2330(conn, as_of_session=today)
    gate_2330_px = prices.get("2330")
    gate_2330_hit = None
    if gate_2330 is not None and gate_2330_px is not None:
        gate_2330_hit = gate_2330_px <= gate_2330

    core4_hits = sum(
        1
        for r in pulse_rows
        if r.base.stock_id in CORE4
        and r.current_price is not None
        and r.base.gate_2pct is not None
        and r.current_price <= r.base.gate_2pct
    )
    portfolio_preview: bool | None = None
    if gate_2330_hit is not None:
        portfolio_preview = core4_hits >= 2 and gate_2330_hit

    stress_on, stress_n = _load_holdings_stress(
        conn,
        session_date=today,
        poll_minute=poll_minute,
        holdings=holdings_qty,
        snapshot_rows=[r.base for r in pulse_rows],
        prices=prices,
    )

    morning_row: dict[str, Any] | None = None
    morning_risk_note: str | None = None
    if refresh_morning_risk:
        morning_row, morning_risk_note = try_refresh_morning_risk(
            conn, trade_date=today, sync=False, dry_run=True
        )
    if morning_row is None:
        morning_row, morning_risk_note = _load_morning_risk(conn, today)

    return HoldingsPulse(
        generated_at=generated_at,
        session_date=today,
        poll_minute=poll_minute,
        market_phase=phase,
        account_label=account_label,
        cash_available=cash_available,
        total_market_value=total_mv if total_mv > 0 else None,
        total_unrealized_pnl=total_pnl,
        bar_as_of=_pit_bar_date(conn, today),
        rrg_close_as_of=rrg_close_as_of,
        rrg_intraday_as_of=rrg_intraday_as_of,
        tx_gap_pct=float(morning_row["tx_gap_live_pct"])
        if morning_row and morning_row.get("tx_gap_live_pct") is not None
        else None,
        tx_price=float(morning_row["tx_price"])
        if morning_row and morning_row.get("tx_price") is not None
        else None,
        tw_spot_prev_close=float(morning_row["tw_spot_prev_close"])
        if morning_row and morning_row.get("tw_spot_prev_close") is not None
        else None,
        te_gap_pct=float(morning_row["te_gap_live_pct"])
        if morning_row and morning_row.get("te_gap_live_pct") is not None
        else None,
        morning_risk_note=morning_risk_note,
        gate_2330_995=gate_2330,
        gate_2330_price=gate_2330_px,
        gate_2330_hit=gate_2330_hit,
        core4_gate_hits=core4_hits,
        portfolio_exit_preview=portfolio_preview,
        portfolio_exit_mode=portfolio_exit_mode,
        portfolio_gate_ran=portfolio_gate_ran,
        holdings_stress=stress_on,
        holdings_stress_count=stress_n,
        rrg_intraday_tick_n=rrg_intraday_tick_n,
        rrg_intraday_kbar_n=rrg_intraday_kbar_n,
        rrg_intraday_price_mode=rrg_intraday_price_mode,
        rrg_intraday_error=rrg_intraday_error,
        holdings=tuple(pulse_rows),
        fubon_error=fubon_error,
        price_errors=tuple(price_errors),
        session_note=(
            f"監控清單模式（{len(pulse_rows)} 檔）· {market_phase_note(phase)}"
            if watch_mode
            else market_phase_note(phase)
        ),
    )


def _rrg_cell(point: RrgPoint) -> str:
    if not point.quadrant:
        return "—"
    seg = f"{point.seg_last:.3f}" if point.seg_last is not None else "—"
    rv = f"{point.rs_ratio:.1f}" if point.rs_ratio is not None else "—"
    mv = f"{point.rs_momentum:.1f}" if point.rs_momentum is not None else "—"
    arrow = trend_arrow(point.trend)
    tick = "K" if point.tick_ok else ("t" if point.tick_ok is False else "")
    return f"{point.quadrant} · seg {seg} · RV {rv} · MV {mv} · {arrow}{tick}"


def _weak_reason(row: HoldingPulseRow) -> str:
    reasons: list[str] = []
    quad = row.rrg_intraday.quadrant or row.rrg_close.quadrant or row.base.rrg_quadrant
    if quad in ("weakening", "lagging"):
        reasons.append(f"RRG {quad}")
    return " · ".join(reasons) if reasons else "tier=weak"


def _flag_action_hint(
    flag: str,
    *,
    tier: str,
    portfolio_exit_mode: bool | None,
) -> str:
    hints: dict[str, str] = {
        "≤-2%gate": "跌逾 2% 觀測線（非自動賣出）",
        "ext≥4%": "漲逾 4% · 留意守利／回落（非弱勢出清）",
        "09:05前": "09:05 前不判旗標",
    }
    return hints.get(flag, flag)


def _format_holdings_status_from_pulse(pulse: HoldingsPulse) -> str:
    """Email-equivalent 【持倉狀態】block（成本／市值／損益／報價來源）。"""
    from order.order_fill_report import (
        format_holdings_status_section,
        holdings_status_rows_from_pulse,
    )

    return format_holdings_status_section(
        holdings_status_rows_from_pulse(pulse),
        cash_available=pulse.cash_available,
        total_market_value=pulse.total_market_value,
        total_unrealized_pnl=pulse.total_unrealized_pnl,
        error=pulse.fubon_error,
    )


def format_holdings_pulse_digest(pulse: HoldingsPulse) -> str:
    """Plain-text holdings section for intraday digest emails."""
    lines: list[str] = [
        "■ 持倉即時脈動（advisory · 非出清指令）",
        "",
        f"  輪詢 {pulse.poll_minute} · 現價：富邦即時 → FinMind tick → 昨收",
        "  量測：現價＝即時報價；RRG 盤中＝"
        f"{_rrg_intraday_source_note(pulse)}",
    ]
    for err in pulse.price_errors:
        lines.append(f"  ⚠ 報價：{err}")

    # Per-symbol status · same fields as fill-notify email
    status = _format_holdings_status_from_pulse(pulse)
    lines.extend(["", status])

    lines.extend(
        [
            "",
            "  組合狀態（觀測 · 結構停損 playbook 已退回 Research）",
            f"  · 2330 {_fmt_px(pulse.gate_2330_price)} · CORE4 ≤昨收×0.98：{pulse.core4_gate_hits}/4",
        ]
    )
    if pulse.holdings_stress:
        lines.append(
            f"  · 持倉壓力參考：{pulse.holdings_stress_count} 檔 ≤昨收×0.98"
        )

    weak = [r for r in pulse.holdings if r.base.structure_tier == "weak"]
    flagged = [
        r
        for r in pulse.holdings
        if r.structural_flags and r.structural_flags != ("09:05前",)
    ]
    big_loss = sorted(
        [r for r in pulse.holdings if r.base.unrealized_pnl < -1500],
        key=lambda r: r.base.unrealized_pnl,
    )

    if weak or flagged or big_loss:
        lines.extend(["", "  優先盯（提醒 · 非出清）"])
    if weak:
        lines.append("  · 結構弱：")
        for r in weak:
            name = r.base.stock_name or "—"
            px = _fmt_px(r.current_price)
            daily = _fmt_pct(r.daily_pct)
            lines.append(
                f"    {r.base.stock_id} {name} · {px} {daily} · {_weak_reason(r)}"
            )
    if flagged:
        lines.append("  · 觸發／接近：")
        for r in flagged:
            name = r.base.stock_name or "—"
            flag_bits = ", ".join(r.structural_flags)
            hint = "；".join(
                _flag_action_hint(
                    f,
                    tier=r.base.structure_tier,
                    portfolio_exit_mode=pulse.portfolio_exit_mode,
                )
                for f in r.structural_flags
            )
            lines.append(
                f"    {r.base.stock_id} {name} · tier={r.base.structure_tier} "
                f"[{flag_bits}] — {hint}"
            )
    if big_loss:
        lines.append("  · 帳面大虧（帳面提醒 · 非賣出規則）：")
        for r in big_loss:
            name = r.base.stock_name or "—"
            lines.append(
                f"    {r.base.stock_id} {name} · {r.base.unrealized_pnl:+,} "
                f"（{_fmt_pct(r.pnl_pct)}）"
            )
    if not weak and not flagged and not big_loss:
        lines.extend(["", "  優先盯：無結構弱、價格旗標或帳面大虧提醒"])

    stamp = pulse.session_date.replace("-", "")
    lines.extend(
        [
            "",
            f"  完整表格：reports/order/snapshots/holdings_pulse_{stamp}.md",
        ]
    )
    return "\n".join(lines)


def _format_priority_watch(pulse: HoldingsPulse) -> list[str]:
    lines: list[str] = [
        "## 優先盯",
        "",
        "> **提醒**：下列為盤中 **人工決策提醒**，**不等於出清指令**。",
        "> 正式賣出 advisory 由 **sell-signal-radar**（09:06–13:20）掃描後寄信；本報告不送單。",
        "",
    ]
    if pulse.market_phase == "after_close":
        lines.append(
            "> 收盤後執行：現價多為昨收 fallback，旗標僅供隔日參考，"
            "不宜作當日減碼依據。"
        )
        lines.append("")

    weak = [r for r in pulse.holdings if r.base.structure_tier == "weak"]
    flagged = [
        r
        for r in pulse.holdings
        if r.structural_flags and r.structural_flags != ("09:05前",)
    ]
    big_loss = sorted(
        [r for r in pulse.holdings if r.base.unrealized_pnl < -1500],
        key=lambda r: r.base.unrealized_pnl,
    )

    if weak:
        lines.extend(
            [
                "### 結構弱（tier=weak）",
                "- **意思**：RRG 象限偏弱（weakening／lagging）；若盤中再跌，"
                "較易進入弱勢結構。**觀測用 · 非自動賣出。**",
                "",
            ]
        )
        for r in weak:
            name = r.base.stock_name or "—"
            reason = _weak_reason(r)
            lines.append(f"- **{r.base.stock_id}** {name} — {reason}")
        lines.append("")

    if flagged:
        lines.extend(
            [
                "### 觸發／接近（價格旗標）",
                "- **意思**：現價已碰或接近技術參考線；僅觀測，非賣出指令。",
                "",
            ]
        )
        for r in flagged:
            name = r.base.stock_name or "—"
            flag_bits = ", ".join(r.structural_flags)
            hints = "；".join(
                _flag_action_hint(
                    f,
                    tier=r.base.structure_tier,
                    portfolio_exit_mode=pulse.portfolio_exit_mode,
                )
                for f in r.structural_flags
            )
            lines.append(
                f"- **{r.base.stock_id}** {name} · tier={r.base.structure_tier} "
                f"· [{flag_bits}] — {hints}"
            )
        lines.append("")

    if big_loss:
        lines.extend(
            [
                "### 帳面大虧",
                "- **意思**：富邦未實現損益 < NT$1,500；帳面壓力提醒，"
                "**非**賣出規則。",
                "",
            ]
        )
        for r in big_loss:
            name = r.base.stock_name or "—"
            pnl_pct = _fmt_pct(r.pnl_pct) if r.pnl_pct is not None else "—"
            lines.append(
                f"- **{r.base.stock_id}** {name} · {r.base.unrealized_pnl:+,} "
                f"（損益% {pnl_pct}）"
            )
        lines.append("")

    if not weak and not flagged and not big_loss:
        lines.append("- 無結構弱、價格旗標或帳面大虧提醒。")

    return lines


def format_holdings_pulse(pulse: HoldingsPulse) -> str:
    lines: list[str] = [
        f"# 持倉即時脈動 · {pulse.session_date}",
        "",
        f"產生時間：{pulse.generated_at} · 輪詢 {pulse.poll_minute} · **{pulse.market_phase}**",
    ]
    if pulse.session_note:
        lines.append(f"時段：{pulse.session_note}")
    lines.append(f"帳戶：{pulse.account_label}")
    lines.append(
        "> **性質**：advisory 儀表板 · 人工確認後下單 · "
        "「優先盯」≠ 出清指令"
    )
    for err in pulse.price_errors:
        lines.append(f"報價：⚠ {err}")

    # 置頂：與 email 同欄位的完整持倉狀態（終端／md 最先可見）
    lines.extend(["", "## 持倉狀態", "", _format_holdings_status_from_pulse(pulse), ""])

    lines.extend(
        [
            "## 資料來源",
            f"- 持倉／損益：**fubon**（live snapshot）",
            f"- 現價：fubon → FinMind tick → 昨收 fallback（即時報價）",
            f"- 結構／昨收：PIT · bar {pulse.bar_as_of or '—'}",
            f"- RRG 收盤：{pulse.rrg_close_as_of or '—'} · "
            f"RRG 盤中：{pulse.rrg_intraday_as_of or '—'} · "
            f"{_rrg_intraday_source_note(pulse)}",
            "",
        ]
    )

    if pulse.rrg_intraday_error:
        lines.append(f"- RRG 盤中同步：⚠ {pulse.rrg_intraday_error}")
    elif pulse.rrg_intraday_tick_n is not None:
        if pulse.rrg_intraday_price_mode == "kbar":
            lines.append(
                f"- RRG 盤中覆蓋：**{pulse.rrg_intraday_tick_n}** 檔 "
                f"（5m K **{pulse.rrg_intraday_kbar_n or 0}** · "
                f"tick fallback **{max(0, (pulse.rrg_intraday_tick_n or 0) - (pulse.rrg_intraday_kbar_n or 0))}**）"
            )
        else:
            lines.append(f"- RRG 盤中 tick 覆蓋：**{pulse.rrg_intraday_tick_n}** 檔")

    lines.append("")
    lines.append("## 台指 gap")
    if pulse.morning_risk_note:
        lines.append(f"- _{pulse.morning_risk_note}_")
    if pulse.tx_gap_pct is not None:
        lines.append(
            f"- TX gap **{pulse.tx_gap_pct:+.2f}%** @ {pulse.tx_price or '—'}"
            f" · 前日加權 {pulse.tw_spot_prev_close or '—'}"
        )
        if pulse.te_gap_pct is not None:
            lines.append(f"- TE gap **{pulse.te_gap_pct:+.2f}%**")
    else:
        lines.append("- 無 morning_risk 資料")

    lines.extend(["", "## 組合狀態（觀測）"])
    lines.append(
        "- 結構停損 playbook（閘門 / 弱檔 -3% 等）已退回 **Research**；"
        "本報告不執行、不寄結構賣訊。"
    )
    gate_px = _fmt_px(pulse.gate_2330_price)
    lines.append(
        f"- 2330 現價 {gate_px} · CORE4 ≤ 昨收×0.98：**{pulse.core4_gate_hits}** / 4 檔"
        "（僅參考）"
    )
    if pulse.holdings_stress:
        lines.append(
            f"- 持倉壓力參考：{pulse.holdings_stress_count} 檔 ≤ 昨收×0.98"
        )
    lines.append("")

    lines.append("## 持倉明細（RRG／結構）")
    lines.append("")
    lines.append(
        "_tier：weak=RRG weakening／lagging；strong=leading／improving。"
        "距-2%/弱-3%/ext 為價格距離參考；旗標為觀測線，非自動賣出。_"
    )
    lines.append("")
    lines.append(
        "| 代號 | 名稱 | 股數 | 成本 | 現價 | 昨收 | 今日% | 損益 | 損益% | 佔比 | "
        "RRG收盤 | RRG盤中 | Δ象限 | tier | 距-2% | 弱-3% | ext | 持有 | 旗標 |"
    )
    lines.append(
        "|------|------|-----:|-----:|-----:|-----:|------:|-----:|------:|-----:|"
        "--------|--------|------|------|-------|------|-----|------|------|"
    )

    for r in pulse.holdings:
        b = r.base
        name = b.stock_name or "—"
        cost = _fmt_px(b.cost_price)
        px = _fmt_px(r.current_price)
        if r.price_source not in ("fubon", "finmind_tick"):
            px = f"{px}*"
        pnl = f"{b.unrealized_pnl:+,}"
        wt = _fmt_pct(r.weight_pct, signed=False) if r.weight_pct is not None else "—"
        close_cell = _rrg_cell(r.rrg_close)
        intra_cell = _rrg_cell(r.rrg_intraday)
        delta_q = r.rrg_quad_delta or "—"
        if r.rrg_seg_delta is not None:
            delta_q = f"{delta_q} · Δseg {r.rrg_seg_delta:+.3f}" if delta_q != "—" else f"Δseg {r.rrg_seg_delta:+.3f}"
        hold = str(r.hold_days) if r.hold_days is not None else "—"
        flags = " · ".join(
            [*r.structural_flags, *r.exit_log_today, *b.notes]
        ) or "—"
        lines.append(
            f"| {b.stock_id} | {name} | {b.shares} | {cost} | {px} | "
            f"{_fmt_px(b.prev_close)} | {_fmt_pct(r.daily_pct)} | {pnl} | "
            f"{_fmt_pct(r.pnl_pct)} | {wt} | {close_cell} | {intra_cell} | {delta_q} | "
            f"{b.structure_tier} | {_fmt_pct(r.dist_gate_2pct_pct)} | "
            f"{_fmt_pct(r.dist_s1b_pct)} | {_fmt_pct(r.dist_extension_pct)} | "
            f"{hold} | {flags} |"
        )

    lines.extend([""])
    lines.extend(_format_priority_watch(pulse))

    uses_fallback = pulse.market_phase == "after_close" or any(
        r.price_source == "prev_close" for r in pulse.holdings
    )
    lines.extend(
        [
            "",
            "## 紀律",
            "- 結構停損 playbook 已退回 Research · 本報告不執行結構賣訊",
            "- sell-signal-radar：僅 extension 持倉交集寄信（人工確認）",
            "- 本報告 advisory · 人工確認後下單",
            "- 建議盤中 09:10–13:20 執行以取得 live 現價與 RRG 盤中",
        ]
    )
    if uses_fallback:
        lines.append("")
        lines.append("_＊部分或全部現價為昨收 fallback（標 *）；旗標僅供參考_")
    return "\n".join(lines)


def default_report_path(session_date: str) -> Path:
    stamp = session_date.replace("-", "")
    return PROJECT_ROOT / "reports" / "order" / "snapshots" / f"holdings_pulse_{stamp}.md"


def write_holdings_pulse(pulse: HoldingsPulse, path: Path | None = None) -> Path:
    out = path or default_report_path(pulse.session_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(format_holdings_pulse(pulse), encoding="utf-8")
    return out


def default_holdings_json_path(session_date: str) -> Path:
    stamp = session_date.replace("-", "")
    return PROJECT_ROOT / "reports" / "order" / "snapshots" / f"holdings_pulse_{stamp}.json"


def holdings_pulse_to_dict(pulse: HoldingsPulse) -> dict[str, Any]:
    """序列化 · 供 RRG 繪圖端（.venv）讀取帳戶／持倉狀態與現價。"""
    return {
        "session_date": pulse.session_date,
        "generated_at": pulse.generated_at,
        "poll_minute": pulse.poll_minute,
        "account_label": pulse.account_label,
        "cash_available": pulse.cash_available,
        "total_market_value": pulse.total_market_value,
        "total_unrealized_pnl": pulse.total_unrealized_pnl,
        "portfolio_exit_mode": pulse.portfolio_exit_mode,
        "rrg_intraday_sync_error": pulse.rrg_intraday_error,
        "rrg_intraday_kbar_n": pulse.rrg_intraday_kbar_n,
        "holdings": [
            {
                "stock_id": r.base.stock_id,
                "stock_name": r.base.stock_name,
                "shares": r.base.shares,
                "cost_price": r.base.cost_price,
                "prev_close": r.base.prev_close,
                "current_price": r.current_price,
                "price_source": r.price_source,
                "daily_pct": r.daily_pct,
                "pnl_pct": r.pnl_pct,
                "unrealized_pnl": r.base.unrealized_pnl,
                "market_value": r.market_value,
                "weight_pct": r.weight_pct,
                "structure_tier": r.base.structure_tier,
                "vcp_composite": r.vcp_composite,
                "vcp_state": r.vcp_state,
                "hold_days": r.hold_days,
                "gate_2pct": r.base.gate_2pct,
                "trigger_s1b": r.base.trigger_s1b,
                "dist_gate_2pct_pct": r.dist_gate_2pct_pct,
                "dist_s1b_pct": r.dist_s1b_pct,
                "dist_extension_pct": r.dist_extension_pct,
                "structural_flags": list(r.structural_flags),
                "rrg_close_quadrant": r.rrg_close.quadrant,
                "rrg_intraday_quadrant": r.rrg_intraday.quadrant,
            }
            for r in pulse.holdings
        ],
    }


def write_holdings_pulse_json(pulse: HoldingsPulse, path: Path | None = None) -> Path:
    out = path or default_holdings_json_path(pulse.session_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(holdings_pulse_to_dict(pulse), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out
