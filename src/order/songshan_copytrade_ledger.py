"""Songshan copytrade ledger · overnight signal → T+1 entry idempotency."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from order.json_state import atomic_write_json, load_json_or_default


def load_ledger(path: Path) -> dict[str, Any]:
    return load_json_or_default(path, {"entries": [], "updated_at": None})


def save_ledger(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(path, state, indent=2, trailing_newline=True)


# Terminal statuses burn the (signal_date, symbol) T+1 slot.
# Include submit_failed so broker rejects (e.g. 全額處置) do not retry every poll.
_HANDLED_STATUSES = frozenset(
    {
        "submitted",
        "filled",
        "working",
        "dry_run",
        "skipped_fail",
        "skipped_duplicate",
        "skipped_already_bought",
        "submit_failed",
    }
)

# Sleeve already owns / is buying this symbol → block another entry
# (unless allow_pyramid; Songshan pyramid rules not adopted yet).
_BOUGHT_STATUSES = frozenset(
    {
        "submitted",
        "filled",
        "working",
    }
)


def already_handled(ledger: dict[str, Any], *, signal_date: str, symbol: str) -> bool:
    for e in ledger.get("entries") or []:
        if str(e.get("signal_date")) == signal_date and str(e.get("symbol")) == symbol:
            if str(e.get("status") or "") in _HANDLED_STATUSES:
                return True
    return False


def symbol_already_bought(ledger: dict[str, Any], *, symbol: str) -> bool:
    """True if this sleeve already submitted/holds ``symbol`` (any signal_date)."""
    for e in ledger.get("entries") or []:
        if str(e.get("symbol")) != symbol:
            continue
        if str(e.get("status") or "") in _BOUGHT_STATUSES:
            return True
    return False


def append_entry(ledger: dict[str, Any], row: dict[str, Any]) -> None:
    entries = list(ledger.get("entries") or [])
    entries.append(row)
    ledger["entries"] = entries
