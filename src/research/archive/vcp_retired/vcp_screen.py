#!/usr/bin/env python3
"""
Minervini VCP 篩選 · VCP-TM（tradermonty lineage）。

Universe：ETF 持股聯集；benchmark：TEJ IX0001。
寫入 vcp_screen_scores_v2（model_id=vcp-tm）。

用法：
  PYTHONPATH=src python src/vcp_screen.py --run --write-report
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from holdings_research import TW_SPOT_CODE
from project_config import ETF_CODES_HOLDINGS
from report_paths import REPORTS_DIR
from score_engine import SCORE_VERSION
from stock_context import _compute_technical_from_rows, load_daily_bars, load_tej_daily_bars
from stock_db import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    load_etf_constituent_watchlist,
    load_latest_pm_watchlist,
    upsert_vcp_screen_scores_v2,
)
from vcp_nse_port.bars import rows_to_ohlcv_df
from vcp_tm.calibration import DEFAULT_CALIBRATION, load_min_composite, load_vcp_tm_params
from vcp_tm.evaluate import evaluate_vcp_tm
from vcp_tm.params import VcpTmParams
from vcp_tm.report_generator import build_section_ab_markdown

MODEL_ID = "vcp-tm"
BAR_LOOKBACK = 280
MIN_BARS = 200
TOP_K = 15
BENCHMARK_CODE = TW_SPOT_CODE


@dataclass(frozen=True)
class VcpEval:
    stock_id: str
    stock_name: str
    as_of_date: str
    composite_score: float
    rating: str
    execution_state: str
    entry_ready: bool
    pattern_type: str
    pivot_price: float | None
    distance_from_pivot_pct: float | None
    stop_loss: float | None
    risk_pct: float | None
    valid_vcp: bool
    vol_dry_ratio: float | None
    position_52w_pct: float | None
    reject_reason: str = ""
    state_cap_applied: bool = False
    extras: dict[str, float | str | bool | None] = field(default_factory=dict)

    @property
    def vcp_score(self) -> float:
        return self.composite_score


def _load_benchmark_df(conn: sqlite3.Connection) -> object:
    bench_rows = load_tej_daily_bars(conn, BENCHMARK_CODE, limit=BAR_LOOKBACK)
    return rows_to_ohlcv_df(bench_rows)


def evaluate_vcp(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    stock_name: str = "",
    bar_limit: int = BAR_LOOKBACK,
    benchmark_df: object | None = None,
    params: VcpTmParams | None = None,
) -> VcpEval | None:
    rows = load_daily_bars(conn, stock_id, limit=bar_limit)
    if len(rows) < MIN_BARS:
        return None

    stock_df = rows_to_ohlcv_df(rows)
    if stock_df.empty:
        return None

    tech = _compute_technical_from_rows(rows, entity_id=stock_id)
    if tech is None or tech.trade_date is None:
        return None

    if benchmark_df is None:
        benchmark_df = _load_benchmark_df(conn)

    result = evaluate_vcp_tm(stock_df, benchmark_df, params=params or load_vcp_tm_params())
    rs = result.get("relative_strength") or {}
    composite = result.get("composite") or {}

    return VcpEval(
        stock_id=stock_id,
        stock_name=stock_name,
        as_of_date=tech.trade_date,
        composite_score=float(result.get("composite_score") or 0.0),
        rating=str(result.get("rating") or "No VCP"),
        execution_state=str(result.get("execution_state") or "Invalid"),
        entry_ready=bool(result.get("entry_ready")),
        pattern_type=str(result.get("pattern_type") or "Damaged"),
        pivot_price=round(float(result["pivot"]), 2) if result.get("pivot") else None,
        distance_from_pivot_pct=result.get("distance_from_pivot_pct"),
        stop_loss=result.get("stop_loss"),
        risk_pct=result.get("risk_pct"),
        valid_vcp=bool(result.get("valid_vcp")),
        vol_dry_ratio=result.get("dry_up_ratio"),
        position_52w_pct=tech.position_52w_pct,
        reject_reason=str(result.get("reject_reason") or ""),
        state_cap_applied=bool(result.get("state_cap_applied")),
        extras={
            "guidance": result.get("guidance"),
            "breakout_volume": result.get("breakout_volume"),
            "trend_score": (result.get("trend") or {}).get("raw_score"),
            "rs_weighted": rs.get("weighted_rs"),
            "rs_score": rs.get("score"),
            "close": tech.close,
            "component_breakdown": composite.get("component_breakdown"),
        },
    )


def run_vcp_screen(
    conn: sqlite3.Connection,
    *,
    etf_codes: tuple[str, ...] = ETF_CODES_HOLDINGS,
    model_id: str = MODEL_ID,
    min_score: float | None = None,
    params: VcpTmParams | None = None,
) -> tuple[str, list[VcpEval]]:
    p = params or load_vcp_tm_params()
    floor = min_score if min_score is not None else load_min_composite()
    watchlist = load_etf_constituent_watchlist(conn, etf_codes)
    name_by_id = {w["stock_id"]: w.get("stock_name", "") for w in watchlist}
    benchmark_df = _load_benchmark_df(conn)
    results: list[VcpEval] = []
    as_of: str | None = None

    for w in watchlist:
        sid = w["stock_id"]
        ev = evaluate_vcp(
            conn,
            sid,
            stock_name=name_by_id.get(sid, ""),
            benchmark_df=benchmark_df,
            params=p,
        )
        if ev is None:
            continue
        as_of = ev.as_of_date
        if ev.composite_score >= floor and ev.valid_vcp:
            results.append(ev)

    results.sort(key=lambda x: x.composite_score, reverse=True)
    if as_of:
        rows = [
            {
                "stock_id": e.stock_id,
                "as_of_date": e.as_of_date,
                "model_id": model_id,
                "stock_name": e.stock_name,
                "composite_score": e.composite_score,
                "rating": e.rating,
                "execution_state": e.execution_state,
                "entry_ready": 1 if e.entry_ready else 0,
                "pattern_type": e.pattern_type,
                "pivot_price": e.pivot_price,
                "distance_from_pivot_pct": e.distance_from_pivot_pct,
                "stop_loss": e.stop_loss,
                "risk_pct": e.risk_pct,
                "valid_vcp": 1 if e.valid_vcp else 0,
                "metadata_json": json.dumps(
                    {
                        "reject_reason": e.reject_reason,
                        "state_cap_applied": e.state_cap_applied,
                        "vol_dry_ratio": e.vol_dry_ratio,
                        "position_52w_pct": e.position_52w_pct,
                        **e.extras,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            }
            for e in results
        ]
        upsert_vcp_screen_scores_v2(conn, rows)

    return as_of or "", results


def build_vcp_brief_markdown(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    candidates: list[VcpEval],
    score_version: str = SCORE_VERSION,
) -> str:
    del conn, score_version  # Section A/B is self-contained; p6-tier cross-ref below
    report_rows = [
        {
            "stock_id": c.stock_id,
            "stock_name": c.stock_name,
            "composite_score": c.composite_score,
            "rating": c.rating,
            "execution_state": c.execution_state,
            "entry_ready": c.entry_ready,
            "pattern_type": c.pattern_type,
            "pivot_price": c.pivot_price,
            "distance_from_pivot_pct": c.distance_from_pivot_pct,
            "risk_pct": c.risk_pct,
            "valid_vcp": c.valid_vcp,
            "state_cap_applied": c.state_cap_applied,
            "relative_strength": {"weighted_rs": c.extras.get("rs_weighted")},
        }
        for c in candidates
    ]
    body = build_section_ab_markdown(
        as_of_date=as_of_date,
        model_id=MODEL_ID,
        benchmark=BENCHMARK_CODE,
        candidates=report_rows,
    )
    return body


def write_vcp_brief(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    candidates: list[VcpEval],
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    md = build_vcp_brief_markdown(conn, as_of_date=as_of_date, candidates=candidates)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = as_of_date.replace("-", "")
    dated = reports_dir / f"{stamp}_vcp_daily_brief.md"
    latest = reports_dir / "vcp_daily_brief.md"
    dated.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    return dated


def main() -> int:
    parser = argparse.ArgumentParser(description="VCP-TM 篩選（ETF 成分股聯集）")
    parser.add_argument("--run", action="store_true", help="執行篩選並寫入 DB v2")
    parser.add_argument("--write-report", action="store_true", help="寫入 reports/*_vcp_daily_brief.md")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--calibrated", type=Path, default=DEFAULT_CALIBRATION)
    args = parser.parse_args()

    if not args.run and not args.write_report:
        parser.print_help()
        return 2

    params = load_vcp_tm_params(args.calibrated)
    conn = connect(args.db)
    try:
        as_of, candidates = run_vcp_screen(
            conn,
            min_score=args.min_score,
            params=params,
        )
        if not as_of:
            print("VCP-TM screen: 略過（universe 無足夠 K 線）")
            return 0
        print(f"VCP-TM screen: as_of={as_of} candidates={len(candidates)}")
        if args.write_report:
            path = write_vcp_brief(conn, as_of_date=as_of, candidates=candidates)
            print(f"  report → {path.relative_to(PROJECT_ROOT)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
