"""Dual-WMA short-length sweep · WMA(3) vs WMA(5) fast-chart validation.

Anchors WMA(20) · compares daily stability, false-reduction proxy, W20 weakening
lead-lag, and optional intraday signal counts (lead_pullback_buy).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Literal

import pandas as pd

from market_benchmark import load_benchmark_close
from project_config import DEFAULT_ETF_CODES
from research.backtest.dual_wma_signal_backtest import EntryParams, _collect_events
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_rotation import compute_rrg_panel, quadrant_flip_rate
from rrg_universe_intraday_panel import (
    WMA_LONG_LENGTH,
    build_dual_wma_intraday_trajectories,
    classify_wma_spread,
)
from stock_db import load_etf_constituent_watchlist

DEFAULT_SHORT_LENGTHS: tuple[int, ...] = (3, 5)
_WEAK = frozenset({"weakening", "lagging"})
_STRONG = frozenset({"leading", "improving"})
_BOUNDARY_EPS = 1.0


def build_short_quadrant_panel(
    close: pd.DataFrame,
    bench: pd.Series,
    *,
    length: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rs, mom, quad = compute_rrg_panel(close, bench, length=length)
    return rs, mom, quad


def build_daily_spread_panel(
    close: pd.DataFrame,
    bench: pd.Series,
    *,
    short_length: int,
    long_length: int = WMA_LONG_LENGTH,
) -> pd.DataFrame:
    rs_s, _, _ = compute_rrg_panel(close, bench, length=short_length)
    rs_l, _, _ = compute_rrg_panel(close, bench, length=long_length)
    common = rs_s.columns.intersection(rs_l.columns)
    return rs_s[common].subtract(rs_l[common], axis=0)


def _universe_close(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    *,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> pd.DataFrame:
    watch = load_etf_constituent_watchlist(conn, etf_codes)
    ids = [w["stock_id"] for w in watch if w["stock_id"] in close.columns]
    return close[ids] if ids else close.iloc[:, :0]


def _boundary_fraction(
    rs: pd.DataFrame,
    mom: pd.DataFrame,
    trade_dates: list[str],
    *,
    eps: float = _BOUNDARY_EPS,
) -> float | None:
    n = 0
    boundary = 0
    for d in trade_dates:
        if d not in rs.index:
            continue
        for sid in rs.columns:
            rv = rs.at[d, sid]
            mv = mom.at[d, sid] if d in mom.index and sid in mom.columns else float("nan")
            if pd.isna(rv) or pd.isna(mv):
                continue
            n += 1
            if abs(float(rv) - 100.0) <= eps and abs(float(mv) - 100.0) <= eps:
                boundary += 1
    return round(boundary / n * 100, 2) if n else None


def _spread_class_counts(
    close: pd.DataFrame,
    bench: pd.Series,
    trade_dates: list[str],
    *,
    short_length: int,
) -> dict[str, Any]:
    rs_s, mom_s, _ = compute_rrg_panel(close, bench, length=short_length)
    rs20, mom20, _ = compute_rrg_panel(close, bench, length=WMA_LONG_LENGTH)
    counts: dict[str, int] = {"aligned": 0, "extension": 0, "pullback": 0, "mixed": 0}
    n = 0
    for d in trade_dates:
        if d not in rs_s.index or d not in rs20.index:
            continue
        for sid in rs_s.columns:
            if sid not in rs20.columns:
                continue
            rv5 = rs_s.at[d, sid]
            mv5 = mom_s.at[d, sid]
            rv20 = rs20.at[d, sid]
            mv20 = mom20.at[d, sid]
            if any(pd.isna(v) for v in (rv5, mv5, rv20, mv20)):
                continue
            cls, _, _, _ = classify_wma_spread(float(rv5), float(mv5), float(rv20), float(mv20))
            counts[cls] = counts.get(cls, 0) + 1
            n += 1
    return {
        "n_stock_days": n,
        "pct_aligned": round(counts.get("aligned", 0) / n * 100, 2) if n else None,
        "pct_extension": round(counts.get("extension", 0) / n * 100, 2) if n else None,
        "pct_pullback": round(counts.get("pullback", 0) / n * 100, 2) if n else None,
        "pct_mixed": round(counts.get("mixed", 0) / n * 100, 2) if n else None,
    }


def _false_reduction_proxy(
    short_quad: pd.DataFrame,
    w20_quad: pd.DataFrame,
    trade_dates: list[str],
) -> dict[str, Any]:
    """Short weak while W20 still strong · same-day stock-days."""
    n_strong = 0
    n_false = 0
    for d in trade_dates:
        if d not in short_quad.index or d not in w20_quad.index:
            continue
        for sid in short_quad.columns:
            if sid not in w20_quad.columns:
                continue
            sq = short_quad.at[d, sid]
            wq = w20_quad.at[d, sid]
            if pd.isna(sq) or pd.isna(wq):
                continue
            if wq not in _STRONG:
                continue
            n_strong += 1
            if sq in _WEAK:
                n_false += 1
    return {
        "n_w20_strong_stock_days": n_strong,
        "false_reduction_pct": round(n_false / n_strong * 100, 2) if n_strong else None,
    }


def _w20_leading_to_weakening_lead(
    w20_quad: pd.DataFrame,
    short_quad: pd.DataFrame,
    trade_dates: list[str],
) -> dict[str, Any]:
    """When W20 flips leading→weakening, was short already weak the prior day?"""
    events = 0
    prior_warn = 0
    same_day_warn = 0
    for i in range(1, len(trade_dates)):
        d_prev, d = trade_dates[i - 1], trade_dates[i]
        if d not in w20_quad.index or d_prev not in w20_quad.index:
            continue
        for sid in w20_quad.columns:
            if sid not in short_quad.columns:
                continue
            w_prev = w20_quad.at[d_prev, sid]
            w_now = w20_quad.at[d, sid]
            if w_prev != "leading" or w_now != "weakening":
                continue
            events += 1
            sq_prev = short_quad.at[d_prev, sid] if d_prev in short_quad.index else None
            sq_now = short_quad.at[d, sid] if d in short_quad.index else None
            if sq_prev in _WEAK:
                prior_warn += 1
            if sq_now in _WEAK:
                same_day_warn += 1
    return {
        "w20_leading_to_weakening_events": events,
        "short_weak_prior_day_pct": round(prior_warn / events * 100, 2) if events else None,
        "short_weak_same_day_pct": round(same_day_warn / events * 100, 2) if events else None,
    }


def _short_spurious_flip(
    short_quad: pd.DataFrame,
    w20_quad: pd.DataFrame,
    trade_dates: list[str],
) -> dict[str, Any]:
    """Short weak on t while W20 strong, short strong again on t+1 while W20 still strong."""
    n = 0
    spurious = 0
    for i in range(1, len(trade_dates) - 1):
        d0, d1, d2 = trade_dates[i - 1], trade_dates[i], trade_dates[i + 1]
        for sid in short_quad.columns:
            if sid not in w20_quad.columns:
                continue
            for d in (d0, d1, d2):
                if d not in short_quad.index or d not in w20_quad.index:
                    break
            else:
                if w20_quad.at[d0, sid] not in _STRONG:
                    continue
                if w20_quad.at[d1, sid] not in _STRONG:
                    continue
                if w20_quad.at[d2, sid] not in _STRONG:
                    continue
                s0, s1, s2 = short_quad.at[d0, sid], short_quad.at[d1, sid], short_quad.at[d2, sid]
                if any(pd.isna(x) for x in (s0, s1, s2)):
                    continue
                n += 1
                if s1 in _WEAK and s2 in _STRONG:
                    spurious += 1
    return {
        "n_w20_strong_3day_windows": n,
        "spurious_short_weak_pct": round(spurious / n * 100, 2) if n else None,
    }


def daily_short_length_metrics(
    close: pd.DataFrame,
    bench: pd.Series,
    trade_dates: list[str],
    *,
    short_length: int,
) -> dict[str, Any]:
    rs, mom, short_quad = build_short_quadrant_panel(close, bench, length=short_length)
    _, _, w20_quad = build_short_quadrant_panel(close, bench, length=WMA_LONG_LENGTH)
    flip = quadrant_flip_rate(short_quad, sample_dates=trade_dates)
    spread = build_daily_spread_panel(close, bench, short_length=short_length)
    spread_rs = spread.reindex(trade_dates).stack().dropna()
    return {
        "short_length": short_length,
        "n_trade_dates": len(trade_dates),
        "n_stocks": len(short_quad.columns),
        "quadrant_flip_rate_pct": flip.get("mean_flip_rate_pct"),
        "boundary_stock_day_pct": _boundary_fraction(rs, mom, trade_dates),
        "spread_rs_mean": round(float(spread_rs.mean()), 3) if len(spread_rs) else None,
        "spread_rs_std": round(float(spread_rs.std()), 3) if len(spread_rs) > 1 else None,
        "spread_classes": _spread_class_counts(close, bench, trade_dates, short_length=short_length),
        "false_reduction": _false_reduction_proxy(short_quad, w20_quad, trade_dates),
        "w20_weakening_lead": _w20_leading_to_weakening_lead(w20_quad, short_quad, trade_dates),
        "spurious_short_weak": _short_spurious_flip(short_quad, w20_quad, trade_dates),
    }


def intraday_signal_counts(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    short_length: int,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    frames, trajectories, meta = build_dual_wma_intraday_trajectories(
        conn,
        dates=dates,
        etf_codes=etf_codes,
        short_length=short_length,
        long_length=WMA_LONG_LENGTH,
    )
    signals = ("lead_pullback_buy", "imp_extension", "imp_early_long")
    out: dict[str, int] = {}
    for sig in signals:
        ep = EntryParams(signal=sig, first_per_day=True)
        out[sig] = len(_collect_events(trajectories, frames, ep))
    return {
        "short_length": short_length,
        "n_dates": len(dates),
        "n_trajectories": meta.get("n_trajectories"),
        "signal_counts": out,
        "signal_total": sum(out.values()),
    }


def _score_fast_chart(row: dict[str, Any]) -> float:
    """Higher = better fast chart (stable + informative)."""
    flip = float(row.get("quadrant_flip_rate_pct") or 999)
    boundary = float(row.get("boundary_stock_day_pct") or 999)
    false_red = float((row.get("false_reduction") or {}).get("false_reduction_pct") or 999)
    spurious = float((row.get("spurious_short_weak") or {}).get("spurious_short_weak_pct") or 999)
    prior = float((row.get("w20_weakening_lead") or {}).get("short_weak_prior_day_pct") or 0)
    # lower noise + higher early warning
    return prior - 0.5 * flip - 0.3 * false_red - 0.2 * spurious - 0.1 * boundary


def verdict_short_length_sweep(
    daily_rows: list[dict[str, Any]],
    *,
    intraday_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_len = {int(r["short_length"]): r for r in daily_rows}
    if 3 not in by_len or 5 not in by_len:
        return {"recommend": None, "summary": "Need WMA3 and WMA5 rows"}

    s3, s5 = by_len[3], by_len[5]
    score3 = _score_fast_chart(s3)
    score5 = _score_fast_chart(s5)

    flip_delta = float(s3.get("quadrant_flip_rate_pct") or 0) - float(
        s5.get("quadrant_flip_rate_pct") or 0
    )
    prior3 = (s3.get("w20_weakening_lead") or {}).get("short_weak_prior_day_pct")
    prior5 = (s5.get("w20_weakening_lead") or {}).get("short_weak_prior_day_pct")
    false3 = (s3.get("false_reduction") or {}).get("false_reduction_pct")
    false5 = (s5.get("false_reduction") or {}).get("false_reduction_pct")

    recommend: Literal["WMA5", "WMA3", "WMA5 (marginal)"] = "WMA5"
    reasons: list[str] = []

    if score3 > score5 + 2.0 and (prior3 or 0) >= (prior5 or 0) + 5:
        recommend = "WMA3"
        reasons.append("WMA3 higher composite score with materially better prior-day warning")
    elif score3 > score5:
        recommend = "WMA5 (marginal)"
        reasons.append("WMA3 slightly better score but not enough to switch anchor from WMA5")
    else:
        reasons.append("WMA5 lower flip / false-reduction noise with comparable lead-lag")

    if flip_delta > 15:
        reasons.append(f"WMA3 flip rate +{flip_delta:.1f}pp vs WMA5 (noisy for holdings)")
    if (false3 or 0) > (false5 or 0) + 3:
        reasons.append(f"WMA3 false-reduction proxy +{(false3 or 0) - (false5 or 0):.1f}pp")

    intraday_note = None
    if intraday_rows:
        ib = {int(r["short_length"]): r for r in intraday_rows}
        if 3 in ib and 5 in ib:
            t3 = ib[3].get("signal_total") or 0
            t5 = ib[5].get("signal_total") or 0
            intraday_note = f"intraday signals WMA3={t3} WMA5={t5} (sample window)"
            if t3 > t5 * 1.4:
                reasons.append("WMA3 inflates intraday signal count >40% vs WMA5")

    return {
        "recommend_fast_chart": recommend,
        "score_wma3": round(score3, 2),
        "score_wma5": round(score5, 2),
        "flip_rate_delta_w3_minus_w5_pp": round(flip_delta, 2),
        "prior_warn_wma3_pct": prior3,
        "prior_warn_wma5_pct": prior5,
        "false_reduction_wma3_pct": false3,
        "false_reduction_wma5_pct": false5,
        "reasons": reasons,
        "intraday_note": intraday_note,
        "summary": (
            f"Recommend fast chart: **{recommend}** · "
            f"score W3={score3:.1f} W5={score5:.1f} · "
            f"flip Δ={flip_delta:+.1f}pp · prior-warn W3={prior3}% W5={prior5}%"
        ),
    }


def run_dual_wma_short_length_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str | None = None,
    short_lengths: tuple[int, ...] = DEFAULT_SHORT_LENGTHS,
    intraday_tail_days: int = 40,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).astype(float)
    uni = _universe_close(conn, close, etf_codes=etf_codes)
    full_dates = uni.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    daily_rows = [
        daily_short_length_metrics(uni, bench, trade_dates, short_length=sl)
        for sl in short_lengths
    ]

    intraday_rows: list[dict[str, Any]] | None = None
    if intraday_tail_days > 0 and len(trade_dates) >= intraday_tail_days:
        intra_dates = trade_dates[-intraday_tail_days:]
        intraday_rows = [
            intraday_signal_counts(
                conn, dates=intra_dates, short_length=sl, etf_codes=etf_codes
            )
            for sl in short_lengths
        ]

    verdict = verdict_short_length_sweep(daily_rows, intraday_rows=intraday_rows)
    return {
        "schema": "dual_wma_short_length_sweep-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_start": date_start,
        "date_end": end,
        "long_length": WMA_LONG_LENGTH,
        "short_lengths": list(short_lengths),
        "universe_stocks": len(uni.columns),
        "daily": daily_rows,
        "intraday_sample": {
            "n_dates": intraday_tail_days,
            "rows": intraday_rows,
        }
        if intraday_rows
        else None,
        "verdict": verdict,
    }


def render_dual_wma_short_length_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# Dual-WMA short-length sweep · WMA(3) vs WMA(5)",
        "",
        f"> {v.get('summary', '')}",
        "",
        f"- Universe: **{payload.get('universe_stocks')}** stocks · "
        f"{payload.get('date_start')} → {payload.get('date_end')}",
        f"- Anchor: WMA({payload.get('long_length')})",
        "",
        "## Daily metrics",
        "",
        "| L | flip% | boundary% | false-red% | prior-warn% | spurious% | spread aligned% |",
        "|--:|------:|----------:|-----------:|------------:|----------:|------------------:|",
    ]
    for row in payload.get("daily") or []:
        fr = row.get("false_reduction") or {}
        wl = row.get("w20_weakening_lead") or {}
        sp = row.get("spurious_short_weak") or {}
        sc = row.get("spread_classes") or {}
        lines.append(
            f"| {row['short_length']} | {row.get('quadrant_flip_rate_pct')} | "
            f"{row.get('boundary_stock_day_pct')} | {fr.get('false_reduction_pct')} | "
            f"{wl.get('short_weak_prior_day_pct')} | {sp.get('spurious_short_weak_pct')} | "
            f"{sc.get('pct_aligned')} |"
        )
    lines.extend(
        [
            "",
            "### Definitions",
            "- **flip%**: adjacent-day short-leg quadrant changes (lower = stabler fast chart)",
            "- **false-red%**: short ∈ {weakening,lagging} while W20 ∈ {leading,improving}",
            "- **prior-warn%**: on W20 leading→weakening, short already weak prior day",
            "- **spurious%**: 3-day window short weak middle day then strong while W20 strong",
            "",
            "## Verdict",
            "",
            f"- **Recommend**: {v.get('recommend_fast_chart')}",
            f"- Score WMA3={v.get('score_wma3')} · WMA5={v.get('score_wma5')}",
        ]
    )
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    if v.get("intraday_note"):
        lines.append(f"- {v['intraday_note']}")
    intra = payload.get("intraday_sample")
    if intra and intra.get("rows"):
        lines.extend(["", "## Intraday signal sample", ""])
        for row in intra["rows"]:
            lines.append(
                f"- WMA({row['short_length']}): {row.get('signal_counts')} "
                f"· total={row.get('signal_total')}"
            )
    lines.extend(
        [
            "",
            "模組：`scripts/run_dual_wma_short_length_sweep.py`",
        ]
    )
    return "\n".join(lines)
