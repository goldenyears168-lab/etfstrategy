#!/usr/bin/env python3
"""
Score Engine：預設 p6-tier 三軸分層 · env SCORE_VERSION=p5-v1 可回退八維加權。
技術（價位分）與籌碼為 Gate，不與 Flow/Expectation 加權混算。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import os

from market_labels import (
    CHIP_NEUTRAL,
    CHIP_TAG_SCORE,
    ENTRY_BREAKOUT,
    ENTRY_OVEREXTENDED,
    ENTRY_PULLBACK,
    ENTRY_SKIP,
    ENTRY_TAG_VOLUME,
    ENTRY_WAIT,
    VOL_DOWN,
    VOL_SURGE,
    VOL_UP,
    WL_CANDIDATE,
    WL_EXCLUDED,
    WL_GENERAL,
    WL_PRIMARY,
    format_entry_display,
)
from investment_themes import stock_theme
from market_analytics import analytics_entry_tags, build_stock_analytics, compute_rs_percentile_map
import project_config as pc
from project_config import (
    CATALYST_BASELINE_EVENT,
    CATALYST_BASELINE_MONEY,
    DEFAULT_ETF_CODES,
    FLOW_NO_SIGNAL,
    NEUTRAL_SUBSCORE,
    NON_TECH_THEMES,
    P5_FLOW_GATE_MIN,
    P5_RISK_GATE_MIN,
    P5_SCORE_GATE_MIN,
    P6_CROWD_GATE_MIN,
    P6_FLOW_CANDIDATE_MIN,
    P6_FLOW_GATE_MIN,
    P6_RISK_GATE_MIN,
    P6_SCORE_GATE_MIN,
    P6_SHORT_FAVOR_GATE_MIN,
    P6_TIMING_GATE_MIN,
    SCORE_VERSION_P5_V2,
    SCORE_VERSION_P6,
    risk_as_gate_enabled,
    is_tier_score_version,
    ROLE_WEIGHT,
    SCORE_VERSION_DEFAULT,
    SCORE_VERSION_P5,
    SMART_MONEY_CHIP_BLEND,
    SMART_MONEY_FLOW_BLEND,
    WEIGHT_CATALYST,
    WEIGHT_EXPECTATION,
    WEIGHT_FUNDAMENTAL,
    WEIGHT_RISK,
    WEIGHT_SMART_MONEY,
    active_score_version,
    score_weights,
    parse_etf_codes,
)
from research_universe import (
    UniverseEntry,
    build_research_universe,
)
from signal_engine import StockSignal, build_aligned_signals
from expectation_engine import SubscoreResult, load_subscores_for_stocks
from chip_narrative import build_chip_scores, compose_chip_narrative
from stock_context import (
    TechnicalSnapshot,
    build_chip_resonance,
    compute_technical,
    load_latest_institutional,
)
from stock_db import (
    DEFAULT_DB_PATH,
    connect,
    load_latest_tech_risk,
    load_stock_beta_map,
    upsert_investment_scores,
)

# 執行期版本（env SCORE_VERSION=p4-v2 可回退）
SCORE_VERSION = active_score_version()

REGIME_NEUTRAL = "neutral"


@dataclass(frozen=True)
class FlowContext:
    regime: str = REGIME_NEUTRAL
    repeat_index: int = 0
    pre_2w_return_pct: float | None = None
    prior_pre_2w_return_pct: float | None = None
    rs_percentile: float | None = None
    l2_level: str = ""
    pyramiding_pass: bool = True
    pyramiding_flags: tuple[str, ...] = ()
    l2_flags: tuple[str, ...] = ()
    prior_add_dates: tuple[str, ...] = ()


def build_flow_context_map(*_args: object, **_kwargs: object) -> dict[str, FlowContext]:
    return {}


def apply_l2_flow_boost(flow: float, _l2_level: str) -> float:
    return flow


def timing_qualifies_for_regime(*_args: object, **_kwargs: object) -> bool:
    return True


def single_qualifies_primary(*_args: object, **_kwargs: object) -> bool:
    return True


def flow_context_from_metadata(_meta: dict) -> FlowContext | None:
    return None


def flow_context_to_metadata(ctx: FlowContext | None) -> dict:
    if ctx is None:
        return {}
    return {"flow_regime": ctx.regime}


OVEREXTENDED_MA_PCT = 18.0
BREAKOUT_POS_52W = 90.0
BREAKOUT_DIST_HIGH_PCT = -3.0
PULLBACK_MA20_BAND_PCT = 5.0
PULLBACK_MAX_POS_52W = 85.0
STRONG_TREND_FLOW_MIN = 65.0
STRONG_TREND_CHIP_MIN = 70.0


@dataclass(frozen=True)
class EntryContext:
    signal: str
    tags: tuple[str, ...]

    @property
    def display(self) -> str:
        return format_entry_display(self.signal, self.tags)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def extension_pct(tech: TechnicalSnapshot | None) -> float | None:
    if tech is None:
        return None
    vals = [v for v in (tech.dist_ma20_pct, tech.dist_ma60_pct) if v is not None]
    return max(vals) if vals else None


def percentile_value(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return OVEREXTENDED_MA_PCT
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def overextended_min_pct(extensions: list[float]) -> float:
    abs_min = _env_float("ENTRY_OVEREXTENDED_ABS_MIN", 12.0)
    rel_pct = _env_float("ENTRY_OVEREXTENDED_REL_PCT", 75.0)
    if not extensions:
        return max(abs_min, OVEREXTENDED_MA_PCT)
    pval = percentile_value(sorted(extensions), rel_pct)
    return max(abs_min, pval)


def _is_extended(tech: TechnicalSnapshot, *, overextended_min: float) -> bool:
    ext = extension_pct(tech)
    return ext is not None and ext >= overextended_min


def has_strong_trend(
    tech: TechnicalSnapshot | None,
    *,
    flow_score: float | None = None,
    chip_score: float | None = None,
    overextended_min: float | None = None,
) -> bool:
    if tech is None or flow_score is None or chip_score is None:
        return False
    if flow_score < STRONG_TREND_FLOW_MIN or chip_score < STRONG_TREND_CHIP_MIN:
        return False
    thresh = overextended_min if overextended_min is not None else OVEREXTENDED_MA_PCT
    if not _is_extended(tech, overextended_min=thresh):
        return False
    if tech.vol_label == VOL_DOWN:
        return False
    if tech.vol_label in (VOL_SURGE, VOL_UP):
        return True
    if tech.position_52w_pct is not None and tech.position_52w_pct >= 85.0:
        return True
    return False


def classify_entry_context(
    tech: TechnicalSnapshot | None,
    *,
    net_side: str | None = None,
    flow_score: float | None = None,
    chip_score: float | None = None,
    overextended_min: float | None = None,
) -> EntryContext:
    if net_side == "reduce":
        return EntryContext(ENTRY_SKIP, ())
    if tech is None:
        return EntryContext(ENTRY_WAIT, ())
    ext_thresh = overextended_min if overextended_min is not None else OVEREXTENDED_MA_PCT
    if _is_extended(tech, overextended_min=ext_thresh):
        signal = ENTRY_OVEREXTENDED
    else:
        signal = ENTRY_WAIT
        pos = tech.position_52w_pct
        dist_hi = tech.dist_from_52w_high_pct
        if (
            pos is not None
            and pos > BREAKOUT_POS_52W
            and dist_hi is not None
            and dist_hi > BREAKOUT_DIST_HIGH_PCT
        ):
            signal = ENTRY_BREAKOUT
        elif (
            tech.dist_ma20_pct is not None
            and abs(tech.dist_ma20_pct) <= PULLBACK_MA20_BAND_PCT
            and (pos is None or pos < PULLBACK_MAX_POS_52W)
        ):
            signal = ENTRY_PULLBACK
    tags: list[str] = []
    if signal == ENTRY_OVEREXTENDED and has_strong_trend(
        tech,
        flow_score=flow_score,
        chip_score=chip_score,
        overextended_min=ext_thresh,
    ):
        tags.append(ENTRY_TAG_VOLUME)
    return EntryContext(signal, tuple(tags))


def classify_entry_context_batch(
    items: list[tuple[str, TechnicalSnapshot | None, str | None, float | None, float | None]],
) -> dict[str, EntryContext]:
    extensions: list[float] = []
    for _sid, tech, _net, _flow, _chip in items:
        if tech is None:
            continue
        ext = extension_pct(tech)
        if ext is not None:
            extensions.append(ext)
    thresh = overextended_min_pct(extensions)
    return {
        sid: classify_entry_context(
            tech,
            net_side=net_side,
            flow_score=flow,
            chip_score=chip,
            overextended_min=thresh,
        )
        for sid, tech, net_side, flow, chip in items
    }


def is_overextended_without_strong_trend(ctx: EntryContext) -> bool:
    return ctx.signal == ENTRY_OVEREXTENDED and ENTRY_TAG_VOLUME not in ctx.tags


def _score_report_banner() -> str:
    if is_tier_score_version():
        return (
            "=== 綜合研究評分（p6-tier · Flow70/Exp30 · "
            "籌碼Gate／風險Gate／價位分層 · 非加權八維）==="
        )
    if SCORE_VERSION == SCORE_VERSION_P5_V2 or risk_as_gate_enabled():
        return (
            "=== 綜合研究評分（p5-v2 · Flow/Inst/短壓/Crowd/Cat/Exp/Fun · "
            "Risk 風控 Gate · 技術僅作價位參考）==="
        )
    if SCORE_VERSION == SCORE_VERSION_P5:
        return (
            "=== 綜合研究評分（p5-v1 · Flow30/Inst20/短壓10/Crowd10/"
            "Cat10/Exp10/Fun5/Risk5 · 技術僅作價位參考）==="
        )
    return (
        "=== 綜合研究評分（p4-v2 · 資金籌碼50／催化10／預期15／基本15／風險10 · "
        "技術僅作價位參考）==="
    )

@dataclass(frozen=True)
class DimensionScores:
    """flow/chip 為分量；p5 另含 crowd / short_favor。"""

    flow: float
    chip: float
    catalyst: float
    expectation: float
    fundamental: float
    risk: float
    timing: float
    crowd: float = NEUTRAL_SUBSCORE
    short_favor: float = NEUTRAL_SUBSCORE

    @property
    def smart_money(self) -> float:
        raw = (
            SMART_MONEY_FLOW_BLEND * self.flow
            + SMART_MONEY_CHIP_BLEND * self.chip
        )
        return round(min(100.0, raw), 1)

    @property
    def investment_score(self) -> float:
        v = pc.active_score_version()
        w = pc.score_weights(v)
        if pc.is_tier_score_version(v):
            raw = w["etf_flow"] * self.flow + w["expectation"] * self.expectation
        elif "etf_flow" in w:
            raw = (
                w["etf_flow"] * self.flow
                + w["institutional"] * self.chip
                + w["short_favor"] * self.short_favor
                + w["crowd"] * self.crowd
                + w["catalyst"] * self.catalyst
                + w["expectation"] * self.expectation
                + w["fundamental"] * self.fundamental
                + w.get("risk", 0.0) * self.risk
            )
        else:
            raw = (
                w["smart_money"] * self.smart_money
                + w["catalyst"] * self.catalyst
                + w["expectation"] * self.expectation
                + w["fundamental"] * self.fundamental
                + w["risk"] * self.risk
            )
        return round(raw, 1)


@dataclass(frozen=True)
class ScoredEntry:
    entry: UniverseEntry
    dimensions: DimensionScores
    watchlist: str
    position_intent: str | None
    tech_risk_flag: str | None
    entry_signal: str
    entry_tags: tuple[str, ...]
    chip_tag: str
    metadata: dict


def _scale_minmax(pool: list[float], value: float) -> float:
    if not pool:
        return 50.0
    lo, hi = min(pool), max(pool)
    if hi <= lo:
        return 50.0
    return 100.0 * (value - lo) / (hi - lo)


def _is_tech_theme(theme: str) -> bool:
    return theme not in NON_TECH_THEMES


def chip_gate_eval(
    *,
    crowd: float,
    short_favor: float,
    chip_ext: dict | None = None,
) -> tuple[bool, list[str]]:
    """籌碼紅綠燈：不通過時觀察名單封頂一般觀察。"""
    flags: list[str] = []
    if crowd < P6_CROWD_GATE_MIN:
        flags.append("Crowd偏低")
    if short_favor < P6_SHORT_FAVOR_GATE_MIN:
        flags.append("短壓偏高")
    ext = chip_ext or {}
    return (len(flags) == 0, flags)


def _cap_watchlist_for_flow(tier: str, flow_score: float | None) -> str:
    if flow_score is not None and flow_score < P6_FLOW_CANDIDATE_MIN:
        if tier == WL_PRIMARY:
            return WL_GENERAL
        if tier == WL_GENERAL:
            return WL_CANDIDATE
    return tier


def watchlist_tier(
    investment_score: float,
    smart_money: float,
    *,
    entry_ctx: EntryContext,
    flow_score: float | None = None,
    risk_score: float | None = None,
    score_version: str | None = None,
    crowd: float | None = None,
    short_favor: float | None = None,
    timing_score: float | None = None,
    chip_ext: dict | None = None,
    flow_ctx: FlowContext | None = None,
) -> str:
    if entry_ctx.signal == ENTRY_SKIP:
        return WL_EXCLUDED
    v = score_version or active_score_version()
    risk_min = P6_RISK_GATE_MIN if is_tier_score_version(v) else P5_RISK_GATE_MIN
    score_min = P6_SCORE_GATE_MIN if is_tier_score_version(v) else P5_SCORE_GATE_MIN
    if risk_as_gate_enabled() and risk_score is not None and risk_score < risk_min:
        if investment_score >= score_min:
            return WL_GENERAL
        return WL_EXCLUDED if investment_score < 60 else WL_GENERAL
    if is_tier_score_version(v):
        gate_pass, _ = chip_gate_eval(
            crowd=crowd if crowd is not None else NEUTRAL_SUBSCORE,
            short_favor=short_favor if short_favor is not None else NEUTRAL_SUBSCORE,
            chip_ext=chip_ext,
        )
        flow_gate = P6_FLOW_GATE_MIN
        gate = flow_score if flow_score is not None else smart_money
        regime = flow_ctx.regime if flow_ctx else REGIME_NEUTRAL
        timing_ok = timing_qualifies_for_regime(
            regime,
            timing_score,
            entry_ctx.signal,
        )
        single_ok = single_qualifies_primary(
            flow_score,
            flow_ctx.l2_level if flow_ctx else "",
        )
        pyramid_ok = flow_ctx.pyramiding_pass if flow_ctx else True
        qualifies_a = (
            investment_score >= score_min
            and gate >= flow_gate
            and gate_pass
            and timing_ok
            and single_ok
            and pyramid_ok
        )
    elif v in (SCORE_VERSION_P5, SCORE_VERSION_P5_V2):
        gate = flow_score if flow_score is not None else smart_money
        qualifies_a = (
            investment_score >= P5_SCORE_GATE_MIN and gate >= P5_FLOW_GATE_MIN
        )
    else:
        qualifies_a = investment_score >= 75.0 and smart_money >= 72.0
    if qualifies_a:
        if is_overextended_without_strong_trend(entry_ctx):
            tier = WL_GENERAL
        else:
            tier = WL_PRIMARY
    elif investment_score >= 65.0:
        tier = WL_GENERAL
    elif investment_score >= 55.0:
        tier = WL_CANDIDATE
    else:
        tier = WL_EXCLUDED
    if is_tier_score_version(v):
        gate_pass, _ = chip_gate_eval(
            crowd=crowd if crowd is not None else NEUTRAL_SUBSCORE,
            short_favor=short_favor if short_favor is not None else NEUTRAL_SUBSCORE,
            chip_ext=chip_ext,
        )
        if not gate_pass and tier == WL_PRIMARY:
            tier = WL_GENERAL
        tier = _cap_watchlist_for_flow(tier, flow_score)
        if flow_ctx is not None:
            if flow_ctx.l2_flags and tier == WL_PRIMARY:
                tier = WL_GENERAL
            if not flow_ctx.pyramiding_pass:
                if tier == WL_PRIMARY:
                    tier = WL_GENERAL
                elif tier == WL_GENERAL:
                    tier = WL_CANDIDATE
    return tier


def _rescored_with_entry_ctx(
    scored: ScoredEntry,
    entry_ctx: EntryContext,
    tech: TechnicalSnapshot | None,
    *,
    overextended_min: float | None = None,
) -> ScoredEntry:
    timing = timing_score(tech, entry_ctx=entry_ctx)
    d = scored.dimensions
    dims = DimensionScores(
        flow=d.flow,
        chip=d.chip,
        catalyst=d.catalyst,
        expectation=d.expectation,
        fundamental=d.fundamental,
        risk=d.risk,
        timing=timing,
        crowd=d.crowd,
        short_favor=d.short_favor,
    )
    wl = watchlist_tier(
        dims.investment_score,
        dims.smart_money,
        entry_ctx=entry_ctx,
        flow_score=d.flow,
        risk_score=d.risk,
        score_version=active_score_version(),
        crowd=d.crowd,
        short_favor=d.short_favor,
        timing_score=timing,
        chip_ext=scored.metadata.get("chip_extended"),
        flow_ctx=flow_context_from_metadata(scored.metadata),
    )
    meta = dict(scored.metadata)
    meta["entry_signal"] = entry_ctx.signal
    meta["entry_tags"] = list(entry_ctx.tags)
    meta["timing_score"] = timing
    if overextended_min is not None:
        meta["overextended_min_pct"] = round(overextended_min, 2)
    return ScoredEntry(
        entry=scored.entry,
        dimensions=dims,
        watchlist=wl,
        position_intent=scored.position_intent,
        tech_risk_flag=scored.tech_risk_flag,
        entry_signal=entry_ctx.signal,
        entry_tags=entry_ctx.tags,
        chip_tag=scored.chip_tag,
        metadata=meta,
    )


def catalyst_subscore(entry: UniverseEntry) -> float:
    if entry.pool_reason == "money":
        return CATALYST_BASELINE_MONEY
    return CATALYST_BASELINE_EVENT


def flow_subscore(
    entry: UniverseEntry,
    signal_by_id: dict[str, StockSignal],
    *,
    conv_pool: list[float],
    cons_pool: list[float],
) -> float:
    sig = signal_by_id.get(entry.stock_id)
    if sig is None:
        return FLOW_NO_SIGNAL
    nc = _scale_minmax(conv_pool, sig.conviction_score)
    ns = _scale_minmax(cons_pool, sig.consensus_score)
    rot = 100.0 if (sig.rotation_in or sig.rotation_out) else 0.0
    role = ROLE_WEIGHT.get(sig.portfolio_role, 0.5) * 100.0
    raw = 0.40 * nc + 0.40 * ns + 0.10 * rot + 0.10 * role
    score = min(100.0, round(raw, 1))
    if sig.net_side != "add":
        score = min(score, 35.0)
    return score


def chip_subscore_for_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    etf_codes: tuple[str, ...],
    name_by_id: dict[str, str],
) -> tuple[float, str]:
    rows = build_chip_resonance(conn, etf_codes, [stock_id], name_by_id)
    tag = rows[0].tag if rows else CHIP_NEUTRAL
    base = CHIP_TAG_SCORE.get(tag, NEUTRAL_SUBSCORE)
    inst = load_latest_institutional(conn, stock_id)
    if inst and inst.foreign_net is not None and inst.foreign_net > 0:
        if inst.investment_trust_net is not None and inst.investment_trust_net > 0:
            base = min(100.0, base + 3.0)
        else:
            base = min(100.0, base + 1.0)
    return round(base, 1), tag


def timing_score(
    tech: TechnicalSnapshot | None,
    *,
    entry_ctx: EntryContext,
) -> float:
    """進場適配度（僅報告/metadata，不進 Investment Score 加權）。"""
    if tech is None:
        return 45.0
    if entry_ctx.signal == ENTRY_OVEREXTENDED:
        if ENTRY_TAG_VOLUME in entry_ctx.tags:
            return 62.0
        return 32.0
    if entry_ctx.signal == ENTRY_BREAKOUT:
        return 88.0
    if entry_ctx.signal == ENTRY_PULLBACK:
        return 78.0
    if entry_ctx.signal == ENTRY_SKIP:
        return 30.0
    if tech.vol_label in (VOL_SURGE, VOL_UP):
        return 62.0
    if tech.vol_label == VOL_DOWN:
        return 48.0
    return 55.0


def risk_subscore(
    stock_id: str,
    *,
    beta_row: sqlite3.Row | None,
    tech_risk: sqlite3.Row | None,
) -> tuple[float, str | None]:
    score = 70.0
    flag: str | None = None
    if beta_row is not None and beta_row["beta"] is not None:
        beta = float(beta_row["beta"])
        if beta >= 1.6:
            score -= 25.0
        elif beta >= 1.3:
            score -= 15.0
        elif beta < 0.8:
            score += 5.0
    theme = stock_theme(stock_id)
    if tech_risk is not None and _is_tech_theme(theme):
        tsm_ret = tech_risk["tsm_daily_return_pct"]
        if tsm_ret is not None and float(tsm_ret) < -2.0:
            score -= 20.0
            flag = "TSM_ADR_LT_-2PCT"
    if beta_row is not None and beta_row["beta"] is not None:
        if float(beta_row["beta"]) >= 1.6 and flag is None:
            flag = "HIGH_BETA"
    return round(max(0.0, min(100.0, score)), 1), flag


def score_universe_entry(
    entry: UniverseEntry,
    *,
    signal_by_id: dict[str, StockSignal],
    conv_pool: list[float],
    cons_pool: list[float],
    beta_map: dict[str, sqlite3.Row],
    tech_risk: sqlite3.Row | None,
    conn: sqlite3.Connection,
    trade_date: str | None,
    etf_codes: tuple[str, ...],
    name_by_id: dict[str, str],
    expectation: SubscoreResult | None = None,
    fundamental: SubscoreResult | None = None,
    flow_ctx: FlowContext | None = None,
) -> ScoredEntry:
    del trade_date
    sig = signal_by_id.get(entry.stock_id)
    tech = compute_technical(conn, entry.stock_id)
    flow = flow_subscore(
        entry,
        signal_by_id,
        conv_pool=conv_pool,
        cons_pool=cons_pool,
    )
    l2_level = sig.consensus_level if sig else ""
    flow = apply_l2_flow_boost(flow, l2_level)
    chip, chip_tag = chip_subscore_for_stock(
        conn, entry.stock_id, etf_codes, name_by_id
    )
    entry_ctx = classify_entry_context(
        tech,
        net_side=sig.net_side if sig else None,
        flow_score=flow,
        chip_score=chip,
    )
    analytics = build_stock_analytics(
        conn,
        entry.stock_id,
        tech=tech,
        entry_signal=entry_ctx.signal,
    )
    extra_tags = analytics_entry_tags(analytics)
    if extra_tags:
        entry_ctx = EntryContext(
            entry_ctx.signal,
            entry_ctx.tags + tuple(extra_tags),
        )
    timing = timing_score(tech, entry_ctx=entry_ctx)
    cat = catalyst_subscore(entry)
    fund_res = fundamental or SubscoreResult(NEUTRAL_SUBSCORE, "DATA_MISSING", {})
    fund = fund_res.score
    exp_res = expectation or SubscoreResult(NEUTRAL_SUBSCORE, "DATA_MISSING", {})
    exp = exp_res.score
    risk, tech_flag = risk_subscore(
        entry.stock_id,
        beta_row=beta_map.get(entry.stock_id),
        tech_risk=tech_risk,
    )
    net_side = sig.net_side if sig else None
    chip_ext = build_chip_scores(conn, entry.stock_id, etf_net_side=net_side)
    crowd = float(chip_ext.get("crowd_score", NEUTRAL_SUBSCORE))
    short_favor = round(
        100.0 - float(chip_ext.get("short_pressure_score", NEUTRAL_SUBSCORE)),
        1,
    )
    dims = DimensionScores(
        flow=flow,
        chip=chip,
        catalyst=cat,
        expectation=exp,
        fundamental=fund,
        risk=risk,
        timing=timing,
        crowd=crowd,
        short_favor=max(0.0, min(100.0, short_favor)),
    )
    wl = watchlist_tier(
        dims.investment_score,
        dims.smart_money,
        entry_ctx=entry_ctx,
        flow_score=flow,
        risk_score=risk,
        crowd=crowd,
        short_favor=dims.short_favor,
        timing_score=timing,
        chip_ext=chip_ext,
        flow_ctx=flow_ctx,
    )
    chip_pass, chip_flags = chip_gate_eval(
        crowd=crowd, short_favor=dims.short_favor, chip_ext=chip_ext
    )
    meta = {
        "expectation": exp_res.status,
        "fundamental": fund_res.status,
        "expectation_detail": exp_res.detail,
        "fundamental_detail": fund_res.detail,
        "entry_signal": entry_ctx.signal,
        "entry_tags": list(entry_ctx.tags),
        "timing_score": timing,
        "chip_tag": chip_tag,
        "flow_score": flow,
        "chip_score": chip,
        "crowd_score": crowd,
        "short_favor_score": dims.short_favor,
        "score_version": active_score_version(),
        "weights": score_weights(),
        "timing_not_in_total": True,
        "chip_gate_pass": chip_pass,
        "chip_gate_flags": chip_flags,
        "scoring_mode": "tier" if is_tier_score_version() else "weighted",
        "analytics": analytics.to_dict(),
    }
    if flow_ctx is not None:
        meta.update(flow_context_to_metadata(flow_ctx))
    if tech_flag:
        meta["risk_gate"] = tech_flag
    meta["chip_extended"] = chip_ext
    if not chip_pass:
        narrative = compose_chip_narrative(conn, entry.stock_id, etf_net_side=net_side)
        if narrative:
            meta["chip_narrative"] = narrative
    if tech is not None:
        meta["technical"] = {
            "ma20": tech.ma20,
            "position_52w_pct": tech.position_52w_pct,
            "dist_from_52w_high_pct": tech.dist_from_52w_high_pct,
            "vol_label": tech.vol_label,
            "vol_ratio_5d": tech.vol_ratio_5d,
        }
    return ScoredEntry(
        entry=entry,
        dimensions=dims,
        watchlist=wl,
        position_intent=sig.position_intent if sig else None,
        tech_risk_flag=tech_flag,
        entry_signal=entry_ctx.signal,
        entry_tags=entry_ctx.tags,
        chip_tag=chip_tag,
        metadata=meta,
    )


def build_signal_map(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
) -> dict[str, StockSignal]:
    aligned = build_aligned_signals(conn, etf_codes)
    if aligned is None:
        return {}
    return {s.stock_id: s for s in aligned.signals}


def run_score_engine(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    top_n: int = 10,
    max_pool: int = 20,
) -> tuple[list[ScoredEntry], str | None] | None:
    universe = build_research_universe(
        conn,
        etf_codes,
        top_n=top_n,
        max_pool=max_pool,
    )
    if universe is None or not universe.entries:
        return None

    signal_by_id = build_signal_map(conn, etf_codes)
    signals_in_pool = [
        signal_by_id[e.stock_id]
        for e in universe.entries
        if e.stock_id in signal_by_id
    ]
    conv_pool = [s.conviction_score for s in signals_in_pool]
    cons_pool = [s.consensus_score for s in signals_in_pool]

    beta_map, _ = load_stock_beta_map(conn)
    tech_risk = load_latest_tech_risk(conn)
    trade_date = universe.curr_date
    stock_ids = [e.stock_id for e in universe.entries]
    name_by_id = {e.stock_id: e.stock_name for e in universe.entries}
    exp_map, fund_map = load_subscores_for_stocks(conn, stock_ids)
    rs_pct_map = compute_rs_percentile_map(conn, stock_ids)
    flow_ctx_map: dict[str, FlowContext] = {}
    if trade_date:
        flow_ctx_map = build_flow_context_map(
            conn,
            trade_date,
            stock_ids,
            signal_by_id,
            rs_pct_map,
        )

    scored = [
        score_universe_entry(
            e,
            signal_by_id=signal_by_id,
            conv_pool=conv_pool,
            cons_pool=cons_pool,
            beta_map=beta_map,
            tech_risk=tech_risk,
            conn=conn,
            trade_date=trade_date,
            etf_codes=etf_codes,
            name_by_id=name_by_id,
            expectation=exp_map.get(e.stock_id),
            fundamental=fund_map.get(e.stock_id),
            flow_ctx=flow_ctx_map.get(e.stock_id),
        )
        for e in universe.entries
    ]

    tech_by_id: dict[str, TechnicalSnapshot | None] = {
        e.stock_id: compute_technical(conn, e.stock_id) for e in universe.entries
    }
    batch_items: list[
        tuple[str, TechnicalSnapshot | None, str | None, float | None, float | None]
    ] = []
    scored_by_id = {s.entry.stock_id: s for s in scored}
    for e in universe.entries:
        prev = scored_by_id[e.stock_id]
        sig = signal_by_id.get(e.stock_id)
        batch_items.append(
            (
                e.stock_id,
                tech_by_id[e.stock_id],
                sig.net_side if sig else None,
                prev.dimensions.flow,
                prev.dimensions.chip,
            )
        )
    extensions = [
        ext
        for tech in tech_by_id.values()
        if (ext := extension_pct(tech)) is not None
    ]
    ext_min = overextended_min_pct(extensions)
    ctx_map = classify_entry_context_batch(batch_items)
    scored = [
        _rescored_with_entry_ctx(
            scored_by_id[e.stock_id],
            ctx_map[e.stock_id],
            tech_by_id[e.stock_id],
            overextended_min=ext_min,
        )
        for e in universe.entries
    ]
    scored.sort(key=lambda x: x.dimensions.investment_score, reverse=True)
    return scored, universe.curr_date


def scored_rows_for_db(
    scored: list[ScoredEntry],
    as_of_date: str,
) -> list[dict]:
    rows: list[dict] = []
    for s in scored:
        e = s.entry
        d = s.dimensions
        rows.append(
            {
                "stock_id": e.stock_id,
                "as_of_date": as_of_date,
                "score_version": active_score_version(),
                "stock_name": e.stock_name,
                "smart_money": d.smart_money,
                "catalyst": d.catalyst,
                "expectation": d.expectation,
                "fundamental": d.fundamental,
                "risk": d.risk,
                "investment_score": d.investment_score,
                "watchlist": s.watchlist,
                "pool_reason": e.pool_reason,
                "money_rank": e.money_rank,
                "event_rank": e.event_rank,
                "position_intent": s.position_intent,
                "tech_risk_flag": s.tech_risk_flag,
                "metadata_json": json.dumps(s.metadata, ensure_ascii=False),
            }
        )
    return rows


def print_score_report(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    *,
    top_n: int = 10,
    max_pool: int = 20,
    quiet: bool = False,
    human: bool = False,
    sync_pm: bool = False,
) -> tuple[list[ScoredEntry], str] | None:
    result = run_score_engine(
        conn,
        etf_codes,
        top_n=top_n,
        max_pool=max_pool,
    )
    if result is None:
        if not human and not quiet:
            print("")
            print(_score_report_banner())
            print("  略過：無 Research Universe（需對齊 cohort）")
        return None

    scored, as_of_date = result
    if as_of_date is None:
        if not human and not quiet:
            print("")
            print(_score_report_banner())
            print("  略過：無 curr_date")
        return None

    if human:
        if sync_pm:
            from pm_watchlist import sync_pm_watchlist_from_scored

            sync_pm_watchlist_from_scored(conn, scored, etf_codes, as_of_date)
        return scored, as_of_date

    print("")
    print(_score_report_banner())
    print(f"  基準日 {as_of_date} · 評分版本 {SCORE_VERSION} · {len(scored)} 檔")
    if tech := load_latest_tech_risk(conn):
        tsm = tech["tsm_daily_return_pct"]
        tsm_s = f"{tsm:+.2f}%" if tsm is not None else "—"
        if not quiet:
            print(f"  tech_risk session={tech['session_date']} TSM ADR {tsm_s}")

    print("")
    print(
        f"{'代號':>6} {'名稱':<8} {'綜合評分':>6} {'觀察名單':<8} {'價位型態':<20} "
        f"{'資金籌碼':>6} {'ETF流向':>6} {'法人籌碼':>6} {'預期差':>6} {'基本面':>6} "
        f"{'風險面':>6} {'價位分':>6} {'籌碼標籤':<16}"
    )
    for s in scored:
        e = s.entry
        d = s.dimensions
        entry_d = format_entry_display(s.entry_signal, s.entry_tags)
        print(
            f"  {e.stock_id:>6} {e.stock_name:<8} {d.investment_score:>6.1f} "
            f"{s.watchlist:<8} {entry_d:<20} "
            f"{d.smart_money:>6.0f} {d.flow:>6.0f} {d.chip:>6.0f} "
            f"{d.expectation:>6.0f} {d.fundamental:>6.0f} {d.risk:>6.0f} "
            f"{d.timing:>6.0f} {s.chip_tag:<16}"
        )
    primary_count = sum(1 for s in scored if s.watchlist == WL_PRIMARY)
    if is_tier_score_version():
        gate_note = (
            f"Flow≥{P6_FLOW_GATE_MIN:.0f} 且籌碼Gate通過 且價位≥{P6_TIMING_GATE_MIN:.0f}"
        )
    elif SCORE_VERSION == SCORE_VERSION_P5:
        gate_note = f"Flow≥{P5_FLOW_GATE_MIN:.0f}"
    else:
        gate_note = "資金籌碼≥72"
    print(
        f"  首要觀察 {primary_count} 檔（綜合評分≥75 且 {gate_note}；"
        f"乖離過大且非量價齊揚者最高僅列一般觀察；價位分不計入綜合評分）"
    )

    if sync_pm:
        from pm_watchlist import print_pm_watchlist_report, sync_pm_watchlist_from_scored

        pm = sync_pm_watchlist_from_scored(conn, scored, etf_codes, as_of_date)
        print_pm_watchlist_report(pm, as_of_date=as_of_date)

    return scored, as_of_date


def main() -> int:
    parser = argparse.ArgumentParser(description="P4-v2 綜合研究評分 + 開盤前觀察名單")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--etf-codes", default=",".join(DEFAULT_ETF_CODES))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--max-pool", type=int, default=20)
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="寫入 investment_scores + pm_watchlist",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--human",
        action="store_true",
        help="收盤 digest 模式：只寫 DB，不印寬表",
    )
    args = parser.parse_args()

    codes = parse_etf_codes(args.etf_codes)
    conn = connect(args.db)
    try:
        out = print_score_report(
            conn,
            codes,
            top_n=args.top_n,
            max_pool=args.max_pool,
            quiet=args.quiet,
            human=args.human,
            sync_pm=args.sync_db,
        )
        if out and args.sync_db:
            scored, as_of = out
            n = upsert_investment_scores(
                conn, scored_rows_for_db(scored, as_of)
            )
            if not args.human:
                print(f"  DB：investment_scores upsert {n} 列")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
