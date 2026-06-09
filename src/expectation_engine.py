"""L8 / L8.5 子分：僅讀 DB，供 score_engine 使用。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from project_config import (
    ACCEL_SCALE_PP,
    GAP_SCALE_EPS_PCT,
    GAP_SCALE_ROE_PP,
    NEUTRAL_SUBSCORE,
)
from stock_db import load_latest_consensus_map, load_latest_fundamental_map


@dataclass(frozen=True)
class L85Inputs:
    actual_roe: float | None
    consensus_roe: float | None
    actual_eps: float | None
    consensus_eps: float | None
    revenue_yoy_pct: float | None
    revenue_mom_accel_pp: float | None


@dataclass(frozen=True)
class L8Inputs:
    pe: float | None
    pb: float | None
    roe_ttm: float | None
    dividend_yield: float | None


@dataclass(frozen=True)
class SubscoreResult:
    score: float
    status: str
    detail: dict[str, float | str | None]


def gap_to_subscore(gap: float, *, scale: float) -> float:
    return round(max(0.0, min(100.0, NEUTRAL_SUBSCORE + gap * scale)), 1)


def compute_expectation_subscore(inputs: L85Inputs) -> SubscoreResult:
    """預期差 + 營收加速度 → 0–100。缺資料回傳中性 50。"""
    parts: list[tuple[float, float]] = []
    detail: dict[str, float | str | None] = {}

    if inputs.actual_roe is not None and inputs.consensus_roe is not None:
        gap_pp = inputs.actual_roe - inputs.consensus_roe
        parts.append((gap_to_subscore(gap_pp, scale=GAP_SCALE_ROE_PP), 0.40))
        detail["roe_gap_pp"] = round(gap_pp, 2)

    if inputs.actual_eps is not None and inputs.consensus_eps is not None:
        denom = max(abs(inputs.consensus_eps), 0.01)
        surprise_pct = (inputs.actual_eps - inputs.consensus_eps) / denom * 100.0
        parts.append((gap_to_subscore(surprise_pct, scale=GAP_SCALE_EPS_PCT), 0.35))
        detail["eps_surprise_pct"] = round(surprise_pct, 2)

    if inputs.revenue_mom_accel_pp is not None:
        parts.append(
            (gap_to_subscore(inputs.revenue_mom_accel_pp, scale=ACCEL_SCALE_PP), 0.25)
        )
        detail["revenue_accel_pp"] = round(inputs.revenue_mom_accel_pp, 2)
    elif inputs.revenue_yoy_pct is not None:
        parts.append((gap_to_subscore(inputs.revenue_yoy_pct, scale=0.8), 0.15))
        detail["revenue_yoy_pct"] = round(inputs.revenue_yoy_pct, 2)

    if not parts:
        return SubscoreResult(
            NEUTRAL_SUBSCORE,
            "DATA_MISSING",
            {"reason": "no L8.5 inputs"},
        )

    weight_sum = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / weight_sum
    return SubscoreResult(round(score, 1), "OK", detail)


def compute_fundamental_subscore(
    inputs: L8Inputs,
    *,
    pe_pool: list[float],
    roe_pool: list[float],
) -> SubscoreResult:
    """L8 水準 + 聯集內 PE/ROE 分位。"""
    parts: list[float] = []
    detail: dict[str, float | str | None] = {}

    if inputs.roe_ttm is not None:
        parts.append(max(20.0, min(100.0, inputs.roe_ttm * 2.5)))
        detail["roe_ttm"] = round(inputs.roe_ttm, 2)

    if inputs.pe is not None and pe_pool:
        lo, hi = min(pe_pool), max(pe_pool)
        if hi > lo:
            pct = (inputs.pe - lo) / (hi - lo)
            parts.append(max(0.0, min(100.0, 100.0 - pct * 100.0)))
        else:
            parts.append(50.0)
        detail["pe"] = round(inputs.pe, 2)

    if inputs.dividend_yield is not None and inputs.dividend_yield > 0:
        parts.append(min(100.0, inputs.dividend_yield * 25.0))
        detail["dividend_yield"] = round(inputs.dividend_yield, 2)

    if not parts:
        return SubscoreResult(
            NEUTRAL_SUBSCORE,
            "DATA_MISSING",
            {"reason": "no L8 inputs"},
        )

    score = sum(parts) / len(parts)
    return SubscoreResult(round(score, 1), "OK", detail)


def build_l85_inputs(
    fund: sqlite3.Row | None,
    consensus: dict[str, float],
) -> L85Inputs:
    actual_roe = None
    actual_eps = None
    if fund is not None:
        if fund["roe_latest_q"] is not None:
            actual_roe = float(fund["roe_latest_q"])
        elif fund["roe_ttm"] is not None:
            actual_roe = float(fund["roe_ttm"])
        if fund["eps_latest_q"] is not None:
            actual_eps = float(fund["eps_latest_q"])
    return L85Inputs(
        actual_roe=actual_roe,
        consensus_roe=consensus.get("roe"),
        actual_eps=actual_eps,
        consensus_eps=consensus.get("eps"),
        revenue_yoy_pct=(
            float(fund["revenue_yoy_pct"])
            if fund and fund["revenue_yoy_pct"] is not None
            else None
        ),
        revenue_mom_accel_pp=(
            float(fund["revenue_mom_accel_pp"])
            if fund and fund["revenue_mom_accel_pp"] is not None
            else None
        ),
    )


def build_l8_inputs(fund: sqlite3.Row | None) -> L8Inputs:
    if fund is None:
        return L8Inputs(None, None, None, None)
    return L8Inputs(
        pe=float(fund["pe"]) if fund["pe"] is not None else None,
        pb=float(fund["pb"]) if fund["pb"] is not None else None,
        roe_ttm=float(fund["roe_ttm"]) if fund["roe_ttm"] is not None else None,
        dividend_yield=(
            float(fund["dividend_yield"]) if fund["dividend_yield"] is not None else None
        ),
    )


def load_subscores_for_stocks(
    conn: sqlite3.Connection,
    stock_ids: list[str],
) -> tuple[dict[str, SubscoreResult], dict[str, SubscoreResult]]:
    """回傳 (expectation_by_id, fundamental_by_id)。"""
    fund_map = load_latest_fundamental_map(conn, stock_ids)
    consensus_map = load_latest_consensus_map(conn, stock_ids)

    pe_pool = [
        float(r["pe"]) for r in fund_map.values() if r["pe"] is not None
    ]
    roe_pool = [
        float(r["roe_ttm"]) for r in fund_map.values() if r["roe_ttm"] is not None
    ]

    exp_out: dict[str, SubscoreResult] = {}
    fund_out: dict[str, SubscoreResult] = {}
    for sid in stock_ids:
        fund = fund_map.get(sid)
        cons = consensus_map.get(sid, {})
        exp_out[sid] = compute_expectation_subscore(build_l85_inputs(fund, cons))
        fund_out[sid] = compute_fundamental_subscore(
            build_l8_inputs(fund),
            pe_pool=pe_pool,
            roe_pool=roe_pool,
        )
    return exp_out, fund_out
