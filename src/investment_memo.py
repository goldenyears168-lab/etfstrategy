#!/usr/bin/env python3
"""
P4 L9 Investment Memo：觀察名單 A Top10（無 A 則總分 Top10 草稿）敘事備忘。

規則評級已在 score_engine；本模組禁止 BUY/HOLD/TRIM、目標價。
輸出：reports/YYYYMMDD_memo.md + research_memos 表。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

from event_ranking import load_all_catalyst_events, score_event
from market_labels import WL_PRIMARY
from stock_db import (
    DATA_DIR,
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    load_latest_consensus_map,
    load_latest_fundamental_map,
    load_latest_tech_risk,
    load_memo_candidates,
    upsert_research_memos,
)

REPORTS_DIR = PROJECT_ROOT / "reports"
MEMO_TOP_N = 10

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("BUY", re.compile(r"\bBUY\b", re.I)),
    ("HOLD", re.compile(r"\bHOLD\b", re.I)),
    ("TRIM", re.compile(r"\bTRIM\b", re.I)),
    ("目標價", re.compile(r"目標價")),
    ("建議買", re.compile(r"建議買|建議賣")),
    ("部位%", re.compile(r"部位\s*[%％]|配置\s*[%％]")),
]


def audit_memo_text(text: str) -> tuple[bool, list[str]]:
    notes: list[str] = []
    for label, pat in FORBIDDEN_PATTERNS:
        if pat.search(text):
            notes.append(label)
    return (len(notes) == 0, notes)


def _parse_metadata(row) -> dict:
    raw = row["metadata_json"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def build_stock_context(
    conn,
    row,
    *,
    events_path: Path | None = None,
) -> dict:
    sid = row["stock_id"]
    fund = load_latest_fundamental_map(conn, [sid]).get(sid)
    cons = load_latest_consensus_map(conn, [sid]).get(sid, {})
    events = [
        {
            "date": e.event_date.isoformat(),
            "type": e.catalyst_type,
            "headline": e.headline,
            "explains_etf_add": e.explains_etf_add,
            "score": round(score_event(e), 3),
        }
        for e in load_all_catalyst_events(conn, events_path, pool_stock_ids={sid})
    ]
    tech = load_latest_tech_risk(conn)
    tech_slice = None
    if tech is not None:
        tech_slice = {
            "session_date": tech["session_date"],
            "tsm_daily_return_pct": tech["tsm_daily_return_pct"],
            "tx_gap_pct": tech["tx_gap_pct"],
        }
    return {
        "stock_id": sid,
        "stock_name": row["stock_name"],
        "watchlist": row["watchlist"],
        "investment_score": row["investment_score"],
        "dimensions": {
            "smart_money": row["smart_money"],
            "catalyst": row["catalyst"],
            "expectation": row["expectation"],
            "fundamental": row["fundamental"],
            "risk": row["risk"],
        },
        "score_metadata": _parse_metadata(row),
        "fundamental": dict(fund) if fund else None,
        "consensus": cons,
        "catalyst_events": events,
        "tech_risk": tech_slice,
        "pool_reason": row["pool_reason"],
        "position_intent": row["position_intent"],
    }


def render_template_section(ctx: dict) -> str:
    d = ctx["dimensions"]
    lines = [
        f"## {ctx['stock_id']} {ctx['stock_name'] or ''}",
        "",
        f"**綜合研究評分 {ctx['investment_score']:.1f}** · 觀察名單 **{ctx['watchlist']}**",
        f"（規則評級；本節 AI/模板不重複給予買賣建議）",
        "",
        "| 維度 | 分數 |",
        "|------|------|",
        f"| Smart Money | {d['smart_money']:.0f} |",
        f"| Catalyst | {d['catalyst']:.0f} |",
        f"| Expectation | {d['expectation']:.0f} |",
        f"| Fundamental | {d['fundamental']:.0f} |",
        f"| Risk | {d['risk']:.0f} |",
        "",
        "### 理由（結構化摘要）",
    ]
    if ctx.get("position_intent"):
        lines.append(f"- 部位意圖（L5）：{ctx['position_intent']}")
    if ctx.get("pool_reason"):
        lines.append(f"- 進入 Universe：{ctx['pool_reason']}")
    meta = ctx.get("score_metadata") or {}
    exp_d = meta.get("expectation_detail") or {}
    if exp_d:
        lines.append(f"- 預期差：{json.dumps(exp_d, ensure_ascii=False)}")
    for ev in (ctx.get("catalyst_events") or [])[:2]:
        lines.append(
            f"- 催化 [{ev['type']}] {ev['headline']}（explains={ev['explains_etf_add']}）"
        )
    if not ctx.get("catalyst_events"):
        lines.append("- 催化：近 7 日無結構化事件入庫")

    lines.extend(
        [
            "",
            "### Bull Case",
            "- 資金/共識面向分數偏高時，敘述 ETF 加碼與產業邏輯一致性（需自行核對來源）。",
            "",
            "### Bear Case",
            "- 留意預期差轉弱、Risk 子分偏低或科技風險哨（TSM/台指 gap）不利。",
            "",
            "### 與 ETF 加碼關聯",
        ]
    )
    explains = [e["explains_etf_add"] for e in ctx.get("catalyst_events") or []]
    if "HIGH" in explains:
        lines.append("- 事件標記 explains_etf_add=HIGH，與持股加碼敘事一致度較高。")
    else:
        lines.append("- 尚無 HIGH 等級催化標記；以 Smart Money / 基本面分數為主軸說明。")
    if ctx.get("tech_risk"):
        tr = ctx["tech_risk"]
        tsm = tr.get("tsm_daily_return_pct")
        if tsm is not None:
            lines.append(f"- 科技風險哨 TSM ADR {tsm:+.2f}%（session {tr.get('session_date')}）。")
    lines.append("")
    return "\n".join(lines)


def llm_enrich_section(ctx: dict) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    prompt = (
        "撰寫台股研究備忘片段（Markdown）。任務：理由條列、Bull、Bear、與 ETF 行為一致性。"
        "禁止：BUY/HOLD/TRIM、目標價、部位%、投資評級。"
        "輸入 JSON：\n"
        + json.dumps(ctx, ensure_ascii=False, default=str)
    )
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.4,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是研究助理，只寫分析不給交易指令。",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=120,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        ok, notes = audit_memo_text(text)
        if not ok:
            print(f"  WARN LLM memo {ctx['stock_id']}: 審計未過 {notes}", file=sys.stderr)
            return None
        return text.strip()
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"  WARN LLM memo {ctx['stock_id']}: {exc}", file=sys.stderr)
        return None


def generate_memo_document(
    conn,
    *,
    as_of_date: str | None = None,
    events_path: Path | None = None,
    use_llm: bool = False,
    top_n: int = MEMO_TOP_N,
) -> tuple[str, list[dict], str]:
    candidates = load_memo_candidates(conn, as_of_date=as_of_date, top_n=top_n)
    if not candidates:
        raise RuntimeError("無 investment_scores；請先 RUN_SCORE_ENGINE=1")

    memo_date = candidates[0]["as_of_date"]
    has_primary = any(r["watchlist"] == WL_PRIMARY for r in candidates)
    sections: list[str] = [
        f"# Investment Memo · {memo_date}",
        "",
        "> 觀察名單由 **score_engine 規則** 決定；本文僅供研究敘事，非交易建議。",
        "",
    ]
    if not has_primary:
        sections.append(
            "> 備註：目前無觀察名單 **首要觀察**，以下為同 as_of 綜合評分 Top 草稿。\n"
        )

    db_rows: list[dict] = []
    for rank, row in enumerate(candidates, start=1):
        ctx = build_stock_context(conn, row, events_path=events_path)
        body = render_template_section(ctx)
        llm_used = 0
        if use_llm:
            enriched = llm_enrich_section(ctx)
            if enriched:
                body = enriched
                llm_used = 1
        ok, notes = audit_memo_text(body)
        if not ok:
            body = render_template_section(ctx)
            llm_used = 0
            audit_notes = f"fallback: {','.join(notes)}"
        else:
            audit_notes = ""
        sections.append(body)
        db_rows.append(
            {
                "memo_date": memo_date,
                "stock_id": row["stock_id"],
                "rank": rank,
                "watchlist": row["watchlist"],
                "investment_score": row["investment_score"],
                "body_md": body,
                "context_json": json.dumps(ctx, ensure_ascii=False, default=str),
                "llm_used": llm_used,
                "audit_passed": 1 if ok or llm_used == 0 else 0,
                "audit_notes": audit_notes or None,
            }
        )

    doc = "\n".join(sections)
    ok_doc, doc_notes = audit_memo_text(doc)
    if not ok_doc:
        raise RuntimeError(f"Memo 審計未通過：{doc_notes}")
    return doc, db_rows, memo_date


def write_memo_report(doc: str, memo_date: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{memo_date.replace('-', '')}_memo.md"
    out.write_text(doc, encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="L9 Investment Memo")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--as-of", default=None, help="investment_scores.as_of_date")
    parser.add_argument("--events-file", type=Path, default=DATA_DIR / "manual_events.json")
    parser.add_argument("--sync-db", action="store_true")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--top-n", type=int, default=MEMO_TOP_N)
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        doc, rows, memo_date = generate_memo_document(
            conn,
            as_of_date=args.as_of,
            events_path=args.events_file,
            use_llm=args.use_llm,
            top_n=args.top_n,
        )
        out_path = write_memo_report(doc, memo_date)
        print(f"Memo 已寫入 {out_path}")
        if args.sync_db:
            n = upsert_research_memos(conn, rows)
            print(f"  DB：research_memos upsert {n} 列")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
