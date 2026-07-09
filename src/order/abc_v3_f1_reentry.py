"""ABC v3+f1 re-entry policy · 對齊 event hold 3d · 取代簡單 skip_if_held。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from order.abc_v3_f1_config import AbcV3F1OrderConfig
from order.abc_v3_f1_lifecycle import entry_blocks_retry, entry_notional
from stock_db import DEFAULT_DB_PATH, PROJECT_ROOT, connect

LEDGER_PATH = PROJECT_ROOT / "data" / "order" / "abc_v3_f1_ledger.json"


def trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    """Count trading sessions with start < d <= end (exclusive start, inclusive end)."""
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


def load_tw_trading_dates(conn: sqlite3.Connection | None = None) -> list[str]:
    from market_benchmark import load_benchmark_close

    own = conn is None
    if own:
        conn = connect(DEFAULT_DB_PATH)
    try:
        bench = load_benchmark_close(conn)
        return bench.dropna().index.astype(str).tolist()
    finally:
        if own:
            conn.close()


@dataclass
class AbcV3F1Ledger:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def entries_on(self, session_date: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if str(e.get("session_date") or "") == session_date]

    def daily_count(self, session_date: str) -> int:
        return len(self.countable_entries_on(session_date))

    def countable_entries_on(self, session_date: str) -> list[dict[str, Any]]:
        return [e for e in self.entries_on(session_date) if entry_blocks_retry(e)]

    def find_by_client_intent_id(self, client_intent_id: str) -> dict[str, Any] | None:
        cid = str(client_intent_id).strip()
        hits = [e for e in self.entries if str(e.get("client_intent_id") or "") == cid]
        if not hits:
            return None
        return max(hits, key=lambda e: str(e.get("submitted_at") or ""))

    def last_entry_for(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol).strip()
        hits = [
            e
            for e in self.entries
            if str(e.get("symbol") or "") == sym and entry_blocks_retry(e)
        ]
        if not hits:
            return None
        return max(hits, key=lambda e: str(e.get("session_date") or ""))

    def symbol_entries_in_window(
        self, symbol: str, *, session_date: str, trading_dates: list[str], lookback_td: int
    ) -> list[dict[str, Any]]:
        sym = str(symbol).strip()
        if session_date not in trading_dates:
            idx = len(trading_dates)
        else:
            idx = trading_dates.index(session_date)
        start_idx = max(0, idx - lookback_td + 1)
        window = set(trading_dates[start_idx : idx + 1])
        return [
            e
            for e in self.entries
            if str(e.get("symbol") or "") == sym and str(e.get("session_date") or "") in window
        ]

    def abc_notional_for_symbol(
        self,
        symbol: str,
        *,
        session_date: str,
        trading_dates: list[str],
        lookback_td: int,
        notional_basis: str = "submitted",
    ) -> float:
        """Sum notional for symbol within rolling lookback window (aligns with entry cap)."""
        sym = str(symbol).strip()
        basis = "filled" if notional_basis == "filled" else "submitted"
        total = 0.0
        for e in self.symbol_entries_in_window(
            sym,
            session_date=session_date,
            trading_dates=trading_dates,
            lookback_td=lookback_td,
        ):
            if not entry_blocks_retry(e):
                continue
            total += entry_notional(e, basis=basis)
        return total


def load_ledger() -> AbcV3F1Ledger:
    if not LEDGER_PATH.is_file():
        return AbcV3F1Ledger()
    try:
        raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AbcV3F1Ledger()
    entries = raw.get("entries") if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        entries = []
    return AbcV3F1Ledger(entries=[e for e in entries if isinstance(e, dict)])


def save_ledger(ledger: AbcV3F1Ledger) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": ledger.entries[-500:]}
    LEDGER_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evaluate_reentry(
    *,
    symbol: str,
    session_date: str,
    ask_price: float,
    cfg: AbcV3F1OrderConfig,
    ledger: AbcV3F1Ledger,
    trading_dates: list[str],
    available_cash: int | None,
) -> tuple[bool, str]:
    """Return (allowed, reason). reason is skip code or allow tag."""
    if not cfg.order_enabled:
        return False, "ABC_V3_F1_ORDER_ENABLED=0"

    if ledger.daily_count(session_date) >= cfg.max_entries_per_day:
        return False, f"max_entries_per_day={cfg.max_entries_per_day}"

    sym = str(symbol).strip()
    if any(
        str(e.get("symbol") or "") == sym and entry_blocks_retry(e)
        for e in ledger.entries_on(session_date)
    ):
        return False, f"same_day_duplicate:{sym}"

    if available_cash is not None and available_cash < cfg.budget_twd:
        return False, f"insufficient_cash:{available_cash}<{cfg.budget_twd}"

    projected_notional = (
        ledger.abc_notional_for_symbol(
            sym,
            session_date=session_date,
            trading_dates=trading_dates,
            lookback_td=cfg.rolling_lookback_trading_days,
            notional_basis=cfg.notional_basis,
        )
        + cfg.budget_twd
    )
    if projected_notional > cfg.max_notional_per_symbol_twd:
        return False, f"max_notional_per_symbol:{int(projected_notional)}>{cfg.max_notional_per_symbol_twd}"

    rolling = ledger.symbol_entries_in_window(
        sym,
        session_date=session_date,
        trading_dates=trading_dates,
        lookback_td=cfg.rolling_lookback_trading_days,
    )
    if len(rolling) >= cfg.max_entries_per_symbol_rolling:
        return False, f"max_entries_per_symbol_{cfg.rolling_lookback_trading_days}td={cfg.max_entries_per_symbol_rolling}"

    last = ledger.last_entry_for(sym)
    if last is None:
        return True, "first_entry"

    last_date = str(last.get("session_date") or "")
    last_ask = float(last.get("ask_price") or 0.0)
    td_gap = trading_days_between(trading_dates, last_date, session_date)

    if td_gap <= 0:
        return False, f"same_day_duplicate:{sym}"

    if td_gap > cfg.hold_days:
        return True, f"new_event_after_hold:td_gap={td_gap}>{cfg.hold_days}"

    # Within hold window: addon only if cheaper + min spacing
    if td_gap < cfg.reentry_min_trading_days:
        return False, f"hold_window_cooldown:td_gap={td_gap}<{cfg.reentry_min_trading_days}"

    if last_ask <= 0 or ask_price <= 0:
        return False, "hold_window_no_price_ref"

    discount_needed = cfg.reentry_discount_pct / 100.0
    threshold = last_ask * (1.0 - discount_needed)
    if ask_price <= threshold:
        return True, f"addon_discount:ask={ask_price:.2f}<={threshold:.2f}({cfg.reentry_discount_pct}%)"

    return False, (
        f"hold_window_no_discount:td_gap={td_gap}<={cfg.hold_days} · "
        f"ask={ask_price:.2f}>{threshold:.2f}"
    )
