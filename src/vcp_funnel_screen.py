#!/usr/bin/env python3
"""
VCP 漏斗 screen（簡化上升趨勢 + 多層漏斗）。

L1 Universe → L2 簡化趨勢（MA50/MA200）→ L3 流動性 → L4 VCP+市況
→ L5 產業 → L6 財務 → L7 成長

Universe：ETF 持股聯集；benchmark：IX0001。
寫入 vcp_screen_scores_v2（model_id=vcp-funnel）。

用法：
  PYTHONPATH=src python src/vcp_funnel_screen.py --run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from flow_returns import sector_for_stock
from holdings_research import TW_SPOT_CODE
from investment_themes import THEME_LABEL, stock_theme
from project_config import ETF_CODES_HOLDINGS
from stock_context import _compute_technical_from_rows, load_daily_bars, load_tej_daily_bars
from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    delete_vcp_screen_scores_v2_for_date,
    load_etf_constituent_watchlist,
    load_fundamental_map_as_of,
    upsert_vcp_screen_scores_v2,
)
from vcp_nse_port.bars import rows_to_ohlcv_df
from stage_analysis import calculate_simple_trend
from vcp_nse_port.volume_pattern import calculate_volume_pattern
from vcp_tm.evaluate import evaluate_vcp_tm
from vcp_tm.params import VcpTmParams

MODEL_ID = "vcp-funnel"
LEGACY_MODEL_ID = "chunge-funnel"
FUNNEL_MODEL_IDS = (MODEL_ID, LEGACY_MODEL_ID)
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "vcp_funnel.yaml"
BAR_LOOKBACK = 280
MIN_BARS = 200
TOP_K = 15
BENCHMARK_CODE = TW_SPOT_CODE


@dataclass(frozen=True)
class VcpFunnelParams:
    lookback_days: int = 90
    min_contractions: int = 1
    t1_depth_min: float = 10.0
    t1_depth_max: float = 90.0
    contraction_ratio: float = 0.98
    trend_min_score: float = 60.0
    liquidity_percentile: int = 25
    min_rs_score: float = 40.0
    require_market_trend: bool = False
    min_roe_ttm: float = 5.0
    min_revenue_yoy_pct: float = 0.0
    industry_top_pct: float = 0.75
    min_l7_candidates: int = 5
    config_path: str | None = None

    def to_vcp_tm_params(self) -> VcpTmParams:
        return VcpTmParams(
            lookback_days=self.lookback_days,
            min_contractions=self.min_contractions,
            t1_depth_min=self.t1_depth_min,
            t1_depth_max=self.t1_depth_max,
            contraction_ratio=self.contraction_ratio,
            trend_min_score=self.trend_min_score,
        )

    def vcp_kwargs(self) -> dict:
        return {
            "lookback_days": self.lookback_days,
            "min_contractions": self.min_contractions,
            "t1_depth_min": self.t1_depth_min,
            "t1_depth_max": self.t1_depth_max,
            "contraction_ratio": self.contraction_ratio,
        }

    def funnel_layer_labels(self) -> tuple[tuple[str, str], ...]:
        mkt = f"大盤站 MA50/200" if self.require_market_trend else "市況不強制"
        liq = f"流動性（50 日均量 ≥ universe P{self.liquidity_percentile}）"
        ind_pct = int(round(self.industry_top_pct * 100))
        rev = (
            f"成長性（營收 YoY≥{self.min_revenue_yoy_pct:.0f}%，有資料才篩）"
            if self.min_revenue_yoy_pct > 0
            else "成長性（有資料才篩 · YoY 不強制）"
        )
        return (
            ("L1", "ETF 成分股聯集（K 線 ≥ 200 日）"),
            ("L2", "簡化上升趨勢（股價 > MA50 & MA200）"),
            ("L3", liq),
            (
                "L4",
                f"VCP（lookback {self.lookback_days}d · T1≤{self.t1_depth_max:.0f}%）"
                f" + RS≥{self.min_rs_score:.0f} + {mkt}",
            ),
            ("L5", f"產業（主題 RS 排名前 {ind_pct}%）"),
            ("L6", f"財務體質（ROE≥{self.min_roe_ttm:.0f}%，有資料才篩）"),
            ("L7", rev),
        )


def load_vcp_funnel_params(path: Path | None = None) -> VcpFunnelParams:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        return VcpFunnelParams()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    p = raw.get("params") or {}
    g = raw.get("gates") or {}
    return VcpFunnelParams(
        lookback_days=int(p.get("lookback_days") or 90),
        min_contractions=int(p.get("min_contractions") or 1),
        t1_depth_min=float(p.get("t1_depth_min") or 10.0),
        t1_depth_max=float(p.get("t1_depth_max") or 90.0),
        contraction_ratio=float(p.get("contraction_ratio") or 0.98),
        trend_min_score=float(p.get("trend_min_score") or 60.0),
        liquidity_percentile=int(p.get("liquidity_percentile") or 25),
        min_rs_score=float(g.get("min_rs_score") or 40.0),
        require_market_trend=bool(g.get("require_market_trend", False)),
        min_roe_ttm=float(g.get("min_roe_ttm") or 5.0),
        min_revenue_yoy_pct=float(g.get("min_revenue_yoy_pct") or 0.0),
        industry_top_pct=float(g.get("industry_top_pct") or 0.75),
        min_l7_candidates=int(raw.get("min_l7_candidates") or 5),
        config_path=str(cfg_path.relative_to(PROJECT_ROOT)),
    )


@dataclass(frozen=True)
class VcpFunnelEval:
    stock_id: str
    stock_name: str
    as_of_date: str
    funnel_score: float
    layers_passed: int
    final_layer: str
    stage: str
    pivot_price: float | None
    dist_pivot_pct: float | None
    contraction_ratio: float | None
    vol_dry_ratio: float | None
    position_52w_pct: float | None
    theme: str
    sector: str
    stop_loss: float | None = None
    risk_pct: float | None = None
    reject_layer: str = ""
    reject_reason: str = ""
    quality: str = "Poor"
    extras: dict[str, float | str | bool | None] = field(default_factory=dict)


def _load_benchmark_df(conn: sqlite3.Connection, *, as_of_date: str | None = None) -> object:
    bench_rows = load_tej_daily_bars(
        conn, BENCHMARK_CODE, limit=BAR_LOOKBACK, as_of_date=as_of_date
    )
    return rows_to_ohlcv_df(bench_rows)


def _liquidity_floor(stock_dfs: dict[str, object], *, percentile: int = 25) -> float:
    avgs: list[float] = []
    for df in stock_dfs.values():
        vol = calculate_volume_pattern(df)
        avg = (vol.get("details") or {}).get("avg_volume_50d")
        if avg and float(avg) > 0:
            avgs.append(float(avg))
    if not avgs:
        return 0.0
    pct = max(0, min(100, percentile))
    if pct <= 0:
        return min(avgs)
    avgs_sorted = sorted(avgs)
    idx = max(0, min(len(avgs_sorted) - 1, (len(avgs_sorted) * pct + 99) // 100 - 1))
    return avgs_sorted[idx]


def _vcp_composite(
    stock_df: object,
    benchmark_df: object,
    *,
    tm_params: VcpTmParams,
) -> dict:
    result = evaluate_vcp_tm(stock_df, benchmark_df, params=tm_params)
    vcp = result.get("vcp") or {}
    contractions = vcp.get("contractions") or []
    t1_depth = None
    final_depth = None
    if contractions:
        t1_depth = contractions[0].get("depth_pct")
        final_depth = contractions[-1].get("depth_pct")
    contraction_ratio_val = None
    if t1_depth and final_depth and t1_depth > 0:
        contraction_ratio_val = round(float(final_depth) / float(t1_depth), 3)
    pivot_prox = result.get("pivot_proximity") or {}
    return {
        "result": result,
        "vcp": vcp,
        "composite_score": float(result.get("composite_score") or 0.0),
        "quality": result.get("rating", "No VCP"),
        "rating": result.get("rating", "No VCP"),
        "execution_state": result.get("execution_state"),
        "entry_ready": result.get("entry_ready"),
        "pattern_type": result.get("pattern_type"),
        "pivot_proximity": pivot_prox,
        "pivot": result.get("pivot"),
        "contraction_ratio": contraction_ratio_val,
        "dry_up_ratio": result.get("dry_up_ratio"),
        "valid_vcp": bool(result.get("valid_vcp")),
    }


def _evaluate_stock_layers(
    stock_id: str,
    stock_name: str,
    stock_df: object,
    benchmark_df: object,
    *,
    as_of_date: str,
    liquidity_floor: float,
    market_ok: bool,
    params: VcpFunnelParams,
    bar_rows: list[sqlite3.Row] | None = None,
) -> VcpFunnelEval:
    tech = _compute_technical_from_rows(bar_rows or [], entity_id=stock_id)
    theme = stock_theme(stock_id)
    sector = sector_for_stock(stock_id)
    base_extras: dict[str, float | str | bool | None] = {
        "theme": theme,
        "theme_label": THEME_LABEL.get(theme, theme),
        "sector": sector,
        "close": tech.close if tech else None,
        "dist_ma20_pct": tech.dist_ma20_pct if tech else None,
    }

    simple = calculate_simple_trend(stock_df)
    if not simple["passed"]:
        return VcpFunnelEval(
            stock_id=stock_id,
            stock_name=stock_name,
            as_of_date=as_of_date,
            funnel_score=0.0,
            layers_passed=1,
            final_layer="L1",
            stage="L1_universe",
            pivot_price=None,
            dist_pivot_pct=None,
            contraction_ratio=None,
            vol_dry_ratio=None,
            position_52w_pct=tech.position_52w_pct if tech else None,
            theme=theme,
            sector=sector,
            reject_layer="L2",
            reject_reason="未站 MA50/MA200",
            extras={**base_extras, **simple.get("details", {})},
        )

    vol = calculate_volume_pattern(stock_df)
    avg_vol = float((vol.get("details") or {}).get("avg_volume_50d") or 0.0)
    if liquidity_floor > 0 and avg_vol < liquidity_floor:
        return VcpFunnelEval(
            stock_id=stock_id,
            stock_name=stock_name,
            as_of_date=as_of_date,
            funnel_score=0.0,
            layers_passed=2,
            final_layer="L2",
            stage="L2_trend",
            pivot_price=None,
            dist_pivot_pct=None,
            contraction_ratio=None,
            vol_dry_ratio=vol.get("dry_up_ratio"),
            position_52w_pct=tech.position_52w_pct if tech else None,
            theme=theme,
            sector=sector,
            reject_layer="L3",
            reject_reason=f"50日均量 {avg_vol:.0f} < 中位數 {liquidity_floor:.0f}",
            extras={**base_extras, "avg_volume_50d": avg_vol},
        )

    tm_params = params.to_vcp_tm_params()
    scored = _vcp_composite(stock_df, benchmark_df, tm_params=tm_params)
    result = scored["result"] or {}
    rs = result.get("relative_strength") or {}
    rs_score = float(rs.get("score") or 0.0)
    market_gate = market_ok if params.require_market_trend else True
    if not scored.get("valid_vcp") or rs_score < params.min_rs_score or not market_gate:
        reasons: list[str] = []
        if not scored.get("valid_vcp"):
            reasons.append(str(result.get("reject_reason") or "非 VCP"))
        if rs_score < params.min_rs_score:
            reasons.append(f"RS {rs_score:.0f} < {params.min_rs_score:.0f}")
        if params.require_market_trend and not market_ok:
            reasons.append("大盤未站 MA50/200")
        return VcpFunnelEval(
            stock_id=stock_id,
            stock_name=stock_name,
            as_of_date=as_of_date,
            funnel_score=0.0,
            layers_passed=3,
            final_layer="L3",
            stage="L3_liquidity",
            pivot_price=None,
            dist_pivot_pct=None,
            contraction_ratio=None,
            vol_dry_ratio=scored.get("dry_up_ratio"),
            position_52w_pct=tech.position_52w_pct if tech else None,
            theme=theme,
            sector=sector,
            reject_layer="L4",
            reject_reason=" · ".join(reasons),
            extras={
                **base_extras,
                "rs_score": rs_score,
                "rs_value": rs.get("weighted_rs"),
                "vcp_ok": scored.get("valid_vcp"),
                "market_ok": market_ok,
            },
        )

    pivot_prox = scored["pivot_proximity"] or {}
    stop_loss = result.get("stop_loss")
    risk_pct = result.get("risk_pct")
    return VcpFunnelEval(
        stock_id=stock_id,
        stock_name=stock_name,
        as_of_date=as_of_date,
        funnel_score=float(scored["composite_score"]),
        layers_passed=4,
        final_layer="L4",
        stage="L4_vcp",
        pivot_price=round(float(scored["pivot"]), 2) if scored.get("pivot") else None,
        dist_pivot_pct=float(pivot_prox.get("distance_from_pivot_pct"))
        if pivot_prox.get("distance_from_pivot_pct") is not None
        else None,
        contraction_ratio=scored.get("contraction_ratio"),
        vol_dry_ratio=scored.get("dry_up_ratio"),
        position_52w_pct=tech.position_52w_pct if tech else None,
        theme=theme,
        sector=sector,
        stop_loss=round(float(stop_loss), 2) if stop_loss else None,
        risk_pct=round(float(risk_pct), 2) if risk_pct is not None else None,
        quality=str(scored.get("rating") or "No VCP"),
        extras={
            **base_extras,
            "rs_score": rs_score,
            "rs_value": rs.get("weighted_rs"),
            "trend_simple_ok": True,
            "above_ma50": simple["details"].get("above_ma50"),
            "above_ma200": simple["details"].get("above_ma200"),
            "avg_volume_50d": avg_vol,
            "execution_state": scored.get("execution_state"),
            "entry_ready": scored.get("entry_ready"),
            "pattern_type": scored.get("pattern_type"),
        },
    )


def _apply_industry_filter(
    candidates: list[VcpFunnelEval],
    *,
    top_pct: float = 0.75,
) -> list[VcpFunnelEval]:
    if not candidates:
        return []
    theme_rs: dict[str, list[float]] = {}
    for c in candidates:
        rs = c.extras.get("rs_value")
        if rs is None:
            continue
        theme_rs.setdefault(c.theme, []).append(float(rs))
    if not theme_rs:
        return [replace(c, layers_passed=5, final_layer="L5", stage="L5_industry") for c in candidates]
    ranked = sorted(
        theme_rs.items(),
        key=lambda kv: statistics.mean(kv[1]),
        reverse=True,
    )
    pct = max(0.5, min(1.0, top_pct))
    top_n = max(1, int(len(ranked) * pct + 0.999))
    top_themes = {t for t, _ in ranked[:top_n]}
    out: list[VcpFunnelEval] = []
    for c in candidates:
        if c.theme in top_themes:
            out.append(
                replace(c, layers_passed=5, final_layer="L5", stage="L5_industry")
            )
        else:
            out.append(
                replace(
                    c,
                    reject_layer="L5",
                    reject_reason=f"主題 {THEME_LABEL.get(c.theme, c.theme)} RS 排名後半",
                )
            )
    return out


def _apply_fundamental_filter(
    candidates: list[VcpFunnelEval],
    fund_map: dict[str, sqlite3.Row],
    *,
    min_roe_ttm: float,
) -> list[VcpFunnelEval]:
    out: list[VcpFunnelEval] = []
    for c in candidates:
        row = fund_map.get(c.stock_id)
        if row is None:
            out.append(
                replace(
                    c,
                    layers_passed=6,
                    final_layer="L6",
                    stage="L6_fundamental_skip",
                    extras={**c.extras, "fund_skip": True},
                )
            )
            continue
        roe = row["roe_ttm"]
        if roe is None or float(roe) < min_roe_ttm:
            out.append(
                replace(
                    c,
                    reject_layer="L6",
                    reject_reason=f"ROE {roe if roe is not None else '—'}% < {min_roe_ttm}%",
                    extras={**c.extras, "roe_ttm": roe},
                )
            )
            continue
        out.append(
            replace(
                c,
                layers_passed=6,
                final_layer="L6",
                stage="L6_fundamental",
                extras={**c.extras, "roe_ttm": roe, "fund_skip": False},
            )
        )
    return out


def _apply_growth_filter(
    candidates: list[VcpFunnelEval],
    fund_map: dict[str, sqlite3.Row],
    *,
    min_revenue_yoy_pct: float,
) -> list[VcpFunnelEval]:
    out: list[VcpFunnelEval] = []
    for c in candidates:
        row = fund_map.get(c.stock_id)
        if row is None:
            out.append(
                replace(
                    c,
                    layers_passed=7,
                    final_layer="L7",
                    stage="L7_growth_skip",
                    extras={**c.extras, "growth_skip": True},
                )
            )
            continue
        rev_yoy = row["revenue_yoy_pct"]
        if (
            min_revenue_yoy_pct > 0
            and (rev_yoy is None or float(rev_yoy) < min_revenue_yoy_pct)
        ):
            out.append(
                replace(
                    c,
                    reject_layer="L7",
                    reject_reason=(
                        f"營收 YoY {rev_yoy if rev_yoy is not None else '—'}% "
                        f"< {min_revenue_yoy_pct}%"
                    ),
                    extras={**c.extras, "revenue_yoy_pct": rev_yoy},
                )
            )
            continue
        out.append(
            replace(
                c,
                layers_passed=7,
                final_layer="L7",
                stage="L7_growth",
                extras={**c.extras, "revenue_yoy_pct": rev_yoy, "growth_skip": False},
            )
        )
    return out


def run_vcp_funnel_screen(
    conn: sqlite3.Connection,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_HOLDINGS,
    model_id: str = MODEL_ID,
    params: VcpFunnelParams | None = None,
    as_of_date: str | None = None,
    persist: bool = True,
    replace_day: bool = True,
) -> tuple[str, list[VcpFunnelEval], dict[str, int], VcpFunnelParams]:
    cfg = params or load_vcp_funnel_params()
    watchlist = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name", "") for w in watchlist}
    benchmark_df = _load_benchmark_df(conn, as_of_date=as_of_date)
    market_ok = bool(calculate_simple_trend(benchmark_df)["passed"]) if not benchmark_df.empty else False

    stock_dfs: dict[str, object] = {}
    stock_rows: dict[str, list[sqlite3.Row]] = {}
    as_of: str | None = as_of_date

    for w in watchlist:
        sid = w["stock_id"]
        rows = load_daily_bars(conn, sid, limit=BAR_LOOKBACK, as_of_date=as_of_date)
        if len(rows) < MIN_BARS:
            continue
        if as_of_date and str(rows[0]["trade_date"]) != as_of_date:
            continue
        stock_df = rows_to_ohlcv_df(rows)
        if stock_df.empty:
            continue
        stock_dfs[sid] = stock_df
        stock_rows[sid] = rows
        if as_of is None:
            tech = _compute_technical_from_rows(rows, entity_id=sid)
            if tech is None or tech.trade_date is None:
                continue
            as_of = tech.trade_date

    liquidity_floor = _liquidity_floor(stock_dfs, percentile=cfg.liquidity_percentile)
    results: list[VcpFunnelEval] = []
    for sid, stock_df in stock_dfs.items():
        ev = _evaluate_stock_layers(
            sid,
            name_by_id.get(sid, ""),
            stock_df,
            benchmark_df,
            as_of_date=as_of or "",
            liquidity_floor=liquidity_floor,
            market_ok=market_ok,
            params=cfg,
            bar_rows=stock_rows[sid],
        )
        results.append(ev)

    l4_pass = [c for c in results if c.layers_passed >= 4]
    l5_pass = _apply_industry_filter(l4_pass, top_pct=cfg.industry_top_pct)
    l5_by_id = {c.stock_id: c for c in l5_pass}
    after_l5: list[VcpFunnelEval] = []
    for c in results:
        if c.layers_passed >= 4:
            after_l5.append(l5_by_id[c.stock_id])
        else:
            after_l5.append(c)

    l5_ok = [c for c in after_l5 if c.layers_passed >= 5]
    fund_map = load_fundamental_map_as_of(conn, list(stock_dfs.keys()), as_of or "")
    l6_pass = _apply_fundamental_filter(l5_ok, fund_map, min_roe_ttm=cfg.min_roe_ttm)
    l6_by_id = {c.stock_id: c for c in l6_pass}
    after_l6: list[VcpFunnelEval] = []
    for c in after_l5:
        if c.layers_passed >= 5:
            after_l6.append(l6_by_id[c.stock_id])
        else:
            after_l6.append(c)

    l6_ok = [c for c in after_l6 if c.layers_passed >= 6]
    l7_pass = _apply_growth_filter(
        l6_ok, fund_map, min_revenue_yoy_pct=cfg.min_revenue_yoy_pct
    )
    l7_by_id = {c.stock_id: c for c in l7_pass}
    final_results: list[VcpFunnelEval] = []
    for c in after_l6:
        if c.layers_passed >= 6:
            final_results.append(l7_by_id[c.stock_id])
        else:
            final_results.append(c)

    layer_counts: dict[str, int] = {"L1": len(stock_dfs)}
    for layer_id, _ in cfg.funnel_layer_labels()[1:]:
        n = int(layer_id[1:])
        layer_counts[layer_id] = sum(1 for c in final_results if c.layers_passed >= n)

    finalists = [c for c in final_results if c.layers_passed >= 7]
    finalists.sort(key=lambda x: x.funnel_score, reverse=True)

    if as_of and finalists and persist:
        if replace_day:
            delete_vcp_screen_scores_v2_for_date(conn, as_of, model_id=model_id)
        upsert_vcp_screen_scores_v2(
            conn,
            [
                {
                    "stock_id": e.stock_id,
                    "as_of_date": e.as_of_date,
                    "model_id": model_id,
                    "stock_name": e.stock_name,
                    "composite_score": e.funnel_score,
                    "rating": e.quality,
                    "execution_state": str(e.extras.get("execution_state") or "Pre-breakout"),
                    "entry_ready": 1 if e.extras.get("entry_ready") else 0,
                    "pattern_type": str(e.extras.get("pattern_type") or "VCP-adjacent"),
                    "pivot_price": e.pivot_price,
                    "distance_from_pivot_pct": e.dist_pivot_pct,
                    "stop_loss": e.stop_loss,
                    "risk_pct": e.risk_pct,
                    "valid_vcp": 1,
                    "metadata_json": json.dumps(
                        {
                            "layers_passed": e.layers_passed,
                            "final_layer": e.final_layer,
                            "reject_layer": e.reject_layer,
                            "reject_reason": e.reject_reason,
                            "quality": e.quality,
                            "theme": e.theme,
                            "sector": e.sector,
                            **e.extras,
                        },
                        ensure_ascii=False,
                    ),
                }
                for e in finalists
            ],
        )

    return as_of or "", final_results, layer_counts, cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="VCP 漏斗 screen（簡化趨勢 + 多層漏斗）")
    parser.add_argument("--run", action="store_true", help="執行漏斗篩選並寫入 DB")
    parser.add_argument("--config", type=Path, default=None, help="校準 yaml（預設 config/vcp_funnel.yaml）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    if not args.run:
        parser.print_help()
        return 2

    params = load_vcp_funnel_params(args.config)
    conn = connect(args.db)
    try:
        as_of, results, layer_counts, cfg = run_vcp_funnel_screen(conn, params=params)
        if not as_of:
            print("VCP funnel: 略過（universe 無足夠 K 線）")
            return 0
        n_final = sum(1 for c in results if c.layers_passed >= 7)
        print(
            f"VCP funnel: as_of={as_of} universe={layer_counts.get('L1', 0)} "
            f"L4={layer_counts.get('L4', 0)} L7={n_final}"
        )
        if cfg.min_l7_candidates and n_final < cfg.min_l7_candidates:
            print(
                f"  WARN: L7={n_final} < min_l7_candidates={cfg.min_l7_candidates} "
                f"（請調整 {cfg.config_path or 'config/vcp_funnel.yaml'}）",
                file=__import__("sys").stderr,
            )
    finally:
        conn.close()
    return 0


# Archive / backfill compatibility
ChungeFunnelParams = VcpFunnelParams
ChungeFunnelEval = VcpFunnelEval
load_chunge_funnel_params = load_vcp_funnel_params
run_chunge_funnel_screen = run_vcp_funnel_screen


if __name__ == "__main__":
    raise SystemExit(main())

