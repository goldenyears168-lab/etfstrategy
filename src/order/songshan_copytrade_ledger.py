"""Songshan copytrade ledger · overnight signal → T+1 entry idempotency."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"entries": [], "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": [], "updated_at": None}


def save_ledger(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
        "submit_failed",
    }
)


def already_handled(ledger: dict[str, Any], *, signal_date: str, symbol: str) -> bool:
    for e in ledger.get("entries") or []:
        if str(e.get("signal_date")) == signal_date and str(e.get("symbol")) == symbol:
            if str(e.get("status") or "") in _HANDLED_STATUSES:
                return True
    return False


def append_entry(ledger: dict[str, Any], row: dict[str, Any]) -> None:
    entries = list(ledger.get("entries") or [])
    entries.append(row)
    ledger["entries"] = entries
