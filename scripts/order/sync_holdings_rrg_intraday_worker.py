#!/usr/bin/env python3
"""持倉 RRG 盤中同步 worker · 須以主環境 .venv 執行（pandas）。

由 order.holdings_pulse.sync_intraday_rrg 以 subprocess 呼叫，避免富邦 .venv-fubon
缺 pandas 導致盤中 RRG 寫入失敗。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.holdings_pulse import sync_intraday_rrg  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="持倉 RRG 盤中同步（主 venv worker）")
    parser.add_argument("--session", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--poll-minute", required=True)
    parser.add_argument("--json", type=Path, help="holdings_pulse JSON（取 stock_id 清單）")
    parser.add_argument("--holdings", nargs="*", help="stock_id 清單（覆蓋 --json）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    holding_ids: list[str] = list(args.holdings or [])
    if not holding_ids and args.json and args.json.is_file():
        payload = json.loads(args.json.read_text(encoding="utf-8"))
        holding_ids = [
            str(h.get("stock_id") or "").strip()
            for h in (payload.get("holdings") or [])
            if str(h.get("stock_id") or "").strip()
        ]

    os.environ["HOLDINGS_RRG_SYNC_WORKER"] = "1"
    conn = connect(args.db)
    try:
        n, priced_n, kbar_n, err = sync_intraday_rrg(
            conn,
            session_date=args.session,
            poll_minute=args.poll_minute,
            holding_ids=holding_ids or None,
        )
    finally:
        conn.close()

    if err:
        print(f"⚠ RRG 盤中同步：{err}", file=sys.stderr)
        return 1
    print(f"RRG 盤中同步 OK · rows={n} · priced={priced_n} · kbar={kbar_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
