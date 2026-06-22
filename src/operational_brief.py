#!/usr/bin/env python3
"""隔日開盤風控 checklist（規則產出 · 純技術／資金面）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from market_labels import (
    CHIP_FOREIGN_SELL_DIV,
    CHIP_SYNC_SELL,
    ENTRY_OVEREXTENDED,
    PM_AVOID,
    PM_BREAKOUT,
    PM_OBSERVE,
)
from research_universe import DEFAULT_ETF_CODES, parse_etf_codes
from score_engine import SCORE_VERSION
from signal_engine import StockSignal, build_aligned_signals
from report_paths import REPORTS_DIR
from stock_db import (
    PROJECT_ROOT,
    connect,
    load_order_tx_gap,
    load_latest_morning_risk,
    load_latest_pm_watchlist,
    load_latest_tech_risk,
    load_tsm_adr_spread_before,
)
from sync_morning_futures import format_morning_risk_line, morning_radar_warnings

MANUAL_RISK_RULES: tuple[str, ...] = (
    "L2 假共識（FALSE）：多檔 ETF 加碼但力度不足 → 不當聰明錢訊號",
    "外資賣超背離：ETF 加碼但外資大賣 → 降優先或觀望",
    "暫不進場 / ETF 減碼：entry_signal 為風控覆寫 → 隔日不追",
    "乖離過大且非量價齊揚：即使評分高也僅列一般觀察",
    "TSM ADR 單日 < -2%：科技折讓加深＋縮倉（IPS adr_weak_size_scale）",
    "指數調整季（MSCI/台灣50 等）：共識分數打折，保守一週",
)


@dataclass(frozen=True)
class MorningCheckItem:
    section: str
    text: str
    checked: bool = False


def signal_map(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, StockSignal]:
    aligned = build_aligned_signals(conn, etf_codes)
    if aligned is None:
        return {}
    return {s.stock_id: s for s in aligned.signals}


def checklist_to_dict(items: list[MorningCheckItem]) -> list[dict[str, str | bool]]:
    return [
        {"section": it.section, "text": it.text, "checked": it.checked}
        for it in items
    ]


def load_tsm_adr_pct(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> float | None:
    from datetime import date as date_cls

    ref = trade_date or date_cls.today().isoformat()
    bar_date, bar_spread = load_tsm_adr_spread_before(conn, ref)
    row = load_latest_tech_risk(conn, trade_date=ref)
    if bar_spread is not None:
        if row is None:
            return bar_spread
        us_date = row["us_trade_date"] if "us_trade_date" in row.keys() else None
        if us_date is None or (bar_date and str(us_date) < bar_date):
            return bar_spread
    if row is None:
        return None
    val = row["tsm_daily_return_pct"]
    return float(val) if val is not None else None


def build_operational_brief_block(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict:
    """隔日 Checklist（結構化）。"""
    checklist = build_morning_checklist_items(conn, etf_codes)
    return {"next_day_checklist": checklist_to_dict(checklist)}


def _tsm_gate_lines(
    conn: sqlite3.Connection,
    *,
    trade_date: str | None = None,
) -> list[str]:
    ref = trade_date or date.today().isoformat()
    tech = load_latest_tech_risk(conn, trade_date=ref)
    if tech is None:
        return ["tech_risk 尚無資料（請先跑早盤同步）"]
    tsm = load_tsm_adr_pct(conn, trade_date=ref)
    lines = [
        f"[隔夜] 台股日 {tech['session_date']} · TSM ADR {_fmt_pct(tsm)} · "
        f"半導體 {_fmt_pct(tech['sox_daily_return_pct'])} · "
        f"台指gap(隔夜) {_fmt_pct(tech['tx_gap_pct'])} · "
        f"電子期(隔夜) {_fmt_pct(tech['te_overnight_pct'])}",
    ]
    if tsm is not None and float(tsm) < -2.0:
        lines.append("⚠ TSM ADR < -2% → 科技折讓加深＋縮倉（非擋單）")

    morning = load_latest_morning_risk(conn, trade_date=ref)
    if morning is not None:
        lines.append(f"[即時] {format_morning_risk_line(morning)}")
        lines.extend(morning_radar_warnings(morning))
    else:
        lines.append("[即時] morning_risk 尚無資料（sync_morning_futures）")

    gap_val, gap_src = load_order_tx_gap(conn, trade_date=ref)
    if gap_val is not None and abs(float(gap_val)) >= 1.0:
        lines.append(f"⚠ 執行用 gap {float(gap_val):+.2f}%（{gap_src}）→ 開盤波動風險")
    return lines


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def build_morning_checklist_items(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[MorningCheckItem]:
    items: list[MorningCheckItem] = []

    for line in _tsm_gate_lines(conn):
        items.append(MorningCheckItem("隔夜風險", line))

    rows = load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
    if not rows:
        items.append(
            MorningCheckItem(
                "隔日名單",
                "尚無 pm_watchlist（請先跑收盤 Score Engine --sync-db）",
            )
        )
    else:
        as_of = rows[0]["as_of_date"]
        items.append(MorningCheckItem("隔日名單", f"名單基準日 {as_of}（score {SCORE_VERSION}）"))

        for bucket, label in (
            (PM_BREAKOUT, "優先：價量突破"),
            (PM_OBSERVE, "列入觀察"),
            (PM_AVOID, "不宜追價"),
        ):
            group = [r for r in rows if r["pm_bucket"] == bucket]
            if not group:
                continue
            for r in group[:8]:
                entry = r["entry_signal"] or "—"
                chip = r["chip_tag"] or "—"
                items.append(
                    MorningCheckItem(
                        label,
                        f"{r['stock_id']} {r['stock_name']} · {entry} · 分 {r['investment_score']:.1f} · {chip}",
                    )
                )

    sig_by_id = signal_map(conn, etf_codes)
    false_cons = [
        s for s in sig_by_id.values() if s.consensus_level == "FALSE" and s.net_side == "add"
    ]
    if false_cons:
        for s in false_cons[:5]:
            items.append(
                MorningCheckItem(
                    "人工風控",
                    f"假共識 {s.stock_id} {s.stock_name}（L2=FALSE）→ 勿當聰明錢",
                )
            )

    for r in rows:
        if r["chip_tag"] == CHIP_FOREIGN_SELL_DIV:
            items.append(
                MorningCheckItem(
                    "人工風控",
                    f"外資賣超背離 {r['stock_id']} {r['stock_name']} → 降優先",
                )
            )
        if r["entry_signal"] == ENTRY_OVEREXTENDED:
            items.append(
                MorningCheckItem(
                    "人工風控",
                    f"乖離過大 {r['stock_id']} {r['stock_name']} → 不追價",
                )
            )
        if r["chip_tag"] == CHIP_SYNC_SELL:
            items.append(
                MorningCheckItem(
                    "人工風控",
                    f"同步賣超 {r['stock_id']} {r['stock_name']}",
                )
            )


    for rule in MANUAL_RISK_RULES:
        items.append(MorningCheckItem("風控守則", rule))

    return items


def print_morning_checklist(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> None:
    items = build_morning_checklist_items(conn, etf_codes)
    print("")
    print("=== 開盤風控 Checklist（早盤）===")
    section: str | None = None
    for it in items:
        if it.section != section:
            section = it.section
            print(f"  --- {section} ---")
        print(f"  [ ] {it.text}")

    stamp = date.today().strftime("%Y%m%d")
    brief = REPORTS_DIR / f"{stamp}_evening_brief.md"
    if not brief.exists():
        candidates = sorted(REPORTS_DIR.glob("*_evening_brief.md"), reverse=True)
        brief = candidates[0] if candidates else None
    if brief is not None and brief.exists():
        print(f"  完整 Checklist → reports/{brief.name}（§隔日 Checklist）")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="隔日 checklist（規則產出）")
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    args = parser.parse_args()
    codes = parse_etf_codes(args.etf_codes)
    conn = connect()
    try:
        print_morning_checklist(conn, codes)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
