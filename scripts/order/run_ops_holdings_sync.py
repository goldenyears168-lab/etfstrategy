#!/usr/bin/env python3
"""Fubon holdings snapshot → ops.holdings（需 .venv-fubon + FUBON_* · 建議 mini）.

Examples:
  .venv-fubon/bin/python scripts/order/run_ops_holdings_sync.py
  .venv-fubon/bin/python scripts/order/run_ops_holdings_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv  # noqa: E402
from ops_console_sync import insert_holdings, supabase_ops_configured  # noqa: E402


def _payload_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    from order.fubon_account import _held_qty

    by_symbol: dict[str, int] = {}
    for h in snap.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        sym = str(h.get("stock_no") or "")
        if not sym:
            continue
        qty = int(_held_qty(h))
        if qty > 0:
            by_symbol[sym] = qty
    return {
        "schema": "ops-holdings-v1",
        "account": snap.get("account"),
        "cash": snap.get("cash"),
        "holdings": snap.get("holdings") or [],
        "unrealized_pnl": snap.get("unrealized_pnl") or [],
        "by_symbol": by_symbol,
        "n_symbols": len(by_symbol),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Fubon holdings → ops.holdings")
    parser.add_argument("--dry-run", action="store_true", help="Print payload only")
    parser.add_argument("--account-label", default="fubon")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    load_project_dotenv()

    try:
        from order.fubon_session import account_label, check_python_version, connect_fubon
        from order.fubon_account import account_snapshot
    except ImportError as exc:
        print(
            f"錯誤：無法載入 Fubon 模組（請用 .venv-fubon）：{exc}",
            file=sys.stderr,
        )
        return 2

    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    try:
        session = connect_fubon(realtime=False)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(
            f"錯誤：Fubon 登入失敗（Book 常缺憑證；請在 mini 設 FUBON_*）：{exc}",
            file=sys.stderr,
        )
        return 1

    snap = account_snapshot(session)
    label = args.account_label or account_label(session.primary)
    payload = _payload_from_snapshot(snap)

    if args.json or args.dry_run:
        print(json.dumps({"account_label": label, "payload": payload}, ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0

    if not supabase_ops_configured():
        print("錯誤：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定", file=sys.stderr)
        return 2

    insert_holdings(payload, account_label=label)
    print(f"ops.holdings ok · {label} · n={payload.get('n_symbols')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
