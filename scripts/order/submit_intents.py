#!/usr/bin/env python3
"""讀取策略層 OrderIntent JSON → 解析 → 富邦下單 / 查委託。

策略層只需產出 JSON（schema: order-intent-v1），勿 import order。

範例：
  # 預覽（不送單）
  .venv-fubon/bin/python scripts/order/submit_intents.py \\
    reports/order/intents/example.json --dry-run

  # 實際送單（需明確 --submit）
  .venv-fubon/bin/python scripts/order/submit_intents.py \\
    reports/order/intents/my_strategy_20260621.json --submit

  # 查今日委託
  .venv-fubon/bin/python scripts/order/submit_intents.py --query-orders
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.fubon_session import check_python_version, connect_fubon  # noqa: E402
from order.intent import load_intent_batch  # noqa: E402


def _reports_dir() -> Path:
    from order.config import load_order_config

    cfg = load_order_config()
    block = cfg.get("reports") if isinstance(cfg.get("reports"), dict) else {}
    rel = str(block.get("root") or "reports/order")
    return ROOT / rel


def _write_json(name: str, payload: object) -> Path:
    out_dir = _reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{stamp}_{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description="Submit strategy OrderIntent batch to Fubon")
    parser.add_argument("intent_file", nargs="?", help="order-intent-v1 JSON 路徑")
    parser.add_argument("--dry-run", action="store_true", help="解析意圖並預覽，不送單（預設）")
    parser.add_argument("--submit", action="store_true", help="實際送單（需明確指定）")
    parser.add_argument("--query-orders", action="store_true", help="查詢今日委託狀態")
    args = parser.parse_args()

    if args.query_orders:
        try:
            session = connect_fubon()
            from order.fubon_orders import order_results

            rows = order_results(session)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"錯誤：{exc}", file=sys.stderr)
            return 1
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        out = _write_json("order_results", rows)
        print(f"已寫入 {out}", file=sys.stderr)
        return 0

    if not args.intent_file:
        parser.error("請提供 intent_file，或使用 --query-orders")

    intent_path = Path(args.intent_file)
    if not intent_path.is_file():
        print(f"錯誤：找不到 {intent_path}", file=sys.stderr)
        return 1

    try:
        batch = load_intent_batch(intent_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"錯誤：無法讀取 intent — {exc}", file=sys.stderr)
        return 1

    submit = bool(args.submit)
    if not submit and not args.dry_run:
        args.dry_run = True

    try:
        session = connect_fubon()
        from order.fubon_orders import place_batch, resolved_orders_preview

        preview = resolved_orders_preview(session, batch)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    payload = {
        "intent_file": str(intent_path),
        "strategy_id": batch.strategy_id,
        "as_of": batch.as_of,
        "mode": "submit" if submit else "dry_run",
        "resolved": preview,
    }

    if not preview:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("無需送單（目標庫存已達或 delta=0）", file=sys.stderr)
        _write_json("intent_preview", payload)
        return 0

    if args.dry_run and not submit:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        out = _write_json("intent_preview", payload)
        print(f"dry-run 完成 · 已寫入 {out}", file=sys.stderr)
        return 0

    try:
        result = place_batch(session, batch)
    except RuntimeError as exc:
        print(f"錯誤：送單失敗 — {exc}", file=sys.stderr)
        return 1

    payload["submit_result"] = result
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    out = _write_json("intent_submit", payload)
    print(f"送單完成 · 已寫入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
