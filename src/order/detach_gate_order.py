"""Detach Gate（台美脫鉤閘門）· Order layer half-flatten at bid1.

Strategy: detach-gate · RED → sell floor(qty/2) each holding at chase_bid (買一).
Idempotent once per session_date:
  live: half_flatten_attempted (any place_order try) · half_flatten_done (success)
  dry: half_flatten_dry_run
Notifies on first RED and first order attempt only (no 5m email / Fubon flood while latched).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from order.oms_lifecycle import build_client_intent_id
from order.fubon_orders import order_master_enabled
from order.intent import ResolvedOrder
from order.json_state import atomic_write_json, load_json_or_default
from order.live_submit_guard import assert_live_submit_allowed, live_submit_block_reason
from stock_db import DATA_DIR

_TZ = ZoneInfo("Asia/Taipei")
STRATEGY_ID = "detach-gate"
USER_DEF_PREFIX = "dg"  # short tag for Fubon user_def · matches lifecycle lookup
LEDGER_PATH = DATA_DIR / "order" / "detach_gate_ledger.json"
SCHEMA = "detach-gate-order-v1"


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def half_qty(shares: int) -> int:
    """Sell floor(n/2); skip singles (cannot half without full exit)."""
    if shares < 2:
        return 0
    return int(shares) // 2


def half_client_intent_id(*, session_date: str, symbol: str, poll_minute: str) -> str:
    """Deterministic id used as Fubon user_def AND lifecycle client_intent_id."""
    return build_client_intent_id(
        strategy_id=USER_DEF_PREFIX,
        session_date=session_date,
        poll_minute=poll_minute,
        symbol=symbol,
    )


def load_ledger(path: Path | None = None) -> dict[str, Any]:
    p = path or LEDGER_PATH
    raw = load_json_or_default(p, {"schema": SCHEMA, "sessions": {}})
    if not isinstance(raw, dict):
        return {"schema": SCHEMA, "sessions": {}}
    raw.setdefault("schema", SCHEMA)
    raw.setdefault("sessions", {})
    return raw


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> None:
    p = path or LEDGER_PATH
    atomic_write_json(p, ledger, indent=2, trailing_newline=True)


def session_already_flattened(ledger: dict[str, Any], session_date: str) -> bool:
    sess = (ledger.get("sessions") or {}).get(session_date) or {}
    return bool(sess.get("half_flatten_done"))


def session_live_attempted(ledger: dict[str, Any], session_date: str) -> bool:
    """True after any live half-flatten attempt today (success or broker reject).

    Prevents 5m launchd from re-calling Fubon ``place_order`` while RED_LATCHED.
    """
    sess = (ledger.get("sessions") or {}).get(session_date) or {}
    return bool(
        sess.get("half_flatten_attempted")
        or sess.get("half_flatten_done")
    )


def session_dry_run_done(ledger: dict[str, Any], session_date: str) -> bool:
    """True after a completed dry-run half-flatten for this session_date."""
    sess = (ledger.get("sessions") or {}).get(session_date) or {}
    return bool(sess.get("half_flatten_dry_run"))


def mark_signal_notified(ledger: dict[str, Any], session_date: str, *, level: str, poll: str) -> bool:
    """Return True if this is the first RED notify of the day."""
    sessions = ledger.setdefault("sessions", {})
    sess = sessions.setdefault(session_date, {})
    if sess.get("signal_notified"):
        return False
    sess["signal_notified"] = True
    sess["signal_level"] = level
    sess["signal_poll"] = poll
    sess["signal_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    return True


def mark_order_notified(ledger: dict[str, Any], session_date: str) -> bool:
    """Return True if this is the first order/email notify of the day.

    Stops launchd ``DETACH_GATE_ORDER=1`` + order email from repeating every 5m
    while RED_LATCHED (dry-run or live retry).
    """
    sessions = ledger.setdefault("sessions", {})
    sess = sessions.setdefault(session_date, {})
    if sess.get("order_notified"):
        return False
    sess["order_notified"] = True
    sess["order_notified_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    return True


def run_half_flatten(
    *,
    session_date: str | None = None,
    dry_run: bool | None = None,
    auto_submit: bool | None = None,
    order_enabled: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Sell floor(qty/2) @ bid1 for **all broker holdings**（全帳戶 · not sleeve-scoped）.

    Guarded by env + live_submit_guard.
    ``force=True`` bypasses dry-run session idempotency (re-preview / ops).
    """
    day = session_date or date.today().isoformat()
    dry = _env_flag("ORDER_DETACH_GATE_DRY_RUN", "1") if dry_run is None else dry_run
    enabled = (
        _env_flag("ORDER_DETACH_GATE_ORDER_ENABLED", "1")
        if order_enabled is None
        else order_enabled
    )
    do_submit = (
        (_env_flag("ORDER_DETACH_GATE_AUTO_SUBMIT", "1") and order_master_enabled())
        if auto_submit is None
        else auto_submit
    )

    out: dict[str, Any] = {
        "schema": SCHEMA,
        "strategy_id": STRATEGY_ID,
        "session_date": day,
        "dry_run": dry,
        "order_enabled": enabled,
        "auto_submit": do_submit,
        "orders": [],
        "status": "ok",
    }

    if not enabled:
        out["status"] = "disabled"
        out["reason"] = "ORDER_DETACH_GATE_ORDER_ENABLED=0"
        return out

    ledger = load_ledger()
    if session_already_flattened(ledger, day):
        out["status"] = "already_done"
        out["reason"] = "half_flatten_already_done_today"
        out["ledger_session"] = (ledger.get("sessions") or {}).get(day)
        return out
    # Live: one place_order wave per session_date (even if all rejects).
    if (not dry) and session_live_attempted(ledger, day) and not force:
        out["status"] = "already_attempted"
        out["reason"] = "half_flatten_already_attempted_today"
        out["ledger_session"] = (ledger.get("sessions") or {}).get(day)
        return out
    # Dry-run is idempotent per session: avoid re-quote / re-notify every 5m poll.
    # Live path (dry=False) may still proceed after a prior dry_run preview.
    if dry and session_dry_run_done(ledger, day) and not force:
        out["status"] = "already_dry_run"
        out["reason"] = "half_flatten_dry_run_already_today"
        out["ledger_session"] = (ledger.get("sessions") or {}).get(day)
        return out

    block = live_submit_block_reason(session_date=day)
    if block and not dry:
        out["status"] = "blocked"
        out["reason"] = block
        return out

    if not dry:
        assert_live_submit_allowed(session_date=day)

    from order.fubon_subprocess import host_needs_fubon_venv, run_fubon_order_worker

    # Preview + live both need Fubon holdings; spawn .venv-fubon when host lacks neo.
    if host_needs_fubon_venv():
        try:
            worker_out = run_fubon_order_worker(
                {
                    "kind": "detach_half_flatten",
                    "session_date": day,
                    "dry_run": dry,
                    "auto_submit": do_submit,
                    "order_enabled": enabled,
                    "force": force,
                }
            )
        except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
            out["status"] = "error"
            out["reason"] = f"fubon_subprocess_failed:{exc}"
            return out
        if isinstance(worker_out, dict):
            return worker_out
        out["status"] = "error"
        out["reason"] = "fubon_subprocess_bad_result"
        return out

    from order.chase_runner import chase_bid_price
    from order.fubon_orders import holdings_shares_by_symbol, place_resolved_order
    from order.fubon_session import connect_fubon
    from order.oms_lifecycle import (
        poll_order_lifecycle,
        reconcile_before_submit,
        outer_status_from_lifecycle,
    )

    session = connect_fubon(realtime=True)
    holdings = holdings_shares_by_symbol(session)
    out["holdings"] = dict(holdings)
    poll_minute = datetime.now(tz=_TZ).strftime("%H:%M")
    rows: list[dict[str, Any]] = []

    # ⚠️ 2026-08-08 code review 修正：live 模式下，在真的送出任何一張賣單之前先把
    # half_flatten_attempted 存檔。原本這個 latch 要整個 for 迴圈跑完才寫——若迴圈跑到
    # 一半時某檔股票的 broker 呼叫拋例外，整支函式會直接崩潰、attempted/done 都沒寫入，
    # 已經真的賣掉的持倉完全不會被記錄。下一次觸發（重試、或跟排程重疊）會重新讀到
    # 縮水後的持倉，對它再賣一次 floor(新股數/2)，等於半倉停損被觸發兩次。提前 latch
    # 能讓任何後續呼叫立刻被 session_live_attempted() 擋下，不會重跑整批。
    if not dry and do_submit:
        sess = ledger.setdefault("sessions", {}).setdefault(day, {})
        sess["half_flatten_attempted"] = True
        sess["attempted_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
        save_ledger(ledger)

    for sym, qty in sorted(holdings.items()):
        sell_n = half_qty(int(qty))
        if sell_n <= 0:
            rows.append(
                {
                    "symbol": sym,
                    "status": "skipped",
                    "reason": "qty_lt_2",
                    "holdings_qty": qty,
                }
            )
            continue
        try:
            bid = float(chase_bid_price(session, sym))
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "symbol": sym,
                    "status": "quote_error",
                    "error": str(exc),
                    "holdings_qty": qty,
                    "sell_qty": sell_n,
                }
            )
            continue

        cid = half_client_intent_id(
            session_date=day, symbol=sym, poll_minute=poll_minute
        )
        market_type = "intraday_odd" if sell_n < 1000 else "common"
        resolved = ResolvedOrder(
            symbol=sym,
            side="sell",
            quantity_shares=sell_n,
            price=f"{bid:.2f}",
            price_type="limit",
            market_type=market_type,  # type: ignore[arg-type]
            time_in_force="rod",
            order_type="stock",
            user_def=cid,  # must equal client_intent_id for lifecycle reconcile
            note=f"Detach Gate half @bid1 · hold={qty}",
            source="delta",
        )
        preview: dict[str, Any] = {
            "symbol": sym,
            "status": "dry_run" if dry or not do_submit else "pending",
            "client_intent_id": cid,
            "holdings_qty": qty,
            "sell_qty": sell_n,
            "bid_price": bid,
            "resolved": asdict(resolved),
        }
        if dry or not do_submit:
            preview["status"] = "dry_run" if dry else "submit_disabled"
            rows.append(preview)
            continue

        try:
            reconciled = reconcile_before_submit(
                session,
                client_intent_id=cid,
                fallback_ask=bid,
                fallback_qty=sell_n,
            )
            if reconciled is not None:
                preview.update(reconciled)
                preview["status"] = "reconciled"
                rows.append(preview)
                continue

            submit_result = place_resolved_order(session, resolved)
            preview["submit_result"] = submit_result
            if not submit_result.get("is_success"):
                preview["status"] = "failed"
                preview["reason"] = str(submit_result.get("message") or "place_order_failed")
                rows.append(preview)
                continue

            lifecycle = poll_order_lifecycle(
                session,
                client_intent_id=cid,
                order_no=str(submit_result.get("order_no") or "") or None,
                fallback_ask=bid,
                fallback_qty=sell_n,
                max_attempts=8,
                interval_sec=0.4,
            )
            preview["lifecycle"] = lifecycle
            preview["status"] = outer_status_from_lifecycle(
                str(lifecycle.get("lifecycle_status") or "")
            )
            rows.append(preview)
        except Exception as exc:  # noqa: BLE001
            # 單一標的的 broker 呼叫失敗不該讓整批崩潰、丟失其餘標的的追蹤——記錄成
            # exception 狀態後繼續處理下一檔。half_flatten_attempted 已經在迴圈開始前
            # 寫入，就算這裡整支函式意外中斷，下一次呼叫也不會重跑整批。
            preview["status"] = "exception"
            preview["error"] = str(exc)
            rows.append(preview)
            continue

    out["orders"] = rows
    submitted_ish = [
        r
        for r in rows
        if r.get("status") in {"submitted", "filled", "partial", "working", "reconciled", "dry_run", "submit_disabled"}
        and r.get("sell_qty")
    ]
    # Mark done only for live successful path or dry_run with intended sells
    if dry:
        out["status"] = "dry_run"
        sess = ledger.setdefault("sessions", {}).setdefault(day, {})
        sess["half_flatten_dry_run"] = True
        sess["dry_run_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
        sess["orders"] = rows
        save_ledger(ledger)
        return out

    if do_submit:
        # Latch immediately after the live wave so 5m polls never re-hit Fubon API.
        sess = ledger.setdefault("sessions", {}).setdefault(day, {})
        sess["half_flatten_attempted"] = True
        sess["attempted_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
        sess["orders"] = rows
        if any(
            r.get("status") in {"submitted", "filled", "partial", "working", "reconciled"}
            for r in rows
        ):
            sess["half_flatten_done"] = True
            sess["flattened_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
            out["status"] = "submitted"
        else:
            out["status"] = "no_orders"
        save_ledger(ledger)
    else:
        out["status"] = "submit_disabled"

    out["n_actionable"] = len(submitted_ish)
    return out


def notify_signal(eval_payload: dict[str, Any], *, first: bool) -> bool:
    if not first:
        return False
    if not _env_flag("RUN_DETACH_GATE_EMAIL", "1"):
        return False
    from notify_email import send_alert

    latest = eval_payload.get("latest") or {}
    day = eval_payload.get("session_date")
    level = eval_payload.get("level")
    subject = f"[Detach Gate] {day} · {level} · 條件達標"
    body = "\n".join(
        [
            "台美脫鉤閘門 Detach Gate · RED 達標（人工／半自動半倉出清）",
            f"session={day}",
            f"level={level}",
            f"action_hint={eval_payload.get('action_hint')}",
            f"first_red={eval_payload.get('first_red_poll')}",
            f"latest={json.dumps(latest, ensure_ascii=False)}",
            f"rule={json.dumps(eval_payload.get('rule') or {}, ensure_ascii=False)}",
            "",
            "下單：買一價賣出各持倉 floor(qty/2) · 見 ORDER_DETACH_GATE_* flags",
        ]
    )
    send_alert(subject, body)
    print("DETACH_GATE_SIGNAL_EMAIL=1")
    return True


def notify_orders(order_payload: dict[str, Any]) -> bool:
    if not _env_flag("RUN_DETACH_GATE_EMAIL", "1"):
        return False
    rows = list(order_payload.get("orders") or [])
    if not rows:
        return False
    # Always notify when we attempted / dry-ran sells after RED
    from notify_email import send_alert
    from order.order_fill_report import maybe_send_fill_report

    # Prefer structured fill report when live statuses present
    fill_like = [
        {
            "symbol": r.get("symbol"),
            "side": "sell",
            "quantity_shares": r.get("sell_qty"),
            "price": r.get("bid_price"),
            "lifecycle_status": r.get("status"),
            "client_intent_id": r.get("client_intent_id"),
            "submit_status": r.get("status"),
            "note": r.get("reason") or r.get("error"),
        }
        for r in rows
        if r.get("sell_qty")
    ]
    if fill_like and order_payload.get("status") in {"submitted", "dry_run"}:
        sent = maybe_send_fill_report(
            fill_like,
            sleeve="Detach Gate",
            env_flag="RUN_DETACH_GATE_EMAIL",
            header=(
                f"Detach Gate half-flatten · status={order_payload.get('status')} · "
                f"dry_run={order_payload.get('dry_run')}"
            ),
        )
        if sent:
            return True

    day = order_payload.get("session_date")
    subject = f"[Detach Gate] {day} · 半倉下單 · {order_payload.get('status')}"
    lines = [
        f"status={order_payload.get('status')}",
        f"dry_run={order_payload.get('dry_run')}",
        f"reason={order_payload.get('reason')}",
        "",
    ]
    for r in rows:
        lines.append(
            f"{r.get('symbol')}: status={r.get('status')} sell={r.get('sell_qty')}/"
            f"{r.get('holdings_qty')} bid={r.get('bid_price')} {r.get('reason') or r.get('error') or ''}"
        )
    send_alert(subject, "\n".join(lines))
    print("DETACH_GATE_ORDER_EMAIL=1")
    return True
