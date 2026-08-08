"""Leading Dip · Order layer poll (isolated from ABC radar + C18acc slots).

Dual-sleeve:
  * main (coverage): Leading ∧ gap3≤−0.80 ∩ excess(昨收)≤−4 ∧ notUR ∧ W5_MV<100
    → day slots ≤2 (busy∧crash→≤4) · total open ≤6 · hold T+3
  * mid (satellite): same structure · excess(今開) · minute≥10:00
    → day slots ≤1 (busy∧crash→≤2) · total open ≤3 · exclude main/C18/ABC

Exit: on entry_date+hold_days (exit_date) sell chase_bid.
Budget default ~20_000 TWD / entry (config/order.yaml).
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from order.oms_lifecycle import build_client_intent_id
from order.account_cap_gate import can_afford_buy
from order.chase import shares_for_budget
from order.intent import ResolvedOrder
from order.leading_dip_cross_sleeve import blocked_symbols_for_leading_dip
from order.leading_dip_ledger import (
    LEDGER_PATH,
    MID_LEDGER_PATH,
    MID_SCHEMA,
    MID_STRATEGY_ID,
    LeadingDipLedger,
    append_position,
    load_ledger,
    update_position,
)
from order.leading_dip_order_config import (
    STRATEGY_ID,
    LeadingDipOrderConfig,
    load_leading_dip_order_config,
)
from order.live_submit_guard import assert_live_submit_allowed, live_submit_block_reason
from research.backtest.leading_dip_sleeve_validate import (
    BUSY_N,
    CRASH_RB_MIN,
    collect_leading_dip_events,
    day_slot_cap,
    load_universe_by_baseline,
)
from stock_db import DEFAULT_DB_PATH, connect

_BENCH_KBAR_IDS = ("IX0001", "0050")


def _row_to_payload(row: Any) -> dict[str, Any]:
    """Serialize a pick row (Series/dict) for the Fubon subprocess worker."""
    if hasattr(row, "to_dict"):
        raw = row.to_dict()
    else:
        raw = dict(row)
    out: dict[str, Any] = {}
    for key, val in raw.items():
        k = str(key)
        if val is None:
            out[k] = None
            continue
        try:
            if pd.isna(val):
                out[k] = None
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(val, (str, bool)):
            out[k] = val
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[k] = val
            continue
        if hasattr(val, "item"):
            try:
                item = val.item()
                if item is None:
                    out[k] = None
                else:
                    try:
                        out[k] = None if pd.isna(item) else item
                    except (TypeError, ValueError):
                        out[k] = item
                continue
            except (ValueError, AttributeError):
                pass
        out[k] = str(val)
    return out

_TZ = ZoneInfo("Asia/Taipei")


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no")


def _prior_weekday(session_date: str) -> str:
    d = date.fromisoformat(session_date)
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _exit_date(entry_date: str, hold_days: int, trading_dates: list[str]) -> str:
    """Next hold_days trading sessions after entry (matches research T+3).

    If the kbar calendar does not yet include T+N (live same-day), walk forward
    on weekdays so exit_date is not collapsed to today.
    """
    td = [str(x) for x in trading_dates]
    n = max(1, int(hold_days))
    try:
        i = td.index(entry_date)
        j = i + n
        if j < len(td):
            return td[j]
    except ValueError:
        pass
    d = date.fromisoformat(str(entry_date))
    left = n
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat()


def _presync_leading_dip_market_data(
    con: sqlite3.Connection,
    *,
    session_date: str,
    poll_minute: str,
) -> dict[str, int]:
    """Yahoo 5m for Leading∪bench (+ stale daily closes) before live scan.

    Without this, ``stock_kbar_5m`` often only has buy-radar mono names and
    Leading Dip always reports ``n_candidates_raw=0``.
    """
    out = {"kbar_upserts": 0, "daily_upserts": 0, "universe_n": 0}
    if not poll_minute or not _env_flag("LEADING_DIP_KBAR_SYNC", "1"):
        return out

    lead_by = load_universe_by_baseline(con, universe_mode="leading")
    bases = sorted(lead_by)
    prior = [b for b in bases if b < session_date]
    leading = sorted(lead_by[prior[-1]]) if prior else []
    ids = sorted({*leading, *_BENCH_KBAR_IDS})
    out["universe_n"] = len(leading)
    if not ids:
        return out

    try:
        sleep_sec = float(os.environ.get("LEADING_DIP_KBAR_SLEEP", "0.12"))
    except ValueError:
        sleep_sec = 0.12

    from rrg_mono_swap_accel_screen import sync_watchlist_kbar

    out["kbar_upserts"] = int(
        sync_watchlist_kbar(
            con,
            ids,
            session_date,
            env_key="LEADING_DIP_KBAR_SYNC",
            sleep_sec=sleep_sec,
            poll_minute=poll_minute,
        )
        or 0
    )

    # Prev-close for excess needs stock_daily_bars through T−1; replicas can lag.
    if _env_flag("LEADING_DIP_DAILY_SYNC", "1"):
        need_through = _prior_weekday(session_date)
        max_daily = con.execute("SELECT MAX(trade_date) FROM stock_daily_bars").fetchone()[0]
        if not max_daily or str(max_daily) < need_through:
            from stock_db.market import upsert_stock_daily_bars
            from yahoo_chart_sync import stock_daily_bars_rows_from_yahoo

            start = date.fromisoformat(session_date) - timedelta(days=21)
            # Stop at T−1 — do not upsert incomplete same-day Yahoo "daily" closes.
            end = date.fromisoformat(need_through)
            daily_n = 0
            for sid in ids:
                if sid == "IX0001":
                    continue
                try:
                    rows, _ = stock_daily_bars_rows_from_yahoo(sid, start, end)
                    if rows:
                        daily_n += int(upsert_stock_daily_bars(con, rows) or 0)
                except Exception:
                    continue
                if sleep_sec > 0:
                    time.sleep(min(sleep_sec, 0.05))
            out["daily_upserts"] = daily_n
    return out


def _scan_today_candidates(
    con: sqlite3.Connection,
    *,
    session_date: str,
    require_t2: bool,
    excess_anchor: str = "prev_close",
    minute_lo: str | None = None,
    minute_hi: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Structure events for ``session_date`` only (window start = day)."""
    events, trading = collect_leading_dip_events(
        con,
        start=session_date,
        apply_structure=True,
        universe_mode="leading",
        trigger="gap_excess",
        excess_anchor=excess_anchor,
        minute_lo=minute_lo,
        minute_hi=minute_hi,
        min_path_bars=3,
        require_exit_anchor=False,
    )
    if events.empty:
        return events, trading
    day = events[events["date"].astype(str) == str(session_date)].copy()
    if require_t2 and len(day):
        day = day[day["T2"].astype(bool)].copy()
    return day, trading


def _pick_day_entries(
    day_events: pd.DataFrame,
    *,
    day_slot_cap_default: int,
    day_slot_cap_busy_crash: int,
    hard_day_cap: int,
    max_entries_per_day: int,
    blocked: set[str],
    already_open: set[str],
    room: int,
    already_today: int,
) -> list[dict[str, Any]]:
    if day_events is None or len(day_events) == 0 or room <= 0:
        return []
    n_sig = len(day_events)
    rb_min = float(day_events["rB_min"].iloc[0]) if "rB_min" in day_events.columns else 0.0
    busy = n_sig >= BUSY_N
    crash = rb_min == rb_min and rb_min <= CRASH_RB_MIN
    cap = int(day_slot_cap_busy_crash) if busy and crash else int(day_slot_cap_default)
    cap = min(cap, int(hard_day_cap), n_sig)
    research_cap = day_slot_cap(n_sig, rb_min if rb_min == rb_min else 0.0)
    # Satellite may use tighter yaml caps than frozen research helper.
    cap = min(cap, research_cap) if hard_day_cap >= 4 else cap

    remaining_day = max(0, int(max_entries_per_day) - int(already_today))
    take_n = min(int(cap), int(room), int(remaining_day))
    if take_n <= 0:
        return []

    ranked = day_events.sort_values("ex0", ascending=True)
    picks: list[dict[str, Any]] = []
    for row in ranked.to_dict("records"):
        sym = str(row.get("sid") or "").strip()
        if not sym or sym in blocked or sym in already_open:
            continue
        if any(p.get("sid") == sym for p in picks):
            continue
        picks.append(row)
        if len(picks) >= take_n:
            break
    return picks


def _cfg_as_pick_kwargs(conf: LeadingDipOrderConfig, *, sleeve: str) -> dict[str, int]:
    if sleeve == "mid":
        return {
            "day_slot_cap_default": conf.mid_day_slot_cap_default,
            "day_slot_cap_busy_crash": conf.mid_day_slot_cap_busy_crash,
            "hard_day_cap": conf.mid_hard_day_cap,
            "max_entries_per_day": conf.mid_max_entries_per_day,
        }
    return {
        "day_slot_cap_default": conf.day_slot_cap_default,
        "day_slot_cap_busy_crash": conf.day_slot_cap_busy_crash,
        "hard_day_cap": conf.hard_day_cap,
        "max_entries_per_day": conf.max_entries_per_day,
    }


def process_leading_dip_poll(
    *,
    session_date: str | None = None,
    poll_minute: str | None = None,
    conn: sqlite3.Connection | None = None,
    dry_run: bool | None = None,
    cfg: LeadingDipOrderConfig | None = None,
) -> dict[str, Any]:
    """One poll: exits due first, then main entries, then mid (≥10:00)."""
    now = datetime.now(tz=_TZ)
    day = session_date or now.strftime("%Y-%m-%d")
    minute = poll_minute or now.strftime("%H:%M")
    conf = cfg or load_leading_dip_order_config()
    dry = conf.dry_run if dry_run is None else bool(dry_run)

    out: dict[str, Any] = {
        "schema": "leading-dip-poll-v2",
        "strategy_id": STRATEGY_ID,
        "session_date": day,
        "poll_minute": minute,
        "dry_run": dry,
        "order_enabled": conf.order_enabled,
        "auto_submit": conf.auto_submit,
        "require_t2": conf.require_t2,
        "budget_twd": conf.budget_twd,
        "mid_enabled": conf.mid_enabled,
        "mid_budget_twd": conf.mid_budget_twd,
        "exits": [],
        "mid_exits": [],
        "entries": [],
        "mid_entries": [],
        "skipped": [],
        "status": "ok",
    }

    if not conf.order_enabled:
        out["status"] = "disabled"
        out["reason"] = "ORDER_LEADING_DIP_ORDER_ENABLED=0 or strategies.leading-dip.enabled=false"
        return out

    block = live_submit_block_reason(session_date=day)
    if block and not dry and conf.auto_submit:
        out["status"] = "blocked"
        out["reason"] = block
        return out

    own_conn = conn is None
    if own_conn:
        conn = connect(DEFAULT_DB_PATH)
    assert conn is not None

    try:
        main_ledger = load_ledger(LEDGER_PATH)
        mid_ledger = load_ledger(MID_LEDGER_PATH)
        if mid_ledger.strategy_id != MID_STRATEGY_ID:
            mid_ledger.strategy_id = MID_STRATEGY_ID
            mid_ledger.schema = MID_SCHEMA

        foreign = blocked_symbols_for_leading_dip(
            exclude_c18acc=conf.exclude_c18acc_slots,
            exclude_abc=conf.exclude_abc_ledger,
            also_exclude=mid_ledger.open_symbols(),
        )
        blocked = set(foreign["blocked"]) | main_ledger.open_symbols()
        out["cross_sleeve"] = foreign

        # --- exits (main then mid) ---
        out["exits"] = _process_exits(
            ledger=main_ledger,
            ledger_path=LEDGER_PATH,
            conf=conf,
            user_def=conf.user_def,
            session_date=day,
            poll_minute=minute,
            dry=dry,
        )
        out["mid_exits"] = _process_exits(
            ledger=mid_ledger,
            ledger_path=MID_LEDGER_PATH,
            conf=conf,
            user_def=conf.mid_user_def,
            session_date=day,
            poll_minute=minute,
            dry=dry,
        )

        main_ledger = load_ledger(LEDGER_PATH)
        mid_ledger = load_ledger(MID_LEDGER_PATH)
        open_pos = main_ledger.open_positions()
        room = max(0, int(conf.total_open_cap) - len(open_pos))
        already_today = len(main_ledger.entries_on_day(day))
        out["open_n"] = len(open_pos)
        out["room"] = room
        out["mid_open_n"] = len(mid_ledger.open_positions())
        out["mid_room"] = max(0, int(conf.mid_total_open_cap) - len(mid_ledger.open_positions()))

        out["presync"] = _presync_leading_dip_market_data(
            conn, session_date=day, poll_minute=minute
        )

        day_events, trading = _scan_today_candidates(
            conn,
            session_date=day,
            require_t2=conf.require_t2,
            excess_anchor="prev_close",
        )
        out["n_candidates_raw"] = int(len(day_events))

        # Refresh blocks after exits
        blocked = (
            set(
                blocked_symbols_for_leading_dip(
                    exclude_c18acc=conf.exclude_c18acc_slots,
                    exclude_abc=conf.exclude_abc_ledger,
                    also_exclude=mid_ledger.open_symbols(),
                )["blocked"]
            )
            | main_ledger.open_symbols()
            | main_ledger.failed_symbols_on_day(day)
        )

        picks = _pick_day_entries(
            day_events,
            blocked=blocked,
            already_open=main_ledger.open_symbols() | mid_ledger.open_symbols(),
            room=room,
            already_today=already_today,
            **_cfg_as_pick_kwargs(conf, sleeve="main"),
        )
        out["n_picks"] = len(picks)

        if picks and (not dry) and conf.auto_submit:
            assert_live_submit_allowed(session_date=day)

        for row in picks:
            ent = _submit_entry(
                row=row,
                conf=conf,
                session_date=day,
                poll_minute=minute,
                trading_dates=trading,
                ledger=main_ledger,
                ledger_path=LEDGER_PATH,
                dry=dry,
                strategy_id=STRATEGY_ID,
                user_def=conf.user_def,
                budget_twd=conf.budget_twd,
                sleeve="main",
            )
            out["entries"].append(ent)
            if ent.get("status") in {"submitted", "dry_run", "filled", "working", "queued_no_submit"}:
                sym = str(ent.get("symbol") or "")
                if sym:
                    blocked.add(sym)

        # --- mid sleeve (≥10:00 · open-anchor · complementary) ---
        mid_active = bool(conf.mid_enabled) and str(minute) >= str(conf.mid_minute_lo)
        out["mid_active"] = mid_active
        if mid_active:
            mid_events, trading_mid = _scan_today_candidates(
                conn,
                session_date=day,
                require_t2=False,
                excess_anchor="open",
                minute_lo=conf.mid_minute_lo,
            )
            # Complement: skip names that already hit main yday-anchor today
            main_hit = (
                set(day_events["sid"].astype(str)) if len(day_events) else set()
            )
            if len(mid_events) and main_hit:
                mid_events = mid_events[~mid_events["sid"].astype(str).isin(main_hit)].copy()
            out["n_mid_candidates_raw"] = int(len(mid_events))

            mid_ledger = load_ledger(MID_LEDGER_PATH)
            main_ledger = load_ledger(LEDGER_PATH)
            mid_blocked = (
                set(
                    blocked_symbols_for_leading_dip(
                        exclude_c18acc=conf.exclude_c18acc_slots,
                        exclude_abc=conf.exclude_abc_ledger,
                        also_exclude=main_ledger.open_symbols(),
                    )["blocked"]
                )
                | mid_ledger.open_symbols()
                | mid_ledger.failed_symbols_on_day(day)
                | {str(e.get("symbol") or "") for e in out["entries"] if e.get("symbol")}
                | {str(p.get("symbol") or "") for p in main_ledger.entries_on_day(day)}
            )
            mid_room = max(0, int(conf.mid_total_open_cap) - len(mid_ledger.open_positions()))
            mid_today = len(mid_ledger.entries_on_day(day))
            mid_picks = _pick_day_entries(
                mid_events,
                blocked=mid_blocked,
                already_open=mid_ledger.open_symbols() | main_ledger.open_symbols(),
                room=mid_room,
                already_today=mid_today,
                **_cfg_as_pick_kwargs(conf, sleeve="mid"),
            )
            out["n_mid_picks"] = len(mid_picks)
            out["mid_room"] = mid_room

            if mid_picks and (not dry) and conf.auto_submit:
                assert_live_submit_allowed(session_date=day)

            for row in mid_picks:
                ent = _submit_entry(
                    row=row,
                    conf=conf,
                    session_date=day,
                    poll_minute=minute,
                    trading_dates=trading_mid or trading,
                    ledger=mid_ledger,
                    ledger_path=MID_LEDGER_PATH,
                    dry=dry,
                    strategy_id=conf.mid_strategy_id,
                    user_def=conf.mid_user_def,
                    budget_twd=conf.mid_budget_twd,
                    sleeve="mid",
                )
                out["mid_entries"].append(ent)

        return out
    finally:
        if own_conn:
            conn.close()


def _process_exits(
    *,
    ledger: LeadingDipLedger,
    ledger_path: Path,
    conf: LeadingDipOrderConfig,
    user_def: str,
    session_date: str,
    poll_minute: str,
    dry: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    due = [
        p
        for p in ledger.open_positions()
        if str(p.get("exit_date") or "") <= session_date and str(p.get("side") or "buy") == "buy"
    ]
    if not due:
        return rows

    if dry or not conf.auto_submit:
        for p in due:
            rows.append(
                {
                    "symbol": p.get("symbol"),
                    "status": "dry_run_exit" if dry else "exit_queued_no_submit",
                    "exit_date": p.get("exit_date"),
                    "entry_date": p.get("entry_date"),
                    "qty": p.get("qty"),
                    "strategy_id": p.get("strategy_id"),
                }
            )
        return rows

    from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

    if host_needs_fubon_venv():
        try:
            worker_out = run_fubon_order_worker(
                {
                    "kind": "leading_dip_exits",
                    "ledger_path": str(ledger_path),
                    "user_def": user_def,
                    "session_date": session_date,
                    "poll_minute": poll_minute,
                    "dry_run": False,
                }
            )
        except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
            return [{"status": "error", "error": f"fubon_subprocess_failed:{exc}"}]
        if isinstance(worker_out, list):
            return worker_out
        return [{"status": "error", "error": "fubon_subprocess_bad_result"}]

    from order.chase_runner import chase_bid_price
    from order.fubon_orders import place_resolved_order
    from order.fubon_session import connect_fubon
    from order.oms_lifecycle import (
        outer_status_from_lifecycle,
        poll_order_lifecycle,
        reconcile_before_submit,
    )

    session = connect_fubon(realtime=True)
    for p in due:
        sym = str(p.get("symbol") or "").strip()
        qty = int(p.get("qty") or 0)
        if not sym or qty <= 0:
            rows.append({"symbol": sym, "status": "skip", "reason": "bad_qty"})
            continue
        cid = build_client_intent_id(
            strategy_id=user_def,
            session_date=session_date,
            poll_minute=poll_minute,
            symbol=f"{sym}-x",
        )
        try:
            bid = float(chase_bid_price(session, sym))
        except Exception as exc:  # noqa: BLE001
            rows.append({"symbol": sym, "status": "quote_error", "error": str(exc)})
            continue
        market_type = "intraday_odd" if qty < 1000 else "common"
        resolved = ResolvedOrder(
            symbol=sym,
            side="sell",
            quantity_shares=qty,
            price=f"{bid:.2f}",
            price_type="limit",
            market_type=market_type,  # type: ignore[arg-type]
            time_in_force=conf.time_in_force,  # type: ignore[arg-type]
            order_type="stock",
            user_def=cid,
            note=f"Leading Dip exit T+{conf.hold_days}",
            source="delta",
        )
        try:
            # 2026-08-08 code review 修正：原本這裡兩個問題——(1) reconcile_before_submit()
            # 少傳 fallback_ask/fallback_qty 兩個必要 keyword-only 參數，poll_order_lifecycle()
            # 少傳 order_no/fallback_ask/fallback_qty 三個，函式定義都沒有預設值，每次真的
            # 執行到這裡都會 TypeError，被下面的 except 吃掉變成 status="error"，出場單
            # 永遠無法正確追蹤成交狀態；(2) reconcile 的回傳值原本被完全丟棄，等於沒有
            # broker 端冪等檢查——同一 client_intent_id 若已有訂單，仍會嘗試重複下單。
            reconciled = reconcile_before_submit(
                session, client_intent_id=cid, fallback_ask=bid, fallback_qty=qty,
            )
            if reconciled is not None:
                status = outer_status_from_lifecycle(str(reconciled.get("lifecycle_status") or ""))
            else:
                submit_result = place_resolved_order(session, resolved)
                lc = poll_order_lifecycle(
                    session,
                    client_intent_id=cid,
                    order_no=str(submit_result.get("order_no") or "") or None,
                    fallback_ask=bid,
                    fallback_qty=qty,
                    max_attempts=conf.poll_max_attempts,
                    interval_sec=conf.poll_interval_sec,
                )
                status = outer_status_from_lifecycle(str(lc.get("lifecycle_status") or ""))
        except Exception as exc:  # noqa: BLE001
            rows.append({"symbol": sym, "status": "error", "error": str(exc)})
            continue
        update_position(
            ledger,
            client_intent_id=str(p.get("client_intent_id") or ""),
            fields={
                "status": "closed" if status in {"filled", "partial"} else "exit_pending",
                "exit_client_intent_id": cid,
                "exit_status": status,
                "exit_poll": poll_minute,
            },
            path=ledger_path,
        )
        rows.append({"symbol": sym, "status": status, "client_intent_id": cid, "qty": qty})
    return rows


def _submit_entry(
    *,
    row: dict[str, Any],
    conf: LeadingDipOrderConfig,
    session_date: str,
    poll_minute: str,
    trading_dates: list[str],
    ledger: LeadingDipLedger,
    ledger_path: Path,
    dry: bool,
    strategy_id: str,
    user_def: str,
    budget_twd: int,
    sleeve: str,
) -> dict[str, Any]:
    sym = str(row.get("sid") or "").strip()
    trigger_minute = str(row.get("minute") or poll_minute)
    exit_d = _exit_date(session_date, conf.hold_days, trading_dates)
    cid = build_client_intent_id(
        strategy_id=user_def,
        session_date=session_date,
        poll_minute=trigger_minute.replace(":", ""),
        symbol=sym,
    )
    base = {
        "symbol": sym,
        "side": "buy",
        "entry_date": session_date,
        "entry_minute": trigger_minute,
        "exit_date": exit_d,
        "client_intent_id": cid,
        "ex0": row.get("ex0"),
        "gap0": row.get("gap0"),
        "rB_min": row.get("rB_min"),
        "mild_down": row.get("mild_down"),
        "excess_anchor": row.get("excess_anchor"),
        "budget_twd": budget_twd,
        "strategy_id": strategy_id,
        "sleeve": sleeve,
    }

    if dry or not conf.auto_submit:
        return {
            **base,
            "status": "dry_run" if dry else "queued_no_submit",
            "qty": 0,
            "note": "no_ledger_write",
        }

    from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

    if host_needs_fubon_venv():
        try:
            worker_out = run_fubon_order_worker(
                {
                    "kind": "leading_dip_entry",
                    "row": _row_to_payload(row),
                    "session_date": session_date,
                    "poll_minute": poll_minute,
                    "trading_dates": list(trading_dates or []),
                    "ledger_path": str(ledger_path),
                    "strategy_id": strategy_id,
                    "user_def": user_def,
                    "budget_twd": int(budget_twd),
                    "sleeve": sleeve,
                    "dry_run": False,
                }
            )
        except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
            # Worker crashed before it could write its own ledger row (e.g. Fubon
            # connect failed, outside _submit_entry's try). Record the failed
            # attempt here so the poll driver can skip this symbol for the rest of
            # the day instead of re-firing every tick.
            append_position(
                ledger,
                {**base, "status": "failed", "qty": 0, "error": f"fubon_subprocess_failed:{exc}"},
                path=ledger_path,
            )
            return {**base, "status": "error", "error": f"fubon_subprocess_failed:{exc}"}
        if isinstance(worker_out, dict):
            return worker_out
        append_position(
            ledger,
            {**base, "status": "failed", "qty": 0, "error": "fubon_subprocess_bad_result"},
            path=ledger_path,
        )
        return {**base, "status": "error", "error": "fubon_subprocess_bad_result"}

    from order.chase_runner import chase_ask_price
    from order.fubon_account import bank_remain
    from order.fubon_orders import place_resolved_order
    from order.fubon_session import connect_fubon
    from order.oms_lifecycle import (
        outer_status_from_lifecycle,
        poll_order_lifecycle,
        reconcile_before_submit,
    )

    session = connect_fubon(realtime=True)
    try:
        ask = float(chase_ask_price(session, sym))
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "quote_error", "error": str(exc)}

    qty = shares_for_budget(budget_twd, ask)
    if qty <= 0:
        return {**base, "status": "skip", "reason": "qty_zero"}

    notional = qty * ask
    try:
        cash = int(bank_remain(session).get("available_balance", 0) or 0)
    except Exception:
        cash = 0
    ok, reason = can_afford_buy(notional_twd=notional, available_balance=cash)
    if not ok:
        return {**base, "status": "skip", "reason": reason, "qty": qty}

    market_type = "intraday_odd" if qty < 1000 else conf.market_type
    resolved = ResolvedOrder(
        symbol=sym,
        side="buy",
        quantity_shares=qty,
        price=f"{ask:.2f}",
        price_type="limit",
        market_type=market_type,  # type: ignore[arg-type]
        time_in_force=conf.time_in_force,  # type: ignore[arg-type]
        order_type="stock",
        user_def=cid,
        note=(
            f"Leading Dip [{sleeve}] gap={row.get('gap0')} ex0={row.get('ex0')} "
            f"anchor={row.get('excess_anchor')}"
        ),
        source="delta",
    )
    try:
        # 2026-08-08 code review 修正：同一個bug在進場路徑也有一份，見exit路徑的
        # 註解——reconcile_before_submit()少傳fallback_ask/fallback_qty、回傳值被丟棄
        # （沒有broker端冪等檢查），poll_order_lifecycle()少傳order_no/fallback_ask/
        # fallback_qty，每次真的執行都會TypeError。
        reconciled = reconcile_before_submit(
            session, client_intent_id=cid, fallback_ask=ask, fallback_qty=qty,
        )
        if reconciled is not None:
            status = outer_status_from_lifecycle(str(reconciled.get("lifecycle_status") or ""))
        else:
            submit_result = place_resolved_order(session, resolved)
            lc = poll_order_lifecycle(
                session,
                client_intent_id=cid,
                order_no=str(submit_result.get("order_no") or "") or None,
                fallback_ask=ask,
                fallback_qty=qty,
                max_attempts=conf.poll_max_attempts,
                interval_sec=conf.poll_interval_sec,
            )
            status = outer_status_from_lifecycle(str(lc.get("lifecycle_status") or ""))
    except Exception as exc:  # noqa: BLE001
        append_position(
            ledger,
            {**base, "status": "failed", "qty": qty, "error": str(exc)},
            path=ledger_path,
        )
        return {**base, "status": "error", "error": str(exc), "qty": qty}

    append_position(
        ledger,
        {
            **base,
            "status": "open" if status in {"filled", "partial", "working", "submitted"} else status,
            "qty": qty,
            "ask": ask,
            "lifecycle_status": status,
        },
        path=ledger_path,
    )
    return {**base, "status": status, "qty": qty, "ask": ask}
