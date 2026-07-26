#!/usr/bin/env python3
"""One-shot · order.yaml + env → ops.sleeve_status.

Examples:
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_sleeve_status.py
  PYTHONPATH=src .venv/bin/python scripts/order/write_ops_sleeve_status.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from project_dotenv import load_project_dotenv  # noqa: E402
from ops_console_sync import supabase_ops_configured, upsert_sleeve_status  # noqa: E402
from order.config import load_order_config  # noqa: E402


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip() not in ("0", "false", "False", "no", "NO")


def _sleeve_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    strategies = cfg.get("strategies") or {}
    rows: list[dict[str, Any]] = []

    def add(
        sleeve_id: str,
        *,
        enabled: bool,
        dry_run: bool,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        rows.append(
            {
                "sleeve_id": sleeve_id,
                "enabled": enabled,
                "dry_run": dry_run,
                "note": note,
                "payload": payload or {},
            }
        )

    # Leading Dip
    ld = strategies.get("leading-dip") or {}
    add(
        "leading-dip",
        enabled=bool(ld.get("enabled", False)) and _env_bool("ORDER_LEADING_DIP_ORDER_ENABLED", True),
        dry_run=_env_bool("ORDER_LEADING_DIP_DRY_RUN", True),
        note=str(ld.get("notes") or "Leading Dip")[:200],
        payload={"strategy_id": ld.get("strategy_id")},
    )
    mid = ld.get("mid") or {}
    if mid:
        add(
            "leading-dip-mid",
            enabled=bool(mid.get("enabled", False)),
            dry_run=_env_bool("ORDER_LEADING_DIP_MID_DRY_RUN", _env_bool("ORDER_LEADING_DIP_DRY_RUN", True)),
            note="Leading Dip mid sleeve",
            payload={"strategy_id": mid.get("strategy_id")},
        )

    # Songshan
    ss = strategies.get("songshan-copytrade") or {}
    add(
        "songshan-copytrade",
        enabled=bool(ss.get("enabled", False)),
        dry_run=_env_bool(
            "ORDER_SONGSHAN_COPYTRADE_DRY_RUN",
            bool(ss.get("dry_run", True)),
        ),
        note=str(ss.get("notes") or "Songshan copytrade")[:200],
    )

    # C18acc
    c18 = strategies.get("rrg-mono-swap-accel") or {}
    add(
        "c18acc",
        enabled=bool(c18.get("enabled", False)) and _env_bool("ORDER_C18ACC_ORDER_ENABLED", True),
        dry_run=_env_bool("ORDER_C18ACC_DRY_RUN", True),
        note=str(c18.get("notes") or "C18acc")[:200],
        payload={"strategy_id": c18.get("strategy_id")},
    )

    # Detach
    detach = cfg.get("detach_gate") or {}
    add(
        "detach-gate",
        enabled=bool(detach.get("enabled", False))
        and _env_bool("ORDER_DETACH_GATE_ORDER_ENABLED", True),
        dry_run=_env_bool("ORDER_DETACH_GATE_DRY_RUN", True),
        note="Detach gate · RED email; ORDER_ENABLED=0 = observe",
    )

    # Expert pool staged gate
    ep = cfg.get("expert_pool_staged_gate") or {}
    add(
        "expert-pool-staged-gate",
        enabled=bool(ep.get("enabled", False)),
        dry_run=_env_bool(
            "ORDER_EP_STAGED_GATE_DRY_RUN",
            bool(ep.get("dry_run", True)),
        ),
        note="Expert pool staged gate",
    )

    # Buy radar observe-only
    add(
        "buy-signal-radar",
        enabled=True,
        dry_run=True,
        note="ABC Order retired · observe/email only",
        payload={"order_enabled": False},
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upsert ops.sleeve_status from order.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    load_project_dotenv()
    cfg = load_order_config()
    rows = _sleeve_rows(cfg)

    if args.json or args.dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0

    if not supabase_ops_configured():
        print("錯誤：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未設定", file=sys.stderr)
        return 2

    for r in rows:
        upsert_sleeve_status(**r)
        print(f"sleeve {r['sleeve_id']}: enabled={r['enabled']} dry_run={r['dry_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
