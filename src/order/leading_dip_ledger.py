"""Leading Dip local ledger · open positions + day-entry accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

LEDGER_PATH = PROJECT_ROOT / "data" / "order" / "leading_dip_ledger.json"
MID_LEDGER_PATH = PROJECT_ROOT / "data" / "order" / "leading_dip_mid_ledger.json"
SCHEMA = "leading-dip-order-v1"
MID_SCHEMA = "leading-dip-mid-order-v1"
STRATEGY_ID = "leading-dip"
MID_STRATEGY_ID = "leading-dip-mid"

OPEN_STATUSES = frozenset({"open", "exit_pending", "working", "submitted", "partial", "filled"})
CLOSED_STATUSES = frozenset({"closed", "failed", "cancelled", "skipped"})


@dataclass
class LeadingDipLedger:
    schema: str = SCHEMA
    strategy_id: str = STRATEGY_ID
    positions: list[dict[str, Any]] = field(default_factory=list)

    def open_positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.positions:
            if not isinstance(p, dict):
                continue
            st = str(p.get("status") or "")
            if st in CLOSED_STATUSES:
                continue
            if st in OPEN_STATUSES or st == "":
                out.append(p)
        return out

    def open_symbols(self) -> set[str]:
        return {str(p.get("symbol") or "").strip() for p in self.open_positions() if p.get("symbol")}

    def entries_on_day(self, session_date: str) -> list[dict[str, Any]]:
        day = str(session_date)
        return [
            p
            for p in self.positions
            if isinstance(p, dict)
            and str(p.get("entry_date") or "") == day
            and str(p.get("side") or "buy").lower() == "buy"
            and str(p.get("status") or "") not in {"failed", "cancelled", "skipped"}
        ]


def load_ledger(path: Path | None = None) -> LeadingDipLedger:
    p = path or LEDGER_PATH
    if not p.is_file():
        return LeadingDipLedger()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LeadingDipLedger()
    if not isinstance(raw, dict):
        return LeadingDipLedger()
    positions = raw.get("positions")
    if not isinstance(positions, list):
        positions = []
    return LeadingDipLedger(
        schema=str(raw.get("schema") or SCHEMA),
        strategy_id=str(raw.get("strategy_id") or STRATEGY_ID),
        positions=[e for e in positions if isinstance(e, dict)],
    )


def save_ledger(ledger: LeadingDipLedger, path: Path | None = None) -> None:
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": ledger.schema,
        "strategy_id": ledger.strategy_id,
        "positions": ledger.positions,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_position(
    ledger: LeadingDipLedger, entry: dict[str, Any], *, path: Path | None = None
) -> dict[str, Any]:
    ledger.positions.append(entry)
    save_ledger(ledger, path=path)
    return entry


def update_position(
    ledger: LeadingDipLedger,
    *,
    client_intent_id: str,
    fields: dict[str, Any],
    path: Path | None = None,
) -> dict[str, Any] | None:
    cid = str(client_intent_id)
    for p in ledger.positions:
        if str(p.get("client_intent_id") or "") == cid:
            p.update(fields)
            save_ledger(ledger, path=path)
            return p
    return None
