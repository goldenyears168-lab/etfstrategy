"""跨 ETF 對齊日：L5 相對核心、L4 conviction、L3 主題輪動。"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

from holdings_research import (
    ADD_ACTIONS,
    REDUCE_ACTIONS,
    holding_growth_pct,
    implied_close_from_holdings,
    implied_flow_ntd,
    resolve_aligned_cohort,
)
from investment_themes import stock_theme, theme_label
from stock_db import compute_etf_holdings_changes, load_etf_holdings

# conviction 分位（橫截面；略降 HIGH 門檻避免整批都是 SATELLITE/LOW）
CONVICTION_HIGH_PCT = 0.85
CONVICTION_MED_PCT = 0.55
# 輪動：主題對流量須達最強對的此比例才標到個股
ROTATION_PAIR_MIN_RATIO = 0.30
ZSCORE_STD_FLOOR = 1e-9


@dataclass
class ChangeLeg:
    stock_id: str
    stock_name: str
    etf_code: str
    action: str
    share_delta: float
    weight_pct_prev: float | None
    weight_pct_curr: float | None
    weight_delta_pp: float
    share_growth_pct: float | None
    flow_ntd: float | None
    weight_rank: int | None
    in_top5: bool
    in_top_decile: bool
    theme: str


@dataclass
class StockSignal:
    stock_id: str
    stock_name: str
    theme: str
    legs: list[ChangeLeg] = field(default_factory=list)
    net_side: str = "flat"  # add | reduce | mixed
    weight_rank_best: int | None = None
    in_top5_any: bool = False
    in_top_decile_any: bool = False
    portfolio_role: str = "SATELLITE"
    weight_delta_pp_max: float = 0.0
    share_growth_pct_max: float | None = None
    flow_ntd_total: float | None = None
    conviction_score: float = 0.0
    conviction_level: str = "NONE"
    consensus_score: float = 0.0
    consensus_level: str = "NONE"
    consensus_etf_effective: int = 0
    position_intent: str = "WATCH"
    rotation_in: str | None = None
    rotation_out: str | None = None


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    w = idx - lo
    return sorted_vals[lo] * (1 - w) + sorted_vals[hi] * w


def _zscore_series(values: list[float | None]) -> list[float]:
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return [0.0 if v is not None else 0.0 for v in values]
    mean = sum(valid) / len(valid)
    var = sum((x - mean) ** 2 for x in valid) / len(valid)
    std = math.sqrt(var) if var > 0 else ZSCORE_STD_FLOOR
    return [(v - mean) / std if v is not None else 0.0 for v in values]


def _weight_ranks_for_etf(
    conn: sqlite3.Connection,
    etf_code: str,
    snapshot_date: str,
) -> dict[str, tuple[int, float, bool, bool]]:
    """stock_id -> (rank 1-based, weight_pct, in_top5, in_top_decile)."""
    rows = load_etf_holdings(conn, etf_code, snapshot_date)
    ranked = sorted(
        [r for r in rows if r["weight_pct"] is not None],
        key=lambda r: float(r["weight_pct"]),
        reverse=True,
    )
    if not ranked:
        return {}
    weights = [float(r["weight_pct"]) for r in ranked]
    p90 = _percentile(sorted(weights), 0.90)
    out: dict[str, tuple[int, float, bool, bool]] = {}
    for i, row in enumerate(ranked, start=1):
        w = float(row["weight_pct"])
        out[row["stock_id"]] = (i, w, i <= 5, w >= p90)
    return out


def _collect_legs_aligned(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    curr_date: str,
    prev_date: str,
) -> list[ChangeLeg]:
    legs: list[ChangeLeg] = []
    for etf_code in etf_codes:
        rank_map = _weight_ranks_for_etf(conn, etf_code, curr_date)
        close_cache: dict[str, float | None] = {}
        for row in compute_etf_holdings_changes(conn, etf_code, curr_date, prev_date):
            action = row["action"]
            if action == "不变":
                continue
            sid = row["stock_id"]
            if sid not in close_cache:
                close_cache[sid] = implied_close_from_holdings(
                    conn, sid, prev_date, curr_date
                )
            rank_info = rank_map.get(sid)
            rank = rank_info[0] if rank_info else None
            in_top5 = rank_info[2] if rank_info else False
            in_top_dec = rank_info[3] if rank_info else False
            wt_prev = row["weight_pct_prev"]
            wt_curr = row["weight_pct_curr"]
            wt_delta = float(row["weight_delta"] or 0)
            share_delta = float(row["share_delta"] or 0)
            legs.append(
                ChangeLeg(
                    stock_id=sid,
                    stock_name=row["stock_name"] or "",
                    etf_code=etf_code,
                    action=action,
                    share_delta=share_delta,
                    weight_pct_prev=float(wt_prev) if wt_prev is not None else None,
                    weight_pct_curr=float(wt_curr) if wt_curr is not None else None,
                    weight_delta_pp=wt_delta,
                    share_growth_pct=holding_growth_pct(
                        row["shares_prev"], row["shares_curr"], action
                    ),
                    flow_ntd=implied_flow_ntd(share_delta, close_cache[sid]),
                    weight_rank=rank,
                    in_top5=in_top5,
                    in_top_decile=in_top_dec,
                    theme=stock_theme(sid),
                )
            )
    return legs


def _flow_magnitude(leg: ChangeLeg) -> float:
    if leg.flow_ntd is not None and leg.flow_ntd != 0:
        return abs(leg.flow_ntd)
    return abs(leg.share_delta)


def _infer_portfolio_role(
    *,
    in_top5: bool,
    in_top_decile: bool,
    action_primary: str,
    weight_delta_pp_max: float,
    share_growth_max: float | None,
    has_new: bool,
) -> str:
    if has_new and not in_top5:
        return "THEMATIC"
    if in_top5:
        if action_primary in REDUCE_ACTIONS:
            return "CORE"
        if weight_delta_pp_max <= 0.25 and (share_growth_max or 0) <= 15:
            return "CORE"
        return "CORE"
    if in_top_decile:
        if (share_growth_max or 0) > 25 or weight_delta_pp_max > 0.35:
            return "THEMATIC"
        return "SATELLITE"
    if (share_growth_max or 0) > 40 or weight_delta_pp_max > 0.5:
        return "THEMATIC"
    if action_primary in REDUCE_ACTIONS:
        return "SATELLITE"
    return "SATELLITE"


def _build_theme_flow_matrix(
    signals: list[StockSignal],
) -> list[tuple[str, str, float]]:
    out_flow: dict[str, float] = {}
    in_flow: dict[str, float] = {}
    for sig in signals:
        for leg in sig.legs:
            mag = _flow_magnitude(leg)
            if leg.action in ADD_ACTIONS and leg.share_delta > 0:
                in_flow[leg.theme] = in_flow.get(leg.theme, 0.0) + mag
            elif leg.action in REDUCE_ACTIONS and leg.share_delta < 0:
                out_flow[leg.theme] = out_flow.get(leg.theme, 0.0) + mag
    pairs: list[tuple[str, str, float]] = []
    for t_out, f_out in out_flow.items():
        if t_out == "UNKNOWN":
            continue
        for t_in, f_in in in_flow.items():
            if t_in == "UNKNOWN" or t_out == t_in:
                continue
            pairs.append((t_out, t_in, min(f_out, f_in)))
    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


def _assign_rotation_tags(
    signals: list[StockSignal],
    pairs: list[tuple[str, str, float]],
) -> None:
    if not pairs:
        return
    top_score = pairs[0][2]
    if top_score <= 0:
        return
    threshold = top_score * ROTATION_PAIR_MIN_RATIO
    active = [(a, b, s) for a, b, s in pairs if s >= threshold][:3]
    active_set = {(a, b) for a, b, _ in active}

    for sig in signals:
        for leg in sig.legs:
            if leg.action in ADD_ACTIONS and leg.share_delta > 0:
                for t_out, t_in, score in active:
                    if leg.theme == t_in and (t_out, t_in) in active_set:
                        sig.rotation_in = f"{t_out}→{t_in}"
                        break
            elif leg.action in REDUCE_ACTIONS and leg.share_delta < 0:
                for t_out, t_in, score in active:
                    if leg.theme == t_out and (t_out, t_in) in active_set:
                        sig.rotation_out = f"{t_out}→{t_in}"
                        break


def _aggregate_stock_signals(legs: list[ChangeLeg]) -> list[StockSignal]:
    by_id: dict[str, StockSignal] = {}
    for leg in legs:
        sig = by_id.setdefault(
            leg.stock_id,
            StockSignal(stock_id=leg.stock_id, stock_name=leg.stock_name, theme=leg.theme),
        )
        if leg.stock_name and not sig.stock_name:
            sig.stock_name = leg.stock_name
        sig.legs.append(leg)
        if leg.theme != "UNKNOWN":
            sig.theme = leg.theme

    for sig in by_id.values():
        adds = [lg for lg in sig.legs if lg.action in ADD_ACTIONS and lg.share_delta > 0]
        reds = [lg for lg in sig.legs if lg.action in REDUCE_ACTIONS and lg.share_delta < 0]
        if adds and reds:
            sig.net_side = "mixed"
        elif adds:
            sig.net_side = "add"
        elif reds:
            sig.net_side = "reduce"
        else:
            sig.net_side = "flat"

        ranks = [lg.weight_rank for lg in sig.legs if lg.weight_rank is not None]
        sig.weight_rank_best = min(ranks) if ranks else None
        sig.in_top5_any = any(lg.in_top5 for lg in sig.legs)
        sig.in_top_decile_any = any(lg.in_top_decile for lg in sig.legs)

        add_deltas = [lg.weight_delta_pp for lg in adds]
        sig.weight_delta_pp_max = max(add_deltas, default=0.0)
        if not add_deltas and reds:
            sig.weight_delta_pp_max = min((lg.weight_delta_pp for lg in reds), default=0.0)

        growths = [lg.share_growth_pct for lg in sig.legs if lg.share_growth_pct is not None]
        sig.share_growth_pct_max = max(growths) if growths else None

        flows = [lg.flow_ntd for lg in sig.legs if lg.flow_ntd is not None]
        sig.flow_ntd_total = sum(flows) if flows else None

        primary_action = adds[0].action if adds else (reds[0].action if reds else "不变")
        sig.portfolio_role = _infer_portfolio_role(
            in_top5=sig.in_top5_any,
            in_top_decile=sig.in_top_decile_any,
            action_primary=primary_action,
            weight_delta_pp_max=abs(sig.weight_delta_pp_max),
            share_growth_max=sig.share_growth_pct_max,
            has_new=any(lg.action == "新进" for lg in sig.legs),
        )

    return list(by_id.values())


def _apply_conviction_scores(signals: list[StockSignal]) -> None:
    movers = [s for s in signals if s.net_side in ("add", "reduce")]
    if not movers:
        return

    z_wt = _zscore_series([s.weight_delta_pp_max for s in movers])
    z_sh = _zscore_series(
        [
            s.share_growth_pct_max if s.share_growth_pct_max is not None else 0.0
            for s in movers
        ]
    )
    z_fl = _zscore_series(
        [
            abs(s.flow_ntd_total) if s.flow_ntd_total is not None else 0.0
            for s in movers
        ]
    )

    raw_scores: list[float] = []
    for i, sig in enumerate(movers):
        score = 0.45 * z_wt[i] + 0.35 * z_sh[i] + 0.20 * z_fl[i]
        if sig.net_side == "reduce":
            score = -abs(score)
        sig.conviction_score = score
        raw_scores.append(abs(score))

    sorted_abs = sorted(raw_scores)
    p50 = _percentile(sorted_abs, CONVICTION_MED_PCT)
    p90 = _percentile(sorted_abs, CONVICTION_HIGH_PCT)

    for sig in movers:
        a = abs(sig.conviction_score)
        if a >= p90:
            sig.conviction_level = "HIGH"
        elif a >= p50:
            sig.conviction_level = "MEDIUM"
        elif a > 0:
            sig.conviction_level = "LOW"
        else:
            sig.conviction_level = "NONE"


@dataclass(frozen=True)
class AlignedSignalResult:
    prev_date: str
    curr_date: str
    etf_codes: tuple[str, ...]
    signals: list[StockSignal]


def build_aligned_signals(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> AlignedSignalResult | None:
    """在最大對齊子集上計算 L3–L5（至少 2 檔 ETF 同窗口）。"""
    cohort = resolve_aligned_cohort(conn, etf_codes, min_etfs=2)
    if cohort is None:
        return None
    prev_date, curr_date = cohort.prev_date, cohort.curr_date
    active = cohort.etf_codes
    legs = _collect_legs_aligned(conn, active, curr_date, prev_date)
    signals = _aggregate_stock_signals(legs)
    _apply_conviction_scores(signals)
    pairs = _build_theme_flow_matrix(signals)
    _assign_rotation_tags(signals, pairs)
    from position_intent import apply_position_intents

    apply_position_intents(signals)
    signals.sort(
        key=lambda s: (s.net_side != "add", -abs(s.conviction_score)),
    )
    return AlignedSignalResult(
        prev_date=prev_date,
        curr_date=curr_date,
        etf_codes=active,
        signals=signals,
    )


def build_aligned_signals_or_raise(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> AlignedSignalResult:
    out = build_aligned_signals(conn, etf_codes)
    if out is None:
        raise ValueError("無足夠 ETF 對齊同一 snapshot 窗口，無法計算 L3–L5")
    return out
