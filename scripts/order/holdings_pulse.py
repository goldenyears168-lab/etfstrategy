#!/usr/bin/env python3
"""持倉即時脈動 · 富邦 snapshot + 現價 + RRG 收盤 vs 盤中 + exit playbook。

  .venv-fubon/bin/python scripts/order/holdings_pulse.py
  .venv-fubon/bin/python scripts/order/holdings_pulse.py --sync-rrg-intraday --write
  .venv-fubon/bin/python scripts/order/holdings_pulse.py --date 2026-06-26 --no-fubon
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from order.fubon_session import check_python_version  # noqa: E402
from order.holdings_pulse import (  # noqa: E402
    build_holdings_pulse,
    default_report_path,
    format_holdings_pulse,
    format_holdings_pulse_digest,
    write_holdings_pulse,
    write_holdings_pulse_json,
)
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402


def main() -> int:
    try:
        check_python_version()
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    load_project_dotenv()

    parser = argparse.ArgumentParser(description="持倉即時脈動 · 富邦 + RRG + exit playbook")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Session date（預設今日台北）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--no-fubon", action="store_true", help="略過富邦 API")
    parser.add_argument(
        "--sync-rrg-intraday",
        action="store_true",
        help="拉 FinMind tick 並寫入 rrg_universe_scores（screen_kind=intraday）",
    )
    parser.add_argument(
        "--no-futures",
        action="store_true",
        help="不刷新 morning_risk（僅讀 DB）",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="寫入 reports/order/snapshots/holdings_pulse_YYYYMMDD.md",
    )
    parser.add_argument(
        "--digest-only",
        action="store_true",
        help="僅輸出精簡 digest 段落（供盤中寄信合併）",
    )
    parser.add_argument(
        "--symbols",
        metavar="IDS",
        help="監控清單模式：逗號分隔代號（略過富邦持倉；可搭配 --require-rrg）",
    )
    parser.add_argument(
        "--symbols-file",
        type=Path,
        help="監控清單模式：檔案內逗號／空白／換行分隔代號",
    )
    parser.add_argument(
        "--require-rrg",
        action="store_true",
        help="監控清單模式：略過沒有收盤 RRG 素材的代號",
    )
    parser.add_argument("-o", "--output", type=Path, help="自訂輸出路徑（需搭配 --write）")
    args = parser.parse_args()

    watch_symbols: list[str] = []
    if args.symbols:
        watch_symbols.extend(part.strip() for part in str(args.symbols).replace("\n", ",").split(","))
    if args.symbols_file:
        text = Path(args.symbols_file).read_text(encoding="utf-8")
        for token in text.replace(",", " ").replace("\n", " ").split():
            watch_symbols.append(token.strip())
    watch_symbols = [s for s in watch_symbols if s]

    conn = connect(args.db)
    try:
        pulse = build_holdings_pulse(
            conn,
            session_date=args.date,
            use_fubon=not args.no_fubon,
            sync_rrg_intraday=args.sync_rrg_intraday,
            refresh_morning_risk=not args.no_futures,
            symbols=watch_symbols or None,
            require_rrg_close=args.require_rrg,
        )
    finally:
        conn.close()

    if watch_symbols and args.require_rrg:
        kept = {r.base.stock_id for r in pulse.holdings}
        skipped = [s for s in watch_symbols if s not in kept]
        # preserve input order unique
        seen: set[str] = set()
        skipped_u: list[str] = []
        for s in skipped:
            if s in seen:
                continue
            seen.add(s)
            skipped_u.append(s)
        print(
            f"監控清單：輸入 {len(set(watch_symbols))} 檔 · "
            f"有 RRG 採納 {len(pulse.holdings)} 檔 · "
            f"跳過 {len(skipped_u)} 檔"
        )
        if skipped_u:
            print(f"跳過（無收盤 RRG）：{', '.join(skipped_u)}")
        print("")

    text = (
        format_holdings_pulse_digest(pulse)
        if args.digest_only
        else format_holdings_pulse(pulse)
    )
    print(text)

    if watch_symbols and args.write and not args.output:
        stamp = (args.date or pulse.session_date).replace("-", "")
        args.output = ROOT / "reports" / "order" / "snapshots" / f"holdings_pulse_watchlist_{stamp}.md"

    out_path = write_holdings_pulse(pulse, args.output) if args.write else default_report_path(pulse.session_date)
    if args.write and not args.digest_only:
        print(f"\n已寫入：{out_path}")
        json_override = None
        if watch_symbols:
            stamp = pulse.session_date.replace("-", "")
            json_override = ROOT / "reports" / "order" / "snapshots" / f"holdings_pulse_watchlist_{stamp}.json"
        json_path = write_holdings_pulse_json(pulse, json_override)
        print(f"已寫入持倉 JSON：{json_path}")

    exit_code = 1 if pulse.fubon_error and not args.no_fubon and not watch_symbols else 0
    if not pulse.holdings and not args.no_fubon and not watch_symbols:
        exit_code = max(exit_code, 1)
    if watch_symbols and not pulse.holdings:
        exit_code = max(exit_code, 1)

    if args.date and args.date != datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat():
        print(f"\n報告路徑（未寫入）：{out_path}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
