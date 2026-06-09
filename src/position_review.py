#!/usr/bin/env python3
"""持倉檢視（Position Review）：持倉 × 研究池訊號 → 賣出雷達草稿。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from holdings_research import build_cross_etf_consensus, build_etf_holdings_changes_block
from market_labels import ENTRY_OVEREXTENDED, ENTRY_SKIP, PM_AVOID
from project_config import ETF_CODES_HOLDINGS
from score_engine import SCORE_VERSION
from signal_engine import build_signal_layers_block
from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    load_latest_pm_watchlist,
    load_portfolio_books,
    load_portfolio_positions,
)

REPORTS_DIR = PROJECT_ROOT / "reports"

ACTION_HOLD = "持有續抱"
ACTION_TRIM = "減碼觀察"
ACTION_EXIT = "出清觀察"
ACTION_CONTRADICTION = "矛盾提示"
ACTION_OUT_OF_POOL = "不在 ETF 研究池 · 不評估"
ACTION_ETF_TRIM = "ETF減碼觀察"
ACTION_ETF_HOLD = "ETF持倉對照"

ETF_TRIM_MIN_REDUCES = 3
SELL_RADAR_ACTIONS = frozenset({ACTION_TRIM, ACTION_EXIT})


@dataclass(frozen=True)
class PositionReviewRow:
    book_id: str
    symbol: str
    asset_type: str
    stock_name: str | None
    holding: bool
    action: str
    reason_codes: tuple[str, ...]
    detail: str
    in_research_pool: bool
    pm_bucket: str | None = None
    entry_signal: str | None = None
    etf_flow: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "symbol": self.symbol,
            "asset_type": self.asset_type,
            "stock_name": self.stock_name,
            "holding": self.holding,
            "action": self.action,
            "reason_codes": list(self.reason_codes),
            "detail": self.detail,
            "in_research_pool": self.in_research_pool,
            "pm_bucket": self.pm_bucket,
            "entry_signal": self.entry_signal,
            "etf_flow": self.etf_flow,
        }


def research_pool_ids(conn: sqlite3.Connection) -> set[str]:
    """當日 investment_scores 有分數的 stock_id（A2-b）。"""
    try:
        row = conn.execute(
            """
            SELECT MAX(as_of_date) AS d
            FROM investment_scores
            WHERE score_version = ?
            """,
            (SCORE_VERSION,),
        ).fetchone()
    except sqlite3.OperationalError:
        return set()
    if row is None or row["d"] is None:
        return set()
    rows = conn.execute(
            """
            SELECT stock_id FROM investment_scores
            WHERE as_of_date = ? AND score_version = ?
            """,
            (row["d"], SCORE_VERSION),
        ).fetchall()
    return {str(r["stock_id"]) for r in rows}


def _pm_map(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
    return {str(r["stock_id"]): r for r in rows}


def _signal_stock_map(conn: sqlite3.Connection, etf_codes: tuple[str, ...]) -> dict[str, dict]:
    block = build_signal_layers_block(conn, etf_codes)
    if not block:
        return {}
    return {str(s["stock_id"]): s for s in block.get("stocks") or []}


def _consensus_map(conn: sqlite3.Connection, etf_codes: tuple[str, ...]) -> dict[str, Any]:
    return {s.stock_id: s for s in build_cross_etf_consensus(conn, etf_codes)}


def _etf_flow_label(sig: dict | None, cons: Any) -> str | None:
    if cons and getattr(cons, "etf_add", 0) >= 2:
        return "強"
    if sig:
        side = sig.get("net_side")
        l2 = sig.get("l2_consensus_level")
        if side == "add" and l2 in ("STRONG", "SINGLE"):
            return "偏強"
        if side == "reduce":
            return "減碼"
    if cons and getattr(cons, "etf_reduce", 0) >= 1:
        return "減碼"
    return None


def _etf_strength(sig: dict | None, cons: Any) -> bool:
    if cons and int(getattr(cons, "etf_add", 0) or 0) >= 2:
        return True
    return bool(
        sig
        and sig.get("net_side") == "add"
        and sig.get("l2_consensus_level") == "STRONG"
    )


def review_stock_position(
    *,
    book_id: str,
    symbol: str,
    stock_name: str | None,
    in_pool: bool,
    pm: sqlite3.Row | None,
    sig: dict | None,
    cons: Any,
) -> PositionReviewRow:
    etf_flow = _etf_flow_label(sig, cons)

    if not in_pool:
        return PositionReviewRow(
            book_id=book_id,
            symbol=symbol,
            asset_type="stock",
            stock_name=stock_name,
            holding=True,
            action=ACTION_OUT_OF_POOL,
            reason_codes=(),
            detail="自選持倉，不納入 ETF 研究",
            in_research_pool=False,
            etf_flow=etf_flow,
        )

    reason_codes: list[str] = []
    bucket = ""
    entry = ""
    name = stock_name

    if pm is not None:
        bucket = pm["pm_bucket"] or ""
        entry = pm["entry_signal"] or ""
        name = name or pm["stock_name"]
        if entry == ENTRY_SKIP:
            reason_codes.append("ENTRY_SKIP")
        if entry == ENTRY_OVEREXTENDED:
            reason_codes.append("ENTRY_OVEREXTENDED")
        if bucket == PM_AVOID:
            reason_codes.append("PM_AVOID")
    if sig and sig.get("net_side") == "reduce":
        reason_codes.append("ETF_NET_REDUCE")
    if sig and sig.get("l2_consensus_level") == "FALSE" and sig.get("net_side") == "add":
        reason_codes.append("FALSE_CONSENSUS")

    action = ACTION_HOLD
    if "ENTRY_SKIP" in reason_codes:
        action = ACTION_EXIT
    elif bucket == PM_AVOID and "ENTRY_OVEREXTENDED" not in reason_codes:
        action = ACTION_EXIT
    elif bucket == PM_AVOID or "ENTRY_OVEREXTENDED" in reason_codes or "ETF_NET_REDUCE" in reason_codes:
        action = ACTION_TRIM

    if _etf_strength(sig, cons) and (
        bucket == PM_AVOID or entry == ENTRY_OVEREXTENDED
    ):
        if action in (ACTION_TRIM, ACTION_EXIT):
            action = ACTION_CONTRADICTION
            reason_codes.append("ETF_RULE_CONTRADICTION")

    parts: list[str] = []
    if bucket:
        parts.append(f"隔日{bucket}")
    if entry:
        parts.append(entry)
    if etf_flow:
        parts.append(f"ETF flow {etf_flow}")
    if pm is not None and "chip_tag" in pm.keys() and pm["chip_tag"]:
        parts.append(pm["chip_tag"])
    if action == ACTION_CONTRADICTION:
        parts.append("ETF 加碼與規則背離 · 不給賣出動作")

    return PositionReviewRow(
        book_id=book_id,
        symbol=symbol,
        asset_type="stock",
        stock_name=name,
        holding=True,
        action=action,
        reason_codes=tuple(reason_codes),
        detail=" · ".join(parts) if parts else "池內持倉 · 訊號中性",
        in_research_pool=True,
        pm_bucket=bucket or None,
        entry_signal=entry or None,
        etf_flow=etf_flow,
    )


def review_etf_position(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    symbol: str,
    stock_name: str | None,
) -> PositionReviewRow:
    blocks = build_etf_holdings_changes_block(conn, (symbol,))
    changes = blocks[0]["changes"] if blocks else []
    reds = [c for c in changes if c.get("action") in ("减码", "出清")]
    if len(reds) >= ETF_TRIM_MIN_REDUCES:
        action = ACTION_ETF_TRIM
        detail = f"成分減碼/出清 {len(reds)} 檔 · 對照 ETF 持股變化"
        codes = ("ETF_CONSTITUENT_REDUCE",)
    else:
        action = ACTION_ETF_HOLD
        detail = "ETF 持倉：今日無明顯成分減碼趨勢"
        codes = ()

    return PositionReviewRow(
        book_id=book_id,
        symbol=symbol,
        asset_type="etf",
        stock_name=stock_name,
        holding=True,
        action=action,
        reason_codes=codes,
        detail=detail,
        in_research_pool=False,
        etf_flow=None,
    )


def build_position_review(
    conn: sqlite3.Connection,
    book_id: str,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_HOLDINGS,
    pool: set[str] | None = None,
) -> list[PositionReviewRow]:
    positions = load_portfolio_positions(conn, book_id)
    if not positions:
        return []

    pool_ids = pool if pool is not None else research_pool_ids(conn)
    stock_positions = [p for p in positions if p["asset_type"] == "stock"]
    if not any(str(p["symbol"]) in pool_ids for p in stock_positions):
        return []

    pm_by_id = _pm_map(conn)
    sig_by_id = _signal_stock_map(conn, etf_codes)
    cons_by_id = _consensus_map(conn, etf_codes)

    rows: list[PositionReviewRow] = []
    for p in positions:
        symbol = str(p["symbol"])
        if p["asset_type"] == "etf":
            rows.append(
                review_etf_position(
                    conn,
                    book_id=book_id,
                    symbol=symbol,
                    stock_name=p["stock_name"],
                )
            )
            continue
        rows.append(
            review_stock_position(
                book_id=book_id,
                symbol=symbol,
                stock_name=p["stock_name"],
                in_pool=symbol in pool_ids,
                pm=pm_by_id.get(symbol),
                sig=sig_by_id.get(symbol),
                cons=cons_by_id.get(symbol),
            )
        )

    rows.sort(key=lambda r: r.symbol)
    return rows


def build_all_books_review(
    conn: sqlite3.Connection,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_HOLDINGS,
) -> dict[str, list[PositionReviewRow]]:
    pool = research_pool_ids(conn)
    out: dict[str, list[PositionReviewRow]] = {}
    for book in load_portfolio_books(conn):
        bid = book["book_id"]
        rows = build_position_review(conn, bid, etf_codes=etf_codes, pool=pool)
        if rows:
            out[bid] = rows
    return out


def build_position_exit_summary(
    reviews: dict[str, list[PositionReviewRow]],
) -> dict[str, list[dict[str, Any]]]:
    """池內持倉賣出雷達（供 Research Writer · 不含池外／矛盾）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for book_id, rows in reviews.items():
        items: list[dict[str, Any]] = []
        for r in rows:
            if not r.in_research_pool or r.action not in SELL_RADAR_ACTIONS:
                continue
            items.append(
                {
                    "symbol": r.symbol,
                    "stock_name": r.stock_name,
                    "action": r.action,
                    "reason_codes": list(r.reason_codes),
                    "detail": r.detail,
                    "etf_flow": r.etf_flow,
                }
            )
        if items:
            out[book_id] = items
    return out


def pending_exit_lines(
    reviews: dict[str, list[PositionReviewRow]],
) -> list[str]:
    """早盤待執行提醒（出清觀察 · 無 SELL intent）。"""
    lines: list[str] = []
    for book_id, rows in reviews.items():
        for r in rows:
            if r.action != ACTION_EXIT:
                continue
            name = f" {r.stock_name}" if r.stock_name else ""
            lines.append(f"待執行：{book_id} {r.symbol}{name} · {ACTION_EXIT}")
    return lines


def format_review_markdown(
    reviews: dict[str, list[PositionReviewRow]],
    *,
    as_of_date: str | None = None,
) -> str:
    as_of = as_of_date or date.today().isoformat()
    lines = [
        f"# 持倉檢視 · {as_of.replace('-', '')}",
        "",
        "> 規則草稿 · 非下單建議；出清觀察僅列早盤「待執行」提醒（不產 SELL intent）",
        "",
    ]
    if not reviews:
        lines.append("（今日無帳本含研究池內持倉）")
        return "\n".join(lines)

    for book_id, rows in sorted(reviews.items()):
        lines.append(f"## {book_id}")
        lines.append("")
        lines.append("| 代號 | 類型 | 動作 | ETF flow | 規則 | 說明 |")
        lines.append("|------|------|------|----------|------|------|")
        for r in rows:
            codes = ",".join(r.reason_codes) if r.reason_codes else "—"
            flow = r.etf_flow or "—"
            lines.append(
                f"| {r.symbol} | {r.asset_type} | {r.action} | {flow} | {codes} | {r.detail} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_position_review_report(
    conn: sqlite3.Connection,
    *,
    reports_dir: Path = REPORTS_DIR,
    as_of_date: str | None = None,
) -> Path | None:
    reviews = build_all_books_review(conn)
    if not reviews:
        return None
    as_of = as_of_date or date.today().isoformat()
    stamp = as_of.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{stamp}_position_review.md"
    path.write_text(format_review_markdown(reviews, as_of_date=as_of) + "\n", encoding="utf-8")
    return path


def print_morning_position_exits(conn: sqlite3.Connection) -> None:
    reviews = build_all_books_review(conn)
    lines = pending_exit_lines(reviews)
    if not lines:
        return
    print("")
    print("=== 持倉待執行（出清觀察 · 人工確認）===")
    for line in lines:
        print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="持倉檢視（多帳本）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--book-id", default=None, help="單一帳本；省略則全部")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", action="store_true", help="寫入 reports/*_position_review.md")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        pool = research_pool_ids(conn)
        if args.book_id:
            rows = build_position_review(conn, args.book_id.lower(), pool=pool)
            reviews = {args.book_id.lower(): rows} if rows else {}
        else:
            reviews = build_all_books_review(conn)

        if not reviews:
            print("尚無研究池內持倉帳本；請確認 portfolio_books 與 investment_scores", file=sys.stderr)
            return 1

        if args.json:
            payload = {bid: [r.to_dict() for r in rs] for bid, rs in reviews.items()}
            payload["position_exit_summary"] = build_position_exit_summary(reviews)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(format_review_markdown(reviews))

        if args.report:
            path = write_position_review_report(conn)
            if path:
                print(f"\n已寫入 {path}", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
