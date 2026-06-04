"""由 StockSignal（L2–L6 + Position Intent）組成經理人可讀註解。"""

from __future__ import annotations

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


def compose_comment(sig: StockSignal) -> str:
    intent = sig.position_intent or "WATCH"
    brackets: list[str] = [ROLE_TAG.get(sig.portfolio_role, sig.portfolio_role)]
    if sig.conviction_level in ("HIGH", "MEDIUM"):
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

    parts: list[str] = []
    lead = INTENT_PHRASE.get(intent, intent)
    conv_p = CONVICTION_PHRASE.get(sig.conviction_level, "")
    if conv_p and intent not in ("MAINTAIN_CORE", "WATCH"):
        lead = f"{conv_p}{lead}"

    parts.append(lead)

    cons_p = CONSENSUS_PHRASE.get(sig.consensus_level, "")
    if cons_p and sig.consensus_level in ("STRONG", "WEAK", "FALSE"):
        parts.append(cons_p)

    rot = _rotation_phrase(sig)
    if rot:
        parts.append(rot)
    elif sig.theme != "UNKNOWN" and sig.net_side == "add":
        parts.append(f"{theme_label(sig.theme)}主線")

    rank_note = ""
    if sig.weight_rank_best is not None:
        rank_note = f"（持股排名約第{sig.weight_rank_best}）"
    body = "，".join(p for p in parts if p)
    tag_str = " ".join(f"[{b}]" for b in brackets)
    name = sig.stock_name or sig.stock_id
    return f"{name} {tag_str}\n  {body}{rank_note}。"


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
