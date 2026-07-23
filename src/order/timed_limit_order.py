"""Timed limit order · place then cancel if unfilled after timeout.

Used for one-shot jobs (e.g. 6451 sell @462; expert-pool A buys @ close×1.01).
Multiple due jobs share one wait (max timeout) so launchd ExitTimeOut stays sane.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from order.intent import ResolvedOrder

_TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parents[2]
ORDER_YAML = ROOT / "config" / "order.yaml"
STATE_PATH = ROOT / "data" / "order" / "timed_limit_orders_state.json"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_timed_jobs() -> list[dict[str, Any]]:
    if not ORDER_YAML.is_file():
        return []
    data = yaml.safe_load(ORDER_YAML.read_text(encoding="utf-8")) or {}
    jobs = data.get("timed_limit_orders") or []
    return list(jobs) if isinstance(jobs, list) else []


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"done": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"done": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def resolve_quantity_shares(job: dict[str, Any]) -> int:
    """quantity_shares, else floor(budget_twd / price), min 1."""
    if job.get("quantity_shares") is not None:
        return max(1, int(job["quantity_shares"]))
    budget = job.get("budget_twd")
    price = float(job.get("price") or 0)
    if budget is not None and price > 0:
        return max(1, int(float(budget) // price))
    return 1000


def _clock_ok(job: dict[str, Any], *, now_hm: str) -> bool:
    if _env_flag("ORDER_TIMED_LIMIT_IGNORE_CLOCK", "0"):
        return True
    want = str(job.get("time") or "09:05")
    h, m = map(int, want.split(":"))
    nh, nm = map(int, now_hm.split(":"))
    return abs((nh * 60 + nm) - (h * 60 + m)) <= 2


def evaluate_job_gate(
    job: dict[str, Any],
    *,
    session_date: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return skip payload, or None if job should run."""
    job_id = str(job.get("id") or f"{job.get('symbol')}_{job.get('side')}")
    once = str(job.get("once_date") or "").strip()
    if once and once != session_date:
        return {
            "status": "skip_date",
            "job_id": job_id,
            "once_date": once,
            "today": session_date,
        }
    if not bool(job.get("enabled", True)):
        return {"status": "disabled", "job_id": job_id}
    state = state if state is not None else _load_state()
    done_key = f"{job_id}|{session_date}"
    if done_key in (state.get("done") or {}) and not _env_flag(
        "ORDER_TIMED_LIMIT_FORCE", "0"
    ):
        return {"status": "already_done", "job_id": job_id, "key": done_key}
    return None


def build_job_payload(
    job: dict[str, Any],
    *,
    session_date: str,
    dry_run: bool,
) -> tuple[dict[str, Any], ResolvedOrder]:
    job_id = str(job.get("id") or f"{job.get('symbol')}_{job.get('side')}")
    symbol = str(job["symbol"])
    side = str(job.get("side") or "sell")
    qty = resolve_quantity_shares(job)
    price = str(job.get("price") or "")
    timeout_sec = int(job.get("timeout_sec") or 600)
    user_def = str(job.get("user_def") or "tlim")
    market_type = str(job.get("market_type") or "common")
    if qty < 1000 and market_type == "common":
        market_type = "intraday_odd"

    resolved = ResolvedOrder(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity_shares=qty,
        price=price,
        price_type="limit",
        market_type=market_type,  # type: ignore[arg-type]
        time_in_force="rod",
        order_type="stock",
        user_def=user_def,
        note=f"timed_limit|{job_id}|{session_date}",
        source="delta",
    )
    payload: dict[str, Any] = {
        "job_id": job_id,
        "session_date": session_date,
        "dry_run": dry_run,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "timeout_sec": timeout_sec,
        "market_type": market_type,
        "budget_twd": job.get("budget_twd"),
        "user_def": user_def,
    }
    return payload, resolved


def _order_still_open(session: Any, *, order_no: Any, symbol: str, user_def: str) -> bool:
    from order.fubon_orders import _result_data, is_open_order

    account = session.primary
    data = _result_data(session.sdk.stock.get_order_results(account))
    for item in list(data or []):
        ono = str(getattr(item, "order_no", "") or "")
        if order_no and ono == str(order_no):
            return bool(is_open_order(item))
        if str(getattr(item, "stock_no", "") or "") == symbol and is_open_order(item):
            if str(getattr(item, "user_def", "") or "") == user_def:
                return True
    return False


def run_timed_limit_job(
    job: dict[str, Any],
    *,
    session_date: str | None = None,
    dry_run: bool | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Place one limit order; optionally sleep timeout_sec then cancel if open."""
    today = session_date or datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    skip = evaluate_job_gate(job, session_date=today)
    if skip is not None:
        return skip

    dry = (
        dry_run
        if dry_run is not None
        else _env_flag("ORDER_TIMED_LIMIT_DRY_RUN", "1")
    )
    payload, resolved = build_job_payload(job, session_date=today, dry_run=dry)
    if dry:
        payload["status"] = "dry_run"
        return payload

    from order.fubon_orders import cancel_open_orders_for_symbols, place_resolved_order
    from order.fubon_session import connect_fubon

    session = connect_fubon(realtime=True)
    submit = place_resolved_order(session, resolved)
    payload["submit"] = submit
    order_no = submit.get("order_no")
    payload["order_no"] = order_no

    if wait:
        time.sleep(max(1, int(payload["timeout_sec"])))
        still_open = _order_still_open(
            session,
            order_no=order_no,
            symbol=payload["symbol"],
            user_def=str(payload["user_def"]),
        )
        cancelled: list[dict] = []
        if still_open:
            cancelled = cancel_open_orders_for_symbols(
                session, {payload["symbol"]}, side=payload["side"]
            )
        payload["still_open_after_timeout"] = still_open
        payload["cancelled"] = cancelled
        payload["status"] = "cancelled_unfilled" if still_open else "filled_or_done"
        done_key = f"{payload['job_id']}|{today}"
        state = _load_state()
        state.setdefault("done", {})[done_key] = {
            "at": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
            "status": payload["status"],
            "order_no": order_no,
        }
        _save_state(state)
        _maybe_email(payload)
    else:
        payload["status"] = "submitted_pending_wait"
    return payload


def _maybe_email(payload: dict[str, Any]) -> None:
    if not _env_flag("ORDER_TIMED_LIMIT_SUBMIT_EMAIL", "1"):
        return
    try:
        from notify_email import send_alert

        send_alert(
            f"[股票研究] 限時限價單 · {payload.get('symbol')} {payload.get('side')} · {payload.get('session_date')}",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        )
    except Exception as exc:  # noqa: BLE001
        payload["notify_error"] = str(exc)


def run_due_timed_jobs(*, session_date: str | None = None) -> dict[str, Any]:
    """Run all clock-due jobs; place together, one shared wait, then cancel."""
    today = session_date or datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    now_hm = datetime.now(tz=_TZ).strftime("%H:%M")
    dry = _env_flag("ORDER_TIMED_LIMIT_DRY_RUN", "1")
    state = _load_state()
    results: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], dict[str, Any], ResolvedOrder]] = []

    for job in load_timed_jobs():
        job_id = job.get("id")
        if not _clock_ok(job, now_hm=now_hm):
            results.append(
                {
                    "job_id": job_id,
                    "status": "skip_time",
                    "want": job.get("time"),
                    "now": now_hm,
                }
            )
            continue
        skip = evaluate_job_gate(job, session_date=today, state=state)
        if skip is not None:
            results.append(skip)
            continue
        payload, resolved = build_job_payload(job, session_date=today, dry_run=dry)
        if dry:
            payload["status"] = "dry_run"
            results.append(payload)
            continue
        pending.append((job, payload, resolved))

    if not pending:
        return {"session_date": today, "results": results, "batch_wait_sec": 0}

    from order.fubon_orders import cancel_open_orders_for_symbols, place_resolved_order
    from order.fubon_session import connect_fubon

    session = connect_fubon(realtime=True)
    max_wait = 1
    for _job, payload, resolved in pending:
        submit = place_resolved_order(session, resolved)
        payload["submit"] = submit
        payload["order_no"] = submit.get("order_no")
        payload["status"] = "submitted_pending_wait"
        max_wait = max(max_wait, int(payload["timeout_sec"]))

    time.sleep(max_wait)

    for _job, payload, _resolved in pending:
        still_open = _order_still_open(
            session,
            order_no=payload.get("order_no"),
            symbol=payload["symbol"],
            user_def=str(payload["user_def"]),
        )
        cancelled: list[dict] = []
        if still_open:
            cancelled = cancel_open_orders_for_symbols(
                session, {payload["symbol"]}, side=payload["side"]
            )
        payload["still_open_after_timeout"] = still_open
        payload["cancelled"] = cancelled
        payload["status"] = "cancelled_unfilled" if still_open else "filled_or_done"
        done_key = f"{payload['job_id']}|{today}"
        state.setdefault("done", {})[done_key] = {
            "at": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
            "status": payload["status"],
            "order_no": payload.get("order_no"),
        }
        _maybe_email(payload)
        results.append(payload)

    _save_state(state)
    return {
        "session_date": today,
        "results": results,
        "batch_wait_sec": max_wait,
        "n_live": len(pending),
    }
