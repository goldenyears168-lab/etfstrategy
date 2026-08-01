"""Broker vs local ledger reconcile · report only（不自動改帳本）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_db import DATA_DIR, PROJECT_ROOT

_TZ = ZoneInfo("Asia/Taipei")
REPORT_DIR = PROJECT_ROOT / "reports" / "order" / "reconcile"
SCHEMA = "order-broker-reconcile-v1"


def _symbols_from_ledger(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entries = raw.get("entries") if isinstance(raw, dict) else []
    out: set[str] = set()
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        lc = str(e.get("lifecycle_status") or e.get("status") or "")
        if lc in {"failed", "cancelled"}:
            continue
        side = str(e.get("side") or "buy").lower()
        # Approximate open exposure: filled/partial/working buys without paired sell tracking.
        if side == "sell":
            continue
        if lc in {"filled", "partial", "working", "submitted", "ambiguous", "reconciled"} or not lc:
            sym = str(e.get("symbol") or "").strip()
            if sym:
                out.add(sym)
    return out


def _ledger_buy_fills(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if str(e.get("side") or "buy").lower() == "sell":
            continue
        lc = str(e.get("lifecycle_status") or e.get("status") or "")
        if lc in {"failed", "cancelled"}:
            continue
        out.append(e)
    return out


def _symbols_from_leading_dip_ledger(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    positions = raw.get("positions") if isinstance(raw, dict) else []
    out: set[str] = set()
    if not isinstance(positions, list):
        return out
    closed = {"closed", "failed", "cancelled", "skipped"}
    for p in positions:
        if not isinstance(p, dict):
            continue
        if str(p.get("status") or "") in closed:
            continue
        if str(p.get("side") or "buy").lower() == "sell":
            continue
        sym = str(p.get("symbol") or "").strip()
        if sym:
            out.add(sym)
    return out


def _c18_slots() -> list[dict[str, Any]]:
    path = DATA_DIR / "rrg_c18acc_slots.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    slots = raw.get("slots") if isinstance(raw, dict) else []
    return [s for s in slots if isinstance(s, dict) and str(s.get("stock_id") or "").strip()]


def format_reconcile_plain(report: dict[str, Any]) -> str:
    lines = [
        f"# 富邦庫存 vs slots／ledger 對帳 · {report.get('as_of', '')}",
        "",
        "## 富邦實際持倉",
    ]
    holdings = report.get("broker_holdings") or {}
    if not holdings:
        lines.append("- （無持股或讀取失敗）")
    else:
        for sym, qty in sorted(holdings.items()):
            lines.append(f"- {sym} · {qty} 股")
    lines.extend(["", "## C18acc 槽位（rrg_c18acc_slots）"])
    slots = report.get("c18_slots") or []
    if not slots:
        lines.append("- （空倉）")
    else:
        for s in slots:
            lines.append(
                f"- slot{s.get('slot')}: {s.get('stock_id')} {s.get('stock_name') or ''} "
                f"· entry {s.get('entry_date')} @ {s.get('entry_px')}"
            )
    lines.extend(["", "## Ledger 買進列（近似未平）"])
    lines.append(f"- ABC：{', '.join(report.get('local_abc_symbols') or []) or '—'}")
    lines.append(f"- C18：{', '.join(report.get('local_c18_symbols') or []) or '—'}")
    lines.extend(["", "## 差異"])
    only_b = report.get("only_in_broker") or []
    only_l = report.get("only_in_local_ledger") or []
    only_slot = report.get("only_in_slots_not_broker") or []
    broker_not_slot = report.get("in_broker_not_in_slots") or []
    if report.get("ok") and not only_slot and not broker_not_slot:
        lines.append("- ✓ 代號集合大致一致（仍請人工核股數）")
    else:
        if only_b:
            lines.append(f"- 只在富邦、ledger 沒記買進：{', '.join(only_b)}")
        if only_l:
            lines.append(f"- 只在 ledger、富邦沒有：{', '.join(only_l)}")
        if only_slot:
            lines.append(f"- 在 C18 槽位、富邦沒有：{', '.join(only_slot)}")
        if broker_not_slot:
            lines.append(
                f"- 在富邦、但不在 C18 槽位（可能是 ABC／手動／舊倉）：{', '.join(broker_not_slot)}"
            )
    lines.extend(["", "## 說明"])
    for n in report.get("notes") or []:
        lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def build_reconcile_report(session: Any) -> dict[str, Any]:
    from order.fubon_orders import holdings_shares_by_symbol

    holdings = holdings_shares_by_symbol(session)
    broker_holdings = {
        str(k).strip(): int(v or 0)
        for k, v in holdings.items()
        if int(v or 0) > 0 and str(k).strip()
    }
    broker_syms = set(broker_holdings)
    # ABC Order removed · former ABC fills are manual holdings (not local sleeve).
    c18_path = DATA_DIR / "order" / "c18acc_order_ledger.json"
    dip_path = DATA_DIR / "order" / "leading_dip_ledger.json"
    abc: set[str] = set()
    c18 = _symbols_from_ledger(c18_path)
    dip = _symbols_from_leading_dip_ledger(dip_path)
    local = c18 | dip
    slots = _c18_slots()
    slot_syms = {str(s.get("stock_id") or "").strip() for s in slots if str(s.get("stock_id") or "").strip()}
    only_broker = sorted(broker_syms - local)
    only_local = sorted(local - broker_syms)
    only_slot = sorted(slot_syms - broker_syms)
    broker_not_slot = sorted(broker_syms - slot_syms)
    return {
        "schema": SCHEMA,
        "as_of": datetime.now(tz=_TZ).isoformat(timespec="seconds"),
        "broker_holdings": broker_holdings,
        "broker_symbols": sorted(broker_syms),
        "local_abc_symbols": sorted(abc),
        "local_c18_symbols": sorted(c18),
        "local_leading_dip_symbols": sorted(dip),
        "c18_slots": [
            {
                "slot": s.get("slot"),
                "stock_id": s.get("stock_id"),
                "stock_name": s.get("stock_name"),
                "entry_date": s.get("entry_date"),
                "entry_px": s.get("entry_px"),
            }
            for s in slots
        ],
        "c18_slot_symbols": sorted(slot_syms),
        "only_in_broker": only_broker,
        "only_in_local_ledger": only_local,
        "only_in_slots_not_broker": only_slot,
        "in_broker_not_in_slots": broker_not_slot,
        "abc_buy_fills": [],
        "c18_buy_fills": [
            {
                "symbol": e.get("symbol"),
                "filled_qty": e.get("filled_qty"),
                "avg_fill_price": e.get("avg_fill_price"),
                "lifecycle_status": e.get("lifecycle_status") or e.get("status"),
                "action_kind": e.get("action_kind"),
                "client_intent_id": e.get("client_intent_id"),
            }
            for e in _ledger_buy_fills(c18_path)
        ],
        "ok": not only_broker and not only_local and not only_slot,
        "notes": [
            "Report-only · does not patch ledgers.",
            "ABC removed from Order · former fills treated as manual holdings.",
            "Leading Dip ledger isolated at data/order/leading_dip_ledger.json.",
            "C18 槽位只記策略三槽；他袖套／手動倉會出現在「富邦有、槽位無」。",
        ],
    }


def write_reconcile_report(session: Any, *, out_dir: Path | None = None) -> Path:
    report = build_reconcile_report(session)
    dest = out_dir or REPORT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=_TZ).strftime("%Y%m%d_%H%M%S")
    path = dest / f"{stamp}_broker_reconcile.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (dest / "latest_broker_reconcile.json").write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    md_path = dest / f"{stamp}_broker_reconcile.md"
    md_path.write_text(format_reconcile_plain(report), encoding="utf-8")
    (dest / "latest_broker_reconcile.md").write_text(
        md_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return path
