#!/usr/bin/env python3
"""收盤待查新聞提示 + 隔日開盤風控 checklist（規則產出 · 不取代人工查證）。"""

from __future__ import annotations

import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from event_ranking import manual_events_enabled
from investment_themes import stock_theme, theme_label
from market_labels import (
    CHIP_FOREIGN_SELL_DIV,
    CHIP_SYNC_SELL,
    ENTRY_OVEREXTENDED,
    ENTRY_SKIP,
    PM_AVOID,
    PM_BREAKOUT,
    PM_OBSERVE,
    WL_EXCLUDED,
    WL_PRIMARY,
)
from research_universe import DEFAULT_ETF_CODES, parse_etf_codes
from score_engine import SCORE_VERSION
from signal_engine import StockSignal, build_aligned_signals
from pre_trade_check import load_tsm_adr_pct
from stock_db import (
    PROJECT_ROOT,
    connect,
    load_execution_tx_gap,
    load_latest_morning_risk,
    load_latest_pm_watchlist,
    load_latest_tech_risk,
)
from sync_morning_futures import format_morning_risk_line, morning_radar_warnings

REPORTS_DIR = PROJECT_ROOT / "reports"

CATALYST_NEEDS_VERIFY_MAX = 45.0
SMART_MONEY_ATTENTION_MIN = 68.0

MANUAL_RISK_RULES: tuple[str, ...] = (
    "L2 假共識（FALSE）：多檔 ETF 加碼但力度不足 → 不當聰明錢訊號",
    "外資賣超背離：ETF 加碼但外資大賣 → 降優先或觀望",
    "暫不進場 / ETF 減碼：entry_signal 為風控覆寫 → 隔日不追",
    "乖離過大且非量價齊揚：即使評分高也僅列一般觀察",
    "TSM ADR 單日 < -2%：科技折讓加深＋縮倉（IPS adr_weak_size_scale）",
    "指數調整季（MSCI/台灣50 等）：共識分數打折，保守一週",
)

THEME_SEARCH_HINTS: dict[str, str] = {
    "AI_SEMIS": "法說 CoWoS 先進製程",
    "MEMORY": "HBM 記憶體 產能",
    "MOBILE_OPTICS": "手機鏡頭 客戶拉貨",
    "PCB": "AI 伺服器 載板",
    "FINANCIAL": "金控 獲利 利差",
    "CYCLE_CHEM": "塑化 景氣 報價",
}


@dataclass(frozen=True)
class NewsVerifyItem:
    stock_id: str
    stock_name: str
    reason: str
    search_query: str
    yahoo_news_url: str


@dataclass(frozen=True)
class MorningCheckItem:
    section: str
    text: str
    checked: bool = False


def yahoo_tw_news_url(stock_id: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{stock_id}.TW/news"


def google_search_url(query: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote(query)


def _search_query(stock_id: str, stock_name: str) -> str:
    theme = stock_theme(stock_id)
    hint = THEME_SEARCH_HINTS.get(theme, theme_label(theme))
    return f"{stock_name} {stock_id} {hint} 新聞"


def signal_map(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, StockSignal]:
    aligned = build_aligned_signals(conn, etf_codes)
    if aligned is None:
        return {}
    return {s.stock_id: s for s in aligned.signals}


def news_verify_to_dict(items: list[NewsVerifyItem]) -> list[dict[str, str]]:
    return [
        {
            "stock_id": it.stock_id,
            "stock_name": it.stock_name,
            "reason": it.reason,
            "search_query": it.search_query,
            "yahoo_news_url": it.yahoo_news_url,
            "google_search_url": google_search_url(it.search_query),
        }
        for it in items
    ]


def checklist_to_dict(items: list[MorningCheckItem]) -> list[dict[str, str | bool]]:
    return [
        {"section": it.section, "text": it.text, "checked": it.checked}
        for it in items
    ]


def build_operational_brief_block(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict:
    """待查新聞 + 隔日 Checklist（結構化 · 供 research_context）。"""
    news = build_news_verify_items(conn, etf_codes)
    checklist = build_morning_checklist_items(conn, etf_codes)
    return {
        "news_verify": news_verify_to_dict(news),
        "news_verify_note": (
            "聯網 LLM 依 news_verify 查證（見 prompt_evening_full §3）；"
            "無聯網則只列待查項，不得臆測（USE_MANUAL_EVENTS=0）"
        ),
        "next_day_checklist": checklist_to_dict(checklist),
    }


def build_news_verify_items(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[NewsVerifyItem]:
    """資金/評分突出但系統無可靠催化的標的 → 提醒人工上網查證。"""
    rows = load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
    if not rows:
        return []

    sig_by_id = signal_map(conn, etf_codes)
    items: list[NewsVerifyItem] = []
    seen: set[str] = set()

    for r in rows:
        sid = r["stock_id"]
        if sid in seen:
            continue
        catalyst = float(r["catalyst_score"] or 0)
        flow = float(r["flow_score"] or 0)
        chip = float(r["chip_score"] or 0)
        smart = flow * 0.55 + chip * 0.45
        wl = r["watchlist"] or ""
        bucket = r["pm_bucket"] or ""
        entry = r["entry_signal"] or ""

        if wl == WL_EXCLUDED and bucket == PM_AVOID:
            continue
        if entry in (ENTRY_SKIP,):
            continue

        reasons: list[str] = []
        if catalyst <= CATALYST_NEEDS_VERIFY_MAX and smart >= SMART_MONEY_ATTENTION_MIN:
            reasons.append(f"資金籌碼偏強({smart:.0f})但催化未確認({catalyst:.0f})")
        if wl == WL_PRIMARY and catalyst <= CATALYST_NEEDS_VERIFY_MAX:
            reasons.append("首要觀察但無結構化催化")
        if bucket in (PM_BREAKOUT, PM_OBSERVE) and catalyst <= CATALYST_NEEDS_VERIFY_MAX:
            reasons.append(f"隔日{bucket}但缺 Why（請自行查新聞）")

        sig = sig_by_id.get(sid)
        if sig and sig.consensus_level == "FALSE":
            reasons.append("L2 假共識（檔數同步但力度弱）— 查是否為指數調整/被動買")

        if not reasons:
            continue

        seen.add(sid)
        name = r["stock_name"] or sid
        q = _search_query(sid, name)
        items.append(
            NewsVerifyItem(
                stock_id=sid,
                stock_name=name,
                reason="；".join(reasons),
                search_query=q,
                yahoo_news_url=yahoo_tw_news_url(sid),
            )
        )

    items.sort(key=lambda x: (x.stock_id,))
    return items


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

    gap_val, gap_src = load_execution_tx_gap(conn, trade_date=ref)
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

    from order_intent_engine import morning_execution_checklist_lines

    exec_lines = morning_execution_checklist_lines(conn)
    for line in exec_lines:
        section = "建議掛單" if line.startswith("✓") else "風控略過"
        items.append(MorningCheckItem(section, line.lstrip("✓✗ ").strip()))

    for rule in MANUAL_RISK_RULES:
        items.append(MorningCheckItem("風控守則", rule))

    return items


def _markdown_news_verify(items: list[NewsVerifyItem], *, as_of: str) -> str:
    lines = [
        f"# 待確認新聞（{as_of}）",
        "",
        "> 系統無 TEJ/Yahoo 新聞 API；以下標的請**自行上網查證**後再解讀 ETF 加碼。",
        "> 勿使用 manual_events.json；查完可在筆記記錄，無需寫回 DB。",
        "",
    ]
    if not items:
        lines.append("（今日 Universe 無需額外查證標的，或尚未跑 Score Engine）")
        return "\n".join(lines) + "\n"

    for i, it in enumerate(items, 1):
        lines.extend(
            [
                f"## {i}. {it.stock_id} {it.stock_name}",
                "",
                f"- **原因**：{it.reason}",
                f"- **建議搜尋**：`{it.search_query}`",
                f"- **Google**：[搜尋]({google_search_url(it.search_query)})",
                f"- **Yahoo 奇摩股市**：[新聞]({it.yahoo_news_url})",
                "",
            ]
        )
    return "\n".join(lines)


def _markdown_checklist(items: list[MorningCheckItem], *, title: str) -> str:
    lines = [f"# {title}", ""]
    section: str | None = None
    for it in items:
        if it.section != section:
            section = it.section
            lines.extend(["", f"## {section}", ""])
        mark = "x" if it.checked else " "
        lines.append(f"- [{mark}] {it.text}")
    lines.append("")
    return "\n".join(lines)


def write_evening_brief(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    as_of: str | None = None,
    reports_dir: Path | None = None,
) -> Path:
    """寫入收盤唯一人類主檔 evening_brief.md。"""
    from evening_digest import write_evening_brief_file

    return write_evening_brief_file(
        conn, etf_codes, as_of=as_of, reports_dir=reports_dir or REPORTS_DIR
    )


def print_evening_brief(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> None:
    news_items = build_news_verify_items(conn, etf_codes)
    checklist_items = build_morning_checklist_items(conn, etf_codes)

    print("")
    print("=== 待確認新聞（請自行上網查證 · 非 API 拉取）===")
    if not manual_events_enabled():
        print("  催化來源：人工查證（USE_MANUAL_EVENTS=0，已停用 manual_events.json）")
    if not news_items:
        print("  （無需額外查證標的，或尚未跑 Score Engine）")
    else:
        for it in news_items:
            print(f"  {it.stock_id} {it.stock_name}")
            print(f"    原因  {it.reason}")
            print(f"    搜尋  {it.search_query}")
            print(f"    Yahoo {it.yahoo_news_url}")

    print("")
    print("=== 隔日開盤風控 Checklist（規則產出 · 開盤前再對照早盤報）===")
    section: str | None = None
    for it in checklist_items:
        if it.section != section:
            section = it.section
            print(f"  --- {section} ---")
        print(f"  [ ] {it.text}")

    brief_path = write_evening_brief(conn, etf_codes)
    print("")
    print(f"  已寫入  {brief_path.relative_to(PROJECT_ROOT)}")


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

    parser = argparse.ArgumentParser(description="收盤待查新聞 + 隔日 checklist")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("evening", "morning"),
    )
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    args = parser.parse_args()
    codes = parse_etf_codes(args.etf_codes)
    conn = connect()
    try:
        if args.mode == "evening":
            print_evening_brief(conn, codes)
        else:
            print_morning_checklist(conn, codes)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
