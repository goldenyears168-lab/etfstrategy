#!/usr/bin/env python3
"""收盤人類 digest：終端精簡摘要 + evening_brief.md（唯一人類主檔）。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from entry_signal import format_entry_display
from consensus_trend import consensus_trend_summary
from etf_signal_performance import build_etf_signal_performance
from market_analytics import build_analytics_map
from holdings_research import (
    build_cross_etf_consensus,
    build_etf_holdings_changes_block,
    fmt_ntd_short,
)
from market_labels import PM_AVOID, PM_BREAKOUT, PM_OBSERVE, WL_PRIMARY
from operational_brief import (
    MANUAL_RISK_RULES,
    build_morning_checklist_items,
    build_news_verify_items,
    google_search_url,
)
from research_context import REPORTS_DIR
from research_universe import DEFAULT_ETF_CODES, parse_etf_codes
from score_engine import SCORE_VERSION
from signal_engine import build_signal_layers_block
from stock_db import (
    PROJECT_ROOT,
    connect,
    load_latest_pm_watchlist,
    load_latest_portfolio_weights,
    load_latest_tech_risk,
    list_etf_snapshot_dates,
)
from pre_trade_check import load_tsm_adr_pct

HOLDINGS_ETF_CODES = DEFAULT_ETF_CODES + ("00407A",)
CHECKLIST_TERMINAL_MAX = 12
CONSENSUS_TOP = 5
SINGLE_ETF_FLOW_TOP = 3
RULE_SECTIONS_OMIT_TERMINAL = frozenset({"風控守則"})


def _rel_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _holdings_sync_summary(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, Any]:
    synced = 0
    parts: list[str] = []
    for code in etf_codes:
        dates = list_etf_snapshot_dates(conn, code)
        if dates:
            synced += 1
            parts.append(f"{code} {dates[-1]}")
    return {
        "synced": synced,
        "total": len(etf_codes),
        "parts": parts,
    }


def _consensus_highlight_lines(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[str]:
    rows = build_cross_etf_consensus(conn, etf_codes)
    if not rows:
        return ["（尚無跨 ETF 共識；需 ≥2 snapshot 日）"]
    multi = [r for r in rows if r.etf_add >= 2 and r.etf_add > r.etf_reduce]
    multi.sort(key=lambda r: abs(r.flow_ntd or 0.0), reverse=True)
    single = [r for r in rows if r.etf_add == 1 and r.etf_add > r.etf_reduce]
    single.sort(key=lambda r: abs(r.flow_ntd or 0.0), reverse=True)

    lines: list[str] = []
    for r in multi[:CONSENSUS_TOP]:
        flow_s = fmt_ntd_short(r.flow_ntd) or "—"
        etfs = ",".join(r.etf_add_list)
        lines.append(f"  {r.stock_id} {r.stock_name}  加碼{r.etf_add}檔  flow {flow_s}  ({etfs})")
    if not lines and single:
        for r in single[:3]:
            flow_s = fmt_ntd_short(r.flow_ntd) or "—"
            lines.append(f"  {r.stock_id} {r.stock_name}  單檔加碼  flow {flow_s}")
    if not lines:
        return ["（今日無明顯加碼共識）"]
    return lines


def _etf_change_one_liners(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[str]:
    blocks = build_etf_holdings_changes_block(conn, etf_codes)
    lines: list[str] = []
    for block in blocks:
        code = block["etf_code"]
        changes = block.get("changes") or []
        if block.get("note"):
            lines.append(f"  {code}  —  {block['note']}")
            continue
        if not changes:
            lines.append(f"  {code}  無持股變化")
            continue
        adds = sum(1 for c in changes if c["action"] in ("新进", "加码"))
        reds = sum(1 for c in changes if c["action"] in ("减码", "出清"))
        top = sorted(
            changes,
            key=lambda c: abs(float(c["flow_ntd"] or 0)),
            reverse=True,
        )[:SINGLE_ETF_FLOW_TOP]
        top_s = " · ".join(
            f"{c['stock_id']} {fmt_ntd_short(c['flow_ntd']) or '—'}"
            for c in top
            if c.get("flow_ntd") is not None
        )
        tail = f"  最大flow {top_s}" if top_s else ""
        lines.append(f"  {code}  加{adds}減{reds}{tail}")
    return lines


def _signal_intent_lines(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[str]:
    block = build_signal_layers_block(conn, etf_codes)
    if not block:
        return ["（無對齊窗口 · L2–L5 略）"]
    stocks = block.get("stocks") or []
    primary = [
        s
        for s in stocks
        if s.get("net_side") == "add"
        and s.get("l4_conviction_level") in ("HIGH", "MEDIUM")
    ]
    if not primary:
        primary = [s for s in stocks if s.get("net_side") == "add"][:5]
    if not primary:
        return ["（無加碼意圖列）"]
    lines: list[str] = []
    for s in primary[:8]:
        flow_s = fmt_ntd_short(s.get("flow_ntd_total")) or "—"
        lines.append(
            f"  {s['stock_id']}  {s.get('l5_position_intent', '—')}  "
            f"L2={s.get('l2_consensus_level')}  conv={s.get('l4_conviction_level')}  "
            f"flow {flow_s}"
        )
    low = sum(
        1
        for s in stocks
        if s.get("net_side") == "add"
        and s.get("l4_conviction_level") not in ("HIGH", "MEDIUM", None)
    )
    if low:
        lines.append(f"  （另有 {low} 檔低力度衛星加碼 · 見 evening_brief.md 附錄）")
    return lines


def _pm_bucket_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
    if not rows:
        return {"as_of": None, "breakout": [], "observe": [], "avoid": []}
    as_of = rows[0]["as_of_date"]
    breakout = [r for r in rows if r["pm_bucket"] == PM_BREAKOUT]
    observe = [r for r in rows if r["pm_bucket"] == PM_OBSERVE]
    avoid = [r for r in rows if r["pm_bucket"] == PM_AVOID]
    return {
        "as_of": as_of,
        "breakout": breakout,
        "observe": observe,
        "avoid": avoid,
        "score_n": len(rows),
    }


def _priority_ticker_line(pm: dict[str, Any]) -> str:
    parts: list[str] = []
    for r in pm.get("breakout", [])[:6]:
        entry = r["entry_signal"] or "—"
        parts.append(f"{r['stock_id']}({entry})")
    for r in pm.get("observe", [])[:6]:
        if len(parts) >= 8:
            break
        parts.append(r["stock_id"])
    return "、".join(parts) if parts else "—"


def _etf_performance_lines(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> list[str]:
    rows = build_etf_signal_performance(conn, etf_codes)
    if not rows:
        return []
    lines = [
        "",
        "## ETF 訊號勝率（H+20 · flow_events 加碼）",
        "",
        "> 各 ETF 歷史「加碼」事件後 20 交易日報酬>0 比例；樣本 <3 顯示 —",
        "",
        "| ETF | 樣本 | 20日勝率 | 均報酬 |",
        "|-----|------|----------|--------|",
    ]
    for r in rows:
        wr = f"{r.win_rate_pct:.0f}%" if r.win_rate_pct is not None else "—"
        mr = f"{r.mean_return_pct:+.1f}%" if r.mean_return_pct is not None else "—"
        lines.append(f"| {r.etf_code} | {r.sample_n} | {wr} | {mr} |")
    return lines


def _fmt_rs(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def _factor_check_section(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    pm_rows: list[sqlite3.Row],
) -> list[str]:
    """五維因子檢核表（RS／籌碼連續／盈餘 proxy／回測／R:R）。"""
    if not pm_rows:
        return []
    import json

    stock_ids = [str(r["stock_id"]) for r in pm_rows]
    meta_by_id: dict[str, dict] = {}
    as_of = pm_rows[0]["as_of_date"] if pm_rows else None
    if as_of:
        try:
            for row in conn.execute(
                """
                SELECT stock_id, metadata_json
                FROM investment_scores
                WHERE as_of_date = ? AND score_version = ?
                  AND stock_id IN ({})
                """.format(",".join("?" * len(stock_ids))),
                (as_of, SCORE_VERSION, *stock_ids),
            ).fetchall():
                try:
                    meta_by_id[str(row["stock_id"])] = json.loads(
                        row["metadata_json"] or "{}"
                    )
                except json.JSONDecodeError:
                    meta_by_id[str(row["stock_id"])] = {}
        except sqlite3.OperationalError:
            pass

    entry_by_id = {str(r["stock_id"]): r["entry_signal"] or "" for r in pm_rows}
    score_by_id = {
        str(r["stock_id"]): float(r["investment_score"])
        for r in pm_rows
        if r["investment_score"] is not None
    }
    analytics = build_analytics_map(
        conn, stock_ids, entry_by_id=entry_by_id, score_by_id=score_by_id
    )
    lines = [
        "",
        "## 因子檢核（RS／籌碼／盈餘／回測／R:R）",
        "",
        "> RS分位=Universe 橫截面；共識趨勢=近幾窗加碼ETF檔數；盈餘=EPS QoQ proxy",
        "",
        "| 代號 | RS分位 | RS60 | 籌碼驗證 | 共識趨勢 | 盈餘 | R:R | 價位型態 |",
        "|------|--------|------|----------|----------|------|-----|----------|",
    ]
    for r in pm_rows:
        sid = str(r["stock_id"])
        a = analytics.get(sid)
        if a is None:
            continue
        meta = meta_by_id.get(sid, {})
        tags = tuple(meta.get("entry_tags") or [])
        entry = format_entry_display(r["entry_signal"] or "—", tags)
        eps_cell = a.eps_revision or "—"
        if a.eps_qoq_pct is not None:
            eps_cell = f"{eps_cell}({a.eps_qoq_pct:+.0f}%)"
        rr_cell = f"{a.risk_reward:.2f}" if a.risk_reward is not None else "—"
        trend = consensus_trend_summary(conn, etf_codes, sid)
        trend_cell = trend.get("consensus_trend_label") or "—"
        pts = trend.get("consensus_trend") or []
        if pts:
            seq = "→".join(str(p["etf_add_count"]) for p in pts)
            trend_cell = f"{trend_cell}({seq})" if trend_cell != "—" else seq
        rs_pct = (
            f"{a.rs_percentile:.0f}"
            if a.rs_percentile is not None
            else "—"
        )
        lines.append(
            f"| {sid} | {rs_pct} | {_fmt_rs(a.rs_60d)} | "
            f"{a.chip_verify or '—'} | {trend_cell} | {eps_cell} | {rr_cell} | {entry} |"
        )
    return lines


def _focus_score_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    pm = _pm_bucket_summary(conn)
    as_of = pm.get("as_of")
    if not as_of:
        return []
    try:
        rows = conn.execute(
            """
            SELECT s.stock_id, s.stock_name, s.investment_score, s.watchlist,
                   p.pm_bucket, s.metadata_json
            FROM investment_scores s
            LEFT JOIN pm_watchlist p
              ON p.stock_id = s.stock_id
             AND p.as_of_date = s.as_of_date
             AND p.score_version = s.score_version
            WHERE s.as_of_date = ? AND s.score_version = ?
            ORDER BY s.investment_score DESC
            """,
            (as_of, SCORE_VERSION),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    import json

    pw = load_latest_portfolio_weights(conn, score_version=SCORE_VERSION)
    alloc_ids = {
        x["stock_id"] for x in pw if float(x["portfolio_weight_pct"] or 0) > 0
    }
    focus: list[sqlite3.Row] = []
    for r in rows:
        wl = r["watchlist"] or ""
        bucket = r["pm_bucket"] or ""
        if wl == WL_PRIMARY or bucket in (PM_BREAKOUT, PM_OBSERVE):
            focus.append(r)
            continue
        if r["stock_id"] in alloc_ids:
            focus.append(r)
    return focus[:15]


def _health_status(conn: sqlite3.Connection) -> tuple[bool, str]:
    issues: list[str] = []
    meta_n = 0
    for code in HOLDINGS_ETF_CODES:
        if list_etf_snapshot_dates(conn, code):
            meta_n += 1
    if meta_n < len(HOLDINGS_ETF_CODES):
        issues.append(f"持股 snapshot {meta_n}/{len(HOLDINGS_ETF_CODES)}")
    try:
        row = conn.execute(
            "SELECT MAX(as_of_date) FROM investment_scores WHERE score_version=?",
            (SCORE_VERSION,),
        ).fetchone()
        if not row or not row[0]:
            issues.append("Score 未寫入")
    except sqlite3.OperationalError:
        issues.append("Score 表缺失")
    if issues:
        return False, " · ".join(issues)
    return True, "資料健康 ✓"


def _checklist_for_terminal(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> tuple[list, int]:
    items = build_morning_checklist_items(conn, etf_codes)
    trimmed = [it for it in items if it.section not in RULE_SECTIONS_OMIT_TERMINAL]
    total = len(trimmed)
    return trimmed[:CHECKLIST_TERMINAL_MAX], total


def _stamp_from_as_of(as_of: str | None) -> str:
    ref = as_of or date.today().isoformat()
    return ref.replace("-", "")


def write_evening_brief_file(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    as_of: str | None = None,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    """收盤唯一人類主檔：摘要 + 研究表 + 查證 + Checklist。"""
    pm = _pm_bucket_summary(conn)
    ref = as_of or pm.get("as_of") or date.today().isoformat()
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{ref.replace('-', '')}_evening_brief.md"

    sync = _holdings_sync_summary(conn, etf_codes)
    news = build_news_verify_items(conn, etf_codes)
    checklist = build_morning_checklist_items(conn, etf_codes)

    lines: list[str] = [
        f"# 收盤研究 brief · {ref}",
        "",
        "> 人類唯一主檔 · 規則引擎產出 · LLM 請用 `research_context.json`。",
        "",
        "## 摘要",
        "",
        f"- 持股同步 {sync['synced']}/{sync['total']} 檔",
    ]
    if pm.get("as_of"):
        lines.append(
            f"- 隔日名單  突破 {len(pm['breakout'])} · "
            f"觀察 {len(pm['observe'])} · 不宜追 {len(pm['avoid'])}"
        )
        lines.append(f"- 優先關注  {_priority_ticker_line(pm)}")
    if news:
        lines.append(f"- 待查新聞 {len(news)} 檔（見 §待查新聞）")
    lines.append("")

    tech = load_latest_tech_risk(conn)
    if tech:
        tsm = load_tsm_adr_pct(conn)
        lines.extend(
            [
                "## 隔夜風險",
                "",
                f"- 台股日 {tech['session_date']} · TSM {_fmt_pct(tsm)} · "
                f"半導體 {_fmt_pct(tech['sox_daily_return_pct'])} · "
                f"台指gap {_fmt_pct(tech['tx_gap_pct'])} · "
                f"電子期 {_fmt_pct(tech['te_overnight_pct'])}",
                "",
            ]
        )

    lines.extend(["## 跨 ETF 共識", ""])
    for ln in _consensus_highlight_lines(conn, etf_codes):
        lines.append(ln.strip())
    lines.extend(["", "## 各 ETF 變化摘要", ""])
    for ln in _etf_change_one_liners(conn, etf_codes):
        lines.append(ln.strip())

    lines.extend(["", "## L2–L5 意圖", ""])
    for ln in _signal_intent_lines(conn, etf_codes):
        lines.append(ln.strip())

    lines.extend(_etf_performance_lines(conn, etf_codes))

    rows = load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
    if rows:
        lines.extend(["", "## 隔日名單（pm_watchlist）", ""])
        lines.append(
            "| 代號 | 名稱 | 隔日等級 | 綜合分 | 觀察名單 | 價位型態 | 籌碼 |"
        )
        lines.append("|------|------|----------|--------|----------|----------|------|")
        for r in rows:
            entry = format_entry_display(r["entry_signal"], [])
            lines.append(
                f"| {r['stock_id']} | {r['stock_name']} | {r['pm_bucket']} | "
                f"{r['investment_score']:.1f} | {r['watchlist']} | {entry} | "
                f"{r['chip_tag'] or '—'} |"
            )
        lines.extend(_factor_check_section(conn, etf_codes, rows))

    pw = load_latest_portfolio_weights(conn, score_version=SCORE_VERSION)
    if pw:
        lines.extend(["", "## 建議部位", ""])
        lines.append("| 代號 | 權重% | 金額 NTD | 隔日等級 |")
        lines.append("|------|-------|----------|----------|")
        for r in pw:
            w = float(r["portfolio_weight_pct"] or 0)
            if w <= 0:
                continue
            lines.append(
                f"| {r['stock_id']} | {w:.1f} | {float(r['suggested_ntd'] or 0):,.0f} | "
                f"{r['pm_bucket']} |"
            )

    lines.extend(
        [
            "",
            "## 待查新聞",
            "",
            "> 人類可手動查證，或貼 `prompt_evening_full.txt` 至**有聯網能力**的外部 LLM 完成 §3 查證（不得改寫 watchlist）。",
            "",
        ]
    )
    if not news:
        lines.append("（今日無需額外查證標的，或尚未跑 Score Engine）")
    else:
        for i, it in enumerate(news, 1):
            lines.extend(
                [
                    f"### {i}. {it.stock_id} {it.stock_name}",
                    "",
                    f"- **原因**：{it.reason}",
                    f"- **建議搜尋**：`{it.search_query}`",
                    f"- **Google**：[搜尋]({google_search_url(it.search_query)})",
                    f"- **Yahoo**：[新聞]({it.yahoo_news_url})",
                    "",
                ]
            )

    lines.extend(["", "## 隔日 Checklist", ""])
    section: str | None = None
    for it in checklist:
        if it.section != section:
            section = it.section
            lines.extend(["", f"### {section}", ""])
        lines.append(f"- [ ] {it.text}")

    lines.extend(["", "## 風控守則（固定）", ""])
    for rule in MANUAL_RISK_RULES:
        lines.append(f"- {rule}")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_evening_digest_markdown(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    """向後相容別名 → evening_brief.md。"""
    return write_evening_brief_file(conn, etf_codes, reports_dir=reports_dir)


def print_evening_human_digest(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    """收盤終端主輸出：精簡 digest + 寫入 evening_brief.md。"""
    pm = _pm_bucket_summary(conn)
    as_of = pm.get("as_of")
    run_date = date.today().isoformat()
    stamp = _stamp_from_as_of(as_of)

    sync = _holdings_sync_summary(conn, etf_codes)
    ok_health, health_msg = _health_status(conn)
    news = build_news_verify_items(conn, etf_codes)
    checklist_trim, checklist_total = _checklist_for_terminal(conn, etf_codes)

    brief_path = write_evening_brief_file(
        conn, etf_codes, as_of=as_of, reports_dir=reports_dir
    )

    print("")
    print("══════════════════════════════════════════════════════════")
    title_as_of = as_of or "—"
    print(f"  【收盤雷達】{run_date}  ·  研究基準日 {title_as_of}")
    print("══════════════════════════════════════════════════════════")

    print("")
    print("① 今日結論")
    sync_mark = "✓" if sync["synced"] == sync["total"] else "⚠"
    score_n = pm.get("score_n") or 0
    print(
        f"  {sync_mark} 持股同步 {sync['synced']}/{sync['total']} 檔"
        + (f"  ·  Score {score_n} 檔" if score_n else "  ·  Score —")
    )
    if pm.get("as_of"):
        print(
            f"  → 隔日  突破 {len(pm['breakout'])} · "
            f"觀察 {len(pm['observe'])} · 不宜追 {len(pm['avoid'])}"
        )
        priority = _priority_ticker_line(pm)
        print(f"  → 優先  {priority}")
    else:
        print("  → 隔日名單 —（請確認 RUN_SCORE_ENGINE=1 且 Universe 對齊）")

    if news:
        print(f"  ⚠ 待查新聞 {len(news)} 檔（evening_brief §待查新聞 或 LLM prompt §3）")
    else:
        print("  ✓ 無額外待查新聞")

    print("")
    print("② 資金與意圖")
    print("  【跨檔共識】")
    for ln in _consensus_highlight_lines(conn, etf_codes):
        print(ln)
    print("  【各 ETF】")
    for ln in _etf_change_one_liners(conn, etf_codes):
        print(ln)
    print("  【L2–L5】")
    for ln in _signal_intent_lines(conn, etf_codes):
        print(ln)

    print("")
    print("③ 規則結論（首要＋突破＋有部位）")
    focus = _focus_score_rows(conn)
    if not focus:
        print("  —（尚無評分或未達門檻）")
    else:
        print(
            f"  {'代號':>6} {'名稱':<8} {'綜合':>5} {'觀察名單':<8} "
            f"{'隔日':<6} {'價位型態':<12} {'籌碼':<12}"
        )
        import json

        chip_by_id = {
            r["stock_id"]: r["chip_tag"]
            for r in load_latest_pm_watchlist(conn, score_version=SCORE_VERSION)
        }
        for r in focus:
            try:
                meta = json.loads(r["metadata_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            entry = format_entry_display(
                meta.get("entry_signal"),
                meta.get("entry_tags") or [],
            )
            chip = chip_by_id.get(r["stock_id"]) or meta.get("chip_tag") or "—"
            print(
                f"  {r['stock_id']:>6} {r['stock_name']:<8} "
                f"{float(r['investment_score']):>5.1f} {r['watchlist'] or '—':<8} "
                f"{r['pm_bucket'] or '—':<6} {entry:<12} {chip:<12}"
            )

    print("")
    print("④ 你的作業")
    if not news:
        print("  （無待查新聞）")
    else:
        for it in news[:5]:
            reason = it.reason
            if len(reason) > 48:
                reason = reason[:48] + "…"
            print(f"  {it.stock_id} {it.stock_name}  —  {reason}")
            print(f"    Yahoo  {it.yahoo_news_url}")
        if len(news) > 5:
            print(f"  … 共 {len(news)} 檔 → {_rel_path(brief_path)}")

    print("")
    print("  隔日 Checklist（精簡）")
    section: str | None = None
    for it in checklist_trim:
        if it.section != section:
            section = it.section
            print(f"    — {section} —")
        print(f"    [ ] {it.text}")
    if checklist_total > len(checklist_trim):
        print(
            f"    … 共 {checklist_total} 項（含風控守則）→ "
            f"{_rel_path(brief_path)}"
        )

    print("")
    print("⑤ 參考")
    mark = "✓" if ok_health else "⚠"
    print(f"  {mark} {health_msg}")
    print("  ────────────────────────────────────────")
    print(f"  📋 人類主檔   {_rel_path(brief_path)}")
    json_path = reports_dir / f"{stamp}_research_context.json"
    prompt_path = reports_dir / f"{stamp}_prompt_evening_full.txt"
    if json_path.exists():
        print(f"  🤖 LLM JSON   {_rel_path(json_path)}")
    if prompt_path.exists():
        print(f"  🤖 LLM 提示詞 {_rel_path(prompt_path)}")
    print("  ────────────────────────────────────────")
    print("  🔧 完整 log   logs/daily_sync_YYYYMMDD.log")
    print("")

    return brief_path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="收盤人類 digest（終端摘要）")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "stocks.db")
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    args = parser.parse_args()
    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        print_evening_human_digest(conn, codes)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
