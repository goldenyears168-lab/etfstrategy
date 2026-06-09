"""收盤研究上下文 JSON + 外部 LLM 決策提示詞（規則產出 · 不寫回 watchlist）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from briefing_builder import build_decision_summary, build_pm_briefing
from position_review import build_all_books_review, build_position_exit_summary
from event_ranking import is_index_rebalance_event, load_all_catalyst_events
from holdings_research import build_cross_etf_consensus
from operational_brief import build_operational_brief_block
from research_universe import build_research_universe
from score_engine import SCORE_VERSION
from consensus_trend import consensus_trend_summary
from etf_signal_performance import build_etf_signal_performance
from market_analytics import build_analytics_map
from signal_engine import build_signal_layers_block
from stock_db import (
    PROJECT_ROOT,
    list_etf_snapshot_dates,
    load_latest_pm_watchlist,
    load_latest_portfolio_weights,
    load_latest_tech_risk,
)

REPORTS_DIR = PROJECT_ROOT / "reports"

DATA_POLICY = (
    "規則引擎欄位（watchlist、pm_bucket、portfolio_weight_pct、entry_signal）"
    "為權威數據；LLM 不得建議覆寫。觀點僅供研究備忘。"
)

ALGORITHM_MARKDOWN = """## 算法摘要（p4-v2）

### 綜合研究評分
```
investment_score = 0.50×smart_money + 0.10×catalyst + 0.15×expectation
                 + 0.15×fundamental + 0.10×risk
smart_money = 0.55×flow + 0.45×chip
```
價位分（timing）不進綜合評分。

### 觀察名單（規則）
- 首要觀察：綜合評分≥75 且資金籌碼≥72，且非暫不進場；乖離過大且非量價齊揚時封頂 **一般觀察**
- 一般觀察：綜合評分≥65
- 候選：綜合評分≥55
- 不列入：其餘或暫不進場

### 價位型態
- **乖離過大**：Universe 內延伸度 ≥ max(ENTRY_OVEREXTENDED_ABS_MIN, P75 分位)
- **突破**：52週位>90 且距52週高>-3%，且未乖離過大
- **量價齊揚**：乖離過大 + flow≥65 + chip≥70 + 量未縮
- **暫不進場**（風控覆寫）：ETF 淨方向減碼

### 隔日等級（pm_watchlist）
- 突破 + 在觀察名單 → **突破**
- 乖離過大 + 高籌碼（外資、投信同步買超／外資買超或 chip≥70）→ **觀察**（觀察名單可仍 不列入／一般觀察）
- 其餘依資金／籌碼門檻 → **觀察** 或 **回避**

### 建議部位
- 乖離過大且非量價齊揚 → 權重 0%（不宜追價）
"""

PROMPT_SYSTEM_DECISION = """你是台股 ETF 持股研究決策助理。pm_briefing 與 decision_summary 為規則引擎預算底稿；你負責敘事潤飾、新聞查證與隔日備忘撰稿。

權威順序（由高到低）：
1) decision_summary 內欄位：watchlist、pm_bucket、portfolio_weight_pct、entry_signal（不可改寫、不可建議覆寫）
2) pm_briefing（Top 觀察／共識擴張／資金集中／矛盾預算／隔日焦點）
3) catalyst_events（產業類；已排除 MSCI/指數調整）
4) §3 待查新聞查證（僅限有來源之聯網結果；不得憑空補故事）
5) 你的敘事與 §5 隔日觀察補充（不得推翻 pm_briefing.contradictions）

禁止：不得依 catalyst／新聞／常識暗示升級 watchlist、加碼、布局或覆寫 decision_summary。

待查新聞（news_verify）：
- 若你可聯網搜尋：必須依 JSON.news_verify 逐檔搜索（優先用 search_query；可參考 google_search_url / yahoo_news_url），近 7 日台股／產業新聞為主
- 每則引用須附來源 URL 與日期；查無則寫「查無近 7 日可解釋新聞」，不得編造標題
- 查證結果不得升級 watchlist、pm_bucket 或 portfolio_weight_pct；僅解釋「Why 可能為何」
- 若你無法聯網：§3 只列待查清單與建議關鍵字，並明寫「未執行搜索，不得臆測新聞內容」

硬性禁止：
- 不得輸出 BUY / HOLD / TRIM、目標價、建議部位%、「升級為首要觀察」等覆寫規則名單的結論
- 不得以 MSCI / 指數成分 / 權重調整 / 被動資金作為主線或第一條結論
- 不得捏造 JSON 中不存在的法人數據；不得在無搜尋來源時捏造新聞、法說日期

輸出語言：繁體中文。語氣：基金經理備忘，簡潔、可執行。"""

PROMPT_USER_EVENING = """以下 JSON 為今日收盤後規則引擎預算的 PM 研究底稿（非模型評分）。請依 pm_briefing 撰稿，不得重新排序或覆寫規則欄位。

<<<RESEARCH_JSON
{research_json}
RESEARCH_JSON>>>

請依序輸出以下 Markdown（不要輸出 JSON）：

# 收盤決策備忘 · {as_of_date}

## 1. ETF Flow（誰在買）
- 隔夜風險：tech_risk（TSM / SOX / 台指 gap / 電子期）≤2 句
- 資金集中：轉述 pm_briefing.capital_concentration（flow／加碼檔數／ETF）
- 重要觀察：轉述 pm_briefing.top_observations（L2／conviction／意圖）
- 可選：etf_signal_performance 各 ETF H+20 勝率（樣本≥3 才列）

## 2. Consensus（共識是否擴散）
- 轉述 pm_briefing.consensus_expansion（擴張標的與加碼檔數）
- 若為空：寫「今日無多檔共識擴張」

## 3. Why（新聞驗證 · news_verify · 須聯網搜尋）
對 JSON.news_verify 每一項（若為空則寫「今日無待查標的」）：
| 代號 | 系統待查原因 | 查證結論 | 近 7 日要點（附 URL） | 能否解釋 ETF 加碼 |
查證結論僅能：**已找到** / **查無** / **inconclusive** / **未執行搜索**
- 已找到：1–3 則 bullet（標題、日期、URL）
- 查無：標「可能為資金面驅動」
- 不得以查證結果修改 watchlist / pm_bucket / 權重
- 對照 catalyst_events（若空則略）

## 4. Contradiction（與市場認知不同處）
- **必須**逐條轉述 pm_briefing.contradictions（etf_side vs rule_side；reason_codes + narrative_hints）
- 若為空：寫「今日無顯著矛盾」
- 不得改名單或建議覆寫規則

## 5. Tomorrow（隔日觀察）
- 轉述 pm_briefing.tomorrow_watch（每檔 1 句觀察重點）
- 規則底稿摘要：decision_summary 中 portfolio_weight_pct > 0 者（權重／隔日等級）
- 建議動作僅能：觀察 / 等拉回 / 突破確認後小倉 / 不交易（最多 5 檔）
- 持倉賣出雷達：轉述 position_exit_summary（減碼／出清觀察；不得覆寫為下單指令）"""

PROMPT_USER_MORNING = """以下是昨晚規則產出與今早風控更新。請只做「是否執行」判斷。

<<<OVERNIGHT_JSON
{overnight_json}
OVERNIGHT_JSON>>>

<<<MORNING_JSON
{morning_json}
MORNING_JSON>>>

# 早盤執行結論 · {as_of_date}

## 規則底稿（轉述）
## 風控是否通過（是/否）
## 今日動作表（最多 3 檔；執行欄：下單/小倉試單/取消/續觀察）
## 若全部不交易，寫明單一主因（≤20 字）"""


def _parse_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _holdings_meta(conn: sqlite3.Connection, etf_codes: tuple[str, ...]) -> list[dict]:
    rows: list[dict] = []
    for code in etf_codes:
        dates = list_etf_snapshot_dates(conn, code)
        rows.append(
            {
                "etf_code": code,
                "latest_snapshot": dates[-1] if dates else None,
                "snapshot_count": len(dates),
            }
        )
    return rows


def _consensus_block(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    top_single: int = 10,
) -> list[dict]:
    stocks = build_cross_etf_consensus(conn, etf_codes)
    multi = [s for s in stocks if s.etf_add >= 2]
    multi.sort(key=lambda s: abs(s.flow_ntd or 0.0), reverse=True)
    single = [s for s in stocks if s.etf_add == 1]
    single.sort(key=lambda s: abs(s.flow_ntd or 0.0), reverse=True)

    def _row(s) -> dict:
        return {
            "stock_id": s.stock_id,
            "stock_name": s.stock_name,
            "etf_add_count": s.etf_add,
            "etf_reduce_count": s.etf_reduce,
            "etf_add_list": list(s.etf_add_list),
            "flow_ntd": s.flow_ntd,
        }

    out = [_row(s) for s in multi]
    out.extend(_row(s) for s in single[:top_single])
    return out


def _enrich_decisions_analytics(
    conn: sqlite3.Connection,
    decisions: list[dict],
    scores: list[dict],
) -> list[dict]:
    """合併 RS／籌碼／盈餘 proxy／R:R 至 decisions（metadata 優先）。"""
    analytics_by_id: dict[str, dict] = {}
    entry_tags_by_id: dict[str, list] = {}
    for s in scores:
        sid = s["stock_id"]
        if s.get("analytics"):
            analytics_by_id[sid] = s["analytics"]
        if s.get("entry_tags"):
            entry_tags_by_id[sid] = s["entry_tags"]

    missing = [d["stock_id"] for d in decisions if d["stock_id"] not in analytics_by_id]
    if missing:
        entry_by_id = {d["stock_id"]: d.get("entry_signal") for d in decisions}
        score_by_id = {
            d["stock_id"]: float(d["total"])
            for d in decisions
            if d.get("total") is not None
        }
        for sid, ana in build_analytics_map(
            conn, missing, entry_by_id=entry_by_id, score_by_id=score_by_id
        ).items():
            analytics_by_id[sid] = ana.to_dict()

    for d in decisions:
        sid = d["stock_id"]
        for k, v in analytics_by_id.get(sid, {}).items():
            if k != "stock_id" and v is not None and k not in d:
                d[k] = v
        if sid in entry_tags_by_id:
            d["entry_tags"] = entry_tags_by_id[sid]
    return decisions


def _enrich_decisions_consensus_trend(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    decisions: list[dict],
) -> list[dict]:
    for d in decisions:
        sid = d["stock_id"]
        for k, v in consensus_trend_summary(conn, etf_codes, sid).items():
            if v is not None:
                d[k] = v
    return decisions


def _pm_watchlist_block(conn: sqlite3.Connection) -> list[dict]:
    rows: list[dict] = []
    for r in load_latest_pm_watchlist(conn, score_version=SCORE_VERSION):
        rows.append(
            {
                "stock_id": r["stock_id"],
                "stock_name": r["stock_name"],
                "watchlist": r["watchlist"],
                "pm_bucket": r["pm_bucket"],
                "entry_signal": r["entry_signal"],
                "chip_tag": r["chip_tag"],
                "investment_score": r["investment_score"],
            }
        )
    return rows


def _build_decisions_block(
    scores: list[dict],
    pm_rows: list[dict],
    portfolio_rows: list[dict],
    universe_rows: list[dict],
    *,
    consensus_stock_ids: list[str] | None = None,
) -> list[dict]:
    """合併 pm_watchlist、評分、部位配置為單表（pm_watchlist 為 superset）。"""
    score_by_id = {r["stock_id"]: r for r in scores}
    pm_by_id = {r["stock_id"]: r for r in pm_rows}
    pw_by_id = {r["stock_id"]: r for r in portfolio_rows}
    uni_by_id = {r["stock_id"]: r for r in universe_rows}
    stock_ids = list(
        dict.fromkeys(
            [r["stock_id"] for r in pm_rows]
            + list(pw_by_id)
            + [r["stock_id"] for r in scores]
            + [r["stock_id"] for r in universe_rows]
            + (consensus_stock_ids or [])
        )
    )

    decisions: list[dict] = []
    for sid in stock_ids:
        sc = score_by_id.get(sid, {})
        pm = pm_by_id.get(sid, {})
        pw = pw_by_id.get(sid, {})
        uni = uni_by_id.get(sid, {})
        row: dict[str, Any] = {
            "stock_id": sid,
            "stock_name": (
                pm.get("stock_name")
                or pw.get("stock_name")
                or sc.get("name")
                or uni.get("name")
            ),
            "watchlist": pw.get("watchlist") or pm.get("watchlist") or sc.get("watchlist"),
            "pm_bucket": pm.get("pm_bucket") or pw.get("pm_bucket"),
            "portfolio_weight_pct": pw.get("portfolio_weight_pct"),
            "suggested_ntd": pw.get("suggested_ntd"),
            "entry_signal": (
                pm.get("entry_signal") or pw.get("entry_signal") or sc.get("entry_signal")
            ),
            "total": sc.get("total") or pm.get("investment_score"),
            "smart_money": sc.get("smart_money"),
            "risk": sc.get("risk"),
            "risk_gate": sc.get("risk_gate"),
            "position_intent": sc.get("position_intent"),
            "chip_tag": pm.get("chip_tag") or sc.get("chip_tag"),
            "money_rank": sc.get("money_rank") or uni.get("money_rank"),
            "pool_reason": sc.get("pool_reason") or uni.get("reason"),
            "headline": uni.get("headline"),
        }
        decisions.append({k: v for k, v in row.items() if v is not None})

    decisions.sort(
        key=lambda r: (
            r.get("total") is None,
            -(float(r["total"]) if r.get("total") is not None else 0.0),
        )
    )
    return decisions


def build_research_context(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    universe = build_research_universe(conn, etf_codes)
    pool = set(universe.stock_ids) if universe else set()

    tech = load_latest_tech_risk(conn)
    tech_block = None
    if tech is not None:
        tech_block = {
            "session_date": tech["session_date"],
            "tsm_pct": tech["tsm_daily_return_pct"],
            "sox_pct": tech["sox_daily_return_pct"],
            "tx_gap_pct": tech["tx_gap_pct"],
            "te_overnight_pct": tech["te_overnight_pct"],
        }

    score_as_of = as_of_date
    scores: list[dict] = []
    try:
        if not score_as_of:
            row = conn.execute(
                "SELECT MAX(as_of_date) AS d FROM investment_scores "
                "WHERE score_version = ?",
                (SCORE_VERSION,),
            ).fetchone()
            score_as_of = row["d"] if row else None
        if score_as_of:
            for r in conn.execute(
                """
                SELECT stock_id, stock_name, investment_score, watchlist,
                       smart_money, catalyst, expectation, fundamental, risk,
                       metadata_json, position_intent, pool_reason, money_rank
                FROM investment_scores
                WHERE as_of_date = ? AND score_version = ?
                ORDER BY investment_score DESC
                LIMIT 20
                """,
                (score_as_of, SCORE_VERSION),
            ).fetchall():
                meta = _parse_metadata(r["metadata_json"])
                scores.append(
                    {
                        "stock_id": r["stock_id"],
                        "name": r["stock_name"],
                        "total": r["investment_score"],
                        "watchlist": r["watchlist"],
                        "smart_money": r["smart_money"],
                        "catalyst": r["catalyst"],
                        "expectation": r["expectation"],
                        "fundamental": r["fundamental"],
                        "risk": r["risk"],
                        "position_intent": r["position_intent"],
                        "pool_reason": r["pool_reason"],
                        "money_rank": r["money_rank"],
                        "entry_signal": meta.get("entry_signal"),
                        "entry_tags": meta.get("entry_tags", []),
                        "flow_score": meta.get("flow_score"),
                        "chip_score": meta.get("chip_score"),
                        "chip_tag": meta.get("chip_tag"),
                        "timing_score": meta.get("timing_score"),
                        "risk_gate": meta.get("risk_gate"),
                        "analytics": meta.get("analytics"),
                    }
                )
    except sqlite3.OperationalError:
        pass

    all_events = load_all_catalyst_events(conn, pool_stock_ids=pool or None)
    industry_events = [e for e in all_events if not is_index_rebalance_event(e)]
    events = sorted(industry_events, key=lambda e: e.confidence, reverse=True)[:10]
    event_block = [
        {
            "stock_id": e.stock_id,
            "date": e.event_date.isoformat(),
            "type": e.catalyst_type,
            "headline": e.headline,
            "confidence": e.confidence,
            "explains_etf_add": e.explains_etf_add,
        }
        for e in events
    ]

    portfolio_block: list[dict] = []
    for row in load_latest_portfolio_weights(conn, score_version=SCORE_VERSION):
        portfolio_block.append(
            {
                "stock_id": row["stock_id"],
                "stock_name": row["stock_name"],
                "watchlist": row["watchlist"],
                "pm_bucket": row["pm_bucket"],
                "portfolio_weight_pct": row["portfolio_weight_pct"],
                "suggested_ntd": row["suggested_ntd"],
                "entry_signal": row["entry_signal"],
            }
        )

    universe_block: list[dict] = []
    if universe:
        for ent in universe.entries[:20]:
            universe_block.append(
                {
                    "stock_id": ent.stock_id,
                    "name": ent.stock_name,
                    "reason": ent.pool_reason,
                    "money_rank": ent.money_rank,
                    "headline": ent.headline,
                }
            )

    ctx_as_of = score_as_of or (
        universe.curr_date if universe else date.today().isoformat()
    )

    brief_block = build_operational_brief_block(conn, etf_codes)
    pm_block = _pm_watchlist_block(conn)
    cross_etf = _consensus_block(conn, etf_codes)
    consensus_top_ids = [r["stock_id"] for r in cross_etf[:5]]

    ctx: dict[str, Any] = {
        "as_of_date": ctx_as_of,
        "universe_window": (
            f"{universe.prev_date} → {universe.curr_date}" if universe else None
        ),
        "tech_risk": tech_block,
        "signal_layers": build_signal_layers_block(conn, etf_codes),
        "cross_etf_consensus": cross_etf,
        "decisions": _enrich_decisions_consensus_trend(
            conn,
            etf_codes,
            _enrich_decisions_analytics(
                conn,
                _build_decisions_block(
                    scores,
                    pm_block,
                    portfolio_block,
                    universe_block,
                    consensus_stock_ids=consensus_top_ids,
                ),
                scores,
            ),
        ),
        "etf_signal_performance": [
            r.to_dict() for r in build_etf_signal_performance(conn, etf_codes)
        ],
        "catalyst_events": event_block,
        "news_verify": brief_block["news_verify"],
        "next_day_checklist": brief_block["next_day_checklist"],
        "appendix": {
            "score_version": SCORE_VERSION,
            "etf_codes": list(etf_codes),
            "holdings_meta": _holdings_meta(conn, etf_codes),
            "data_policy": DATA_POLICY,
            "catalyst_events_note": (
                "industry_only; INDEX_REBALANCE/MSCI excluded from JSON"
            ),
            "news_verify_note": brief_block["news_verify_note"],
        },
    }
    ctx["pm_briefing"] = build_pm_briefing(ctx)
    book_reviews = build_all_books_review(conn, etf_codes=etf_codes)
    ctx["position_exit_summary"] = build_position_exit_summary(book_reviews)
    return ctx


def build_evening_context(conn, etf_codes: tuple[str, ...]) -> dict:
    """Perplexity 收盤摘要用（與 research_context 同構，向後相容）。"""
    return build_research_context(conn, etf_codes)


@dataclass(frozen=True)
class LlmPrompts:
    as_of_date: str
    system: str
    user_evening: str
    user_morning: str
    evening_full: str


def _etf_performance_for_llm(rows: list[dict] | None) -> list[dict] | None:
    if not rows:
        return None
    useful = [r for r in rows if int(r.get("sample_n") or 0) >= 3]
    return useful or None


def build_llm_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    """LLM 輸入：策展 briefing + decision_summary（完整 decisions 見 research_context.json）。"""
    briefing = ctx.get("pm_briefing") or build_pm_briefing(ctx)
    decisions = ctx.get("decisions") or []
    payload: dict[str, Any] = {
        "as_of_date": ctx.get("as_of_date"),
        "universe_window": ctx.get("universe_window"),
        "tech_risk": ctx.get("tech_risk"),
        "pm_briefing": briefing,
        "decision_summary": build_decision_summary(decisions),
        "position_exit_summary": ctx.get("position_exit_summary") or {},
        "news_verify": ctx.get("news_verify"),
        "catalyst_events": ctx.get("catalyst_events"),
        "appendix": {
            "data_policy": (ctx.get("appendix") or {}).get("data_policy", DATA_POLICY),
            "full_context_note": (
                "完整 decisions／signal_layers 見 reports/*_research_context.json"
            ),
        },
    }
    perf = _etf_performance_for_llm(ctx.get("etf_signal_performance"))
    if perf:
        payload["etf_signal_performance"] = perf
    return payload


def build_llm_prompts(ctx: dict[str, Any]) -> LlmPrompts:
    """由 research_context 組裝可貼給外部 LLM 的提示詞。"""
    as_of = ctx.get("as_of_date") or date.today().isoformat()
    research_json = json.dumps(build_llm_payload(ctx), ensure_ascii=False, indent=2)
    overnight = {
        "as_of_date": as_of,
        "decision_summary": build_decision_summary(ctx.get("decisions") or []),
        "tech_risk": ctx.get("tech_risk"),
    }
    overnight_json = json.dumps(overnight, ensure_ascii=False, indent=2)
    system = PROMPT_SYSTEM_DECISION.strip()
    user_evening = PROMPT_USER_EVENING.format(
        research_json=research_json,
        as_of_date=as_of,
    ).strip()
    user_morning = PROMPT_USER_MORNING.format(
        overnight_json=overnight_json,
        morning_json='{"note": "填入今早 TSM/大盤/開盤量價"}',
        as_of_date=as_of,
    ).strip()
    evening_full = (
        f"[SYSTEM]\n{system}\n\n[USER]\n{user_evening}\n"
    )
    return LlmPrompts(
        as_of_date=as_of,
        system=system,
        user_evening=user_evening,
        user_morning=user_morning,
        evening_full=evening_full,
    )


def write_evening_prompt_file(
    prompts: LlmPrompts,
    *,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    """收盤 LLM 唯一提示詞檔（SYSTEM + USER 合併）。"""
    stamp = prompts.as_of_date.replace("-", "")
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{stamp}_prompt_evening_full.txt"
    path.write_text(prompts.evening_full + "\n", encoding="utf-8")
    return path


def print_llm_prompts_cli(prompts: LlmPrompts) -> None:
    """終端輸出可一鍵複製的提示詞（收盤 + 早盤）。"""
    print("")
    print("=== LLM 提示詞 · 收盤決策（依序貼：SYSTEM → USER）===")
    print("")
    print("----- SYSTEM -----")
    print(prompts.system)
    print("")
    print("----- USER · 收盤決策備忘 -----")
    print(prompts.user_evening)
    print("")
    print("=== LLM 提示詞 · 早盤執行（隔日開盤前 · SYSTEM 同上）===")
    print("")
    print("----- USER · 早盤 -----")
    print(prompts.user_morning)
    print("")
    print(
        "  單檔合併（部分 API 一欄位）→ "
        f"reports/{prompts.as_of_date.replace('-', '')}_prompt_evening_full.txt"
    )


def build_ai_bundle_markdown(ctx: dict[str, Any]) -> str:
    """組裝 ai_bundle.md：算法 + 提示詞 + 內嵌 JSON。"""
    as_of = ctx.get("as_of_date") or date.today().isoformat()
    research_json = json.dumps(ctx, ensure_ascii=False, indent=2)
    prompts = build_llm_prompts(ctx)

    parts = [
        f"# AI 研究決策包 · {as_of.replace('-', '')}",
        "",
        "> Phase A 規則產出 · Phase B 複製下方提示詞至外部 LLM",
        "",
        ALGORITHM_MARKDOWN,
        "",
        "---",
        "",
        "## System Prompt（固定）",
        "",
        "```text",
        prompts.system,
        "```",
        "",
        "## User Prompt · 收盤決策備忘",
        "",
        "```text",
        prompts.user_evening,
        "```",
        "",
        "## User Prompt · 早盤執行",
        "",
        "```text",
        prompts.user_morning,
        "```",
        "",
        "---",
        "",
        "## 當日 research_context.json（內嵌）",
        "",
        "```json",
        research_json,
        "```",
        "",
    ]
    return "\n".join(parts)
