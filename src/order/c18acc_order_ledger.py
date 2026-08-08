"""C18acc order ledger · submit idempotency + swap buy retry queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from order.json_state import atomic_write_json, load_json_or_default
from stock_db import DATA_DIR

LEDGER_PATH = DATA_DIR / "order" / "c18acc_order_ledger.json"
LEDGER_SCHEMA = "c18acc-order-ledger-v1"


@dataclass
class C18accOrderLedger:
    entries: list[dict[str, Any]] = field(default_factory=list)
    pending_buys: list[dict[str, Any]] = field(default_factory=list)

    def find_by_client_intent_id(self, client_intent_id: str) -> dict[str, Any] | None:
        cid = str(client_intent_id).strip()
        hits = [e for e in self.entries if str(e.get("client_intent_id") or "") == cid]
        if not hits:
            return None
        return max(hits, key=lambda e: str(e.get("submitted_at") or ""))


def load_c18acc_order_ledger(path: Path | None = None) -> C18accOrderLedger:
    p = path or LEDGER_PATH
    raw = load_json_or_default(p, {})
    if not isinstance(raw, dict):
        return C18accOrderLedger()
    entries = raw.get("entries")
    pending = raw.get("pending_buys")
    return C18accOrderLedger(
        entries=[e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else [],
        pending_buys=[e for e in pending if isinstance(e, dict)] if isinstance(pending, list) else [],
    )


def save_c18acc_order_ledger(ledger: C18accOrderLedger, path: Path | None = None) -> None:
    p = path or LEDGER_PATH
    payload = {
        "schema": LEDGER_SCHEMA,
        "entries": ledger.entries,
        "pending_buys": ledger.pending_buys,
    }
    atomic_write_json(p, payload, indent=2, trailing_newline=True)
