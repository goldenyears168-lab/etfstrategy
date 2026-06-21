"""由 StockSignal（L2–L6 + Position Intent）組成經理人可讀註解。"""

from __future__ import annotations

import sqlite3

from investment_themes import theme_label
from position_intent import CONSENSUS_PHRASE, INTENT_PHRASE
from signal_engine import StockSignal

ROLE_TAG: dict[str, str] = {
    "CORE": "CORE",
    "THEMATIC": "THEMATIC",
    "SATELLITE": "SATELLITE",
}

CONVICTION_PHRASE: dict[str, str] = {
    "HIGH": "高信心",
    "MEDIUM": "中等信心",
    "LOW": "低力度",
    "NONE": "",
}


def _rotation_phrase(sig: StockSignal) -> str | None:
    if sig.rotation_in:
        parts = sig.rotation_in.split("→", 1)
        if len(parts) == 2:
            return f"資金由{theme_label(parts[0])}轉入{theme_label(parts[1])}"
    if sig.rotation_out:
        parts = sig.rotation_out.split("→", 1)
        if len(parts) == 2:
            return f"資金由{theme_label(parts[0])}轉出至{theme_label(parts[1])}"
    return None


def _theme_token(sig: StockSignal) -> str:
    if sig.rotation_in:
        return f"ROT→{sig.rotation_in.split('→', 1)[-1]}"
    if sig.rotation_out:
        return f"ROT→{sig.rotation_out.split('→', 1)[-1]}"
    if sig.theme != "UNKNOWN":
        return sig.theme
    return ""


def _chinese_intent_line(sig: StockSignal) -> str:
    """單句中文意圖（不含 bracket tags）。"""
    intent = sig.position_intent or "WATCH"
    lead = INTENT_PHRASE.get(intent, intent)
    parts: list[str] = [lead] if lead else []

    cons_p = CONSENSUS_PHRASE.get(sig.consensus_level, "")
    if cons_p and sig.consensus_level in ("STRONG", "WEAK", "FALSE"):
        parts.append(cons_p)

    rot = _rotation_phrase(sig)
    if rot:
        parts.append(rot)
    elif (
        sig.theme != "UNKNOWN"
        and sig.net_side == "add"
        and sig.consensus_level not in ("STRONG", "WEAK", "FALSE")
        and not sig.rotation_in
    ):
        parts.append(f"{theme_label(sig.theme)}主線")

    return "；".join(p for p in parts if p)


def compose_intent_tags(sig: StockSignal) -> str:
    """除錯用 bracket tags（預設輸出不顯示）。"""
    intent = sig.position_intent or "WATCH"
    brackets: list[str] = [ROLE_TAG.get(sig.portfolio_role, sig.portfolio_role)]
    if sig.conviction_level in ("HIGH", "MEDIUM", "LOW"):
        brackets.append(f"{sig.conviction_level}_CONVICTION")
    if sig.consensus_level in ("STRONG", "WEAK", "FALSE"):
        brackets.append(f"CONSENSUS_{sig.consensus_level}")
    elif sig.consensus_level == "SINGLE":
        brackets.append("LONE_MANAGER")
    if sig.theme != "UNKNOWN":
        brackets.append(sig.theme)
    if sig.rotation_in:
        brackets.append(f"ROTATION_IN:{sig.rotation_in}")
    elif sig.rotation_out:
        brackets.append(f"ROTATION_OUT:{sig.rotation_out}")
    brackets.append(intent)
    return " ".join(f"[{b}]" for b in brackets)


def compose_intent_compact(sig: StockSignal) -> str:
    """單行緊湊摘要：代號名稱 | 結構欄位 | 中文意圖。"""
    name = sig.stock_name or sig.stock_id
    conv = sig.conviction_level if sig.conviction_level != "NONE" else "—"
    role = sig.portfolio_role
    tokens: list[str] = [name, conv, role]
    theme_tok = _theme_token(sig)
    if theme_tok:
        tokens.append(theme_tok)
    if sig.weight_rank_best is not None:
        tokens.append(f"rank#{sig.weight_rank_best}")
    if sig.weight_delta_pp_max:
        tokens.append(f"{sig.weight_delta_pp_max:+.2f}pp")
    cn = _chinese_intent_line(sig)
    head = " | ".join(tokens)
    return f"{head} | {cn}" if cn else head


def compose_intent_tail(
    sig: StockSignal,
    *,
    table_mode: bool = False,
    conn: sqlite3.Connection | None = None,
) -> str:
    """合併寬表「意圖」欄（不含代號／名稱）。"""
    tokens: list[str] = []
    if sig.conviction_level not in ("NONE",):
        tokens.append(sig.conviction_level)
    tokens.append(sig.portfolio_role)
    theme_tok = _theme_token(sig)
    if theme_tok:
        tokens.append(theme_tok)
    if not table_mode:
        if sig.weight_rank_best is not None:
            tokens.append(f"rank#{sig.weight_rank_best}")
        if sig.weight_delta_pp_max:
            tokens.append(f"{sig.weight_delta_pp_max:+.2f}pp")
    cn = _chinese_intent_line(sig)
    head = " | ".join(tokens)
    return f"{head} | {cn}" if cn else head


def compose_comment(sig: StockSignal, *, conn: sqlite3.Connection | None = None) -> str:
    """相容舊 API：單行緊湊 + 除錯 tags 另列時請用 compose_intent_compact。"""
    compact = compose_intent_compact(sig)
    if conn is None:
        return compact
    cn = _chinese_intent_line(sig)
    if not cn:
        return compact
    name = sig.stock_name or sig.stock_id
    head = compact.split(" | ", 1)[0] if " | " in compact else name
    return f"{head} | {cn}"


def format_signal_debug(sig: StockSignal) -> str:
    rank = sig.weight_rank_best if sig.weight_rank_best is not None else "—"
    wt = f"{sig.weight_delta_pp_max:+.2f}pp"
    gr = (
        f"{sig.share_growth_pct_max:+.1f}%"
        if sig.share_growth_pct_max is not None
        else "—"
    )
    return (
        f"rank={rank} top5={int(sig.in_top5_any)} "
        f"L2={sig.consensus_level}({sig.consensus_score:+.2f},n={sig.consensus_etf_effective}) "
        f"conv={sig.conviction_level}({sig.conviction_score:+.2f}) "
        f"intent={sig.position_intent} Δwt={wt} grow={gr}"
    )
