"""PM Memo 策展層：從 research_context 萃取 Top-N 觀察、矛盾、隔日焦點。"""

from __future__ import annotations

from typing import Any

from holdings_research import fmt_ntd_short
from market_labels import (
    CHIP_FOREIGN_SELL_DIV,
    ENTRY_OVEREXTENDED,
    PM_AVOID,
    PM_OBSERVE,
    WL_EXCLUDED,
)

TOP_OBSERVATIONS = 5
TOP_CONSENSUS_EXPANSION = 3
TOP_CAPITAL_CONCENTRATION = 3
TOP_CONTRADICTIONS = 8
TOP_TOMORROW_WATCH = 5

CONSENSUS_EXPANSION_LABEL = "擴張"


def _stock_flow_ntd(stock: dict) -> float:
    total = 0.0
    for leg in stock.get("legs") or []:
        v = leg.get("flow_ntd")
        if v is not None:
            total += abs(float(v))
    return total


def _index_by_id(rows: list[dict] | None) -> dict[str, dict]:
    if not rows:
        return {}
    return {str(r["stock_id"]): r for r in rows if r.get("stock_id")}


def _etf_side_summary(
    *,
    consensus: dict | None,
    signal: dict | None,
) -> str:
    parts: list[str] = []
    if consensus:
        n = consensus.get("etf_add_count") or 0
        if n:
            etfs = ",".join(consensus.get("etf_add_list") or [])
            flow_s = fmt_ntd_short(consensus.get("flow_ntd")) or "—"
            parts.append(f"{n}檔加碼 · flow {flow_s}" + (f" ({etfs})" if etfs else ""))
    if signal:
        l2 = signal.get("l2_consensus_level")
        l4 = signal.get("l4_conviction_level")
        if l2:
            parts.append(f"L2={l2}")
        if l4:
            parts.append(f"conv={l4}")
    return " · ".join(parts) if parts else "ETF 加碼"


def _rule_side_summary(decision: dict | None) -> str:
    if not decision:
        return "規則資料缺"
    parts = [
        decision.get("pm_bucket") or "—",
        decision.get("entry_signal") or "—",
    ]
    wl = decision.get("watchlist")
    if wl:
        parts.append(wl)
    w = decision.get("portfolio_weight_pct")
    if w is not None:
        parts.append(f"權重{float(w):.0f}%")
    return " · ".join(str(p) for p in parts if p)


def build_top_observations(ctx: dict[str, Any]) -> list[dict]:
    """Top 5：加碼力度 × conviction × money_rank。"""
    layers = ctx.get("signal_layers") or {}
    stocks = [
        s for s in (layers.get("stocks") or []) if s.get("net_side") == "add"
    ]
    decisions = _index_by_id(ctx.get("decisions"))
    ranked: list[tuple[float, dict]] = []
    for s in stocks:
        sid = str(s["stock_id"])
        flow = _stock_flow_ntd(s)
        conv = s.get("l4_conviction_score") or 0.0
        l2 = s.get("l2_consensus_score") or 0.0
        money_rank = decisions.get(sid, {}).get("money_rank")
        rank_bonus = max(0, 11 - int(money_rank)) if money_rank else 0
        score = flow / 1e8 + float(conv) + float(l2) * 0.5 + rank_bonus
        ranked.append((score, s))
    ranked.sort(key=lambda x: x[0], reverse=True)

    out: list[dict] = []
    for _, s in ranked[:TOP_OBSERVATIONS]:
        sid = str(s["stock_id"])
        d = decisions.get(sid, {})
        out.append(
            {
                "stock_id": sid,
                "stock_name": s.get("stock_name") or d.get("stock_name"),
                "flow_ntd": _stock_flow_ntd(s),
                "flow_short": fmt_ntd_short(_stock_flow_ntd(s)),
                "l2_consensus_level": s.get("l2_consensus_level"),
                "l4_conviction_level": s.get("l4_conviction_level"),
                "l5_position_intent": s.get("l5_position_intent"),
                "pm_bucket": d.get("pm_bucket"),
                "watchlist": d.get("watchlist"),
            }
        )
    return out


def build_consensus_expansion(ctx: dict[str, Any]) -> list[dict]:
    """Top 3：共識標籤為擴張且多檔 ETF 加碼。"""
    consensus_by_id = _index_by_id(ctx.get("cross_etf_consensus"))
    decisions = ctx.get("decisions") or []
    candidates: list[tuple[float, dict]] = []
    for d in decisions:
        if d.get("consensus_trend_label") != CONSENSUS_EXPANSION_LABEL:
            continue
        sid = str(d["stock_id"])
        c = consensus_by_id.get(sid, {})
        etf_add = int(c.get("etf_add_count") or d.get("consensus_etf_add_latest") or 0)
        if etf_add < 2:
            continue
        flow = abs(float(c.get("flow_ntd") or 0))
        candidates.append(
            (
                flow,
                {
                    "stock_id": sid,
                    "stock_name": d.get("stock_name") or c.get("stock_name"),
                    "etf_add_count": etf_add,
                    "etf_add_list": c.get("etf_add_list") or [],
                    "flow_ntd": c.get("flow_ntd"),
                    "flow_short": fmt_ntd_short(c.get("flow_ntd")),
                    "consensus_trend_label": d.get("consensus_trend_label"),
                },
            )
        )
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in candidates[:TOP_CONSENSUS_EXPANSION]]


def build_capital_concentration(ctx: dict[str, Any]) -> list[dict]:
    """Top 3：跨 ETF flow 集中。"""
    rows = ctx.get("cross_etf_consensus") or []
    multi = [r for r in rows if (r.get("etf_add_count") or 0) >= 1]
    multi.sort(key=lambda r: abs(float(r.get("flow_ntd") or 0)), reverse=True)
    out: list[dict] = []
    for r in multi[:TOP_CAPITAL_CONCENTRATION]:
        out.append(
            {
                "stock_id": r["stock_id"],
                "stock_name": r.get("stock_name"),
                "etf_add_count": r.get("etf_add_count"),
                "etf_add_list": r.get("etf_add_list") or [],
                "flow_ntd": r.get("flow_ntd"),
                "flow_short": fmt_ntd_short(r.get("flow_ntd")),
            }
        )
    return out


def build_contradictions(ctx: dict[str, Any]) -> list[dict]:
    """ETF 資金面 vs 規則引擎背離（預算 · 供 §4）。"""
    consensus_by_id = _index_by_id(ctx.get("cross_etf_consensus"))
    signal_by_id = _index_by_id((ctx.get("signal_layers") or {}).get("stocks"))
    decisions_by_id = _index_by_id(ctx.get("decisions"))

    stock_ids: list[str] = []
    for sid in consensus_by_id:
        if sid not in stock_ids:
            stock_ids.append(sid)
    for sid in signal_by_id:
        if signal_by_id[sid].get("net_side") == "add" and sid not in stock_ids:
            stock_ids.append(sid)

    found: list[tuple[int, dict]] = []
    seen_codes: set[tuple[str, str]] = set()

    def _add(
        sid: str,
        reason_code: str,
        narrative_hint: str,
        priority: int,
    ) -> None:
        key = (sid, reason_code)
        if key in seen_codes:
            return
        seen_codes.add(key)
        c = consensus_by_id.get(sid)
        s = signal_by_id.get(sid)
        d = decisions_by_id.get(sid)
        if not c and not (s and s.get("net_side") == "add"):
            return
        etf_add = int((c or {}).get("etf_add_count") or 0)
        if etf_add < 1 and not (s and s.get("net_side") == "add"):
            return
        found.append(
            (
                priority,
                {
                    "stock_id": sid,
                    "stock_name": (
                        (d or {}).get("stock_name")
                        or (c or {}).get("stock_name")
                        or (s or {}).get("stock_name")
                    ),
                    "reason_code": reason_code,
                    "etf_side": _etf_side_summary(consensus=c, signal=s),
                    "rule_side": _rule_side_summary(d),
                    "narrative_hint": narrative_hint,
                },
            )
        )

    for sid in stock_ids:
        c = consensus_by_id.get(sid)
        s = signal_by_id.get(sid)
        d = decisions_by_id.get(sid)
        etf_add = int((c or {}).get("etf_add_count") or 0)
        pm_bucket = (d or {}).get("pm_bucket")
        entry = (d or {}).get("entry_signal")
        chip = (d or {}).get("chip_tag")
        wl = (d or {}).get("watchlist")
        l2 = (s or {}).get("l2_consensus_level")

        if etf_add >= 2 and pm_bucket == PM_AVOID:
            _add(
                sid,
                "ETF_ADD_VS_AVOID",
                "多檔 ETF 加碼但規則引擎回避",
                100,
            )
        if etf_add >= 1 and entry == ENTRY_OVEREXTENDED:
            _add(
                sid,
                "ETF_ADD_VS_OVEREXTENDED",
                "ETF 加碼但乖離過大不宜追價",
                90,
            )
        if l2 == "FALSE" and (s or {}).get("net_side") == "add":
            _add(
                sid,
                "ETF_ADD_VS_FALSE_CONSENSUS",
                "L2 假共識：檔數同步但力度弱",
                70,
            )
        if chip == CHIP_FOREIGN_SELL_DIV and etf_add >= 1:
            _add(
                sid,
                "ETF_ADD_VS_FOREIGN_SELL",
                "ETF 加碼但外資賣超背離",
                80,
            )
        if (
            etf_add >= 2
            and wl == WL_EXCLUDED
            and pm_bucket == PM_AVOID
            and entry != ENTRY_OVEREXTENDED
        ):
            _add(
                sid,
                "HIGH_FLOW_LOW_WATCHLIST",
                "資金集中但未進觀察名單",
                60,
            )
        if etf_add >= 2 and pm_bucket == PM_OBSERVE and entry == ENTRY_OVEREXTENDED:
            _add(
                sid,
                "ETF_ADD_OBSERVE_OVEREXTENDED",
                "研究可跟、執行不宜追價（觀察＋乖離過大）",
                50,
            )

    found.sort(key=lambda x: (-x[0], x[1]["stock_id"]))
    return _merge_contradictions([row for _, row in found])


def _merge_contradictions(rows: list[dict]) -> list[dict]:
    """同檔多 reason_code 合併為一列（供 §4 可讀）。"""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        sid = str(r["stock_id"])
        if sid not in by_id:
            order.append(sid)
            by_id[sid] = {
                "stock_id": sid,
                "stock_name": r.get("stock_name"),
                "reason_codes": [r["reason_code"]],
                "etf_side": r["etf_side"],
                "rule_side": r["rule_side"],
                "narrative_hints": [r["narrative_hint"]],
            }
            continue
        merged = by_id[sid]
        code = r["reason_code"]
        if code not in merged["reason_codes"]:
            merged["reason_codes"].append(code)
        hint = r["narrative_hint"]
        if hint not in merged["narrative_hints"]:
            merged["narrative_hints"].append(hint)
    return [by_id[sid] for sid in order][:TOP_CONTRADICTIONS]


def _decision_reason(d: dict) -> str:
    for key in ("chip_verify", "chip_tag", "headline", "pool_reason"):
        val = d.get(key)
        if val:
            return str(val)
    return "—"


def build_decision_summary(decisions: list[dict]) -> dict[str, dict]:
    """規則決策精簡表（LLM 用 · 不含 RS/EPS／suggested_ntd）。

    suggested_ntd 為 Portfolio Builder 依帳戶規模衍生的金額，非 Research 決策；
    完整值見 research_context.decisions 或 portfolio_weights。
    """
    out: dict[str, dict] = {}
    for d in decisions:
        sid = str(d["stock_id"])
        row: dict[str, Any] = {
            "stock_name": d.get("stock_name"),
            "watchlist": d.get("watchlist"),
            "pm_bucket": d.get("pm_bucket"),
            "entry_signal": d.get("entry_signal"),
            "reason": _decision_reason(d),
        }
        weight = d.get("portfolio_weight_pct")
        if weight is not None:
            row["portfolio_weight_pct"] = weight
        out[sid] = {k: v for k, v in row.items() if v is not None}
    return out


def build_tomorrow_watch(
    ctx: dict[str, Any],
    *,
    contradictions: list[dict] | None = None,
) -> list[dict]:
    """隔日觀察焦點（策展 checklist + 矛盾標的）。"""
    out: list[dict] = []
    seen: set[str] = set()

    contra = contradictions if contradictions is not None else build_contradictions(ctx)
    for c in contra[:3]:
        sid = c["stock_id"]
        if sid in seen:
            continue
        seen.add(sid)
        hints = c.get("narrative_hints") or []
        if not hints and c.get("narrative_hint"):
            hints = [c["narrative_hint"]]
        watch_reason = hints[0] if hints else (c.get("reason_codes") or ["—"])[0]
        out.append(
            {
                "stock_id": sid,
                "stock_name": c.get("stock_name"),
                "watch_reason": watch_reason,
            }
        )

    for item in ctx.get("next_day_checklist") or []:
        if len(out) >= TOP_TOMORROW_WATCH:
            break
        section = item.get("section") or ""
        if section not in ("列入觀察", "優先：價量突破", "人工風控"):
            continue
        text = str(item.get("text") or "")
        sid = text.split()[0] if text else ""
        if not sid or not sid.isdigit() or sid in seen:
            continue
        seen.add(sid)
        out.append(
            {
                "stock_id": sid,
                "watch_reason": text,
            }
        )

    decisions = _index_by_id(ctx.get("decisions"))
    for d in decisions.values():
        if len(out) >= TOP_TOMORROW_WATCH:
            break
        if d.get("chip_verify") and d["stock_id"] not in seen:
            seen.add(d["stock_id"])
            out.append(
                {
                    "stock_id": d["stock_id"],
                    "stock_name": d.get("stock_name"),
                    "watch_reason": d["chip_verify"],
                }
            )

    return out[:TOP_TOMORROW_WATCH]


def build_pm_briefing(ctx: dict[str, Any]) -> dict[str, Any]:
    """組裝 PM Memo 策展 JSON（規則產出 · 供 LLM 撰稿）。"""
    contradictions = build_contradictions(ctx)
    return {
        "top_observations": build_top_observations(ctx),
        "consensus_expansion": build_consensus_expansion(ctx),
        "capital_concentration": build_capital_concentration(ctx),
        "contradictions": contradictions,
        "tomorrow_watch": build_tomorrow_watch(ctx, contradictions=contradictions),
    }
