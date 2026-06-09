#!/usr/bin/env python3
"""E0 Order Intent：pm_watchlist + portfolio_weights → 待核准訂單。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from investment_policy import (
    InvestmentPolicy,
    compute_risk_budget_qty,
    load_investment_policy,
)
from market_labels import PM_ALLOC_BUCKETS
from execution_timeline import layer_heading, next_step_lines
from open_execution_policy import ORDER_PENDING_OPEN, hypothetical_execution_note
from pre_trade_check import (
    IntentDraft,
    PreTradeContext,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_DRAFT,
    apply_pre_trade_checks,
    assess_sync_health,
    load_tsm_adr_pct,
)
from project_config import SCORE_VERSION
from pre_trade_check import is_tech_theme
from rule_limit_price import compute_execution_prices, compute_ref_price
from stock_context import compute_technical
from stock_db import (
    INTENT_VERSION_DEFAULT,
    PROJECT_ROOT,
    apply_open_prices_to_intents,
    approve_order_intents,
    connect,
    count_approved_order_intents,
    demote_approved_order_intents,
    load_latest_pm_watchlist,
    load_latest_portfolio_weights,
    load_execution_tx_gap,
    load_stock_beta_map,
    upsert_order_intents,
)

REPORTS_DIR = PROJECT_ROOT / "reports"

SNAPSHOT_EVAL_MODES = frozenset({"auction", "open", "intraday"})

SNAPSHOT_PRICE_LABEL = {
    "auction": "試撮",
    "open": "開盤",
    "intraday": "現價",
}


def parse_trade_date(arg: str | None) -> str:
    if not arg or arg.lower() in {"today", "now"}:
        return date.today().isoformat()
    return arg


def _compute_size_scale(
    *,
    open_gap_pct: float | None,
    ips: InvestmentPolicy,
    tsm_adr_pct: float | None,
    stock_id: str,
) -> float:
    scale = 1.0
    if open_gap_pct is not None and abs(open_gap_pct) > ips.max_open_gap_pct:
        scale *= ips.gap_size_multiplier
    if (
        tsm_adr_pct is not None
        and tsm_adr_pct <= ips.tsm_adr_block_new_tech_pct
        and ips.adr_weak_size_scale != 1.0
        and is_tech_theme(stock_id)
    ):
        scale *= ips.adr_weak_size_scale
    return round(scale, 4)


def build_intent_drafts(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ips: InvestmentPolicy,
    score_version: str = SCORE_VERSION,
    intent_version: str = INTENT_VERSION_DEFAULT,
    evaluation_mode: str = "pre_open",
    price_snapshots: dict[str, float] | None = None,
) -> list[IntentDraft]:
    pm_rows = load_latest_pm_watchlist(conn, score_version=score_version)
    pw_rows = load_latest_portfolio_weights(conn, score_version=score_version)
    if not pm_rows:
        return []
    as_of = pm_rows[0]["as_of_date"]
    pw_by_id = {r["stock_id"]: r for r in pw_rows if r["as_of_date"] == as_of}
    beta_map, _ = load_stock_beta_map(conn)
    tx_gap_pct, _tx_gap_src = load_execution_tx_gap(conn, trade_date=trade_date)
    tsm_adr_pct = load_tsm_adr_pct(conn, trade_date=trade_date)
    drafts: list[IntentDraft] = []
    use_snapshot = evaluation_mode in SNAPSHOT_EVAL_MODES
    snapshots = price_snapshots or {}

    for pm in pm_rows:
        if pm["as_of_date"] != as_of:
            continue
        bucket = pm["pm_bucket"]
        if bucket not in PM_ALLOC_BUCKETS:
            continue
        pw = pw_by_id.get(pm["stock_id"])
        if pw is None:
            continue
        suggested = float(pw["suggested_ntd"] or 0)
        if suggested <= 0:
            continue
        entry_signal = pm["entry_signal"]
        tech = compute_technical(conn, pm["stock_id"])
        beta_row = beta_map.get(pm["stock_id"])
        beta = (
            float(beta_row["beta"])
            if beta_row is not None and beta_row["beta"] is not None
            else None
        )
        stock_id = pm["stock_id"]
        snapshot_price: float | None = None
        open_gap_pct: float | None = None
        size_scale = 1.0
        status = STATUS_DRAFT
        block_reason = ""

        if use_snapshot:
            snap = snapshots.get(stock_id)
            if snap is None:
                drafts.append(
                    IntentDraft(
                        trade_date=trade_date,
                        as_of_date=as_of,
                        stock_id=stock_id,
                        stock_name=pm["stock_name"] or "",
                        side="BUY",
                        ref_price=0.0,
                        limit_price=0.0,
                        qty=0,
                        suggested_ntd=suggested,
                        pm_bucket=bucket,
                        entry_signal=entry_signal,
                        entry_tags_json=pm["entry_tags_json"] or "[]",
                        benchmark_type="",
                        benchmark_price=0.0,
                        stop_price=None,
                        target_price=None,
                        score_version=score_version,
                        investment_score=float(pm["investment_score"]),
                        chip_tag=pm["chip_tag"] or "",
                        status=STATUS_BLOCKED,
                        block_reason="缺少 snapshot 價",
                    )
                )
                continue
            snapshot_price = float(snap)
            if tech is None or tech.close is None or tech.close <= 0:
                continue
            open_gap_pct = round((snapshot_price / float(tech.close) - 1.0) * 100.0, 2)
            if (
                ips.gap_block_new_entry_pct > 0
                and open_gap_pct >= ips.gap_block_new_entry_pct
            ):
                status = STATUS_BLOCKED
                block_reason = f"gap {open_gap_pct:+.2f}% >= {ips.gap_block_new_entry_pct}%"
            ref = compute_execution_prices(
                entry_signal=entry_signal,
                pm_bucket=bucket,
                tech=tech,
                ips=ips,
                snapshot_price=snapshot_price,
                investment_score=float(pm["investment_score"]),
                beta=beta,
                tx_gap_pct=tx_gap_pct,
                tsm_adr_pct=tsm_adr_pct,
            )
        else:
            ref = compute_ref_price(
                entry_signal=entry_signal,
                pm_bucket=bucket,
                tech=tech,
                ips=ips,
                investment_score=float(pm["investment_score"]),
                beta=beta,
                tx_gap_pct=tx_gap_pct,
                tsm_adr_pct=tsm_adr_pct,
            )

        if ref.ref_price is None:
            if ref.skip_reason:
                drafts.append(
                    IntentDraft(
                        trade_date=trade_date,
                        as_of_date=as_of,
                        stock_id=stock_id,
                        stock_name=pm["stock_name"] or "",
                        side="BUY",
                        ref_price=0.0,
                        limit_price=0.0,
                        qty=0,
                        suggested_ntd=suggested,
                        pm_bucket=bucket,
                        entry_signal=entry_signal,
                        entry_tags_json=pm["entry_tags_json"] or "[]",
                        benchmark_type=ref.benchmark_type or "",
                        benchmark_price=float(ref.benchmark_price or 0.0),
                        stop_price=ref.stop_price,
                        target_price=ref.target_price,
                        score_version=score_version,
                        investment_score=float(pm["investment_score"]),
                        chip_tag=pm["chip_tag"] or "",
                        price_snapshot=snapshot_price,
                        open_gap_pct=open_gap_pct,
                        status=STATUS_BLOCKED,
                        block_reason=ref.skip_reason,
                    )
                )
            continue

        if status != STATUS_BLOCKED:
            size_scale = _compute_size_scale(
                open_gap_pct=open_gap_pct,
                ips=ips,
                tsm_adr_pct=tsm_adr_pct,
                stock_id=stock_id,
            )
        qty = 0
        if status != STATUS_BLOCKED:
            qty = compute_risk_budget_qty(
                suggested_ntd=suggested,
                ref_price=ref.ref_price,
                stop_price=ref.stop_price,
                size_scale=size_scale,
                ips=ips,
            )
        snap_json = ""
        snap_payload: dict = {}
        if snapshot_price is not None:
            snap_payload = {
                "snapshot": snapshot_price,
                "open_gap_pct": open_gap_pct,
                "size_scale": size_scale,
                "db_prev_close": float(tech.close) if tech and tech.close else None,
            }
        if status != STATUS_BLOCKED and ref.ref_price and ref.stop_price:
            per_share_risk = ref.ref_price - ref.stop_price
            risk_ntd = ips.capital_ntd * ips.risk_budget_pct_per_trade / 100.0
            snap_payload.update(
                {
                    "sizing_mode": ips.sizing_mode,
                    "qty_cap": int(suggested * size_scale // ref.ref_price),
                    "qty_risk": int(risk_ntd // per_share_risk) if per_share_risk > 0 else 0,
                    "per_share_risk": round(per_share_risk, 4),
                    "risk_ntd": round(risk_ntd, 2),
                }
            )
        if snap_payload:
            snap_json = json.dumps(snap_payload, ensure_ascii=False)
        drafts.append(
            IntentDraft(
                trade_date=trade_date,
                as_of_date=as_of,
                stock_id=stock_id,
                stock_name=pm["stock_name"] or "",
                side="BUY",
                ref_price=ref.ref_price,
                limit_price=ref.ref_price,
                qty=qty,
                suggested_ntd=suggested,
                pm_bucket=bucket,
                entry_signal=entry_signal,
                entry_tags_json=pm["entry_tags_json"] or "[]",
                benchmark_type=ref.benchmark_type or "",
                benchmark_price=float(ref.benchmark_price or ref.ref_price),
                stop_price=ref.stop_price,
                structural_stop_price=ref.structural_stop_price,
                target_price=ref.target_price,
                score_version=score_version,
                investment_score=float(pm["investment_score"]),
                chip_tag=pm["chip_tag"] or "",
                discount_pct=ref.discount_pct,
                pricing_note=ref.pricing_note or "",
                price_snapshot=snapshot_price,
                open_gap_pct=open_gap_pct,
                size_scale=size_scale,
                price_snapshot_json=snap_json,
                status=status,
                block_reason=block_reason,
            )
        )
    drafts.sort(key=lambda d: (-d.investment_score, d.stock_id))
    if ips.max_daily_positions > 0:
        drafts = drafts[: ips.max_daily_positions]
    return drafts


def list_eval_stock_ids(
    conn: sqlite3.Connection,
    *,
    ips: InvestmentPolicy,
    score_version: str = SCORE_VERSION,
) -> list[str]:
    """今日執行評估標的（與 build_intent_drafts 篩選一致，供 FinMind 拉價）。"""
    pm_rows = load_latest_pm_watchlist(conn, score_version=score_version)
    pw_rows = load_latest_portfolio_weights(conn, score_version=score_version)
    if not pm_rows:
        return []
    as_of = pm_rows[0]["as_of_date"]
    pw_by_id = {r["stock_id"]: r for r in pw_rows if r["as_of_date"] == as_of}
    candidates: list[tuple[float, str]] = []
    for pm in pm_rows:
        if pm["as_of_date"] != as_of:
            continue
        if pm["pm_bucket"] not in PM_ALLOC_BUCKETS:
            continue
        pw = pw_by_id.get(pm["stock_id"])
        if pw is None or float(pw["suggested_ntd"] or 0) <= 0:
            continue
        candidates.append((float(pm["investment_score"]), pm["stock_id"]))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    limit = ips.max_daily_positions if ips.max_daily_positions > 0 else len(candidates)
    return [sid for _, sid in candidates[:limit]]


FORCE_REGENERATE_MODES = frozenset({"pre_open", "auction"})


def drafts_to_db_rows(
    intents: list[IntentDraft],
    *,
    ips: InvestmentPolicy,
    intent_version: str = INTENT_VERSION_DEFAULT,
    evaluation_mode: str | None = None,
    price_source: str | None = None,
    eval_run_id: str | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for it in intents:
        rows.append(
            {
                "stock_id": it.stock_id,
                "trade_date": it.trade_date,
                "intent_version": intent_version,
                "as_of_date": it.as_of_date,
                "score_version": it.score_version,
                "stock_name": it.stock_name,
                "side": it.side,
                "ref_price": it.ref_price,
                "limit_price": it.limit_price,
                "qty": it.qty,
                "suggested_ntd": it.suggested_ntd,
                "pm_bucket": it.pm_bucket,
                "entry_signal": it.entry_signal,
                "entry_tags_json": it.entry_tags_json,
                "benchmark_type": it.benchmark_type,
                "benchmark_price": it.benchmark_price,
                "stop_price": it.stop_price,
                "target_price": it.target_price,
                "order_type_planned": it.order_type_planned or ORDER_PENDING_OPEN,
                "open_price": it.open_price,
                "order_type_effective": it.order_type_effective,
                "status": it.status,
                "block_reason": it.block_reason or "",
                "ips_version": ips.version,
                "chip_tag": it.chip_tag,
                "investment_score": it.investment_score,
                "evaluation_mode": evaluation_mode,
                "price_source": price_source,
                "eval_run_id": eval_run_id,
                "price_snapshot": it.price_snapshot,
                "price_snapshot_json": it.price_snapshot_json or None,
                "size_scale": it.size_scale,
            }
        )
    return rows


def build_execution_context(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ips: InvestmentPolicy | None = None,
    evaluation_mode: str = "pre_open",
    price_snapshots: dict[str, float] | None = None,
) -> PreTradeContext:
    ips = ips or load_investment_policy()
    sync = assess_sync_health(conn, trade_date=trade_date, ips=ips)
    drafts = build_intent_drafts(
        conn,
        trade_date=trade_date,
        ips=ips,
        evaluation_mode=evaluation_mode,
        price_snapshots=price_snapshots,
    )
    tsm = load_tsm_adr_pct(conn, trade_date=trade_date)
    return apply_pre_trade_checks(drafts, ips=ips, sync=sync, tsm_adr_pct=tsm)


def build_morning_execution_context(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ips: InvestmentPolicy | None = None,
) -> PreTradeContext:
    return build_execution_context(conn, trade_date=trade_date, ips=ips)


def _split_intents(
    intents: list[IntentDraft],
) -> tuple[list[IntentDraft], list[IntentDraft]]:
    recommend = [i for i in intents if i.status == STATUS_DRAFT]
    skip = [i for i in intents if i.status == STATUS_BLOCKED]
    return recommend, skip


def _format_limit_price_line(it: IntentDraft, *, evaluation_mode: str = "pre_open") -> str:
    base = f"{it.stock_id} {it.stock_name}"
    if evaluation_mode in SNAPSHOT_EVAL_MODES and it.price_snapshot is not None:
        snap_label = SNAPSHOT_PRICE_LABEL.get(evaluation_mode, "快照")
        parts = [
            f"{base}  {snap_label} {it.price_snapshot:,.0f}",
            f"限價 {it.ref_price:,.0f}",
        ]
        if it.ref_price > 0:
            vs_limit = (it.price_snapshot / it.ref_price - 1.0) * 100.0
            parts.append(f"距限價 {vs_limit:+.2f}%")
        if it.open_gap_pct is not None:
            parts.append(f"gap昨收 {it.open_gap_pct:+.2f}%")
        return "  ·  ".join(parts)
    return f"{base}  限價 {it.ref_price:,.0f}"


def _format_recommend_reference_lines(
    it: IntentDraft,
    *,
    ips: InvestmentPolicy,
    evaluation_mode: str = "pre_open",
) -> list[str]:
    """建議掛單價以外的張數、金額與定價理由（參考用）。"""
    est = it.qty * it.ref_price
    scale_s = f" · 縮倉×{it.size_scale:.2f}" if it.size_scale != 1.0 else ""
    lines = [
        (
            f"{it.stock_id}  {it.qty} 張 · 約 {est:,.0f} 元 · "
            f"{it.entry_signal} · 分 {it.investment_score:.1f}{scale_s}"
        ),
    ]
    if it.discount_pct is not None:
        lines.append(f"      折讓 {it.discount_pct:.2f}%（昨收 anchor）")
    if it.pricing_note:
        lines.append(f"      {it.pricing_note}")
    if it.structural_stop_price is not None:
        lines.append(
            f"      結構停損 {it.structural_stop_price:,.0f}"
            f"（僅風控參考，未抬高限價）"
        )
    if it.stop_price is not None:
        lines.append(
            f"      執行停損 {it.stop_price:,.0f}（限價下方 · 成交後參考）"
        )
    if evaluation_mode in SNAPSHOT_EVAL_MODES and it.price_snapshot is not None:
        snap_label = SNAPSHOT_PRICE_LABEL.get(evaluation_mode, "快照")
        lines.append(
            f"      成交判斷 "
            f"{hypothetical_execution_note(it.ref_price, assumed_open=it.price_snapshot, ips=ips)}"
            f"（以{snap_label}代入）"
        )
    else:
        lines.append(f"      開盤 {hypothetical_execution_note(it.ref_price, ips=ips)}")
    return lines


def _format_recommend_line(it: IntentDraft) -> str:
    """Checklist 用：單行摘要（含掛單價）。"""
    est = it.qty * it.ref_price
    scale_s = f" · 縮倉×{it.size_scale:.2f}" if it.size_scale != 1.0 else ""
    return (
        f"{it.stock_id} {it.stock_name}  "
        f"限價 {it.ref_price:,.0f} 元 · {it.qty} 張 · 約 {est:,.0f} 元"
        f"{scale_s} · {it.entry_signal} · 分 {it.investment_score:.1f}"
    )


def _format_skip_line(it: IntentDraft) -> str:
    reason = it.block_reason or "風控"
    return f"{it.stock_id} {it.stock_name}  不掛單 · {reason}"


def morning_execution_checklist_lines(
    conn: sqlite3.Connection,
    *,
    trade_date: str | None = None,
) -> list[str]:
    trade_date = parse_trade_date(trade_date)
    ctx = build_morning_execution_context(conn, trade_date=trade_date)
    lines: list[str] = []
    recommend, skip = _split_intents(ctx.intents)
    for it in sorted(recommend, key=lambda x: (-x.investment_score, x.stock_id)):
        lines.append(f"✓ {_format_recommend_line(it)}")
    for it in sorted(skip, key=lambda x: (-x.investment_score, x.stock_id)):
        lines.append(f"✗ {_format_skip_line(it)}")
    return lines


def print_morning_execution_summary(
    conn: sqlite3.Connection,
    *,
    trade_date: str | None = None,
    persist: bool = False,
    evaluation_mode: str = "pre_open",
    price_snapshots: dict[str, float] | None = None,
) -> None:
    """執行層終端：建議掛單價；persist 時寫入 order_intents 表與 reports。"""
    trade_date = parse_trade_date(trade_date)
    ips = load_investment_policy()
    ctx = build_execution_context(
        conn,
        trade_date=trade_date,
        ips=ips,
        evaluation_mode=evaluation_mode,
        price_snapshots=price_snapshots,
    )

    print("")
    print(f"=== 建議掛單價 · {layer_heading(evaluation_mode)} ===")
    if not ctx.intents:
        print("  （無建議配置標的或未算出參考價）")
        return

    recommend, skip = _split_intents(ctx.intents)
    sorted_recommend = sorted(
        recommend, key=lambda x: (-x.investment_score, x.stock_id)
    )
    if sorted_recommend:
        if evaluation_mode in SNAPSHOT_EVAL_MODES:
            print("")
            print("  代號 名稱  ·  現價  ·  限價  ·  距限價  ·  gap昨收")
        print("")
        for it in sorted_recommend:
            print(f"  {_format_limit_price_line(it, evaluation_mode=evaluation_mode)}")
    else:
        print("  （今日無建議掛單價）")

    has_reference = (
        sorted_recommend
        or skip
        or not ctx.sync.ok
        or ctx.sync.as_of_date
        or ctx.tsm_adr_pct is not None
    )
    if has_reference:
        print("")
        print("  --- 參考 ---")
        if not ctx.sync.ok:
            print(f"  同步  ⚠ {ctx.sync.message}")
        elif ctx.sync.as_of_date:
            meta = f"基準日 {ctx.sync.as_of_date} · IPS {ips.version}"
            if ctx.tsm_adr_pct is not None:
                meta += f" · TSM ADR {ctx.tsm_adr_pct:+.2f}%"
            print(f"  {meta}")
        elif ctx.tsm_adr_pct is not None:
            print(f"  TSM ADR {ctx.tsm_adr_pct:+.2f}%")
        for it in sorted_recommend:
            for line in _format_recommend_reference_lines(
                it, ips=ips, evaluation_mode=evaluation_mode
            ):
                print(f"  {line}")
        if skip:
            print(f"  風控略過（{len(skip)} 筆 · 不掛單）")
            for it in sorted(skip, key=lambda x: (-x.investment_score, x.stock_id)):
                print(f"  ✗ {_format_skip_line(it)}")

    print("")
    if sorted_recommend:
        total_ntd = sum(it.qty * it.ref_price for it in sorted_recommend)
        print(f"  結論  建議掛單價 {len(sorted_recommend)} 筆 · 合計約 {total_ntd:,.0f} 元")
    else:
        print("  結論  今日無掛單建議（請見風控略過）")
    for line in next_step_lines(evaluation_mode, trade_date=trade_date):
        print(f"  {line}")

    if persist:
        intent_version = INTENT_VERSION_DEFAULT
        md = format_report_md(
            ctx, ips=ips, trade_date=trade_date, intent_version=intent_version
        )
        stamp = trade_date.replace("-", "")
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        md_path = REPORTS_DIR / f"{stamp}_order_intents.md"
        md_path.write_text(md, encoding="utf-8")
        rows = drafts_to_db_rows(ctx.intents, ips=ips, intent_version=intent_version)
        upsert_order_intents(conn, rows)
        json_path = REPORTS_DIR / f"{stamp}_order_intents.json"
        json_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  報告  {md_path.relative_to(PROJECT_ROOT)}")


def format_report_md(
    ctx,
    *,
    ips: InvestmentPolicy,
    trade_date: str,
    intent_version: str,
    preview: bool = False,
) -> str:
    lines = [
        f"# Order Intents · {trade_date}"
        + ("（預覽）" if preview else ""),
        "",
        f"- intent_version: `{intent_version}` · IPS: `{ips.version}`",
        f"- 同步：{'OK' if ctx.sync.ok else 'WARN'} — {ctx.sync.message}",
    ]
    if ctx.tsm_adr_pct is not None:
        lines.append(f"- TSM ADR：{ctx.tsm_adr_pct:+.2f}%")
    lines.append("")

    groups = [
        ("可核准（draft）", [i for i in ctx.intents if i.status == STATUS_DRAFT]),
        ("已擋（blocked）", [i for i in ctx.intents if i.status == STATUS_BLOCKED]),
        (
            "已核准（approved）",
            [i for i in ctx.intents if i.status == STATUS_APPROVED],
        ),
    ]
    for title, items in groups:
        lines.append(f"## {title}（{len(items)}）")
        if not items:
            lines.append("")
            lines.append("（無）")
            lines.append("")
            continue
        lines.append("")
        lines.append(
            "| 代號 | 名稱 | 參考價 | 張數 | 金額 | gap% | scale | 型態 | 基準 | 開盤策略 | 原因 |"
        )
        lines.append(
            "|------|------|--------|------|------|------|-------|------|------|----------|------|"
        )
        for it in items:
            bench = f"{it.benchmark_type} {it.benchmark_price:.2f}"
            open_note = (
                f"{it.order_type_effective}"
                if it.order_type_effective
                else hypothetical_execution_note(it.ref_price, ips=ips)
            )
            reason = it.block_reason or "—"
            gap_s = f"{it.open_gap_pct:+.2f}" if it.open_gap_pct is not None else "—"
            scale_s = f"{it.size_scale:.2f}" if it.size_scale != 1.0 else "1.0"
            lines.append(
                f"| {it.stock_id} | {it.stock_name} | {it.ref_price:.2f} | "
                f"{it.qty} | {it.suggested_ntd:,.0f} | {gap_s} | {scale_s} | "
                f"{it.entry_signal} | {bench} | {open_note} | {reason} |"
            )
        lines.append("")

    lines.append("## 開盤執行（§21.6）")
    lines.append("")
    lines.append("- `ref_price >= open` → **market_rod**")
    lines.append("- `ref_price < open` → **limit_rod** @ ref_price")
    lines.append("")
    return "\n".join(lines)


def run_generate(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ips: InvestmentPolicy,
    pre_trade: bool,
    preview: bool = False,
    quiet: bool = False,
    evaluation_mode: str = "pre_open",
    price_source: str = "last_close",
    eval_run_id: str | None = None,
    force_regenerate: bool = False,
    persist: bool = True,
    price_snapshots: dict[str, float] | None = None,
) -> tuple[int, PreTradeContext | None]:
    intent_version = INTENT_VERSION_DEFAULT
    write_db = persist and not preview

    if write_db:
        approved_n = count_approved_order_intents(
            conn, trade_date=trade_date, intent_version=intent_version
        )
        if approved_n > 0 and not force_regenerate:
            if not quiet:
                print(
                    f"拒絕覆寫：已有 {approved_n} 筆 approved。"
                    " 請用 --preview 或 --force-regenerate（需重新 --approve）",
                    file=__import__("sys").stderr,
                )
            return 2, None
        if approved_n > 0 and force_regenerate:
            if evaluation_mode not in FORCE_REGENERATE_MODES:
                if not quiet:
                    print(
                        f"--force-regenerate 僅允許模式 {sorted(FORCE_REGENERATE_MODES)}",
                        file=__import__("sys").stderr,
                    )
                return 2, None
            demote_approved_order_intents(
                conn, trade_date=trade_date, intent_version=intent_version
            )
            if not quiet:
                print(
                    f"已將 {approved_n} 筆 approved 降為 draft（eval_run 重算）",
                    file=__import__("sys").stderr,
                )

    if pre_trade:
        ctx = build_execution_context(
            conn,
            trade_date=trade_date,
            ips=ips,
            evaluation_mode=evaluation_mode,
            price_snapshots=price_snapshots,
        )
    else:
        sync = assess_sync_health(conn, trade_date=trade_date, ips=ips)
        drafts = build_intent_drafts(
            conn,
            trade_date=trade_date,
            ips=ips,
            evaluation_mode=evaluation_mode,
            price_snapshots=price_snapshots,
        )
        tsm = load_tsm_adr_pct(conn, trade_date=trade_date)
        ctx = PreTradeContext(
            sync=sync,
            global_block=not sync.ok,
            global_message=sync.message if not sync.ok else "",
            tsm_adr_pct=tsm,
            intents=drafts,
        )

    md = format_report_md(
        ctx, ips=ips, trade_date=trade_date, intent_version=intent_version, preview=preview
    )
    stamp = trade_date.replace("-", "")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_order_intents_preview.md" if preview else "_order_intents.md"
    md_path = REPORTS_DIR / f"{stamp}{suffix}"
    md_path.write_text(md, encoding="utf-8")

    if write_db:
        rows = drafts_to_db_rows(
            ctx.intents,
            ips=ips,
            intent_version=intent_version,
            evaluation_mode=evaluation_mode,
            price_source=price_source,
            eval_run_id=eval_run_id,
        )
        upsert_order_intents(conn, rows)
        json_path = REPORTS_DIR / f"{stamp}_order_intents.json"
        json_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        json_path = REPORTS_DIR / f"{stamp}_order_intents_preview.json"
        json_path.write_text(
            json.dumps(
                drafts_to_db_rows(
                    ctx.intents,
                    ips=ips,
                    intent_version=intent_version,
                    evaluation_mode=evaluation_mode,
                    price_source=price_source,
                    eval_run_id=eval_run_id,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if not quiet:
        print("")
        print("=== E0 Order Intents ===")
        print(f"  交易日 {trade_date} · 基準 {ctx.sync.as_of_date or '—'}")
        print(f"  同步 {ctx.sync.message}")
        draft_n = sum(1 for i in ctx.intents if i.status == STATUS_DRAFT)
        block_n = sum(1 for i in ctx.intents if i.status == STATUS_BLOCKED)
        print(f"  草稿 {draft_n} · 已擋 {block_n}")
        print(f"  報告 {md_path.relative_to(PROJECT_ROOT)}")
        if write_db:
            print(f"  JSON {json_path.relative_to(PROJECT_ROOT)}")
    return (0 if not ctx.global_block else 1), ctx


def run_approve(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    ips: InvestmentPolicy,
    quiet: bool = False,
) -> int:
    sync = assess_sync_health(conn, trade_date=trade_date, ips=ips)
    if not sync.ok:
        if not quiet:
            print(f"核准失敗：{sync.message}")
        return 1
    n = approve_order_intents(conn, trade_date=trade_date)
    if not quiet:
        print(f"已核准 {n} 筆 order_intents（{trade_date}）")
    if n == 0 and not quiet:
        print("  無可核准草稿（可能皆已 blocked）")
    return 0


def parse_open_prices(arg: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in arg.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        sid, px = part.split("=", 1)
        out[sid.strip()] = float(px.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E0 Order Intent 引擎")
    parser.add_argument("--trade-date", default="today")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--pre-trade", action="store_true")
    parser.add_argument("--preview", action="store_true", help="收盤預覽，不寫 approved")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument(
        "--apply-open",
        action="store_true",
        help="寫入開盤價並決定 market_rod / limit_rod",
    )
    parser.add_argument(
        "--open-price",
        default="",
        help="2330=1080,2454=1200 開盤價對照",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    trade_date = parse_trade_date(args.trade_date)
    ips = load_investment_policy()
    conn = connect(args.db) if args.db else connect()
    try:
        if args.generate or args.preview:
            code, _ = run_generate(
                conn,
                trade_date=trade_date,
                ips=ips,
                pre_trade=args.pre_trade or args.preview,
                preview=args.preview,
                quiet=args.quiet,
            )
            return code
        if args.approve:
            return run_approve(conn, trade_date=trade_date, ips=ips, quiet=args.quiet)
        if args.apply_open:
            if not args.open_price:
                print("請提供 --open-price 2330=1080,...", file=__import__("sys").stderr)
                return 2
            prices = parse_open_prices(args.open_price)
            n = apply_open_prices_to_intents(
                conn, trade_date=trade_date, open_by_stock=prices
            )
            if not args.quiet:
                print(f"已更新 {n} 筆開盤執行方式")
            return 0
        parser.print_help()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
