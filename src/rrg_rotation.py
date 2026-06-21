"""RRG 四象限（Julius de Kempenaer · TradingView WMA 實作）。

公式（Pine / TradingView 開源 RRG clone）::

    RS = Close(asset) / Close(benchmark)
    RS-Ratio = WMA(RS / WMA(RS, L), L) × 100
    RS-Momentum = RS-Ratio / WMA(RS-Ratio, L) × 100

象限（baseline = 100）::

    Leading    : RS-Ratio > 100 且 RS-Momentum > 100
    Weakening  : RS-Ratio > 100 且 RS-Momentum ≤ 100
    Lagging    : RS-Ratio ≤ 100 且 RS-Momentum ≤ 100
    Improving  : RS-Ratio ≤ 100 且 RS-Momentum > 100

定位：策略群內選股 GPS；市場層廣度見 `market_breadth_ma`（% Above MA）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from analytics.bench import bench_return_entry_to_exit, compute_excess_significance
from research.backtest.finpilot_local_backtest import (
    basket_return_h9,
    load_price_panels,
    month_end_trading_dates,
    pit_fundamental_at,
    load_fundamental_snapshot,
    load_financial_history,
    summarize_periods,
)
from flow_returns import trading_dates_after
from market_benchmark import load_benchmark_close
from market_breadth_ma import BREADTH_ZONE_DISPLAY, BREADTH_ZONES_ORDER, build_breadth_panel
from stock_context import load_tej_daily_bars
from stock_db import DEFAULT_DB_PATH, connect, load_vcp_screen_v2_for_date
from vcp_nse_port.bars import rows_to_ohlcv_df
from vcp_tm.calibration import load_min_composite, load_vcp_tm_params
from vcp_tm.evaluate import evaluate_vcp_tm
from vcp_tm.params import VcpTmParams

PRIMARY_STRATEGY_COHORT = "leading_vcp65_top15"
VCP_BAR_LOOKBACK = 280
VCP_MIN_BARS = 200
BENCHMARK_CODE = "IX0001"

RrgQuadrant = Literal["leading", "weakening", "lagging", "improving"]

QUADRANT_LABEL: dict[RrgQuadrant, str] = {
    "leading": "Leading",
    "weakening": "Weakening",
    "lagging": "Lagging",
    "improving": "Improving",
}

DEFAULT_LENGTH = 20
DEFAULT_TOP_N = 20


def wma(series: pd.Series, length: int) -> pd.Series:
    if length < 1:
        raise ValueError("length must be >= 1")

    def _wma_window(x: np.ndarray) -> float:
        w = np.arange(1, len(x) + 1, dtype=float)
        return float(np.dot(x, w) / w.sum())

    return series.rolling(length, min_periods=length).apply(_wma_window, raw=True)


def rs_ratio_momentum(
    asset_close: pd.Series,
    bench_close: pd.Series,
    *,
    length: int = DEFAULT_LENGTH,
) -> tuple[pd.Series, pd.Series]:
    """單檔 vs 基準的 JdK RS-Ratio / RS-Momentum（對齊 index）。"""
    aligned = pd.concat({"asset": asset_close, "bench": bench_close}, axis=1).dropna()
    if aligned.empty:
        empty = pd.Series(dtype=float)
        return empty, empty
    rs = aligned["asset"] / aligned["bench"]
    wma_rs = wma(rs, length)
    rs_ratio = wma(rs / wma_rs, length) * 100.0
    rs_momentum = rs_ratio / wma(rs_ratio, length) * 100.0
    return rs_ratio, rs_momentum


def classify_quadrant(rs_ratio: float, rs_momentum: float) -> RrgQuadrant | None:
    if not np.isfinite(rs_ratio) or not np.isfinite(rs_momentum):
        return None
    strong = rs_ratio > 100.0
    accel = rs_momentum > 100.0
    if strong and accel:
        return "leading"
    if strong and not accel:
        return "weakening"
    if not strong and not accel:
        return "lagging"
    return "improving"


def compute_rrg_panel(
    close: pd.DataFrame,
    bench: pd.Series,
    *,
    length: int = DEFAULT_LENGTH,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """回傳 (rs_ratio, rs_momentum, quadrant) 三個 wide panel。"""
    ratio_cols: dict[str, pd.Series] = {}
    mom_cols: dict[str, pd.Series] = {}
    quad_cols: dict[str, pd.Series] = {}
    bench_aligned = bench.reindex(close.index).astype(float).ffill()

    for sid in close.columns:
        r, m = rs_ratio_momentum(close[sid], bench_aligned, length=length)
        ratio_cols[str(sid)] = r
        mom_cols[str(sid)] = m
        quad_cols[str(sid)] = pd.Series(
            [
                classify_quadrant(float(rv), float(mv)) if pd.notna(rv) and pd.notna(mv) else None
                for rv, mv in zip(r, m, strict=True)
            ],
            index=r.index,
            dtype=object,
        )

    rs_ratio = pd.DataFrame(ratio_cols, index=close.index)
    rs_momentum = pd.DataFrame(mom_cols, index=close.index)
    quadrant = pd.DataFrame(quad_cols, index=close.index)
    return rs_ratio, rs_momentum, quadrant


@dataclass(frozen=True)
class RrgCohortSpec:
    cohort_id: str
    label: str
    quadrant: RrgQuadrant | None
    rank_by: Literal["rs_ratio", "rs_momentum", "mom60", "vcp_score"] = "rs_ratio"
    mom60_min: float | None = None
    min_vcp_score: float | None = None
    top_n: int = DEFAULT_TOP_N


def _default_cohorts(min_vcp: float) -> tuple[RrgCohortSpec, ...]:
    return (
        RrgCohortSpec("leading_top20", "Leading Top20", "leading", "rs_ratio", top_n=20),
        RrgCohortSpec("weakening_top20", "Weakening Top20", "weakening", "rs_ratio", top_n=20),
        RrgCohortSpec("lagging_top20", "Lagging Top20", "lagging", "rs_ratio", top_n=20),
        RrgCohortSpec("improving_top20", "Improving Top20", "improving", "rs_momentum", top_n=20),
        RrgCohortSpec(
            "leading_mom60_top20",
            "Leading + Mom60 Top20",
            "leading",
            "mom60",
            top_n=20,
        ),
        RrgCohortSpec(
            "leading_vcp65_top15",
            f"Leading + VCP≥{min_vcp:.0f} Top15",
            "leading",
            "vcp_score",
            min_vcp_score=min_vcp,
            top_n=15,
        ),
        RrgCohortSpec("mom60_top30", "Mom60 Top30（對照）", None, "mom60", top_n=30),
    )


RRG_COHORTS: tuple[RrgCohortSpec, ...] = _default_cohorts(load_min_composite())


def _load_stock_bars_as_of(
    conn: sqlite3.Connection,
    stock_id: str,
    signal_date: str,
    *,
    limit: int = VCP_BAR_LOOKBACK,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, signal_date, limit),
    ).fetchall()


def _load_benchmark_df_as_of(
    conn: sqlite3.Connection,
    signal_date: str,
    *,
    limit: int = VCP_BAR_LOOKBACK,
) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date AS trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE code = ? AND date <= ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (BENCHMARK_CODE, signal_date, limit),
    ).fetchall()
    if not rows:
        rows = load_tej_daily_bars(conn, BENCHMARK_CODE, limit=limit)
        rows = [r for r in rows if str(r["trade_date"]) <= signal_date]
    return rows_to_ohlcv_df(rows)


def vcp_composite_at_date(
    conn: sqlite3.Connection,
    stock_id: str,
    signal_date: str,
    *,
    bench_df: pd.DataFrame | None = None,
    params: VcpTmParams | None = None,
) -> float | None:
    rows = _load_stock_bars_as_of(conn, stock_id, signal_date)
    if len(rows) < VCP_MIN_BARS:
        return None
    stock_df = rows_to_ohlcv_df(rows)
    if stock_df.empty:
        return None
    if bench_df is None:
        bench_df = _load_benchmark_df_as_of(conn, signal_date)
    result = evaluate_vcp_tm(stock_df, bench_df, params=params or load_vcp_tm_params())
    score = result.get("composite_score")
    return float(score) if score is not None else None


def vcp_scores_for_candidates(
    conn: sqlite3.Connection,
    candidates: list[str],
    signal_date: str,
    *,
    params: VcpTmParams | None = None,
) -> dict[str, float]:
    bench_df = _load_benchmark_df_as_of(conn, signal_date)
    out: dict[str, float] = {}
    p = params or load_vcp_tm_params()
    for sid in candidates:
        score = vcp_composite_at_date(conn, sid, signal_date, bench_df=bench_df, params=p)
        if score is not None:
            out[sid] = score
    return out


def _breadth_map_for_months(
    conn: sqlite3.Connection,
    *,
    start_month: str,
    end_month: str,
) -> dict[str, str]:
    date_start = f"{start_month}-01"
    date_end = f"{end_month}-31"
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    if panel.empty:
        return {}
    out: dict[str, str] = {}
    for ym in sorted({d[:7] for d in panel["trade_date"]}):
        if ym < start_month or ym > end_month:
            continue
        sub = panel[panel["trade_date"].str.startswith(ym)]
        if sub.empty:
            continue
        last = sub.iloc[-1]
        out[str(last["trade_date"])] = str(last["zone_200"])
    return out


def summarize_by_breadth_zone(
    periods: list[dict],
    *,
    zones: tuple[str, ...] = BREADTH_ZONES_ORDER,
) -> tuple[list[dict], dict | None]:
    buckets: dict[str, list[dict]] = {z: [] for z in zones}
    for p in periods:
        slug = p.get("breadth_zone_200")
        if slug in buckets:
            buckets[slug].append(p)
    rows: list[dict] = []
    for slug in zones:
        sub = buckets[slug]
        if not sub:
            rows.append(
                {
                    "zone": slug,
                    "display": BREADTH_ZONE_DISPLAY.get(slug, slug),  # type: ignore[arg-type]
                    "n_periods": 0,
                    "mean_excess_pct": None,
                    "win_rate_vs_bench_pct": None,
                }
            )
            continue
        n = len(sub)
        rows.append(
            {
                "zone": slug,
                "display": BREADTH_ZONE_DISPLAY.get(slug, slug),  # type: ignore[arg-type]
                "n_periods": n,
                "mean_excess_pct": round(sum(p["excess_pct"] for p in sub) / n, 4),
                "win_rate_vs_bench_pct": round(
                    sum(1 for p in sub if p["beat_bench"]) / n * 100, 2
                ),
            }
        )
    ranked = sorted(
        [r for r in rows if r["n_periods"] > 0],
        key=lambda r: (r["mean_excess_pct"] or -999.0, r["win_rate_vs_bench_pct"] or 0),
        reverse=True,
    )
    best = ranked[0] if ranked else None
    return rows, best


def cross_validate_live_vcp_rrg(
    conn: sqlite3.Connection,
    *,
    model_id: str = "vcp-tm",
    min_vcp_score: float | None = None,
    length: int = DEFAULT_LENGTH,
) -> list[dict]:
    """vcp_screen_scores_v2 × RRG Leading 即時交叉驗證。"""
    min_vcp = min_vcp_score if min_vcp_score is not None else load_min_composite()
    dates = conn.execute(
        """
        SELECT DISTINCT as_of_date FROM vcp_screen_scores_v2
        WHERE model_id = ? ORDER BY as_of_date DESC LIMIT 10
        """,
        (model_id,),
    ).fetchall()
    if not dates:
        return []

    close, _, _vol = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    _rs_ratio, _rs_momentum, quadrant = compute_rrg_panel(close, bench, length=length)
    rows: list[dict] = []
    for (as_of_date,) in dates:
        d = str(as_of_date)
        if d not in quadrant.index:
            continue
        vcp_rows = load_vcp_screen_v2_for_date(
            conn, d, model_id=model_id, min_score=min_vcp
        )
        qrow = quadrant.loc[d]
        for vr in vcp_rows:
            sid = str(vr["stock_id"])
            quad = qrow.get(sid)
            rows.append(
                {
                    "as_of_date": d,
                    "stock_id": sid,
                    "composite_score": float(vr["composite_score"]),
                    "execution_state": str(vr["execution_state"]),
                    "rrg_quadrant": quad,
                    "rrg_leading": quad == "leading",
                }
            )
    return rows


def _mom60_at(close: pd.DataFrame, signal_date: str) -> pd.Series | None:
    hist = close.loc[:signal_date]
    if len(hist) < 60:
        return None
    return close.loc[signal_date] / hist.iloc[-60]


def select_rrg_cohort(
    spec: RrgCohortSpec,
    *,
    signal_date: str,
    close: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    rs_momentum: pd.DataFrame,
    quadrant: pd.DataFrame,
    vol: pd.DataFrame,
    vcp_scores: dict[str, float] | None = None,
) -> list[str]:
    if signal_date not in close.index:
        return []
    vol_ma20 = vol.loc[:signal_date].tail(20).mean()
    liquid = vol_ma20 > 3_000_000

    if spec.quadrant is not None:
        qrow = quadrant.loc[signal_date]
        mask = qrow == spec.quadrant
    else:
        mask = pd.Series(True, index=close.columns)

    mask = mask.reindex(close.columns).fillna(False) & liquid.reindex(close.columns).fillna(False)
    if not mask.any():
        return []

    if spec.rank_by == "vcp_score":
        if not vcp_scores:
            return []
        ranks = pd.Series(vcp_scores, dtype=float).reindex(close.columns)
    elif spec.rank_by == "rs_ratio":
        ranks = rs_ratio.loc[signal_date].reindex(close.columns)
    elif spec.rank_by == "rs_momentum":
        ranks = rs_momentum.loc[signal_date].reindex(close.columns)
    else:
        mom = _mom60_at(close, signal_date)
        if mom is None:
            return []
        ranks = mom.reindex(close.columns)

    eligible = mask & ranks.notna()
    if spec.min_vcp_score is not None:
        if not vcp_scores:
            return []
        vcp_s = pd.Series(vcp_scores, dtype=float).reindex(close.columns)
        eligible = eligible & (vcp_s >= spec.min_vcp_score)
    if spec.mom60_min is not None:
        mom = _mom60_at(close, signal_date)
        if mom is None:
            return []
        eligible = eligible & (mom.reindex(close.columns) >= spec.mom60_min)

    if not eligible.any():
        return []
    ranked = ranks[eligible].sort_values(ascending=False).head(spec.top_n)
    return ranked.index.astype(str).tolist()


def quadrant_flip_rate(quadrant: pd.DataFrame, *, sample_dates: list[str] | None = None) -> dict:
    """日頻象限抖動：相鄰交易日 flip 比例。"""
    dates = sample_dates or quadrant.index.astype(str).tolist()
    flips = 0
    pairs = 0
    by_quad: dict[str, dict[str, float]] = {}
    for sid in quadrant.columns:
        sub = quadrant[sid].reindex(dates).dropna()
        if len(sub) < 2:
            continue
        prev = None
        q_flips = 0
        q_pairs = 0
        for q in sub:
            if prev is not None and q != prev:
                flips += 1
                q_flips += 1
            if prev is not None:
                pairs += 1
                q_pairs += 1
            prev = q
        if q_pairs:
            by_quad[str(sid)] = {"flip_rate_pct": round(q_flips / q_pairs * 100, 2), "pairs": q_pairs}
    return {
        "mean_flip_rate_pct": round(flips / pairs * 100, 2) if pairs else None,
        "n_pairs": pairs,
        "per_stock": by_quad,
    }


def run_rrg_monthly_backtest(
    conn: sqlite3.Connection,
    *,
    start_month: str = "2023-01",
    end_month: str = "2026-06",
    length: int = DEFAULT_LENGTH,
    hold_days: int = 9,
    cohorts: tuple[RrgCohortSpec, ...] | None = None,
    min_vcp_score: float | None = None,
    include_vcp: bool = True,
) -> dict:
    min_vcp = min_vcp_score if min_vcp_score is not None else load_min_composite()
    if cohorts is None:
        cohorts = _default_cohorts(min_vcp)
    needs_vcp = include_vcp and any(c.min_vcp_score is not None for c in cohorts)
    close, _opn, vol = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_momentum, quadrant = compute_rrg_panel(close, bench, length=length)

    all_dates = close.index.astype(str).tolist()
    month_ends = [
        d
        for d in month_end_trading_dates(all_dates)
        if start_month <= d[:7] <= end_month
    ]

    fund = load_fundamental_snapshot(conn)
    fin_hist = load_financial_history(conn)
    stock_ids = [str(c) for c in close.columns]

    results: dict[str, list[dict]] = {c.cohort_id: [] for c in cohorts}
    regime_by_eval = _breadth_map_for_months(
        conn, start_month=start_month, end_month=end_month
    )

    for signal_date in month_ends:
        fund_snap = pit_fundamental_at(fund, fin_hist, stock_ids, signal_date)
        _ = fund_snap  # reserved for future ROE filter

        entry_idx = all_dates.index(signal_date)
        if entry_idx + 1 >= len(all_dates):
            continue
        entry_date = all_dates[entry_idx + 1]
        breadth_zone_200 = regime_by_eval.get(signal_date, "neutral")

        vcp_scores: dict[str, float] | None = None
        if needs_vcp:
            leading_liquid = select_rrg_cohort(
                RrgCohortSpec("pool", "pool", "leading", "rs_ratio", top_n=999),
                signal_date=signal_date,
                close=close,
                rs_ratio=rs_ratio,
                rs_momentum=rs_momentum,
                quadrant=quadrant,
                vol=vol,
            )
            if leading_liquid:
                vcp_scores = vcp_scores_for_candidates(conn, leading_liquid, signal_date)

        for spec in cohorts:
            if spec.min_vcp_score is not None and not include_vcp:
                continue
            picks = select_rrg_cohort(
                spec,
                signal_date=signal_date,
                close=close,
                rs_ratio=rs_ratio,
                rs_momentum=rs_momentum,
                quadrant=quadrant,
                vol=vol,
                vcp_scores=vcp_scores,
            )
            ret = basket_return_h9(conn, picks, entry_date, hold_days=hold_days)
            exit_dates = trading_dates_after(conn, entry_date, count=hold_days)
            if len(exit_dates) < hold_days:
                continue
            exit_date = exit_dates[hold_days - 1]
            bench_ret = bench_return_entry_to_exit(
                conn, entry_date, exit_date, entry_price_mode="open"
            )
            if ret is None or bench_ret is None:
                continue
            period = {
                "signal_date": signal_date,
                "entry_date": entry_date,
                "n_picks": len(picks),
                "return_pct": ret,
                "bench_return_pct": bench_ret,
                "excess_pct": ret - bench_ret,
                "beat_bench": ret > bench_ret,
                "gross_win": ret > 0,
                "breadth_zone_200": breadth_zone_200,
            }
            results[spec.cohort_id].append(period)

    flip = quadrant_flip_rate(quadrant, sample_dates=all_dates[-252:])

    summary: dict[str, dict] = {}
    for spec in cohorts:
        periods = results[spec.cohort_id]
        base = summarize_periods(periods)
        sig = compute_excess_significance(
            [
                type("R", (), {"status": "complete", "return_pct": p["return_pct"], "bench_return_pct": p["bench_return_pct"]})()
                for p in periods
            ]
        )
        summary[spec.cohort_id] = {
            "label": spec.label,
            **base,
            "mean_excess_pct": sig.get("mean_excess_pct"),
            "t_stat_excess": sig.get("t_stat"),
        }

    primary_id = PRIMARY_STRATEGY_COHORT if PRIMARY_STRATEGY_COHORT in results else "leading_mom60_top20"
    primary_periods = results.get(primary_id, [])
    regime_rows, best_regime = summarize_by_breadth_zone(primary_periods)
    mom60_rows, mom60_best = summarize_by_breadth_zone(
        results.get("leading_mom60_top20", [])
    )
    live_xval = cross_validate_live_vcp_rrg(conn, min_vcp_score=min_vcp) if include_vcp else []

    return {
        "length": length,
        "hold_days": hold_days,
        "start_month": start_month,
        "end_month": end_month,
        "min_vcp_score": min_vcp,
        "primary_cohort_id": primary_id,
        "cohort_periods": results,
        "cohort_summary": summary,
        "quadrant_flip": flip,
        "regime_by_structure": regime_rows,
        "best_breadth_zone_200": best_regime,
        "regime_by_structure_mom60": mom60_rows,
        "best_breadth_zone_200_mom60": mom60_best,
        "live_vcp_crossval": live_xval,
    }


def render_rrg_backtest_markdown(result: dict) -> str:
    lines = [
        "# RRG 四象限回測（de Kempenaer · TradingView WMA）",
        "",
        f"> WMA length={result['length']} · H{result['hold_days']} · "
        f"{result['start_month']}～{result['end_month']} · 基準 IX0001",
        "",
        "## 方法定位",
        "",
        "| 維度 | 評分 | 說明 |",
        "|------|------|------|",
        "| A | 4/5 | 業界標準相對輪動語言 |",
        "| B | 4/5 | 對選股/板塊有效；非全市場狀態 |",
        "| C | 3.5/5 | 日頻象限可抖動 |",
        "| D | 4/5 | Leading → VCP；Weakening → 減碼 |",
        "| E | 4/5 | 方法公開、可複製 |",
        "",
        "**結論**：策略群內的選股 GPS；市場層廣度見 `market_breadth_ma`（% Above MA）。",
        "",
        "## 象限抖動（維度 C）",
        "",
        f"- 近 252 交易日平均 flip rate：**{result['quadrant_flip'].get('mean_flip_rate_pct')}%**",
        f"- 配對數：{result['quadrant_flip'].get('n_pairs')}",
        "",
        "## 月頻 H9 回測",
        "",
        "| 組別 | n | 勝率 vs 基準 | 平均超額 | t-stat |",
        "|------|---|-------------|---------|--------|",
    ]
    for cid, row in result["cohort_summary"].items():
        lines.append(
            f"| {row['label']} | {row.get('n_periods', 0)} | "
            f"{row.get('win_rate_vs_bench_pct', '—')}% | "
            f"{row.get('mean_excess_pct', '—')} | "
            f"{row.get('t_stat_excess', '—')} |"
        )

    lines.extend(
        [
            "",
            "## 廣度區間適配（market_breadth_ma · 200MA % Above MA）",
            "",
            f"主策略：**{result.get('cohort_summary', {}).get(result.get('primary_cohort_id', ''), {}).get('label', result.get('primary_cohort_id'))}**",
            "",
            "| 200MA 廣度區間 | n | 勝率 vs 基準 | 平均超額 |",
            "|----------------|---|-------------|---------|",
        ]
    )
    for row in result.get("regime_by_structure", []):
        lines.append(
            f"| {row['display']} | {row['n_periods']} | "
            f"{row.get('win_rate_vs_bench_pct', '—')}% | "
            f"{row.get('mean_excess_pct', '—')} |"
        )
    best = result.get("best_breadth_zone_200")
    if best:
        lines.extend(
            [
                "",
                f"**最適廣度區間（VCP 管線）**：{best['display']}（`{best['zone']}`）"
                f" — n={best['n_periods']}，平均超額 {best['mean_excess_pct']}%，"
                f"勝率 {best['win_rate_vs_bench_pct']}%",
            ]
        )

    mom60_rows = result.get("regime_by_structure_mom60") or []
    if mom60_rows:
        lines.extend(
            [
                "",
                "### 穩健性對照：Leading + Mom60（n=21 全樣本）",
                "",
                "| 動能結構 | n | 勝率 vs 基準 | 平均超額 |",
                "|----------|---|-------------|---------|",
            ]
        )
        for row in mom60_rows:
            lines.append(
                f"| {row['display']} | {row['n_periods']} | "
                f"{row.get('win_rate_vs_bench_pct', '—')}% | "
                f"{row.get('mean_excess_pct', '—')} |"
            )
        mom60_best = result.get("best_breadth_zone_200_mom60")
        if mom60_best:
            lines.append(
                f"\n**Mom60 最適結構**：{mom60_best['display']}（`{mom60_best['structure']}`）"
                f" — n={mom60_best['n_periods']}，平均超額 {mom60_best['mean_excess_pct']}%"
            )

    live = result.get("live_vcp_crossval") or []
    if live:
        n_lead = sum(1 for r in live if r.get("rrg_leading"))
        lines.extend(
            [
                "",
                "## 即時交叉驗證（vcp_screen × RRG）",
                "",
                f"- 樣本筆數：{len(live)}（VCP≥{result.get('min_vcp_score', 65):.0f}）",
                f"- RRG Leading 重合：{n_lead} / {len(live)}（{round(n_lead / len(live) * 100, 1)}%）",
            ]
        )

    lines.extend(
        [
            "",
            "## 應用解讀",
            "",
            "- **Leading Top20**：相對強勢且動能加速 → 順勢選股主池",
            "- **Leading + VCP≥50**：Leading 池內 VCP-TM composite 篩選（門檻見 config/vcp_tm_calibrated.yaml）",
            "- **Leading + Mom60**：VCP 歷史回測的輕量代理",
            "- **Weakening Top20**：相對仍強但動能轉弱 → 回測通常弱於 Leading，支持減碼邏輯",
            "- **Mom60 Top30**：純動能對照；RRG Leading 提供相對輪動濾網",
            "",
            "## 公式來源",
            "",
            "TradingView 開源 RRG clone（WMA 雙平滑）· "
            "[StockCharts RRG ChartSchool](https://chartschool.stockcharts.com/table-of-contents/chart-analysis/chart-types/relative-rotation-graphs-rrg-charts)",
            "",
        ]
    )
    return "\n".join(lines)
