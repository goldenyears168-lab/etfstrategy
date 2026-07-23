"""Cross-sleeve symbol exclusion · prevent Leading Dip vs C18acc/ABC collisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

C18_SLOTS_PATH = PROJECT_ROOT / "data" / "rrg_c18acc_slots.json"
ABC_LEDGER_PATH = PROJECT_ROOT / "data" / "order" / "abc_v3_f1_ledger.json"


def c18acc_slot_symbols(path: Path | None = None) -> set[str]:
    p = path or C18_SLOTS_PATH
    if not p.is_file():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    slots = raw.get("slots") if isinstance(raw, dict) else []
    out: set[str] = set()
    if not isinstance(slots, list):
        return out
    for s in slots:
        if not isinstance(s, dict):
            continue
        sym = str(s.get("stock_id") or s.get("symbol") or "").strip()
        if sym:
            out.add(sym)
    return out


def abc_ledger_open_symbols(path: Path | None = None) -> set[str]:
    """Optional legacy ABC ledger reader · live exclude defaults off (manual holdings)."""
    p = path or ABC_LEDGER_PATH
    if not p.is_file():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entries = raw.get("entries") if isinstance(raw, dict) else []
    out: set[str] = set()
    if not isinstance(entries, list):
        return out
    closed = {"failed", "cancelled", "closed"}
    for e in entries:
        if not isinstance(e, dict):
            continue
        if str(e.get("side") or "buy").lower() == "sell":
            continue
        lc = str(e.get("lifecycle_status") or e.get("status") or "")
        if lc in closed:
            continue
        sym = str(e.get("symbol") or "").strip()
        if sym:
            out.add(sym)
    return out


def blocked_symbols_for_leading_dip(
    *,
    exclude_c18acc: bool = True,
    exclude_abc: bool = True,
    also_exclude: set[str] | None = None,
) -> dict[str, Any]:
    """Return union of foreign sleeve symbols + breakdown for logs."""
    c18 = c18acc_slot_symbols() if exclude_c18acc else set()
    abc = abc_ledger_open_symbols() if exclude_abc else set()
    extra = set(also_exclude or ())
    blocked = set(c18) | set(abc) | extra
    return {
        "blocked": sorted(blocked),
        "c18acc_slots": sorted(c18),
        "abc_ledger": sorted(abc),
        "extra": sorted(extra),
    }
