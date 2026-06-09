#!/usr/bin/env python3
"""
L7 新聞同步（Perplexity）：Research Universe 內股票 → catalyst_events（source=perplexity）。

僅在 RUN_NEWS_SYNC=1 且 PERPLEXITY_API_KEY 設定時由 daily_sync 收盤段呼叫。
手動事件仍以 data/manual_events.json 為準（USE_MANUAL_EVENTS=1 時；預設關閉）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from catalyst_engine import event_to_row
from perplexity_client import chat_completion, extract_json_payload, get_config
from event_ranking import CatalystEvent, normalize_catalyst_type
from research_universe import DEFAULT_ETF_CODES, build_research_universe, parse_etf_codes
from stock_db import DEFAULT_DB_PATH, connect, upsert_catalyst_events

SOURCE_PERPLEXITY = "perplexity"
FORBIDDEN_RATING_RE = re.compile(
    r"\b(BUY|HOLD|TRIM|STRONG\s+BUY|SELL)\b|買進|賣出|目標價|建議買|建議賣",
    re.IGNORECASE,
)
def _parse_events_payload(text: str) -> list[dict]:
    data = extract_json_payload(text)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("events", "catalyst_events", "items"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def _normalize_event(item: dict, *, allowed_ids: set[str]) -> CatalystEvent | None:
    sid = str(item.get("stock_id", "")).strip()
    if not sid or sid not in allowed_ids:
        return None
    if FORBIDDEN_RATING_RE.search(json.dumps(item, ensure_ascii=False)):
        return None
    try:
        event_date = date.fromisoformat(str(item["event_date"])[:10])
    except (KeyError, ValueError):
        return None
    headline = str(item.get("headline", "")).strip()[:80]
    ctype = normalize_catalyst_type(
        str(item.get("catalyst_type", "EARNINGS")),
        headline,
    )
    if ctype == "INDEX_REBALANCE":
        return None
    polarity = str(item.get("polarity", "NEUTRAL")).upper()
    if polarity not in {"BULL", "BEAR", "NEUTRAL"}:
        polarity = "NEUTRAL"
    explains = str(item.get("explains_etf_add", "NONE")).upper()
    if explains not in {"HIGH", "MED", "LOW", "NONE"}:
        explains = "NONE"
    try:
        conf = int(item.get("confidence", 55))
    except (TypeError, ValueError):
        conf = 55
    conf = max(0, min(100, conf))
    if not headline:
        return None
    sources = item.get("sources")
    if not isinstance(sources, list):
        sources = []
    clean_sources = []
    for s in sources[:3]:
        if isinstance(s, dict) and s.get("title"):
            clean_sources.append(
                {
                    "title": str(s["title"])[:120],
                    "date": str(s.get("date", ""))[:10],
                    "url": str(s.get("url", ""))[:500],
                }
            )
    return CatalystEvent(
        stock_id=sid,
        event_date=event_date,
        catalyst_type=ctype,
        headline=headline,
        polarity=polarity,
        explains_etf_add=explains,
        confidence=conf,
        sources=clean_sources or None,
    )


def _dedupe_max_two(events: list[CatalystEvent]) -> list[CatalystEvent]:
    by_stock: dict[str, list[CatalystEvent]] = {}
    for ev in sorted(events, key=lambda e: (e.event_date, e.confidence), reverse=True):
        by_stock.setdefault(ev.stock_id, []).append(ev)
    out: list[CatalystEvent] = []
    for sid in sorted(by_stock):
        out.extend(by_stock[sid][:2])
    return out


def fetch_perplexity_events(
    universe_entries: list,
    *,
    lookback_days: int = 7,
    api_key: str,
    model: str,
    timeout: int = 120,
) -> list[CatalystEvent]:
    if not universe_entries:
        return []
    stocks = [
        {
            "stock_id": e.stock_id,
            "name": e.stock_name or e.stock_id,
            "pool_reason": e.pool_reason,
            "headline_hint": e.headline,
        }
        for e in universe_entries
    ]
    today = date.today().isoformat()
    prompt = (
        f"今天是 {today}。以下台股為 ETF 研究池（最近 {lookback_days} 日新聞）。\n"
        f"股票清單 JSON：{json.dumps(stocks, ensure_ascii=False)}\n\n"
        "任務：每檔 0–2 個最重要、有公開來源的事件；僅回傳 JSON 物件："
        '{"events":[{"stock_id":"2330","event_date":"YYYY-MM-DD",'
        '"catalyst_type":"PRODUCT_CYCLE|SUPPLY_CHAIN|POLICY|CAPX|EARNINGS|SELL_SIDE|VALUATION",'
        '"headline":"≤80字","polarity":"BULL|BEAR|NEUTRAL",'
        '"explains_etf_add":"HIGH|MED|LOW|NONE","confidence":0-100,'
        '"sources":[{"title":"","date":"YYYY-MM-DD","url":""}]}]}\n'
        "禁止 MSCI/指數調整/成分股調整/被動資金權重變動作為主事件；"
        "須寫產業催化（產品、供應鏈、資本支出、法說、訂單等）。"
        "禁止 BUY/HOLD/TRIM、目標價、建議買賣；不得臆測無來源事實。"
    )
    from perplexity_client import PerplexityConfig

    cfg = PerplexityConfig(api_key=api_key, model=model, timeout=timeout)
    content = chat_completion(
        [
            {
                "role": "system",
                "content": "你是台股研究助理，只輸出合法 JSON，不輸出投資評級。",
            },
            {"role": "user", "content": prompt},
        ],
        cfg=cfg,
        temperature=0.1,
    )
    allowed = {e.stock_id for e in universe_entries}
    raw_items = _parse_events_payload(content)
    parsed: list[CatalystEvent] = []
    for item in raw_items:
        ev = _normalize_event(item, allowed_ids=allowed)
        if ev is None:
            continue
        age = (date.today() - ev.event_date).days
        if age < 0 or age > lookback_days:
            continue
        parsed.append(ev)
    return _dedupe_max_two(parsed)


def sync_news_for_universe(
    conn,
    etf_codes: tuple[str, ...],
    *,
    lookback_days: int = 7,
    dry_run: bool = False,
) -> tuple[int, list[CatalystEvent]]:
    universe = build_research_universe(conn, etf_codes)
    if universe is None or not universe.entries:
        print("  SKIP Perplexity：無法建立 Research Universe", file=sys.stderr)
        return 0, []

    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        print("  SKIP Perplexity：PERPLEXITY_API_KEY 未設定", file=sys.stderr)
        return 0, []

    model = os.environ.get("PERPLEXITY_MODEL", "sonar").strip() or "sonar"
    try:
        events = fetch_perplexity_events(
            universe.entries,
            lookback_days=lookback_days,
            api_key=api_key,
            model=model,
        )
    except requests.RequestException as exc:
        print(f"  WARN Perplexity API: {exc}", file=sys.stderr)
        return 0, []

    if dry_run:
        return len(events), events

    rows = [
        {**event_to_row(ev), "source": SOURCE_PERPLEXITY}
        for ev in events
    ]
    n = upsert_catalyst_events(conn, rows)
    return n, events


def print_news_report(events: list[CatalystEvent], *, upserted: int) -> None:
    print("")
    print("=== Perplexity 催化（→ catalyst_events）===")
    print(f"  寫入 {upserted} 筆（source=perplexity）")
    if not events:
        print("  本輪無合格事件")
        return
    for ev in events[:12]:
        print(
            f"  {ev.stock_id} {ev.event_date} [{ev.catalyst_type}] "
            f"{ev.explains_etf_add} {ev.headline[:50]}"
        )
    if len(events) > 12:
        print(f"  … 另有 {len(events) - 12} 筆")


def main() -> int:
    parser = argparse.ArgumentParser(description="Perplexity → catalyst_events")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help="印終端摘要")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("NEWS_LOOKBACK_DAYS", "7")),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        n, events = sync_news_for_universe(
            conn,
            codes,
            lookback_days=args.lookback_days,
            dry_run=args.dry_run,
        )
        if args.sync_db and not args.dry_run:
            if not args.quiet:
                print(f"  perplexity → catalyst_events upsert {n} 列")
        if args.report:
            print_news_report(events, upserted=n if args.sync_db else len(events))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
