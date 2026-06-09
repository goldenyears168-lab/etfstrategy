#!/usr/bin/env python3
"""E0.2 執行評估：統一入口（pre_open / auction / open / intraday）。"""

from __future__ import annotations

import argparse
import io
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from investment_policy import InvestmentPolicy, load_investment_policy
from order_intent_engine import (
    FORCE_REGENERATE_MODES,
    SNAPSHOT_EVAL_MODES,
    list_eval_stock_ids,
    parse_open_prices,
    parse_trade_date,
    run_approve,
    run_generate,
)
from price_adapter import is_price_notice_error, resolve_snapshot_prices
from pre_trade_check import STATUS_BLOCKED, STATUS_DRAFT
from project_config import SCORE_VERSION
from report_summary import print_execution_eval_report
from stock_db import (
    PROJECT_ROOT,
    apply_open_prices_to_intents,
    connect,
    insert_execution_eval_run,
    load_stock_market_map,
)

REPORTS_DIR = PROJECT_ROOT / "reports"

EVALUATION_MODES = (
    "pre_open",
    "auction",
    "open",
    "intraday",
    "preview_close",
)

DEFAULT_PRICE_SOURCE = {
    "pre_open": "last_close",
    "auction": "manual_auction",
    "open": "manual_open",
    "intraday": "manual_last",
    "preview_close": "last_close",
}

MANUAL_PRICE_SOURCE = {
    "auction": "manual_auction",
    "open": "manual_open",
    "intraday": "manual_last",
}

PRICE_SOURCE_CHOICES = ("manual", "finmind", "auto", "yahoo")


def default_price_source_pref() -> str:
    raw = __import__("os").environ.get("EXECUTION_EVAL_PRICE_SOURCE", "manual").strip().lower()
    return raw if raw in PRICE_SOURCE_CHOICES else "manual"


def resolve_evaluation_prices(
    conn,
    *,
    mode: str,
    ips: InvestmentPolicy,
    prices_arg: str,
    price_source_pref: str,
) -> tuple[dict[str, float] | None, str, list[str], int | None]:
    """snapshot 模式：FinMind tick 或 manual；回傳 error_code=2 表示無法繼續。"""
    manual = parse_open_prices(prices_arg) if prices_arg.strip() else {}
    stock_ids = list_eval_stock_ids(conn, ips=ips)

    if price_source_pref == "manual" and not manual:
        return None, MANUAL_PRICE_SOURCE.get(mode, "manual_last"), [], 2

    market_map = load_stock_market_map(conn, stock_ids)
    resolved, fm_label, warnings = resolve_snapshot_prices(
        stock_ids,
        manual=manual,
        source=price_source_pref,
        market_map=market_map,
    )

    missing = [sid for sid in stock_ids if sid not in resolved]
    if missing:
        if price_source_pref == "manual" and not manual:
            return None, MANUAL_PRICE_SOURCE.get(mode, "manual_last"), warnings, 2
        hint = (
            "請提供 --prices 手動價，或 --price-source yahoo（Yahoo 1m；延遲可能 15 分）"
        )
        if hint not in warnings:
            warnings.append(hint)
        return None, fm_label or "finmind_tick", warnings, 2

    if fm_label:
        price_source = fm_label
    elif manual:
        price_source = MANUAL_PRICE_SOURCE.get(mode, "manual_last")
    else:
        price_source = DEFAULT_PRICE_SOURCE.get(mode, "last_close")

    return resolved, price_source, warnings, None


def new_eval_run_id() -> str:
    return datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%dT%H%M%S")


def _report_basename(trade_date: str, mode: str, *, preview: bool) -> str:
    stamp = trade_date.replace("-", "")
    if preview:
        return f"{stamp}_execution_eval_preview"
    if mode == "pre_open":
        return f"{stamp}_execution_eval"
    return f"{stamp}_execution_eval_{mode}"


def _gap_diagnostic_lines(ctx) -> list[str]:
    lines: list[str] = []
    for it in ctx.intents:
        if it.price_snapshot is None and it.open_gap_pct is None:
            continue
        snap = f"{it.price_snapshot:,.0f}" if it.price_snapshot is not None else "—"
        gap = f"{it.open_gap_pct:+.2f}%" if it.open_gap_pct is not None else "—"
        scale = f"{it.size_scale:.2f}" if it.size_scale != 1.0 else "1.00"
        lines.append(
            f"- {it.stock_id} {it.stock_name}  現價 {snap}  限價 {it.ref_price:,.0f}"
            f"  gap {gap}  scale={scale}  qty={it.qty}  status={it.status}"
            + (f"  ({it.block_reason})" if it.block_reason else "")
        )
    return lines


def write_execution_eval_md(
    path: Path,
    *,
    trade_date: str,
    evaluation_mode: str,
    price_source: str,
    eval_run_id: str,
    terminal_excerpt: str,
    intents_md_rel: str,
    gap_lines: list[str] | None = None,
) -> None:
    lines = [
        f"# 執行評估 · {trade_date}",
        "",
        f"- evaluation_mode: `{evaluation_mode}`",
        f"- price_source: `{price_source}`",
        f"- eval_run_id: `{eval_run_id}`",
        f"- score_version: `{SCORE_VERSION}`",
        "",
        f"詳細 intent 表 → [{intents_md_rel}](./{intents_md_rel})",
        "",
    ]
    if gap_lines:
        lines.append("## Gap 診斷")
        lines.append("")
        lines.extend(gap_lines)
        lines.append("")
    lines.extend(
        [
            "## 終端摘要",
            "",
            "```",
            terminal_excerpt.rstrip(),
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_evaluation(
    conn,
    *,
    trade_date: str,
    ips: InvestmentPolicy,
    mode: str,
    persist: bool,
    preview: bool,
    force_regenerate: bool,
    prices: str,
    quiet: bool,
    price_source_pref: str = "manual",
    ingest_ran: bool = False,
) -> int:
    price_warnings: list[str] = []
    if mode in SNAPSHOT_EVAL_MODES:
        price_snapshots, price_source, price_warnings, price_err = resolve_evaluation_prices(
            conn,
            mode=mode,
            ips=ips,
            prices_arg=prices,
            price_source_pref=price_source_pref,
        )
        if price_err == 2:
            if price_source_pref == "manual":
                print(
                    f"模式 `{mode}` 需要 --prices 2330=2310,6223=5810"
                    "（或 --price-source yahoo|finmind|auto）",
                    file=sys.stderr,
                )
            else:
                for w in price_warnings:
                    print(w, file=sys.stderr)
            return 2
    else:
        price_snapshots = None
        price_source = DEFAULT_PRICE_SOURCE.get(mode, "last_close")

    if mode == "intraday" and persist and not ips.allow_intraday_overwrite_approved:
        print(
            "intraday 預設僅預覽；若要寫入 DB 請設 IPS allow_intraday_overwrite_approved: true",
            file=sys.stderr,
        )
        return 2

    eval_run_id = new_eval_run_id()
    is_preview = preview or mode == "preview_close" or (
        mode == "intraday" and not ips.allow_intraday_overwrite_approved
    )
    do_persist = persist and not is_preview

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_execution_eval_report(
            conn,
            trade_date=trade_date,
            evaluation_mode=mode,
            price_source=price_source,
            eval_run_id=eval_run_id,
            persist_intents=False,
            price_snapshots=price_snapshots,
        )
    terminal_text = buf.getvalue()
    if not quiet:
        print(terminal_text, end="")
        for w in price_warnings:
            if is_price_notice_error(w):
                print(f"  ✗ {w}", file=sys.stderr)
            elif mode in SNAPSHOT_EVAL_MODES and w.startswith("Yahoo"):
                continue
            else:
                print(f"  ℹ {w}")

    code, ctx = run_generate(
        conn,
        trade_date=trade_date,
        ips=ips,
        pre_trade=True,
        preview=is_preview,
        quiet=True,
        evaluation_mode=mode,
        price_source=price_source,
        eval_run_id=eval_run_id,
        force_regenerate=force_regenerate,
        persist=do_persist,
        price_snapshots=price_snapshots,
    )
    if ctx is None:
        return code

    stamp = trade_date.replace("-", "")
    intents_suffix = (
        "_order_intents_preview.md" if is_preview else "_order_intents.md"
    )
    intents_rel = f"reports/{stamp}{intents_suffix}"
    gap_lines = _gap_diagnostic_lines(ctx) if mode in SNAPSHOT_EVAL_MODES else None

    if do_persist or is_preview:
        base = _report_basename(trade_date, mode, preview=is_preview)
        md_path = REPORTS_DIR / f"{base}.md"
        write_execution_eval_md(
            md_path,
            trade_date=trade_date,
            evaluation_mode=mode,
            price_source=price_source,
            eval_run_id=eval_run_id,
            terminal_excerpt=terminal_text,
            intents_md_rel=intents_rel,
            gap_lines=gap_lines,
        )
        if do_persist:
            draft_n = sum(1 for i in ctx.intents if i.status == STATUS_DRAFT)
            block_n = sum(1 for i in ctx.intents if i.status == STATUS_BLOCKED)
            insert_execution_eval_run(
                conn,
                eval_run_id=eval_run_id,
                trade_date=trade_date,
                evaluation_mode=mode,
                ingest_ran=ingest_ran,
                summary_json=json.dumps(
                    {"draft": draft_n, "blocked": block_n},
                    ensure_ascii=False,
                ),
                report_path=str(md_path.relative_to(PROJECT_ROOT)),
            )
        if not quiet:
            draft_n = sum(1 for i in ctx.intents if i.status == STATUS_DRAFT)
            block_n = sum(1 for i in ctx.intents if i.status == STATUS_BLOCKED)
            print("")
            print("=== 執行評估寫入 ===")
            print(f"  交易日 {trade_date} · 基準 {ctx.sync.as_of_date or '—'}")
            print(f"  草稿 {draft_n} · 已擋 {block_n}")
            print(f"  主報告 {md_path.relative_to(PROJECT_ROOT)}")
            print(f"  Intents {intents_rel}")
            if gap_lines:
                print("  Gap 診斷")
                for line in gap_lines:
                    print(f"    {line.lstrip('- ')}")

    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E0.2 執行評估（Execution Eval）")
    parser.add_argument(
        "--mode",
        default="pre_open",
        choices=EVALUATION_MODES,
        help="評估模式",
    )
    parser.add_argument("--trade-date", default="today")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="寫入 order_intents + 報告（排程預設）",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="不寫 DB（intraday 預設）",
    )
    parser.add_argument(
        "--force-regenerate",
        action="store_true",
        help=f"覆寫 approved（僅 {sorted(FORCE_REGENERATE_MODES)}）",
    )
    parser.add_argument(
        "--prices",
        default="",
        help="2330=2310,6223=5810（manual；可覆寫 FinMind 價）",
    )
    parser.add_argument(
        "--price-source",
        choices=PRICE_SOURCE_CHOICES,
        default=None,
        help="manual | yahoo | finmind | auto（yahoo=Yahoo 1m；auto 先 FinMind 再 Yahoo）",
    )
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--apply-open", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    trade_date = parse_trade_date(args.trade_date)
    ips = load_investment_policy()
    conn = connect(args.db) if args.db else connect()
    try:
        if args.approve:
            return run_approve(conn, trade_date=trade_date, ips=ips, quiet=args.quiet)
        if args.apply_open:
            if not args.prices:
                print("請提供 --prices 2330=1080,...", file=sys.stderr)
                return 2
            n = apply_open_prices_to_intents(
                conn,
                trade_date=trade_date,
                open_by_stock=parse_open_prices(args.prices),
            )
            if not args.quiet:
                print(f"已更新 {n} 筆開盤執行方式（mode=open）")
            return 0

        persist = args.persist
        if not persist and not args.preview and args.mode == "pre_open":
            persist = __import__("os").environ.get("RUN_ORDER_INTENT", "1") == "1"

        price_source_pref = args.price_source or default_price_source_pref()

        return run_evaluation(
            conn,
            trade_date=trade_date,
            ips=ips,
            mode=args.mode,
            persist=persist,
            preview=args.preview,
            force_regenerate=args.force_regenerate,
            prices=args.prices,
            quiet=args.quiet,
            price_source_pref=price_source_pref,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
