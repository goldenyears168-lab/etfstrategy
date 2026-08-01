"""C18acc pyramid-add ledger · links second units to slot parent_cid."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_db import DATA_DIR

LEDGER_PATH = DATA_DIR / "order" / "c18acc_pyramid_ledger.json"
LEDGER_SCHEMA = "c18acc-pyramid-ledger-v1"


@dataclass
class C18accPyramidLedger:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def has_parent(self, pyramid_parent_cid: str) -> bool:
        cid = str(pyramid_parent_cid).strip()
        return any(str(e.get("pyramid_parent_cid") or "") == cid for e in self.entries)


def load_c18acc_pyramid_ledger(path: Path | None = None) -> C18accPyramidLedger:
    p = path or LEDGER_PATH
    if not p.is_file():
        return C18accPyramidLedger()
    raw = json.loads(p.read_text(encoding="utf-8"))
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return C18accPyramidLedger()
    return C18accPyramidLedger(entries=[e for e in entries if isinstance(e, dict)])


def save_c18acc_pyramid_ledger(ledger: C18accPyramidLedger, path: Path | None = None) -> None:
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": LEDGER_SCHEMA,
        "entries": ledger.entries,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_pyramid_entry(
    *,
    pyramid_parent_cid: str,
    symbol: str,
    session_date: str,
    poll_minute: str,
    slot: int,
    status: str,
    dry_run: bool,
    note: str = "",
    ledger: C18accPyramidLedger | None = None,
) -> None:
    book = ledger or load_c18acc_pyramid_ledger()
    if book.has_parent(pyramid_parent_cid):
        return
    book.entries.append(
        {
            "pyramid_parent_cid": pyramid_parent_cid,
            "symbol": symbol,
            "slot": slot,
            "session_date": session_date,
            "poll_minute": poll_minute,
            "status": status,
            "dry_run": dry_run,
            "note": note,
        }
    )
    save_c18acc_pyramid_ledger(book)
