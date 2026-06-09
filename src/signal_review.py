#!/usr/bin/env python3
"""④ 策略回顧：訊號事後歸因 + Paper 10 萬每日全換（只讀 stocks.db）。"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean, median

from holdings_research import AlignedCohort, TW_SPOT_CODE
from market_labels import (
    CHIP_FOREIGN_SELL_DIV,
    ENTRY_OVEREXTENDED,
    ENTRY_SKIP,
    PM_AVOID,
    PM_BREAKOUT,
    PM_OBSERVE,
)
from position_intent import apply_position_intents
from flow_attribution import format_flow_section, run_flow_attribution
from project_config import (
    DEFAULT_CAPITAL_NTD,
    DEFAULT_ETF_CODES,
    DEFAULT_FLOW_EVENT_LOOKBACK,
    SCORE_VERSION,
    parse_etf_codes,
)
from research_universe import DEFAULT_ETF_CODES as RESEARCH_ETF_CODES
from signal_engine import (
    StockSignal,
    _aggregate_stock_signals,
    _apply_conviction_scores,
    _assign_rotation_tags,
    _build_theme_flow_matrix,
    _collect_legs_aligned,
)
from stock_db import PROJECT_ROOT, connect, list_etf_snapshot_dates, load_stock_beta_map

REPORTS_DIR = PROJECT_ROOT / "reports"
LOG_DIR = PROJECT_ROOT / "logs"

BENCHMARK_CODE = TW_SPOT_CODE
MIN_BUCKET_N = 5
MIN_RULE_CHANGE_N = 20
HORIZON_DAYS = (1, 2, 3, 4, 5)
BUCKET_ORDER = (PM_BREAKOUT, PM_OBSERVE, PM_AVOID)
DEFAULT_BETA = 1.0


@dataclass(frozen=True)
class OutcomeRow:
    stock_id: str
    stock_name: str
    as_of_date: str
    outcome_date: str
    pm_bucket: str
    entry_signal: str
    chip_tag: str
    investment_score: float
    ret_pct: float
    bench_ret_pct: float
    alpha_pct: float  # raw excess: R_i − R_m
    capm_alpha_pct: float
    beta: float
    status: str = "complete"


@dataclass(frozen=True)
class PaperDayRow:
    signal_day: str
    outcome_day: str
    deployed_ntd: float
    pnl_ntd: float
    day_return_pct: float
    bench_return_pct: float
    alpha_ntd: float  # raw excess NTD（β=1 · 滿倉 capital）
    capm_alpha_ntd: float
    portfolio_beta: float
    status: str = "complete"


@dataclass(frozen=True)
class HorizonCell:
    horizon: int
    outcome_day: str | None
    pnl_ntd: float | None
    return_pct: float | None
    bench_return_pct: float | None
    alpha_ntd: float | None
    capm_alpha_ntd: float | None
    portfolio_beta: float | None
    status: str


@dataclass(frozen=True)
class PaperHorizonRow:
    signal_day: str
    deployed_ntd: float
    cells: tuple[HorizonCell, ...]
    status: str = "complete"


@dataclass
class BucketStats:
    bucket: str
    n: int
    mean_alpha: float | None
    median_alpha: float | None
    hit_rate: float | None
    mean_capm_alpha: float | None
    median_capm_alpha: float | None
    capm_hit_rate: float | None


@dataclass
class ReviewResult:
    window_start: str | None
    window_end: str | None
    signal_dates: list[str] = field(default_factory=list)
    outcomes: list[OutcomeRow] = field(default_factory=list)
    paper_days: list[PaperDayRow] = field(default_factory=list)
    paper_horizons: list[PaperHorizonRow] = field(default_factory=list)
    ic_by_date: dict[str, float | None] = field(default_factory=dict)
    bucket_stats: list[BucketStats] = field(default_factory=list)
    skipped_outcomes: int = 0
    beta_as_of: str | None = None
    message: str | None = None


def return_pct(close_t: float, close_t1: float) -> float:
    if close_t <= 0:
        return 0.0
    return (close_t1 - close_t) / close_t * 100.0


def capm_alpha_pct(ret_pct: float, bench_pct: float, beta: float) -> float:
    """CAPM α = R − β·R_m（報酬%）。"""
    return ret_pct - beta * bench_pct


def _beta_for_stock(
    beta_map: dict[str, sqlite3.Row],
    stock_id: str,
) -> float:
    row = beta_map.get(stock_id)
    if row is None or row["beta"] is None:
        return DEFAULT_BETA
    return float(row["beta"])


def _portfolio_beta(
    beta_map: dict[str, sqlite3.Row],
    weights: list[sqlite3.Row],
) -> float:
    deployed = 0.0
    weighted = 0.0
    for r in weights:
        ntd = float(r["suggested_ntd"] or 0)
        if ntd <= 0:
            continue
        deployed += ntd
        weighted += ntd * _beta_for_stock(beta_map, r["stock_id"])
    if deployed <= 0:
        return DEFAULT_BETA
    return weighted / deployed


def spearman_correlation(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None

    def _ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den_x = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    den_y = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def _bench_close(conn: sqlite3.Connection, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (BENCHMARK_CODE, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _stock_close(conn: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = 'finmind'
        """,
        (stock_id, trade_date),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _outcome_date_after_k(
    conn: sqlite3.Connection,
    signal_date: str,
    k: int,
) -> str | None:
    if k < 1:
        return None
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date AS d
        FROM stock_daily_bars
        WHERE trade_date > ? AND source = 'finmind'
        ORDER BY d ASC
        LIMIT ?
        """,
        (signal_date, k),
    ).fetchall()
    if len(rows) < k:
        return None
    outcome = str(rows[k - 1]["d"])
    if _bench_close(conn, signal_date) is None or _bench_close(conn, outcome) is None:
        return None
    return outcome


def _next_outcome_date(conn: sqlite3.Connection, signal_date: str) -> str | None:
    return _outcome_date_after_k(conn, signal_date, 1)


def list_signal_dates(
    conn: sqlite3.Connection,
    *,
    score_version: str,
    as_of: str,
    lookback: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT as_of_date AS d
        FROM pm_watchlist
        WHERE score_version = ? AND as_of_date <= ?
        ORDER BY d DESC
        LIMIT ?
        """,
        (score_version, as_of, lookback),
    ).fetchall()
    dates = [str(r["d"]) for r in rows]
    dates.reverse()
    return dates


def load_pm_for_date(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    score_version: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM pm_watchlist
        WHERE as_of_date = ? AND score_version = ?
        """,
        (as_of_date, score_version),
    ).fetchall()


def load_portfolio_for_date(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    score_version: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM portfolio_weights
        WHERE as_of_date = ? AND score_version = ?
          AND suggested_ntd > 0 AND portfolio_weight_pct > 0
        """,
        (as_of_date, score_version),
    ).fetchall()


def resolve_cohort_for_curr_date(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    curr_date: str,
    *,
    min_etfs: int = 2,
) -> AlignedCohort | None:
    buckets: dict[tuple[str, str], list[str]] = {}
    for etf_code in etf_codes:
        dates = list_etf_snapshot_dates(conn, etf_code)
        if curr_date not in dates:
            continue
        idx = dates.index(curr_date)
        if idx + 1 >= len(dates):
            continue
        prev = dates[idx + 1]
        buckets.setdefault((curr_date, prev), []).append(etf_code)
    if not buckets:
        return None
    (curr, prev), members = max(buckets.items(), key=lambda item: len(item[1]))
    if len(members) < min_etfs:
        return None
    return AlignedCohort(prev_date=prev, curr_date=curr, etf_codes=tuple(members))


def replay_signals_for_date(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...],
    curr_date: str,
) -> dict[str, StockSignal]:
    cohort = resolve_cohort_for_curr_date(conn, etf_codes, curr_date)
    if cohort is None:
        return {}
    legs = _collect_legs_aligned(
        conn, cohort.etf_codes, cohort.curr_date, cohort.prev_date
    )
    signals = _aggregate_stock_signals(legs)
    _apply_conviction_scores(signals)
    pairs = _build_theme_flow_matrix(signals)
    _assign_rotation_tags(signals, pairs)
    apply_position_intents(signals)
    return {s.stock_id: s for s in signals}


def build_outcome_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    outcome_date: str,
    bench_ret: float,
    *,
    beta_map: dict[str, sqlite3.Row],
) -> OutcomeRow | None:
    close_t = _stock_close(conn, row["stock_id"], row["as_of_date"])
    close_t1 = _stock_close(conn, row["stock_id"], outcome_date)
    if close_t is None or close_t1 is None:
        return None
    ret = return_pct(close_t, close_t1)
    beta = _beta_for_stock(beta_map, row["stock_id"])
    raw_excess = ret - bench_ret
    return OutcomeRow(
        stock_id=row["stock_id"],
        stock_name=row["stock_name"] or row["stock_id"],
        as_of_date=row["as_of_date"],
        outcome_date=outcome_date,
        pm_bucket=row["pm_bucket"] or "",
        entry_signal=row["entry_signal"] or "",
        chip_tag=row["chip_tag"] or "",
        investment_score=float(row["investment_score"]),
        ret_pct=ret,
        bench_ret_pct=bench_ret,
        alpha_pct=raw_excess,
        capm_alpha_pct=capm_alpha_pct(ret, bench_ret, beta),
        beta=beta,
    )


def aggregate_bucket_stats(outcomes: list[OutcomeRow]) -> list[BucketStats]:
    stats: list[BucketStats] = []
    complete = [o for o in outcomes if o.status == "complete"]
    for bucket in BUCKET_ORDER:
        rows = [o for o in complete if o.pm_bucket == bucket]
        if not rows:
            stats.append(
                BucketStats(
                    bucket=bucket,
                    n=0,
                    mean_alpha=None,
                    median_alpha=None,
                    hit_rate=None,
                    mean_capm_alpha=None,
                    median_capm_alpha=None,
                    capm_hit_rate=None,
                )
            )
            continue
        alphas = [o.alpha_pct for o in rows]
        capm_alphas = [o.capm_alpha_pct for o in rows]
        hits = sum(1 for a in alphas if a > 0)
        capm_hits = sum(1 for a in capm_alphas if a > 0)
        stats.append(
            BucketStats(
                bucket=bucket,
                n=len(rows),
                mean_alpha=mean(alphas),
                median_alpha=median(alphas),
                hit_rate=hits / len(rows) * 100.0,
                mean_capm_alpha=mean(capm_alphas),
                median_capm_alpha=median(capm_alphas),
                capm_hit_rate=capm_hits / len(rows) * 100.0,
            )
        )
    return stats


def _bench_return_between(
    conn: sqlite3.Connection,
    signal_day: str,
    outcome_day: str,
) -> float | None:
    bench_t = _bench_close(conn, signal_day)
    bench_t1 = _bench_close(conn, outcome_day)
    if bench_t is None or bench_t1 is None:
        return None
    return return_pct(bench_t, bench_t1)


def compute_paper_hold(
    conn: sqlite3.Connection,
    signal_day: str,
    outcome_day: str,
    *,
    score_version: str,
    capital_ntd: float,
) -> tuple[float, float, float, float, float, str]:
    """回傳 (deployed, pnl, return_pct, bench_ret, portfolio_beta, status)。"""
    rows = load_portfolio_for_date(conn, signal_day, score_version=score_version)
    if not rows:
        return 0.0, 0.0, 0.0, 0.0, DEFAULT_BETA, "skip_no_weights"
    bench_ret = _bench_return_between(conn, signal_day, outcome_day)
    if bench_ret is None:
        return 0.0, 0.0, 0.0, 0.0, DEFAULT_BETA, "skip_no_benchmark"
    beta_map, _ = load_stock_beta_map(conn)
    portfolio_beta = _portfolio_beta(beta_map, rows)
    deployed = 0.0
    pnl = 0.0
    priced = 0
    for r in rows:
        ntd = float(r["suggested_ntd"] or 0)
        if ntd <= 0:
            continue
        close_t = _stock_close(conn, r["stock_id"], signal_day)
        close_t1 = _stock_close(conn, r["stock_id"], outcome_day)
        if close_t is None or close_t1 is None:
            continue
        ret = return_pct(close_t, close_t1)
        deployed += ntd
        pnl += ntd * ret / 100.0
        priced += 1
    if deployed <= 0 or priced == 0:
        return 0.0, 0.0, 0.0, bench_ret, portfolio_beta, "skip_no_prices"
    return deployed, pnl, pnl / deployed * 100.0, bench_ret, portfolio_beta, "complete"


def compute_paper_day(
    conn: sqlite3.Connection,
    signal_day: str,
    outcome_day: str,
    bench_ret: float,
    *,
    score_version: str,
    capital_ntd: float,
) -> PaperDayRow:
    deployed, pnl, ret_pct, bench, portfolio_beta, status = compute_paper_hold(
        conn,
        signal_day,
        outcome_day,
        score_version=score_version,
        capital_ntd=capital_ntd,
    )
    if status != "complete":
        return PaperDayRow(
            signal_day=signal_day,
            outcome_day=outcome_day,
            deployed_ntd=deployed,
            pnl_ntd=pnl,
            day_return_pct=ret_pct,
            bench_return_pct=bench_ret if status == "skip_no_weights" else bench,
            alpha_ntd=0.0,
            capm_alpha_ntd=0.0,
            portfolio_beta=portfolio_beta,
            status=status,
        )
    bench_pnl = capital_ntd * bench / 100.0
    capm_bench_pnl = capital_ntd * portfolio_beta * bench / 100.0
    return PaperDayRow(
        signal_day=signal_day,
        outcome_day=outcome_day,
        deployed_ntd=deployed,
        pnl_ntd=pnl,
        day_return_pct=ret_pct,
        bench_return_pct=bench,
        alpha_ntd=pnl - bench_pnl,
        capm_alpha_ntd=pnl - capm_bench_pnl,
        portfolio_beta=portfolio_beta,
        status="complete",
    )


def compute_horizon_cell(
    conn: sqlite3.Connection,
    signal_day: str,
    horizon: int,
    *,
    score_version: str,
    capital_ntd: float,
) -> HorizonCell:
    outcome_day = _outcome_date_after_k(conn, signal_day, horizon)
    if outcome_day is None:
        return HorizonCell(
            horizon=horizon,
            outcome_day=None,
            pnl_ntd=None,
            return_pct=None,
            bench_return_pct=None,
            alpha_ntd=None,
            capm_alpha_ntd=None,
            portfolio_beta=None,
            status="skip_no_date",
        )
    deployed, pnl, ret_pct, bench_ret, portfolio_beta, status = compute_paper_hold(
        conn,
        signal_day,
        outcome_day,
        score_version=score_version,
        capital_ntd=capital_ntd,
    )
    if status != "complete":
        return HorizonCell(
            horizon=horizon,
            outcome_day=outcome_day,
            pnl_ntd=None,
            return_pct=None,
            bench_return_pct=bench_ret if bench_ret else None,
            alpha_ntd=None,
            capm_alpha_ntd=None,
            portfolio_beta=portfolio_beta if portfolio_beta else None,
            status=status,
        )
    bench_pnl = capital_ntd * bench_ret / 100.0
    capm_bench_pnl = capital_ntd * portfolio_beta * bench_ret / 100.0
    return HorizonCell(
        horizon=horizon,
        outcome_day=outcome_day,
        pnl_ntd=pnl,
        return_pct=ret_pct,
        bench_return_pct=bench_ret,
        alpha_ntd=pnl - bench_pnl,
        capm_alpha_ntd=pnl - capm_bench_pnl,
        portfolio_beta=portfolio_beta,
        status="complete",
    )


def compute_paper_horizon_row(
    conn: sqlite3.Connection,
    signal_day: str,
    *,
    score_version: str,
    capital_ntd: float,
    horizons: tuple[int, ...] = HORIZON_DAYS,
) -> PaperHorizonRow:
    cells = tuple(
        compute_horizon_cell(
            conn,
            signal_day,
            h,
            score_version=score_version,
            capital_ntd=capital_ntd,
        )
        for h in horizons
    )
    weights = load_portfolio_for_date(conn, signal_day, score_version=score_version)
    if not weights:
        return PaperHorizonRow(signal_day=signal_day, deployed_ntd=0.0, cells=cells, status="skip_no_weights")
    deployed = sum(float(r["suggested_ntd"] or 0) for r in weights)
    if deployed <= 0:
        return PaperHorizonRow(signal_day=signal_day, deployed_ntd=0.0, cells=cells, status="skip_no_weights")
    if not any(c.status == "complete" for c in cells):
        return PaperHorizonRow(signal_day=signal_day, deployed_ntd=deployed, cells=cells, status="skip_no_prices")
    return PaperHorizonRow(
        signal_day=signal_day,
        deployed_ntd=deployed,
        cells=cells,
        status="complete",
    )


def monotonicity_verdict(stats: list[BucketStats]) -> str:
    by_bucket = {s.bucket: s for s in stats}
    needed = [PM_BREAKOUT, PM_OBSERVE, PM_AVOID]
    if any(by_bucket.get(b) is None or by_bucket[b].n < MIN_BUCKET_N for b in needed):
        return "樣本不足（每桶需 N≥5）"
    b = by_bucket[PM_BREAKOUT].mean_capm_alpha or 0.0
    o = by_bucket[PM_OBSERVE].mean_capm_alpha or 0.0
    a = by_bucket[PM_AVOID].mean_capm_alpha or 0.0
    if b >= o >= a:
        return "支持 H1（突破 ≥ 觀察 ≥ 回避）"
    return "不支持 H1"


def run_review(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    lookback: int = 7,
    score_version: str = SCORE_VERSION,
    capital_ntd: float = DEFAULT_CAPITAL_NTD,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> ReviewResult:
    ref = as_of or date.today().isoformat()
    signal_dates = list_signal_dates(
        conn, score_version=score_version, as_of=ref, lookback=lookback
    )
    if not signal_dates:
        return ReviewResult(
            window_start=None,
            window_end=None,
            message="尚無 pm_watchlist 紀錄（請先跑收盤 Score Engine --sync-db）",
        )

    outcomes: list[OutcomeRow] = []
    paper_days: list[PaperDayRow] = []
    paper_horizons: list[PaperHorizonRow] = []
    ic_by_date: dict[str, float | None] = {}
    skipped = 0
    beta_map, beta_as_of = load_stock_beta_map(conn)

    for signal_day in signal_dates:
        paper_horizons.append(
            compute_paper_horizon_row(
                conn,
                signal_day,
                score_version=score_version,
                capital_ntd=capital_ntd,
            )
        )
        outcome_day = _next_outcome_date(conn, signal_day)
        if outcome_day is None:
            skipped += len(load_pm_for_date(conn, signal_day, score_version=score_version))
            continue
        bench_t = _bench_close(conn, signal_day)
        bench_t1 = _bench_close(conn, outcome_day)
        if bench_t is None or bench_t1 is None:
            skipped += len(load_pm_for_date(conn, signal_day, score_version=score_version))
            continue
        bench_ret = return_pct(bench_t, bench_t1)

        pm_rows = load_pm_for_date(conn, signal_day, score_version=score_version)
        day_scores: list[float] = []
        day_alphas: list[float] = []
        for row in pm_rows:
            out = build_outcome_row(
                conn, row, outcome_day, bench_ret, beta_map=beta_map
            )
            if out is None:
                skipped += 1
                continue
            outcomes.append(out)
            day_scores.append(out.investment_score)
            day_alphas.append(out.capm_alpha_pct)

        ic_by_date[signal_day] = spearman_correlation(day_scores, day_alphas)
        paper_days.append(
            compute_paper_day(
                conn,
                signal_day,
                outcome_day,
                bench_ret,
                score_version=score_version,
                capital_ntd=capital_ntd,
            )
        )

    return ReviewResult(
        window_start=signal_dates[0],
        window_end=signal_dates[-1],
        signal_dates=signal_dates,
        outcomes=outcomes,
        paper_days=paper_days,
        paper_horizons=paper_horizons,
        ic_by_date=ic_by_date,
        bucket_stats=aggregate_bucket_stats(outcomes),
        skipped_outcomes=skipped,
        beta_as_of=beta_as_of,
    )


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _fmt_ntd(v: float) -> str:
    return f"{v:+,.0f}"


def _fmt_horizon_cell(cell: HorizonCell) -> str:
    if cell.status != "complete" or cell.pnl_ntd is None or cell.return_pct is None:
        return "—"
    capm = (
        f" / CAPM {_fmt_ntd(cell.capm_alpha_ntd)}"
        if cell.capm_alpha_ntd is not None
        else ""
    )
    return f"{_fmt_ntd(cell.pnl_ntd)} / {_fmt_pct(cell.return_pct)}{capm}"


def _horizon_summary_row(rows: list[PaperHorizonRow]) -> list[str]:
    """各 H+k 窗口平均報酬%（有 complete 列才計）。"""
    lines: list[str] = []
    for h in HORIZON_DAYS:
        rets = [
            c.return_pct
            for row in rows
            for c in row.cells
            if c.horizon == h and c.status == "complete" and c.return_pct is not None
        ]
        if rets:
            lines.append(f"- H+{h} 平均報酬 {_fmt_pct(mean(rets))}（N={len(rets)} 列）")
        else:
            lines.append(f"- H+{h} 平均報酬 —")
    return lines


def _top_outliers(outcomes: list[OutcomeRow], n: int = 3) -> tuple[list[OutcomeRow], list[OutcomeRow]]:
    complete = [o for o in outcomes if o.status == "complete"]
    if not complete:
        return [], []
    pos = sorted(complete, key=lambda o: o.capm_alpha_pct, reverse=True)[:n]
    neg = sorted(complete, key=lambda o: o.capm_alpha_pct)[:n]
    return pos, neg


def _rule_subset_lines(
    conn: sqlite3.Connection,
    outcomes: list[OutcomeRow],
    etf_codes: tuple[str, ...],
) -> list[str]:
    lines: list[str] = []
    complete = [o for o in outcomes if o.status == "complete"]
    if not complete:
        lines.append("（無完整 outcome，略過 R1–R5）")
        return lines

    breakout_alphas = [o.capm_alpha_pct for o in complete if o.pm_bucket == PM_BREAKOUT]
    breakout_mean = mean(breakout_alphas) if breakout_alphas else None

    r1 = [o for o in complete if o.entry_signal == ENTRY_OVEREXTENDED]
    if r1:
        m = mean(o.capm_alpha_pct for o in r1)
        cmp_txt = _fmt_pct(breakout_mean) if breakout_mean is not None else "—"
        lines.append(
            f"- **R1 乖離過大** N={len(r1)} Mean CAPM α {_fmt_pct(m)}（突破組 {cmp_txt}）"
        )
    else:
        lines.append("- **R1 乖離過大** —（本窗口無）")

    r2 = [o for o in complete if o.chip_tag == CHIP_FOREIGN_SELL_DIV]
    if r2:
        m = mean(o.capm_alpha_pct for o in r2)
        lines.append(f"- **R2 外資賣超背離** N={len(r2)} Mean CAPM α {_fmt_pct(m)}")
    else:
        lines.append("- **R2 外資賣超背離** —（本窗口無）")

    r3 = [o for o in complete if o.pm_bucket == PM_AVOID]
    if r3:
        m = mean(o.capm_alpha_pct for o in r3)
        lines.append(f"- **R3 回避** N={len(r3)} Mean CAPM α {_fmt_pct(m)}")
    else:
        lines.append("- **R3 回避** —（本窗口無）")

    regime_rows: list[OutcomeRow] = []
    other_rows: list[OutcomeRow] = []
    for o in complete:
        tech = conn.execute(
            """
            SELECT tsm_daily_return_pct FROM tech_risk_daily_snapshot
            WHERE session_date = ?
            """,
            (o.as_of_date,),
        ).fetchone()
        if tech and tech["tsm_daily_return_pct"] is not None and float(tech["tsm_daily_return_pct"]) < -2.0:
            regime_rows.append(o)
        else:
            other_rows.append(o)
    if regime_rows:
        rm = mean(o.capm_alpha_pct for o in regime_rows)
        om = mean(o.capm_alpha_pct for o in other_rows) if other_rows else None
        lines.append(
            f"- **R4 TSM ADR<-2%** N={len(regime_rows)} Mean CAPM α {_fmt_pct(rm)}"
            + (f"（非 regime {_fmt_pct(om)}）" if om is not None else "")
        )
    else:
        lines.append("- **R4 TSM ADR<-2%** —（本窗口無 regime 日）")

    false_rows: list[OutcomeRow] = []
    for signal_day in {o.as_of_date for o in complete}:
        sig_map = replay_signals_for_date(conn, etf_codes, signal_day)
        if not sig_map:
            continue
        for o in complete:
            if o.as_of_date != signal_day:
                continue
            sig = sig_map.get(o.stock_id)
            if sig and sig.consensus_level == "FALSE" and sig.net_side == "add":
                false_rows.append(o)
    if false_rows:
        m = mean(o.capm_alpha_pct for o in false_rows)
        non_false = [o for o in complete if o not in false_rows]
        nm = mean(o.capm_alpha_pct for o in non_false) if non_false else None
        lines.append(
            f"- **R5 假共識 FALSE** N={len(false_rows)} Mean CAPM α {_fmt_pct(m)}"
            + (f"（其餘 {_fmt_pct(nm)}）" if nm is not None else "")
        )
    else:
        lines.append("- **R5 假共識 FALSE** —（本窗口無或無法 replay）")

    return lines


def _exit_stats_lines(
    conn: sqlite3.Connection,
    signal_dates: list[str],
    outcomes: list[OutcomeRow],
    *,
    score_version: str,
) -> list[str]:
    lines: list[str] = []
    skip_count = sum(1 for o in outcomes if o.entry_signal == ENTRY_SKIP)
    avoid_count = sum(1 for o in outcomes if o.pm_bucket == PM_AVOID)
    lines.append(f"- **暫不進場** 列數 {skip_count}")
    lines.append(f"- **回避** 列數 {avoid_count}")

    weight_zero = 0
    for i in range(1, len(signal_dates)):
        prev_d, curr_d = signal_dates[i - 1], signal_dates[i]
        prev_w = {
            r["stock_id"]: float(r["portfolio_weight_pct"])
            for r in load_portfolio_for_date(conn, prev_d, score_version=score_version)
        }
        curr_w = {
            r["stock_id"]: float(r["portfolio_weight_pct"])
            for r in load_portfolio_for_date(conn, curr_d, score_version=score_version)
        }
        for sid, pct in prev_w.items():
            if pct > 0 and curr_w.get(sid, 0) <= 0:
                weight_zero += 1
    lines.append(f"- **權重→0（日間）** 次數 {weight_zero}")

    avoid_alphas = [o.capm_alpha_pct for o in outcomes if o.pm_bucket == PM_AVOID]
    if avoid_alphas:
        lines.append(f"- 回避標的 Mean CAPM α {_fmt_pct(mean(avoid_alphas))}")
    return lines


def format_report(
    result: ReviewResult,
    conn: sqlite3.Connection,
    *,
    score_version: str = SCORE_VERSION,
    capital_ntd: float = DEFAULT_CAPITAL_NTD,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback_event_days: int = DEFAULT_FLOW_EVENT_LOOKBACK,
) -> str:
    flow_result = run_flow_attribution(
        conn,
        as_of=result.window_end,
        lookback=lookback_event_days,
    )
    flow_lines = format_flow_section(flow_result)

    if result.message:
        return (
            f"# Signal Attribution Report\n\n"
            + "\n".join(flow_lines)
            + f"\n> {result.message}\n"
        )

    complete_n = len([o for o in result.outcomes if o.status == "complete"])
    window = f"{result.window_start} ~ {result.window_end}"
    lines = [
        f"# Signal Attribution Report（窗口：{window}）",
        "",
        f"> Signal Book · 非 Live P&L · Score {score_version} · 基準 {BENCHMARK_CODE}",
        "> raw excess = R−R_m · CAPM α = R−β·R_m · β 來源 stock_beta（缺值 β=1）",
        "",
        *flow_lines,
        "## §1 資料覆蓋",
        "",
        f"- signal-days：{len(result.signal_dates)}",
        f"- complete outcomes：{complete_n}",
        f"- skipped：{result.skipped_outcomes}",
        f"- stock_beta as_of：{result.beta_as_of or '—'}",
        "",
        "## §2 分桶表現（Portfolio Sort）",
        "",
        "| pm_bucket | N | Mean raw | Mean CAPM α | Median CAPM α | CAPM Hit |",
        "|-----------|---|----------|-------------|---------------|----------|",
    ]
    for s in result.bucket_stats:
        lines.append(
            f"| {s.bucket} | {s.n} | {_fmt_pct(s.mean_alpha)} | "
            f"{_fmt_pct(s.mean_capm_alpha)} | {_fmt_pct(s.median_capm_alpha)} | "
            f"{(f'{s.capm_hit_rate:.0f}%' if s.capm_hit_rate is not None else '—')} |"
        )

    lines.extend(["", "## §3 Monotonicity 檢視", "", f"- 結論：**{monotonicity_verdict(result.bucket_stats)}**", ""])

    ics = [v for v in result.ic_by_date.values() if v is not None]
    lines.extend(["## §4 橫截面 IC", ""])
    lines.append("- IC = Spearman(investment_score, **CAPM α**)  per signal-day")
    if ics:
        pos = sum(1 for v in ics if v > 0)
        lines.append(f"- Mean IC {mean(ics):+.3f}")
        lines.append(f"- IC>0 日比例 {pos}/{len(ics)}")
        for d, ic in result.ic_by_date.items():
            lines.append(f"  - {d}  IC {(f'{ic:+.3f}' if ic is not None else '—')}")
    else:
        lines.append("- （無足夠 IC 樣本）")
    lines.append("")

    lines.extend(["## §5 風控規則子集（R1–R5）", ""])
    lines.extend(_rule_subset_lines(conn, result.outcomes, etf_codes))
    lines.append("")

    pos, neg = _top_outliers(result.outcomes)
    lines.extend(["## §6 異常個案（Top ±CAPM α）", ""])
    if pos:
        lines.append("**Top +CAPM α**")
        for o in pos:
            lines.append(
                f"- {o.stock_id} {o.stock_name} {o.as_of_date}→{o.outcome_date} "
                f"{o.pm_bucket} CAPM {_fmt_pct(o.capm_alpha_pct)} "
                f"(raw {_fmt_pct(o.alpha_pct)} β={o.beta:.2f})"
            )
    if neg:
        lines.append("")
        lines.append("**Top −CAPM α**")
        for o in neg:
            lines.append(
                f"- {o.stock_id} {o.stock_name} {o.as_of_date}→{o.outcome_date} "
                f"{o.pm_bucket} CAPM {_fmt_pct(o.capm_alpha_pct)} "
                f"(raw {_fmt_pct(o.alpha_pct)} β={o.beta:.2f})"
            )
    if not pos and not neg:
        lines.append("（無 complete outcome）")
    lines.append("")

    lines.extend(
        [
            "## §2b Paper Portfolio（10 萬 · 每日全換 · T+1）",
            "",
            "> Signal Book · 1-day hold · Gross · No costs · β_port = Σ(w_i·β_i)",
            "",
            "| signal-day | 投入 | β_port | 損益 | 報酬 | R_m | raw excess | CAPM α |",
            "|------------|------|--------|------|------|-----|------------|--------|",
        ]
    )
    total_pnl = 0.0
    total_raw = 0.0
    total_capm = 0.0
    day_returns: list[float] = []
    paper_by_day = {p.signal_day: p for p in result.paper_days}
    horizon_by_day = {h.signal_day: h for h in result.paper_horizons}
    for signal_day in result.signal_dates:
        p = paper_by_day.get(signal_day)
        if p is None:
            h = horizon_by_day.get(signal_day)
            deployed = f"{h.deployed_ntd:,.0f}" if h and h.deployed_ntd > 0 else "—"
            lines.append(f"| {signal_day} | {deployed} | — | — | — | — | — | skip |")
            continue
        if p.status != "complete":
            dep = f"{p.deployed_ntd:,.0f}" if p.deployed_ntd > 0 else "—"
            lines.append(
                f"| {p.signal_day} | {dep} | — | — | — | "
                f"{_fmt_pct(p.bench_return_pct)} | — | skip |"
            )
            continue
        total_pnl += p.pnl_ntd
        total_raw += p.alpha_ntd
        total_capm += p.capm_alpha_ntd
        day_returns.append(p.day_return_pct)
        lines.append(
            f"| {p.signal_day} | {p.deployed_ntd:,.0f} | {p.portfolio_beta:.2f} | "
            f"{_fmt_ntd(p.pnl_ntd)} | {_fmt_pct(p.day_return_pct)} | "
            f"{_fmt_pct(p.bench_return_pct)} | {_fmt_ntd(p.alpha_ntd)} | "
            f"{_fmt_ntd(p.capm_alpha_ntd)} |"
        )
    avg_ret = mean(day_returns) if day_returns else None
    bench_sum_pct = sum(p.bench_return_pct for p in result.paper_days if p.status == "complete")
    lines.append(
        f"| **累計** | — | — | **{_fmt_ntd(total_pnl)}** | "
        f"**{_fmt_pct(avg_ret)} avg** | **{_fmt_pct(bench_sum_pct)} Σ** | "
        f"**{_fmt_ntd(total_raw)}** | **{_fmt_ntd(total_capm)}** |"
    )
    lines.append("")

    h_cols = " | ".join(f"H+{h}" for h in HORIZON_DAYS)
    lines.extend(
        [
            "## §2c Paper 持有天數曲線（10 萬 · 全窗口 · 同一買入日）",
            "",
            "> 買入：T 收盤 · 賣出：T+k 收盤 · 每格：損益 / 報酬% / CAPM α NTD",
            "",
            f"| signal-day | 投入 (NTD) | {h_cols} |",
            "|------------|------------|" + "|".join(["------"] * len(HORIZON_DAYS)) + "|",
        ]
    )
    for row in result.paper_horizons:
        if row.status == "skip_no_weights":
            lines.append(f"| {row.signal_day} | — | " + " | ".join(["—"] * len(HORIZON_DAYS)) + " |")
            continue
        cells_by_h = {c.horizon: c for c in row.cells}
        cell_txt = " | ".join(_fmt_horizon_cell(cells_by_h[h]) for h in HORIZON_DAYS)
        lines.append(f"| {row.signal_day} | {row.deployed_ntd:,.0f} | {cell_txt} |")
    lines.append("")
    lines.append("**窗口平均**")
    lines.extend(_horizon_summary_row(result.paper_horizons))
    lines.append("")

    lines.extend(["## §7 出場訊號統計（輕量）", ""])
    lines.extend(
        _exit_stats_lines(
            conn, result.signal_dates, result.outcomes, score_version=score_version
        )
    )
    lines.append("")

    advise = complete_n < MIN_RULE_CHANGE_N
    lines.extend(
        [
            "## §8 策略調整備忘（人工）",
            "",
            f"- [{'x' if advise else ' '}] 本窗口 complete outcomes < {MIN_RULE_CHANGE_N}：**不建議**改 rule",
            "- [ ] 待討論：",
            "",
            "## §9 參考文獻",
            "",
            "見 [signal-review-PRD.md](./signal-review-PRD.md) §3",
            "",
        ]
    )
    return "\n".join(lines)


def print_terminal_report(
    report_text: str,
    *,
    report_path: Path | None = None,
    log_path: Path | None = None,
) -> None:
    print("")
    print("=== ④ 策略回顧（Signal Review · 只讀 DB）===")
    print("")
    print(report_text)
    print("---")
    if report_path:
        print(f"完整章節見報告檔：{report_path.relative_to(PROJECT_ROOT)}")
    if log_path:
        print(f"log  logs/{log_path.name}")


def write_report(
    result: ReviewResult,
    conn: sqlite3.Connection,
    *,
    score_version: str = SCORE_VERSION,
    capital_ntd: float = DEFAULT_CAPITAL_NTD,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    reports_dir: Path | None = None,
    report_text: str | None = None,
) -> Path:
    out_dir = reports_dir or REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    path = out_dir / f"{stamp}_signal_review.md"
    path.write_text(
        report_text
        if report_text is not None
        else build_report_text(
            result,
            conn,
            score_version=score_version,
            capital_ntd=capital_ntd,
            etf_codes=etf_codes,
        ),
        encoding="utf-8",
    )
    return path


def build_report_text(
    result: ReviewResult,
    conn: sqlite3.Connection,
    *,
    score_version: str = SCORE_VERSION,
    capital_ntd: float = DEFAULT_CAPITAL_NTD,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
    lookback_event_days: int = DEFAULT_FLOW_EVENT_LOOKBACK,
) -> str:
    return format_report(
        result,
        conn,
        score_version=score_version,
        capital_ntd=capital_ntd,
        etf_codes=etf_codes,
        lookback_event_days=lookback_event_days,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="④ 策略回顧（Signal Review）")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", default=None, help="窗口終點 YYYY-MM-DD")
    parser.add_argument("--lookback-trading-days", type=int, default=7)
    parser.add_argument(
        "--lookback-event-days",
        type=int,
        default=DEFAULT_FLOW_EVENT_LOOKBACK,
        help="§0 Flow Attribution 窗口（flow_events event-days）",
    )
    parser.add_argument("--score-version", default=SCORE_VERSION)
    parser.add_argument("--capital-ntd", type=float, default=None)
    parser.add_argument("--etf-codes", default=",".join(RESEARCH_ETF_CODES))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    capital = args.capital_ntd if args.capital_ntd is not None else DEFAULT_CAPITAL_NTD
    etf_codes = parse_etf_codes(args.etf_codes)
    db_path = args.db
    conn = connect(db_path) if db_path else connect()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"signal_review_{date.today().strftime('%Y%m%d')}.log"

    try:
        result = run_review(
            conn,
            as_of=args.as_of,
            lookback=args.lookback_trading_days,
            score_version=args.score_version,
            capital_ntd=capital,
            etf_codes=etf_codes,
        )
        report_text = build_report_text(
            result,
            conn,
            score_version=args.score_version,
            capital_ntd=capital,
            etf_codes=etf_codes,
            lookback_event_days=args.lookback_event_days,
        )
        report_path = write_report(
            result,
            conn,
            score_version=args.score_version,
            capital_ntd=capital,
            etf_codes=etf_codes,
            report_text=report_text,
        )
        log_line = (
            f"signal_review {date.today().isoformat()} "
            f"window={result.window_start}..{result.window_end} "
            f"outcomes={len(result.outcomes)} report={report_path.name}"
        )
        log_path.write_text(log_line + "\n", encoding="utf-8")
        if not args.quiet:
            print_terminal_report(
                report_text,
                report_path=report_path,
                log_path=log_path,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
