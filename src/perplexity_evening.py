#!/usr/bin/env python3
"""
收盤 Perplexity 高價值用法（在持股/Score 之後，讀 DB 不打 TEJ）：

  --summary   今日收盤執行摘要（5–7 條，繁中）
  --verify    查證 catalyst_events（CONFIRMED/PARTIAL/UNCONFIRMED/RUMOR）並可調 confidence

環境變數：
  RUN_PERPLEXITY_SUMMARY=1
  RUN_PERPLEXITY_VERIFY=1
  PERPLEXITY_VERIFY_MAX=8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from catalyst_engine import event_to_row
from event_ranking import (
    CatalystEvent,
    catalyst_event_id,
    is_index_rebalance_event,
    load_all_catalyst_events,
    row_to_catalyst_event,
)
from perplexity_client import (
    audit_narrative,
    chat_completion,
    extract_json_payload,
    get_config,
)
from research_context import build_evening_context
from research_universe import DEFAULT_ETF_CODES, parse_etf_codes
from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    load_catalyst_events,
    upsert_catalyst_events,
)

REPORTS_DIR = PROJECT_ROOT / "reports"
VERIFY_STATUSES = frozenset({"CONFIRMED", "PARTIAL", "UNCONFIRMED", "RUMOR"})
_INDEX_MAINLINE_RE = re.compile(
    r"MSCI|指數調整|成分股調整|被動資金|權重下調|權重調升|FTSE|富時",
    re.IGNORECASE,
)


def audit_evening_no_index_mainline(text: str) -> tuple[bool, list[str]]:
    """收盤摘要不得把指數調整寫成催化主線。"""
    notes: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        if _INDEX_MAINLINE_RE.search(s) and re.search(
            r"催化|新聞|要點|主線|最主要|核心訊息", s
        ):
            notes.append("催化敘事以指數調整為主線")
            break
    return (len(notes) == 0, notes)


@dataclass(frozen=True)
class VerifyResult:
    event_id: str
    stock_id: str
    headline: str
    status: str
    note: str
    confidence_delta: int
    old_confidence: int
    new_confidence: int


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def run_evening_summary(
    conn,
    etf_codes: tuple[str, ...],
    *,
    write_report: bool = True,
) -> str:
    cfg = get_config(model_env="PERPLEXITY_SUMMARY_MODEL")
    if cfg is None:
        raise RuntimeError("PERPLEXITY_API_KEY 未設定")

    ctx = build_evening_context(conn, etf_codes)
    prompt = (
        "你是台股 ETF 持股研究助理。以下 JSON 為今日收盤已入庫的量化結果（勿臆測未列事實）。\n"
        f"{json.dumps(ctx, ensure_ascii=False)}\n\n"
        "請用繁體中文輸出「今日收盤執行摘要」，格式：\n"
        "1) 第一行標題：# 今日收盤摘要\n"
        "2) 5–7 條 bullet（`- ` 開頭），依序涵蓋：\n"
        "   - 隔夜/開盤風險（tech_risk）\n"
        "   - ETF 資金共識與輪動（從 decisions / signal_layers 推斷，點名 2–3 檔）\n"
        "   - 催化/新聞要點：僅用 catalyst_events（已排除 MSCI/指數調整）。"
        "若為空，改寫 ETF 持股輪動與產業主線（decisions / signal_layers），"
        "禁止以 MSCI/指數成分/權重調整/被動資金作主線或首句。\n"
        "   - Rule 觀察名單：依 decisions.watchlist 與 pm_bucket（觀察／突破）；"
        "勿因指數調整事件將標的升格為 A。\n"
        "   - 資料限制（若 universe_window 或 ETF 日期不同步須提醒）\n"
        "硬性禁止：MSCI/FTSE/指數調整/成分股權重作為摘要主線；"
        "BUY/HOLD/TRIM、目標價、建議買賣、部位%。\n"
        "不要輸出 JSON，只輸出 Markdown。"
    )
    text = chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "你只寫研究摘要，不給投資評級。"
                    "不得以 MSCI/指數調整為敘事主軸；產業與 ETF 資金行為優先。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        cfg=cfg,
        temperature=0.3,
    )
    ok, notes = audit_narrative(text)
    ok2, notes2 = audit_evening_no_index_mainline(text)
    warn = notes + notes2
    if warn:
        text += f"\n\n（審計警告：{', '.join(warn)}）"

    if write_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"{date.today().strftime('%Y%m%d')}_evening_summary.md"
        path.write_text(text.strip() + "\n", encoding="utf-8")
    return text.strip()


def print_evening_summary(text: str, *, report_path: Path | None = None) -> None:
    print("")
    print("=== 今日收盤摘要（Perplexity · 讀 DB 上下文）===")
    for line in text.splitlines():
        print(f"  {line}" if line.strip() else "")
    if report_path and report_path.exists():
        print(f"  → 已存 {report_path}")


def _events_for_verify(
    conn,
    etf_codes: tuple[str, ...],
    *,
    max_n: int,
) -> list:
    universe = build_research_universe(conn, etf_codes)
    pool = set(universe.stock_ids) if universe else None
    if not pool:
        return []
    rows = load_catalyst_events(
        conn,
        stock_ids=list(pool) if pool else None,
        window_days=int(os.environ.get("NEWS_LOOKBACK_DAYS", "7")),
    )
    by_id: dict[str, tuple] = {}
    for row in rows:
        ev = row_to_catalyst_event(row)
        eid = catalyst_event_id(ev)
        src = row["source"] if "source" in row.keys() else "unknown"
        by_id[eid] = (ev, src)

    ordered = sorted(
        by_id.values(),
        key=lambda x: (0 if x[1] == "perplexity" else 1, -x[0].confidence),
    )
    return ordered[:max_n]


def run_event_verification(
    conn,
    etf_codes: tuple[str, ...],
    *,
    max_events: int = 8,
    apply_db: bool = False,
) -> list[VerifyResult]:
    cfg = get_config(model_env="PERPLEXITY_VERIFY_MODEL")
    if cfg is None:
        raise RuntimeError("PERPLEXITY_API_KEY 未設定")

    packed = _events_for_verify(conn, etf_codes, max_n=max_events)
    if not packed:
        return []

    payload = [
        {
            "event_id": catalyst_event_id(ev),
            "stock_id": ev.stock_id,
            "event_date": ev.event_date.isoformat(),
            "catalyst_type": ev.catalyst_type,
            "headline": ev.headline,
            "confidence": ev.confidence,
            "source": src,
        }
        for ev, src in packed
    ]
    today = date.today().isoformat()
    prompt = (
        f"今天是 {today}。請用公開新聞/公告查證下列台股催化事件是否屬實。\n"
        f"事件 JSON：{json.dumps(payload, ensure_ascii=False)}\n\n"
        "僅回傳 JSON："
        '{"checks":[{"event_id":"...","status":"CONFIRMED|PARTIAL|UNCONFIRMED|RUMOR",'
        '"note":"≤60字","confidence_delta":整數}]}\n'
        "規則：CONFIRMED=多來源一致 +10；PARTIAL=部分成立 0；"
        "UNCONFIRMED=找不到可靠來源 -15；RUMOR=傳聞/社群 -25。"
        "禁止 BUY/HOLD/TRIM、目標價。"
    )
    raw = chat_completion(
        [
            {
                "role": "system",
                "content": "你是事實查核助理，只輸出 JSON。",
            },
            {"role": "user", "content": prompt},
        ],
        cfg=cfg,
        temperature=0.1,
    )
    parsed = extract_json_payload(raw)
    checks: list[dict] = []
    if isinstance(parsed, dict):
        raw_checks = parsed.get("checks") or parsed.get("verifications") or []
        if isinstance(raw_checks, list):
            checks = [c for c in raw_checks if isinstance(c, dict)]
    elif isinstance(parsed, list):
        checks = [c for c in parsed if isinstance(c, dict)]

    ev_by_id = {catalyst_event_id(ev): ev for ev, _src in packed}
    results: list[VerifyResult] = []
    db_rows: list[dict] = []

    for item in checks:
        eid = str(item.get("event_id", "")).strip()
        ev = ev_by_id.get(eid)
        if ev is None:
            continue
        status = str(item.get("status", "PARTIAL")).upper()
        if status not in VERIFY_STATUSES:
            status = "PARTIAL"
        try:
            delta = int(item.get("confidence_delta", 0))
        except (TypeError, ValueError):
            delta = {"CONFIRMED": 10, "PARTIAL": 0, "UNCONFIRMED": -15, "RUMOR": -25}.get(
                status, 0
            )
        note = str(item.get("note", ""))[:60]
        old = ev.confidence
        new = max(0, min(100, old + delta))
        results.append(
            VerifyResult(
                event_id=eid,
                stock_id=ev.stock_id,
                headline=ev.headline,
                status=status,
                note=note,
                confidence_delta=delta,
                old_confidence=old,
                new_confidence=new,
            )
        )
        if apply_db and new != old:
            updated = CatalystEvent(
                stock_id=ev.stock_id,
                event_date=ev.event_date,
                catalyst_type=ev.catalyst_type,
                headline=ev.headline,
                polarity=ev.polarity,
                explains_etf_add=ev.explains_etf_add,
                confidence=new,
                sources=ev.sources,
            )
            row = event_to_row(updated)
            # preserve source from DB
            src_row = conn.execute(
                "SELECT source FROM catalyst_events WHERE event_id = ?",
                (eid,),
            ).fetchone()
            if src_row:
                row["source"] = src_row["source"]
            db_rows.append(row)

    if apply_db and db_rows:
        upsert_catalyst_events(conn, db_rows)

    return results


def print_verification_report(results: list[VerifyResult], *, applied: bool) -> None:
    print("")
    print("=== 催化查證（Perplexity · 事實確認）===")
    if not results:
        print("  無可查證事件（請依 reports/*_evening_brief.md 自行上網查；或 RUN_NEWS_SYNC=1）")
        return
    for r in results:
        print(
            f"  [{r.status:12}] {r.stock_id} conf {r.old_confidence}→{r.new_confidence} "
            f"| {r.headline[:36]} … {r.note}"
        )
    if applied:
        print("  → 已依查證結果更新 catalyst_events.confidence")
    else:
        print("  → 僅顯示（加 --apply-confidence 寫入 DB）")


def main() -> int:
    parser = argparse.ArgumentParser(description="Perplexity 收盤摘要 / 查證")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--report", action="store_true", help="印到終端")
    parser.add_argument(
        "--apply-confidence",
        action="store_true",
        help="查證後寫回 confidence",
    )
    parser.add_argument(
        "--verify-max",
        type=int,
        default=int(os.environ.get("PERPLEXITY_VERIFY_MAX", "8")),
    )
    args = parser.parse_args()

    if not args.summary and not args.verify:
        parser.error("請指定 --summary 或 --verify")

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        if args.summary:
            text = run_evening_summary(conn, codes)
            if args.report:
                rpath = REPORTS_DIR / f"{date.today().strftime('%Y%m%d')}_evening_summary.md"
                print_evening_summary(text, report_path=rpath)

        if args.verify:
            results = run_event_verification(
                conn,
                codes,
                max_events=args.verify_max,
                apply_db=args.apply_confidence,
            )
            if args.report:
                print_verification_report(results, applied=args.apply_confidence)
            elif args.apply_confidence and results:
                print(f"  catalyst verify: 更新 {len(results)} 筆 confidence", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"  WARN Perplexity: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"  SKIP: {exc}", file=sys.stderr)
        return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
