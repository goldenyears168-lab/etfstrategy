#!/usr/bin/env python3
"""
P4 L7 Catalyst Engine：Research Universe 內事件入庫 catalyst_events。

預設 USE_MANUAL_EVENTS=0：催化改由收盤 operational_brief 提醒人工上網查。
USE_MANUAL_EVENTS=1 時可從 manual_events.json 入庫。
可選 --llm-validate：以 OPENAI_API_KEY 校驗/補全 taxonomy（仍禁止評級）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

from event_ranking import (
    CATALYST_TYPES,
    DEFAULT_EVENTS_PATH,
    CatalystEvent,
    catalyst_event_id,
    load_all_catalyst_events,
    load_manual_events,
    purge_index_rebalance_from_db,
    events_path_hint,
)
from research_universe import DEFAULT_ETF_CODES, build_research_universe, parse_etf_codes
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_etf_constituent_watchlist,
    upsert_catalyst_events,
)

SOURCE_MANUAL = "manual"
FORBIDDEN_RATING_RE = re.compile(
    r"\b(BUY|HOLD|TRIM|STRONG\s+BUY|SELL)\b|買進|賣出|加碼買|減碼賣|目標價|建議買|建議賣",
    re.IGNORECASE,
)


def event_to_row(ev: CatalystEvent, *, source: str = SOURCE_MANUAL) -> dict:
    return {
        "event_id": catalyst_event_id(ev),
        "stock_id": ev.stock_id,
        "event_date": ev.event_date.isoformat(),
        "catalyst_type": ev.catalyst_type,
        "headline": ev.headline,
        "polarity": ev.polarity,
        "explains_etf_add": ev.explains_etf_add,
        "confidence": ev.confidence,
        "sources_json": json.dumps(ev.sources or [], ensure_ascii=False),
        "source": source,
    }


def filter_universe_events(
    events: list[CatalystEvent],
    conn,
    etf_codes: tuple[str, ...],
) -> list[CatalystEvent]:
    universe = build_research_universe(conn, etf_codes)
    if universe is None:
        watchlist = load_etf_constituent_watchlist(conn, etf_codes)
        pool = {w["stock_id"] for w in watchlist}
    else:
        pool = set(universe.stock_ids)
    if not pool:
        return []
    return [e for e in events if e.stock_id in pool]


def llm_validate_events(events: list[CatalystEvent]) -> list[CatalystEvent]:
    """可選：請 LLM 僅回傳 JSON 陣列校正 taxonomy（無評級）。"""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return events

    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    payload_events = [
        {
            "stock_id": e.stock_id,
            "event_date": e.event_date.isoformat(),
            "catalyst_type": e.catalyst_type,
            "headline": e.headline,
            "polarity": e.polarity,
            "explains_etf_add": e.explains_etf_add,
            "confidence": e.confidence,
        }
        for e in events
    ]
    prompt = (
        "你是台股研究助理。僅校正下列事件的 catalyst_type（枚舉）與 explains_etf_add，"
        f"枚舉 type={sorted(CATALYST_TYPES)} explains=HIGH|MED|LOW|NONE。"
        "禁止輸出投資評級、目標價、BUY/HOLD/TRIM。回傳 JSON 陣列，欄位同輸入。\n"
        + json.dumps(payload_events, ensure_ascii=False)
    )
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "只回 JSON 陣列，無 markdown。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if FORBIDDEN_RATING_RE.search(content):
            print("  WARN LLM 回覆含禁止評級字樣，略過 LLM 校正", file=sys.stderr)
            return events
        start = content.find("[")
        end = content.rfind("]") + 1
        if start < 0 or end <= start:
            return events
        parsed = json.loads(content[start:end])
    except (requests.RequestException, KeyError, json.JSONDecodeError, ValueError) as exc:
        print(f"  WARN LLM validate: {exc}", file=sys.stderr)
        return events

    out: list[CatalystEvent] = []
    for item, orig in zip(parsed, events):
        if not isinstance(item, dict):
            out.append(orig)
            continue
        ctype = str(item.get("catalyst_type", orig.catalyst_type)).upper()
        if ctype not in CATALYST_TYPES:
            ctype = orig.catalyst_type
        explains = str(item.get("explains_etf_add", orig.explains_etf_add)).upper()
        if explains not in {"HIGH", "MED", "LOW", "NONE"}:
            explains = orig.explains_etf_add
        out.append(
            CatalystEvent(
                stock_id=orig.stock_id,
                event_date=orig.event_date,
                catalyst_type=ctype,
                headline=str(item.get("headline", orig.headline))[:80],
                polarity=orig.polarity,
                explains_etf_add=explains,
                confidence=orig.confidence,
                sources=orig.sources,
            )
        )
    return out


def sync_catalyst_events(
    conn,
    etf_codes: tuple[str, ...],
    *,
    events_path: Path | None = None,
    universe_only: bool = True,
    llm_validate: bool = False,
) -> int:
    manual = load_manual_events(events_path)
    if universe_only:
        manual = filter_universe_events(manual, conn, etf_codes)
    if llm_validate and manual:
        manual = llm_validate_events(manual)
    rows = [event_to_row(e) for e in manual]
    return upsert_catalyst_events(conn, rows)


def print_catalyst_report(conn, etf_codes: tuple[str, ...], events_path: Path | None) -> None:
    from event_ranking import filter_events_in_window, is_index_rebalance_event

    universe = build_research_universe(conn, etf_codes)
    pool = set(universe.stock_ids) if universe else None
    all_events = load_all_catalyst_events(conn, events_path, pool_stock_ids=pool)
    index_n = sum(1 for e in all_events if is_index_rebalance_event(e))
    events = filter_events_in_window(
        all_events,
        pool_stock_ids=pool,
        industry_only=True,
    )
    events.sort(key=lambda e: (e.event_date, e.confidence), reverse=True)
    print("")
    print("=== Catalyst Events（L7 · 產業催化 · Universe 7 日）===")
    hint = events_path_hint()
    if not events:
        print(f"  無產業催化（可編輯 {hint} 後 --sync-db；決策看 Score/pm/prompt JSON）")
        if index_n:
            print(f"  （已略 {index_n} 筆指數調整類；--purge-index 可自 DB 刪除）")
        return
    for ev in events[:15]:
        print(
            f"  {ev.stock_id} {ev.event_date} [{ev.catalyst_type}] "
            f"{ev.explains_etf_add} conf={ev.confidence} {ev.headline[:40]}"
        )
    tail = f"  合計 {len(events)} 筆產業事件（{hint} ∪ DB）"
    if index_n:
        tail += f"；略 {index_n} 筆指數調整"
    print(tail)


def main() -> int:
    parser = argparse.ArgumentParser(description="L7 Catalyst → catalyst_events")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument(
        "--purge-index",
        action="store_true",
        help="刪除 DB 內 MSCI/指數調整事件（--sync-db 時預設一併執行）",
    )
    parser.add_argument(
        "--no-purge-index",
        action="store_true",
        help="--sync-db 時不清理指數調整事件",
    )
    parser.add_argument(
        "--all-holdings",
        action="store_true",
        help="不限制 Research Universe，寫入聯集持股內手動事件",
    )
    parser.add_argument("--llm-validate", action="store_true")
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        purged = 0
        do_purge = args.purge_index or (
            args.sync_db and not args.no_purge_index
        )
        if do_purge:
            purged = purge_index_rebalance_from_db(conn)
            if purged:
                print(f"  DB：已刪除 {purged} 筆指數調整事件")
        if args.sync_db:
            n = sync_catalyst_events(
                conn,
                codes,
                events_path=args.events_file,
                universe_only=not args.all_holdings,
                llm_validate=args.llm_validate,
            )
            print(f"  DB：catalyst_events upsert {n} 列（來源 {events_path_hint()}）")
        print_catalyst_report(conn, codes, args.events_file)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
