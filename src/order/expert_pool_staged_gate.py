"""Expert-pool staged entry gate · gap → 09:05 → 09:25.

Practical tree (research funnel ops · not look-ahead P5):
  S0 open: gap < soft → buy; gap ≥ hard or gap > chase_gc → wait_25; else wait_05
  S1 05:   wait_05 only · perfect → buy; else wait_25
  S2 25:   wait_25 only · ret25 ≤ skip or fail_break → skip; else buy

Live: Fubon quote → chase_ask odd-lot · state in data/order/expert_pool_staged_gate_state.json
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

from order.fubon_orders import order_master_enabled
from order.json_state import atomic_write_json, load_json_or_default
from order.songshan_copytrade_order import evaluate_25m_nonfail
from stock_db import DATA_DIR

_TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parents[2]
ORDER_YAML = ROOT / "config" / "order.yaml"
STATE_PATH = DATA_DIR / "order" / "expert_pool_staged_gate_state.json"

_TRUE = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUE


def load_gate_config() -> dict[str, Any]:
    if not ORDER_YAML.is_file():
        return {}
    data = yaml.safe_load(ORDER_YAML.read_text(encoding="utf-8")) or {}
    raw = data.get("expert_pool_staged_gate") or {}
    return raw if isinstance(raw, dict) else {}


def _load_state() -> dict[str, Any]:
    return load_json_or_default(STATE_PATH, {"jobs": {}})


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    atomic_write_json(STATE_PATH, state, indent=2, trailing_newline=True)


def _job_key(job_id: str, session_date: str) -> str:
    return f"{job_id}|{session_date}"


def gap_pct(open_px: float, signal_close: float) -> float:
    if signal_close <= 0 or open_px <= 0:
        return float("nan")
    return (open_px / signal_close - 1.0) * 100.0


def classify_open(
    gap: float,
    *,
    soft_gap_pct: float = 2.0,
    hard_gap_pct: float = 4.0,
    chase_gc_pct: float = 3.0,
) -> str:
    """Return buy_open | wait_05 | wait_25."""
    if gap != gap:  # NaN
        return "wait_25"
    if gap >= hard_gap_pct or gap > chase_gc_pct:
        return "wait_25"
    if gap >= soft_gap_pct:
        return "wait_05"
    return "buy_open"


def classify_e05(
    gap: float,
    open_px: float,
    high_px: float | None,
    low_px: float | None,
    last_px: float,
    *,
    perfect_gap_max_pct: float = 3.0,
    ret05_perfect_min_pct: float = -0.5,
    dd05_perfect_min_pct: float = -2.0,
) -> str:
    """Return buy_05 | wait_25. Perfect ≈ gap≤3% ∧ ret05≥−0.5% ∧ dd05>−2%."""
    if open_px <= 0 or last_px <= 0:
        return "wait_25"
    ret05 = (last_px / open_px - 1.0) * 100.0
    low = low_px if low_px and low_px > 0 else last_px
    dd05 = (low / open_px - 1.0) * 100.0
    _ = high_px  # available for diagnostics; perfect uses ret/dd
    if (
        gap == gap
        and gap <= perfect_gap_max_pct
        and ret05 >= ret05_perfect_min_pct
        and dd05 > dd05_perfect_min_pct
    ):
        return "buy_05"
    return "wait_25"


def classify_e25(
    open_px: float,
    high_px: float | None,
    last_px: float,
    *,
    ret25_skip_max_pct: float = -2.0,
) -> str:
    """Return buy_25 | skip."""
    if open_px <= 0 or last_px <= 0:
        return "skip"
    ret25 = (last_px / open_px - 1.0) * 100.0
    fail = evaluate_25m_nonfail(open_px, high_px, last_px)
    if fail.get("fail") or ret25 <= ret25_skip_max_pct:
        return "skip"
    return "buy_25"


def detect_stage(now_hm: str | None = None) -> str | None:
    """Map wall clock → open | e05 | e25 | None."""
    if _env_flag("ORDER_EP_STAGED_GATE_IGNORE_CLOCK", "0"):
        forced = os.environ.get("ORDER_EP_STAGED_GATE_STAGE", "").strip().lower()
        if forced in ("open", "e05", "e25"):
            return forced
    hm = now_hm or datetime.now(tz=_TZ).strftime("%H:%M")
    h, m = map(int, hm.split(":"))
    mins = h * 60 + m
    # open: 09:00–09:02 (retry window for auction settle)
    if 9 * 60 <= mins <= 9 * 60 + 2:
        return "open"
    if 9 * 60 + 5 <= mins <= 9 * 60 + 7:
        return "e05"
    if 9 * 60 + 25 <= mins <= 9 * 60 + 27:
        return "e25"
    return None


def _thresholds(cfg: dict[str, Any]) -> dict[str, float]:
    t = cfg.get("thresholds") or {}
    return {
        "soft_gap_pct": float(t.get("soft_gap_pct", 2.0)),
        "hard_gap_pct": float(t.get("hard_gap_pct", 4.0)),
        "chase_gc_pct": float(t.get("chase_gc_pct", 3.0)),
        "perfect_gap_max_pct": float(t.get("perfect_gap_max_pct", 3.0)),
        "ret05_perfect_min_pct": float(t.get("ret05_perfect_min_pct", -0.5)),
        "dd05_perfect_min_pct": float(t.get("dd05_perfect_min_pct", -2.0)),
        "ret25_skip_max_pct": float(t.get("ret25_skip_max_pct", -2.0)),
    }


def fetch_live_quote(session: Any, symbol: str) -> dict[str, Any]:
    from fubon_neo.constant import MarketType

    from order.fubon_orders import _result_ok

    account = session.primary
    open_px = high_px = low_px = last_px = None
    for mt in (MarketType.Common, MarketType.IntradayOdd):
        res = session.sdk.stock.query_symbol_quote(account, symbol, mt)
        if not _result_ok(res) or res.data is None:
            continue
        d = res.data
        o = getattr(d, "open_price", None) or getattr(d, "open", None)
        h = getattr(d, "high_price", None) or getattr(d, "high", None)
        lo = getattr(d, "low_price", None) or getattr(d, "low", None)
        last = getattr(d, "last_price", None) or getattr(d, "close_price", None)
        if o is not None and float(o) > 0:
            open_px = float(o)
        if h is not None and float(h) > 0:
            high_px = float(h)
        if lo is not None and float(lo) > 0:
            low_px = float(lo)
        if last is not None and float(last) > 0:
            last_px = float(last)
        if open_px and last_px:
            break
    return {
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "last": last_px,
    }


def _resolve_qty(*, budget_twd: float, price: float, quantity_shares: int | None) -> int:
    if quantity_shares is not None:
        return max(1, int(quantity_shares))
    if price > 0 and budget_twd > 0:
        return max(1, int(float(budget_twd) // price))
    return 1


def _maybe_email(payload: dict[str, Any]) -> None:
    if not _env_flag("ORDER_EP_STAGED_GATE_SUBMIT_EMAIL", "1"):
        return
    try:
        from notify_email import send_alert

        send_alert(
            f"[股票研究] 專家池漏斗閘門 · {payload.get('session_date')} · {payload.get('stage')}",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        )
    except Exception as exc:  # noqa: BLE001
        payload["notify_error"] = str(exc)


def _place_buy(
    session: Any,
    *,
    symbol: str,
    qty: int,
    user_def: str,
    note: str,
    market_type: str,
    timeout_sec: int,
    dry: bool,
) -> dict[str, Any]:
    from order.chase_runner import chase_ask_price
    from order.fubon_orders import cancel_open_orders_for_symbols, place_resolved_order
    from order.intent import ResolvedOrder

    ask = float(chase_ask_price(session, symbol))
    out: dict[str, Any] = {
        "ask": ask,
        "quantity_shares": qty,
        "market_type": market_type if qty < 1000 else "common",
    }
    mt = out["market_type"]
    resolved = ResolvedOrder(
        symbol=symbol,
        side="buy",
        quantity_shares=qty,
        price=str(ask),
        price_type="limit",
        market_type=mt,  # type: ignore[arg-type]
        time_in_force="rod",
        order_type="stock",
        user_def=user_def,
        note=note,
        source="delta",
    )
    if dry:
        out["status"] = "dry_run"
        return out

    submit = place_resolved_order(session, resolved)
    out["submit"] = submit
    out["order_no"] = submit.get("order_no")
    if timeout_sec > 0:
        time.sleep(max(1, int(timeout_sec)))
        from order.fubon_orders import order_still_open

        still = order_still_open(
            session,
            order_no=out.get("order_no"),
            symbol=symbol,
            user_def=user_def,
        )
        cancelled: list[dict] = []
        if still:
            cancelled = cancel_open_orders_for_symbols(session, {symbol}, side="buy")
        out["still_open_after_timeout"] = still
        out["cancelled"] = cancelled
        out["status"] = "cancelled_unfilled" if still else "filled_or_done"
    else:
        out["status"] = "submitted" if submit.get("is_success") else "submit_failed"
    return out


def process_job(
    job: dict[str, Any],
    *,
    cfg: dict[str, Any],
    stage: str,
    session_date: str,
    state: dict[str, Any],
    session: Any | None,
    dry_run: bool,
) -> dict[str, Any]:
    job_id = str(job.get("id") or "")
    symbol = str(job.get("symbol") or "").strip()
    thr = _thresholds(cfg)
    key = _job_key(job_id, session_date)
    row = dict((state.get("jobs") or {}).get(key) or {})
    phase = str(row.get("phase") or "pending")
    payload: dict[str, Any] = {
        "job_id": job_id,
        "symbol": symbol,
        "session_date": session_date,
        "stage": stage,
        "phase_before": phase,
        "dry_run": dry_run,
    }

    if not job.get("enabled", True):
        payload["status"] = "skip_disabled"
        return payload
    once = job.get("once_date")
    if once and str(once) != session_date and not _env_flag("ORDER_EP_STAGED_GATE_FORCE", "0"):
        payload["status"] = "skip_date"
        return payload
    if phase in ("bought", "skipped", "done"):
        payload["status"] = f"already_{phase}"
        payload["phase"] = phase
        return payload

    signal_close = float(job.get("signal_close") or 0)
    if signal_close <= 0:
        payload["status"] = "missing_signal_close"
        return payload
    payload["signal_close"] = signal_close

    if session is None:
        payload["status"] = "no_session"
        return payload

    quote = fetch_live_quote(session, symbol)
    payload["quote"] = quote
    open_px = quote.get("open")
    last_px = quote.get("last")
    if not open_px or not last_px:
        payload["status"] = "quote_pending"
        return payload

    g = gap_pct(float(open_px), signal_close)
    payload["gap_pct"] = round(g, 4)
    budget = float(job.get("budget_twd") or cfg.get("budget_twd") or 10000)
    user_def = str(job.get("user_def") or cfg.get("user_def") or "epsg")
    timeout_sec = int(job.get("timeout_sec") or cfg.get("timeout_sec") or 600)
    market_type = str(job.get("market_type") or cfg.get("market_type") or "intraday_odd")
    qty_fixed = job.get("quantity_shares")
    qty_fixed_i = int(qty_fixed) if qty_fixed is not None else None

    decision: str | None = None
    if stage == "open":
        if phase not in ("pending",):
            payload["status"] = f"skip_phase_{phase}"
            return payload
        decision = classify_open(
            g,
            soft_gap_pct=thr["soft_gap_pct"],
            hard_gap_pct=thr["hard_gap_pct"],
            chase_gc_pct=thr["chase_gc_pct"],
        )
        row["open_px"] = float(open_px)
        row["gap_pct"] = round(g, 4)
        row["signal_close"] = signal_close
    elif stage == "e05":
        if phase != "wait_05":
            payload["status"] = f"skip_phase_{phase}"
            return payload
        decision = classify_e05(
            g,
            float(open_px),
            quote.get("high"),
            quote.get("low"),
            float(last_px),
            perfect_gap_max_pct=thr["perfect_gap_max_pct"],
            ret05_perfect_min_pct=thr["ret05_perfect_min_pct"],
            dd05_perfect_min_pct=thr["dd05_perfect_min_pct"],
        )
        row["ret05_pct"] = round((float(last_px) / float(open_px) - 1.0) * 100.0, 4)
    elif stage == "e25":
        if phase != "wait_25":
            payload["status"] = f"skip_phase_{phase}"
            return payload
        decision = classify_e25(
            float(open_px),
            quote.get("high"),
            float(last_px),
            ret25_skip_max_pct=thr["ret25_skip_max_pct"],
        )
        row["ret25_pct"] = round((float(last_px) / float(open_px) - 1.0) * 100.0, 4)
        row["fail_gate"] = evaluate_25m_nonfail(
            float(open_px), quote.get("high"), float(last_px)
        )
    else:
        payload["status"] = "bad_stage"
        return payload

    payload["decision"] = decision
    log = list(row.get("log") or [])
    log.append(
        {
            "at": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
            "stage": stage,
            "decision": decision,
            "gap_pct": round(g, 4),
            "quote": quote,
        }
    )
    row["log"] = log

    if decision in ("wait_05", "wait_25"):
        row["phase"] = decision
        if not dry_run:
            state.setdefault("jobs", {})[key] = row
            _save_state(state)
        else:
            # dry_run still persists deferral so 05/25 ticks can be simulated
            state.setdefault("jobs", {})[key] = row
            _save_state(state)
        payload["phase"] = row["phase"]
        payload["status"] = decision
        return payload

    if decision == "skip":
        row["phase"] = "skipped"
        state.setdefault("jobs", {})[key] = row
        _save_state(state)
        payload["phase"] = "skipped"
        payload["status"] = "skipped"
        _maybe_email(payload)
        return payload

    # buy_*
    ask_for_qty = float(last_px)
    try:
        from order.chase_runner import chase_ask_price

        ask_for_qty = float(chase_ask_price(session, symbol))
    except Exception:  # noqa: BLE001
        pass
    qty = _resolve_qty(
        budget_twd=budget, price=ask_for_qty, quantity_shares=qty_fixed_i
    )
    buy = _place_buy(
        session,
        symbol=symbol,
        qty=qty,
        user_def=user_def,
        note=f"ep_staged|{decision}|{session_date}|g={g:.2f}",
        market_type=market_type,
        timeout_sec=timeout_sec if not dry_run else 0,
        dry=dry_run,
    )
    payload["buy"] = buy
    if dry_run:
        # Preview only — do not lock phase so live/re-run can still act.
        payload["status"] = "dry_run_buy"
        payload["phase"] = phase
        return payload

    row["phase"] = "bought"
    row["buy"] = buy
    state.setdefault("jobs", {})[key] = row
    _save_state(state)
    payload["phase"] = "bought"
    payload["status"] = buy.get("status") or "bought"
    _maybe_email(payload)
    return payload


def run_staged_gate(
    *,
    session_date: str | None = None,
    stage: str | None = None,
    dry_run: bool | None = None,
    now_hm: str | None = None,
) -> dict[str, Any]:
    cfg = load_gate_config()
    today = session_date or datetime.now(tz=_TZ).strftime("%Y-%m-%d")
    dry = (
        dry_run
        if dry_run is not None
        else (
            _env_flag(
                "ORDER_EP_STAGED_GATE_DRY_RUN",
                "1" if cfg.get("dry_run", True) else "0",
            )
            # Master switch forces the env-derived default to dry-run; an explicit
            # dry_run= passed by a caller/test is left alone (matches how the other
            # 4 sleeves only gate their auto_submit-equivalent, not explicit params).
            or not order_master_enabled()
        )
    )
    enabled = bool(cfg.get("enabled", True))
    if os.environ.get("ORDER_EP_STAGED_GATE_ENABLED", "").strip():
        enabled = _env_flag("ORDER_EP_STAGED_GATE_ENABLED", "1")

    stage_use = stage or detect_stage(now_hm)
    out: dict[str, Any] = {
        "status": "ok",
        "session_date": today,
        "stage": stage_use,
        "enabled": enabled,
        "dry_run": dry,
        "results": [],
    }
    if not enabled:
        out["status"] = "disabled"
        return out
    if stage_use is None:
        out["status"] = "outside_stage_window"
        return out

    jobs = cfg.get("jobs") or []
    if not isinstance(jobs, list) or not jobs:
        out["status"] = "no_jobs"
        return out

    state = _load_state()
    session = None
    try:
        from order.fubon_session import connect_fubon

        session = connect_fubon(realtime=True)
    except Exception as exc:  # noqa: BLE001
        out["status"] = "session_error"
        out["error"] = str(exc)
        return out

    for job in jobs:
        if not isinstance(job, dict):
            continue
        out["results"].append(
            process_job(
                job,
                cfg=cfg,
                stage=stage_use,
                session_date=today,
                state=state,
                session=session,
                dry_run=dry,
            )
        )
    return out
