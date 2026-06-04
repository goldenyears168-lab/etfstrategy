"""L2 加權共識 + Position Intent 決策（意圖優先於交易現象）。"""

from __future__ import annotations

from holdings_research import ADD_ACTIONS
from signal_engine import ChangeLeg, StockSignal, _zscore_series

# L2：單檔 ETF 貢獻須達此 z 門檻才算「有效同步」
CONSENSUS_LEG_Z_MIN = 0.35
CONSENSUS_STRONG_SCORE = 1.8
CONSENSUS_WEAK_SCORE = 0.9
# 檔數多但分數低 → 假共識
CONSENSUS_FALSE_ETF_MIN = 2

INTENT_PHRASE: dict[str, str] = {
    "MAINTAIN_CORE": "維持核心配置、權重微調",
    "REINFORCE_CORE": "強化核心持股",
    "BUILD_THEMATIC": "主題建倉、積極擴倉",
    "SCALE_SATELLITE": "衛星加碼",
    "TRIM_CORE": "核心減碼、調節比重",
    "EXIT_THEME": "主題減碼撤退",
    "TRIM_SATELLITE": "衛星減碼",
    "ROTATION_PLAY": "跟隨資金輪動加碼",
    "ROTATION_EXIT": "輪動減碼出場",
    "MIXED_REBALANCE": "跨檔分歧調倉",
    "WATCH": "變動力度有限、持續觀察",
}

CONSENSUS_PHRASE: dict[str, str] = {
    "STRONG": "多檔 ETF 同步且力度一致",
    "WEAK": "多檔加碼但力度分歧",
    "FALSE": "檔數同步但資金力度不足（假共識）",
    "SINGLE": "單一經理人觀點",
    "NONE": "",
}


def _add_legs_for_signal(sig: StockSignal) -> list[ChangeLeg]:
    return [
        lg
        for lg in sig.legs
        if lg.action in ADD_ACTIONS and lg.share_delta > 0
    ]


def apply_l2_consensus(signals: list[StockSignal]) -> None:
    """對齊窗口內：每檔加碼 leg 做 z(flow)、z(Δwt)，加總為 consensus_score。"""
    pool: list[tuple[StockSignal, ChangeLeg]] = []
    for sig in signals:
        for leg in _add_legs_for_signal(sig):
            pool.append((sig, leg))
    if not pool:
        return

    flow_raw = [
        abs(leg.flow_ntd) if leg.flow_ntd is not None else abs(leg.share_delta)
        for _, leg in pool
    ]
    wt_raw = [abs(leg.weight_delta_pp) for _, leg in pool]
    z_flows = _zscore_series(flow_raw)
    z_wts = _zscore_series(wt_raw)

    per_stock: dict[str, dict] = {}
    for i, (sig, leg) in enumerate(pool):
        entry = per_stock.setdefault(
            sig.stock_id,
            {"score": 0.0, "effective": 0, "etf_count": 0, "etfs": set()},
        )
        zf, zw = z_flows[i], z_wts[i]
        contrib = 0.55 * zf + 0.45 * zw
        entry["score"] += contrib
        entry["etf_count"] += 1
        entry["etfs"].add(leg.etf_code)
        if zf >= CONSENSUS_LEG_Z_MIN or zw >= CONSENSUS_LEG_Z_MIN:
            entry["effective"] += 1

    for sig in signals:
        agg = per_stock.get(sig.stock_id)
        if not agg:
            sig.consensus_score = 0.0
            sig.consensus_level = "NONE"
            sig.consensus_etf_effective = 0
            continue
        sig.consensus_score = round(agg["score"], 3)
        sig.consensus_etf_effective = agg["effective"]
        n_etf = len(agg["etfs"])
        if n_etf >= CONSENSUS_FALSE_ETF_MIN and agg["score"] < CONSENSUS_WEAK_SCORE:
            sig.consensus_level = "FALSE"
        elif n_etf >= 2 and agg["score"] >= CONSENSUS_STRONG_SCORE:
            sig.consensus_level = "STRONG"
        elif n_etf >= 2 and agg["score"] >= CONSENSUS_WEAK_SCORE:
            sig.consensus_level = "WEAK"
        elif n_etf >= 2:
            sig.consensus_level = "FALSE"
        elif n_etf == 1:
            sig.consensus_level = "SINGLE"
        else:
            sig.consensus_level = "NONE"


def resolve_position_intent(sig: StockSignal) -> str:
    """由 L2–L5 推導單一主意圖（Comment 主句用）。"""
    side = sig.net_side
    role = sig.portfolio_role
    conv = sig.conviction_level

    if sig.rotation_in and side == "add":
        return "ROTATION_PLAY"
    if sig.rotation_out and side == "reduce":
        return "ROTATION_EXIT"
    if side == "mixed":
        return "MIXED_REBALANCE"

    if side == "add":
        if role == "CORE":
            if conv in ("LOW", "NONE") or sig.weight_delta_pp_max <= 0.25:
                return "MAINTAIN_CORE"
            return "REINFORCE_CORE"
        if role == "THEMATIC":
            return "BUILD_THEMATIC"
        if conv == "HIGH":
            return "BUILD_THEMATIC"
        return "SCALE_SATELLITE"

    if side == "reduce":
        if role == "CORE":
            return "TRIM_CORE"
        if role == "THEMATIC":
            return "EXIT_THEME"
        return "TRIM_SATELLITE"

    return "WATCH"


def apply_position_intents(signals: list[StockSignal]) -> None:
    apply_l2_consensus(signals)
    for sig in signals:
        sig.position_intent = resolve_position_intent(sig)
