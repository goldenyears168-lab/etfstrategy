"""Songshan copytrade Order · 五日累積淨比95 ∩ !mega + live 25m nonfail → 買零股或整股.

Signal: previous session ``scan_5d_net95`` (branch 9217).
Entry: T+1 after ``entry_after`` (default 09:25); fail-break skip; else chase_ask.
Sizing: budget_twd (約 10 萬，零股) or quantity_shares (固定股數).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from order.songshan_copytrade_config import (
    SongshanCopytradeOrderConfig,
    load_songshan_copytrade_order_config,
)
from order.songshan_copytrade_ledger import (
    already_handled,
    append_entry,
    load_ledger,
    save_ledger,
)

_TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parents[2]


def _load_mega(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return {str(x) for x in (raw.get("symbols") or [])}
    if isinstance(raw, list):
        return {str(x) for x in raw}
    return set()


def prev_trading_day(conn, *, asof: str) -> str | None:
    row = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE stock_id='2330' AND source='finmind' AND trade_date<? AND close>0
        ORDER BY trade_date DESC LIMIT 1
        """,
        (asof,),
    ).fetchone()
    return str(row[0]) if row else None


def _minute_in_window(minute: str, lo: str, hi: str) -> bool:
    return lo <= minute <= hi


def evaluate_25m_nonfail(
    open_px: float | None, high_px: float | None, last_px: float | None
) -> dict[str, Any]:
    """Fail = session high ≥ open×1.01 and last < open (aligned with research)."""
    if not open_px or not last_px:
        return {
            "ok": False,
            "fail": True,
            "reason": "quote_incomplete",
            "open": open_px,
            "high": high_px,
            "last": last_px,
        }
    high = high_px or last_px
    popped = high >= open_px * 1.01
    fail = bool(popped and last_px < open_px)
    return {
        "ok": not fail,
        "fail": fail,
        "reason": "fail_break" if fail else "nonfail",
        "open": open_px,
        "high": high,
        "last": last_px,
        "popped": popped,
    }


def live_25m_nonfail(session: Any, symbol: str) -> dict[str, Any]:
    """Live quote → ``evaluate_25m_nonfail``."""
    from fubon_neo.constant import MarketType

    from order.fubon_orders import _result_ok

    account = session.primary
    open_px = high_px = last_px = None
    for mt in (MarketType.Common, MarketType.IntradayOdd):
        res = session.sdk.stock.query_symbol_quote(account, symbol, mt)
        if not _result_ok(res) or res.data is None:
            continue
        d = res.data
        o = getattr(d, "open_price", None) or getattr(d, "open", None)
        h = getattr(d, "high_price", None) or getattr(d, "high", None)
        last = getattr(d, "last_price", None) or getattr(d, "close_price", None)
        if o is not None and float(o) > 0:
            open_px = float(o)
        if h is not None and float(h) > 0:
            high_px = float(h)
        if last is not None and float(last) > 0:
            last_px = float(last)
        if open_px and high_px and last_px:
            break
    return evaluate_25m_nonfail(open_px, high_px, last_px)


def _scan_candidates(conn, *, signal_date: str, cfg: SongshanCopytradeOrderConfig) -> list[dict]:
    # Import watch helpers without running CLI side effects
    import importlib.util
    import sys

    path = ROOT / "scripts" / "research" / "run_songshan_follow_watch.py"
    name = "run_songshan_follow_watch"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    else:
        mod = sys.modules[name]
    mega = _load_mega(cfg.mega_path)
    return mod.scan_5d_net95(
        conn,
        asof=signal_date,
        branch_id=cfg.branch_id,
        mega=mega,
        buy_floor=cfg.buy_floor,
        net_min=cfg.net_min,
    )


def process_songshan_copytrade_poll(
    *,
    session_date: str,
    poll_minute: str,
    conn: Any,
    dry_run: bool | None = None,
    cfg: SongshanCopytradeOrderConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_songshan_copytrade_order_config()
    dry = cfg.dry_run if dry_run is None else dry_run
    payload: dict[str, Any] = {
        "status": "ok",
        "session_date": session_date,
        "poll_minute": poll_minute,
        "enabled": cfg.enabled,
        "dry_run": dry,
        "entries": [],
    }
    if not cfg.enabled:
        payload["status"] = "disabled"
        return payload
    if not _minute_in_window(poll_minute, cfg.entry_after, cfg.entry_until):
        payload["status"] = "outside_entry_window"
        return payload

    signal_date = prev_trading_day(conn, asof=session_date)
    payload["signal_date"] = signal_date
    if not signal_date:
        payload["status"] = "no_signal_date"
        return payload

    cands = _scan_candidates(conn, signal_date=signal_date, cfg=cfg)
    payload["n_candidates"] = len(cands)
    if not cands:
        payload["status"] = "no_signal"
        return payload

    primary = cands[0]
    symbol = str(primary["stock_id"])
    payload["primary"] = {
        "symbol": symbol,
        "buy_5d": primary.get("buy_5d"),
        "net_ratio": primary.get("net_ratio"),
        "day_whale": primary.get("day_whale"),
    }

    ledger = load_ledger(cfg.ledger_path)
    if already_handled(ledger, signal_date=signal_date, symbol=symbol):
        payload["status"] = "already_handled"
        return payload

    # Live quote gate + submit
    from order.chase_runner import chase_ask_price
    from order.fubon_orders import place_resolved_order
    from order.fubon_session import connect_fubon
    from order.intent import ResolvedOrder, SCHEMA_VERSION

    try:
        session = connect_fubon(realtime=True)
        gate = live_25m_nonfail(session, symbol)
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "quote_error"
        payload["error"] = str(exc)
        return payload

    payload["nonfail_gate"] = gate
    if gate.get("fail"):
        row = {
            "signal_date": signal_date,
            "entry_date": session_date,
            "symbol": symbol,
            "status": "skipped_fail",
            "gate": gate,
            "at": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
        }
        append_entry(ledger, row)
        save_ledger(cfg.ledger_path, ledger)
        payload["entries"].append(row)
        payload["status"] = "skipped_fail"
        return payload

    try:
        ask = float(chase_ask_price(session, symbol))
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "price_error"
        payload["error"] = str(exc)
        return payload

    # 計算股數：預算制優先
    if cfg.budget_twd is not None:
        qty = int(cfg.budget_twd / ask)
        if qty < 1:
            payload["status"] = "budget_too_low"
            payload["ask"] = ask
            payload["budget_twd"] = cfg.budget_twd
            return payload
        # <1 張走盤中零股；≥1000 必須整股（不可沿用 yaml 的 intraday_odd）
        market = "intraday_odd" if qty < 1000 else "common"
    elif cfg.quantity_shares is not None:
        qty = cfg.quantity_shares
        market = "intraday_odd" if qty < 1000 else (cfg.market_type or "common")
    else:
        payload["status"] = "config_error"
        payload["error"] = "Neither budget_twd nor quantity_shares configured"
        return payload

    resolved = ResolvedOrder(
        symbol=symbol,
        side="buy",
        quantity_shares=qty,
        price=str(ask),
        price_type="limit",  # chase already materialized
        market_type=market,  # type: ignore[arg-type]
        time_in_force="rod",
        order_type="stock",
        user_def=cfg.user_def,
        note=f"songshan_5d_net95_nonfail|{signal_date}",
        source="delta",
    )

    entry: dict[str, Any] = {
        "signal_date": signal_date,
        "entry_date": session_date,
        "symbol": symbol,
        "quantity_shares": qty,
        "ask": ask,
        "budget_twd": cfg.budget_twd,
        "market_type": market,
        "gate": gate,
        "schema_hint": SCHEMA_VERSION,
        "at": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
    }

    if dry or not cfg.auto_submit:
        # dry_run preview must not burn T+1 live slot (already_handled).
        entry["status"] = "dry_run" if dry else "queued_no_submit"
        if not dry:
            append_entry(ledger, entry)
            save_ledger(cfg.ledger_path, ledger)
        payload["entries"].append(entry)
        payload["status"] = entry["status"]
        return payload

    broker = place_resolved_order(session, resolved)
    entry["status"] = "submitted" if broker.get("is_success") else "submit_failed"
    entry["broker"] = broker
    append_entry(ledger, entry)
    save_ledger(cfg.ledger_path, ledger)
    payload["entries"].append(entry)
    payload["status"] = entry["status"]

    if cfg.submit_notify:
        try:
            from notify_email import send_alert

            send_alert(
                f"[股票研究] 松山跟單下單 · {symbol} · {session_date}",
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            )
        except Exception as exc:  # noqa: BLE001
            payload["notify_error"] = str(exc)
    return payload
